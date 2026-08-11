"""Small ONNX Runtime-compatible facade backed by OpenVINO directly.

The model helper packages used by the application own the image pre/post-processing but
construct an ``onnxruntime.InferenceSession`` internally.  This facade supplies the tiny
part of that API they use while compiling and executing the graph with the current
standalone OpenVINO runtime.  It avoids pinning the whole application to the much older
OpenVINO version bundled in ONNX Runtime's provider wheel.
"""

from __future__ import annotations

import contextlib
import sys
import threading
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import numpy as np

from app.core.logging import get_logger

log = get_logger(__name__)

_module_patch_lock = threading.RLock()
_core_lock = threading.Lock()
_core: Any | None = None


@dataclass(frozen=True, slots=True)
class TensorInfo:
    """The input/output metadata consumed by the upstream model helpers."""

    name: str
    shape: tuple[int | str, ...]


def _get_core() -> Any:
    global _core
    if _core is not None:
        return _core
    with _core_lock:
        if _core is None:
            import openvino as ov

            _core = ov.Core()
    return _core


def available_devices() -> list[str]:
    """Devices reported by the installed OpenVINO runtime."""
    try:
        return list(_get_core().available_devices)
    except Exception as exc:
        log.debug("OpenVINO device discovery failed", error=f"{type(exc).__name__}: {exc}")
        return []


def selected_device() -> str | None:
    """Resolve the configured device against what OpenVINO can actually open."""
    devices = available_devices()
    if not devices:
        return None

    requested = "auto"
    try:
        from app.core.settings_service import get_settings_service

        requested = str(get_settings_service().get_nowait("processing.inference_device"))
    except Exception as exc:
        log.debug("could not read inference device setting", error=str(exc))

    if requested != "auto":
        if requested in devices:
            return requested
        variant = next((item for item in devices if item.startswith(f"{requested}.")), None)
        if variant:
            return variant
        log.warning(
            "requested inference device is unavailable; falling back",
            requested=requested,
            available=devices,
        )

    for kind in ("GPU", "NPU", "CPU"):
        exact = next((item for item in devices if item == kind), None)
        if exact:
            return exact
        variant = next((item for item in devices if item.startswith(f"{kind}.")), None)
        if variant:
            return variant
    return devices[0]


def selected_performance_hint() -> str:
    """Optimise for the configured workload rather than one synthetic request."""
    workers = 2
    try:
        from app.core.settings_service import get_settings_service

        workers = int(get_settings_service().get_nowait("processing.max_workers"))
    except Exception as exc:
        log.debug("could not read processing worker count", error=str(exc))
    return "THROUGHPUT" if workers > 1 else "LATENCY"


def _port_name(port: Any, fallback: str) -> str:
    try:
        return str(port.get_any_name())
    except Exception:
        try:
            names = sorted(str(name) for name in port.get_names())
            if names:
                return names[0]
        except Exception:
            pass
    return fallback


def _port_shape(port: Any) -> tuple[int | str, ...]:
    dimensions: list[int | str] = []
    try:
        partial_shape = port.get_partial_shape()
    except Exception:
        partial_shape = getattr(port, "partial_shape", ())
    for index, dimension in enumerate(partial_shape):
        try:
            dimensions.append(int(dimension.get_length()))
        except Exception:
            dimensions.append(f"dynamic_{index}")
    return tuple(dimensions)


class OpenVINOSession:
    """The ``InferenceSession`` subset required by the detector and OCR packages.

    Each worker thread owns an infer request.  OpenVINO can therefore schedule requests
    from concurrent recordings through the model's shared GPU streams without duplicating
    weights or serialising all workers behind one Python lock.
    """

    def __init__(self, model_path: str | Path, *, device: str | None = None) -> None:
        core = _get_core()
        requested = device or selected_device()
        if requested is None:
            raise RuntimeError("OpenVINO exposes no inference device")

        model_path = Path(model_path)
        model = core.read_model(str(model_path))
        target = requested
        performance_hint = selected_performance_hint()
        config: dict[str, str] = {"PERFORMANCE_HINT": performance_hint}
        try:
            from app.config import get_config

            cache_dir = get_config().data_dir / "openvino_cache_2026"
            cache_dir.mkdir(parents=True, exist_ok=True)
            config["CACHE_DIR"] = str(cache_dir)
        except Exception as exc:
            log.debug("could not prepare OpenVINO cache", error=str(exc))

        started = time.monotonic()
        try:
            compiled = core.compile_model(model, target, config)
        except Exception as exc:
            if target == "CPU":
                raise
            log.warning(
                "OpenVINO model could not compile on requested device; using CPU",
                model=model_path.name,
                requested=target,
                error=f"{type(exc).__name__}: {exc}",
            )
            target = "CPU"
            compiled = core.compile_model(model, target, config)

        self.device = target
        self._compiled = compiled
        self._local = threading.local()
        self._inputs = tuple(
            TensorInfo(_port_name(port, f"input_{index}"), _port_shape(port))
            for index, port in enumerate(model.inputs)
        )
        self._outputs = tuple(
            TensorInfo(_port_name(port, f"output_{index}"), _port_shape(port))
            for index, port in enumerate(model.outputs)
        )
        self._output_ports = {
            info.name: port for info, port in zip(self._outputs, compiled.outputs, strict=True)
        }

        try:
            requests = int(compiled.get_property("OPTIMAL_NUMBER_OF_INFER_REQUESTS"))
        except Exception:
            requests = None
        log.info(
            "OpenVINO model compiled",
            model=model_path.name,
            device=target,
            seconds=round(time.monotonic() - started, 3),
            performance_hint=performance_hint,
            optimal_requests=requests,
        )

    def get_inputs(self) -> list[TensorInfo]:
        return list(self._inputs)

    def get_outputs(self) -> list[TensorInfo]:
        return list(self._outputs)

    def get_providers(self) -> list[str]:
        return [f"OpenVINO:{self.device}"]

    def _request(self) -> Any:
        request = getattr(self._local, "request", None)
        if request is None:
            request = self._compiled.create_infer_request()
            self._local.request = request
        return request

    def run(
        self,
        output_names: Sequence[str] | None,
        input_feed: dict[str, np.ndarray],
    ) -> list[np.ndarray]:
        result = self._request().infer(input_feed)
        wanted = list(output_names) if output_names else [item.name for item in self._outputs]
        arrays: list[np.ndarray] = []
        for name in wanted:
            port = self._output_ports.get(name)
            if port is None:
                raise KeyError(f"OpenVINO model has no output named {name!r}")
            arrays.append(np.asarray(result[port]))
        return arrays


class _OrtFacade:
    """Delegate ONNX Runtime metadata APIs but replace session construction."""

    def __init__(self, original: ModuleType) -> None:
        self._original = original

    def __getattr__(self, name: str) -> Any:
        return getattr(self._original, name)

    def InferenceSession(
        self,
        model_path: str | Path,
        sess_options: Any = None,
        providers: Any = None,
        **kwargs: Any,
    ) -> Any:
        del providers, kwargs
        try:
            return OpenVINOSession(model_path)
        except Exception as exc:
            log.warning(
                "direct OpenVINO session failed; using ONNX Runtime CPU",
                model=Path(model_path).name,
                error=f"{type(exc).__name__}: {exc}",
            )
            return self._original.InferenceSession(
                str(model_path),
                sess_options=sess_options,
                providers=["CPUExecutionProvider"],
            )


@contextlib.contextmanager
def use_openvino_session(owner: type[Any]) -> Iterator[None]:
    """Make one upstream inference class construct :class:`OpenVINOSession`.

    Only that class's defining module is patched, rather than the process-wide
    ``onnxruntime`` module.  The short construction window is locked and restored in a
    ``finally`` block, so unrelated ONNX Runtime calls cannot observe the facade.
    """
    module = sys.modules[owner.__module__]
    original = module.ort
    with _module_patch_lock:
        module.ort = _OrtFacade(original)
        try:
            yield
        finally:
            module.ort = original


def reset_runtime_for_tests() -> None:
    """Clear process-wide runtime state. Intended for isolated tests."""
    global _core
    with _core_lock:
        _core = None
