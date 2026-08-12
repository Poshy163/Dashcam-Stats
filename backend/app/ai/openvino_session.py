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

# Alder Lake's iGPU shares memory and execution resources between VAAPI and OpenVINO. The
# driver advertises multiple infer requests, but two pipeline workers issuing requests
# through different compiled models produced CL_OUT_OF_RESOURCES / event failures and then
# aborted the entire process. One GPU request at a time is still substantially faster than
# CPU inference and lets the second worker overlap decode, telemetry and database work.
# CPU/NPU sessions remain concurrent.
_gpu_inference_lock = threading.Lock()

#: Substrings that mean the GPU *context* has failed, not that this one request was bad.
#:
#: Taken verbatim from the deployment's own logs. Once any of these appears every
#: subsequent request on the same compiled model fails the same way until the process is
#: replaced, so there is no such thing as retrying past one of them.
_GPU_CONTEXT_FAILURE_MARKERS = (
    "cl_out_of_resources",
    "cl_exec_status_error_for_events_in_wait_list",
    "cl_invalid_command_queue",
    "cl_device_not_available",
    "clflush",
    "clwaitforevents",
    "clfinish",
    "drm_buffer_object.cpp",
    "intel_gpu/src/runtime",
)

#: Set once the iGPU has failed in this process; never cleared without a restart.
_gpu_disabled_reason: str | None = None
_gpu_state_lock = threading.Lock()


def is_gpu_context_failure(exc: BaseException) -> bool:
    """Whether *exc* is the Intel driver saying its context is gone."""
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(marker in text for marker in _GPU_CONTEXT_FAILURE_MARKERS)


def gpu_backend_disabled() -> str | None:
    """Why the iGPU is no longer used for inference in this process, or None."""
    return _gpu_disabled_reason


def disable_gpu_backend(reason: str) -> bool:
    """Take the iGPU out of service for inference. Returns True on the first caller.

    Deliberately one-way. The failure mode this exists for is not a bad request but a
    poisoned OpenCL context: the deployment logged one ``CL_OUT_OF_RESOURCES`` and then
    thirty-seven more for the same recording, every frame of which silently returned no
    detections because the model helper catches its own inference errors. Re-arming the GPU
    on the next job would simply reproduce that.
    """
    global _gpu_disabled_reason
    with _gpu_state_lock:
        first = _gpu_disabled_reason is None
        if first:
            _gpu_disabled_reason = reason
    # The cached answer named a device that has just been taken out of service.
    _clear_device_cache()
    if first:
        log.error(
            "the Intel GPU inference context has failed; inference moves to the CPU for "
            "the life of this process",
            reason=reason,
        )
    return first


def gpu_inference_engaged() -> bool:
    """Whether inference currently owns the Intel iGPU.

    Read by the media layer to decide whether any decode may use VAAPI. The two cannot
    share this chip, so this is the question that settles the whole resource policy.
    """
    if _gpu_disabled_reason is not None:
        return False
    device = selected_device()
    return bool(device and device.upper().startswith("GPU"))


def reset_gpu_backend_for_tests() -> None:
    """Clear the one-way disable. Intended for isolated tests."""
    global _gpu_disabled_reason
    with _gpu_state_lock:
        _gpu_disabled_reason = None
    _clear_device_cache()


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


#: Last resolved device, as ``(requested_setting, resolved)``.
#:
#: Resolving asks OpenVINO to enumerate its devices, which is a *native, synchronous* call.
#: That is fine once and disastrous per request: `select_hwaccel` consults the device on
#: every decode and the health endpoint consults it on every poll, so an iGPU that stalls
#: -- the exact condition this whole area exists to survive -- took the event loop with it.
#: The container stayed up, accepted connections and answered nothing, /health included.
#:
#: The answer cannot change underneath this cache: the setting is part of the key, and the
#: only other thing that moves it is `disable_gpu_backend`, which clears it.
_device_cache: tuple[str, str | None] | None = None


def _clear_device_cache() -> None:
    global _device_cache
    _device_cache = None


def selected_device() -> str | None:
    """Resolve the configured device against what OpenVINO can actually open.

    Memoised. See :data:`_device_cache` for why that is not an optimisation.
    """
    global _device_cache

    requested_setting = "auto"
    try:
        from app.core.settings_service import get_settings_service

        # An in-memory dictionary read, so this stays cheap enough to do every time and
        # keeps a settings change from being served a stale device.
        requested_setting = str(get_settings_service().get_nowait("processing.inference_device"))
    except Exception:
        pass

    cached = _device_cache
    if cached is not None and cached[0] == requested_setting:
        return cached[1]

    resolved = _resolve_device(requested_setting)
    _device_cache = (requested_setting, resolved)
    return resolved


def _resolve_device(requested: str) -> str | None:
    devices = available_devices()
    if _gpu_disabled_reason is not None:
        # The chip is still enumerated and still broken. Removing it here is what makes
        # every later decision -- new sessions, the decode policy, the status page -- agree
        # that inference is on the CPU now, instead of each rediscovering it separately.
        devices = [item for item in devices if not item.upper().startswith("GPU")]
    if not devices:
        return None

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


def selected_performance_hint(device: str | None = None) -> str:
    """Optimise for the configured workload rather than one synthetic request."""
    # A global single-request lane is intentional on this iGPU. LATENCY asks OpenVINO not
    # to reserve extra GPU streams behind that lane, reducing both memory pressure and the
    # chance of a native driver abort.
    if device and device.upper().startswith("GPU"):
        return "LATENCY"
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
        performance_hint = selected_performance_hint(target)
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
            performance_hint = selected_performance_hint(target)
            config["PERFORMANCE_HINT"] = performance_hint
            compiled = core.compile_model(model, target, config)

        self.device = target
        self._compiled = compiled
        self._local = threading.local()
        # Kept so the session can rebuild itself on the CPU if the GPU context dies.
        self._model = model
        self._model_name = model_path.name
        self._config = config
        self._rebuild_lock = threading.Lock()
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

    def _move_to_cpu(self, reason: str) -> None:
        """Recompile this session on the CPU after the GPU context has failed.

        Done in place, because the caller is an upstream model helper that owns the object
        and would otherwise never learn anything had changed. Recompiling costs seconds
        once; the alternative is what the deployment actually did -- return no detections
        for every remaining frame while reporting the job complete.
        """
        with self._rebuild_lock:
            if not self.device.upper().startswith("GPU"):
                return  # another thread rebuilt it while this one waited
            config = {**self._config, "PERFORMANCE_HINT": selected_performance_hint("CPU")}
            compiled = _get_core().compile_model(self._model, "CPU", config)
            self._compiled = compiled
            self._local = threading.local()
            self._output_ports = {
                info.name: port for info, port in zip(self._outputs, compiled.outputs, strict=True)
            }
            self.device = "CPU"
        log.warning(
            "inference session rebuilt on the CPU after a GPU driver failure",
            model=self._model_name,
            reason=reason,
        )

    def ensure_cpu(self, reason: str) -> bool:
        """Move this session off the iGPU for good. Returns True if it moved.

        Called both when the driver has already failed and when the media layer says the
        chip is unsafe to touch -- an ffmpeg child that will not die holds exactly the
        resources OpenVINO is about to ask for.
        """
        if not self.device.upper().startswith("GPU"):
            return False
        disable_gpu_backend(reason)
        self._move_to_cpu(reason)
        return True

    def run(
        self,
        output_names: Sequence[str] | None,
        input_feed: dict[str, np.ndarray],
    ) -> list[np.ndarray]:
        if self.device.upper().startswith("GPU"):
            try:
                with _gpu_inference_lock:
                    result = self._request().infer(input_feed)
            except Exception as exc:
                if not is_gpu_context_failure(exc):
                    raise
                # The context is gone, so this request and every one after it would fail.
                # Rebuild on the CPU and answer the question that was actually asked --
                # the caller above catches inference errors and returns no detections, so
                # re-raising here is indistinguishable from an empty frame.
                disable_gpu_backend(f"{type(exc).__name__}: {exc}".strip()[:500])
                self._move_to_cpu(str(exc)[:200])
                result = self._request().infer(input_feed)
        else:
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
    reset_gpu_backend_for_tests()
