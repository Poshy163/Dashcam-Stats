"""Inference runtime abstraction.

Every heavy import is guarded. The application must boot, serve the UI and index footage
on a machine with no OpenVINO, no ONNX Runtime and no models at all — those features
simply report themselves unavailable rather than taking the process down.

Preference order is OpenVINO GPU, then OpenVINO CPU, then ONNX Runtime CPU. The target
host has an Intel iGPU reachable through the same ``/dev/dri`` render node that VAAPI
uses, and OpenVINO drives it without any CUDA dependency.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from app.core.logging import get_logger
from app.core.settings_service import get_settings_service

log = get_logger(__name__)


@dataclass(slots=True)
class BackendInfo:
    name: str = "unavailable"
    device: str = "CPU"
    version: str | None = None
    devices: list[str] = field(default_factory=list)
    detail: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "backend": self.name,
            "device": self.device,
            "version": self.version,
            "devices": self.devices,
            "detail": self.detail,
        }


class LoadedModel:
    """A compiled model plus the metadata callers need to shape their inputs."""

    def __init__(self, runner: Any, backend: str, device: str, input_shape: tuple[int, ...]):
        self._runner = runner
        self.backend = backend
        self.device = device
        self.input_shape = input_shape
        # Compiled models are not guaranteed thread-safe across runtimes, and the worker
        # pool runs several jobs at once.
        self._lock = threading.Lock()

    def infer_sync(self, inputs: np.ndarray) -> list[np.ndarray]:
        with self._lock:
            return self._runner(inputs)

    async def infer(self, inputs: np.ndarray) -> list[np.ndarray]:
        """Run inference off the event loop — it is pure blocking compute."""
        return await asyncio.to_thread(self.infer_sync, inputs)


class InferenceBackend:
    """Chooses and drives whichever runtime is actually present."""

    def __init__(self) -> None:
        self._info = BackendInfo()
        self._probed = False
        self._ov_core: Any = None

    # -- discovery ---------------------------------------------------------------------

    def _probe(self) -> None:
        if self._probed:
            return
        self._probed = True

        try:
            import openvino as ov

            core = ov.Core()
            devices = list(core.available_devices)
            self._ov_core = core
            self._info = BackendInfo(
                name="openvino",
                device="GPU" if "GPU" in devices else "CPU",
                version=getattr(ov, "__version__", None),
                devices=devices,
            )
            return
        except Exception as exc:
            log.debug("OpenVINO unavailable", error=f"{type(exc).__name__}: {exc}")

        try:
            import onnxruntime as ort

            self._info = BackendInfo(
                name="onnxruntime",
                device="CPU",
                version=getattr(ort, "__version__", None),
                devices=list(ort.get_available_providers()),
                detail="OpenVINO not available; using ONNX Runtime on CPU",
            )
            return
        except Exception as exc:
            log.debug("ONNX Runtime unavailable", error=f"{type(exc).__name__}: {exc}")

        self._info = BackendInfo(
            detail="no inference runtime installed; detection and plate reading are disabled"
        )

    @property
    def info(self) -> BackendInfo:
        self._probe()
        return self._info

    @property
    def available(self) -> bool:
        return self.info.name != "unavailable"

    @property
    def backend_name(self) -> str:
        return self.info.name

    def resolve_device(self) -> str:
        """Honour the processing.inference_device setting, falling back when unmet."""
        info = self.info
        requested = str(get_settings_service().get_nowait("processing.inference_device"))
        if requested != "auto" and requested in info.devices:
            return requested
        if requested != "auto" and requested not in info.devices:
            log.warning(
                "requested inference device is not available; falling back",
                requested=requested,
                available=info.devices,
                using=info.device,
            )
        return info.device

    def describe(self) -> dict[str, object]:
        data = self.info.as_dict()
        data["available"] = self.available
        data["resolved_device"] = self.resolve_device() if self.available else None
        return data

    # -- loading -----------------------------------------------------------------------

    def load(self, model_path: Path, *, device: str | None = None) -> LoadedModel | None:
        """Compile a model, or return None when it cannot be loaded.

        Returning None rather than raising is deliberate: a missing or corrupt model must
        disable one feature, not fail the recording being processed.
        """
        self._probe()
        if not self.available or not model_path.exists():
            return None

        target = device or self.resolve_device()

        if self._info.name == "openvino":
            try:
                model = self._ov_core.read_model(str(model_path))
                compiled = self._ov_core.compile_model(model, target)
                output_ports = list(compiled.outputs)
                input_port = compiled.inputs[0]
                shape = tuple(
                    int(d) if d > 0 else -1 for d in input_port.get_partial_shape().get_min_shape()
                )

                def run(batch: np.ndarray, _compiled=compiled, _outs=output_ports):
                    result = _compiled(batch)
                    return [np.asarray(result[port]) for port in _outs]

                log.info("loaded model", model=model_path.name, device=target, backend="openvino")
                return LoadedModel(run, "openvino", target, shape)
            except Exception as exc:
                log.warning(
                    "could not load model with OpenVINO", model=model_path.name, error=str(exc)
                )
                return None

        if self._info.name == "onnxruntime":
            try:
                import onnxruntime as ort

                session = ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])
                input_meta = session.get_inputs()[0]
                shape = tuple(d if isinstance(d, int) else -1 for d in input_meta.shape)
                names = [o.name for o in session.get_outputs()]

                def run(batch: np.ndarray, _s=session, _n=names, _in=input_meta.name):
                    return list(_s.run(_n, {_in: batch}))

                log.info("loaded model", model=model_path.name, device="CPU", backend="onnxruntime")
                return LoadedModel(run, "onnxruntime", "CPU", shape)
            except Exception as exc:
                log.warning(
                    "could not load model with ONNX Runtime", model=model_path.name, error=str(exc)
                )
                return None

        return None


_backend: InferenceBackend | None = None


def get_backend() -> InferenceBackend:
    global _backend
    if _backend is None:
        _backend = InferenceBackend()
    return _backend
