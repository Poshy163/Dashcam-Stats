"""Small ONNX Runtime-compatible facade backed by OpenVINO directly.

The model helper packages used by the application own the image pre/post-processing but
construct an ``onnxruntime.InferenceSession`` internally.  This facade supplies the tiny
part of that API they use while compiling and executing the graph with the current
standalone OpenVINO runtime.  It avoids pinning the whole application to the much older
OpenVINO version bundled in ONNX Runtime's provider wheel.
"""

from __future__ import annotations

import contextlib
import os
import sys
import tempfile
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
_gpu_inference_lock = threading.RLock()
_native_condition = threading.Condition(_gpu_inference_lock)
_native_writers = 0
_active_cpu_inference = 0


@contextlib.contextmanager
def _exclusive_native_lane():
    """GPU/native model changes exclude CPU calls; healthy CPU inference stays parallel."""
    global _native_writers
    with _native_condition:
        _native_writers += 1
        try:
            while _active_cpu_inference:
                _native_condition.wait()
            yield
        finally:
            _native_writers -= 1
            _native_condition.notify_all()


@contextlib.contextmanager
def _cpu_native_lane():
    global _active_cpu_inference
    with _native_condition:
        while _native_writers:
            _native_condition.wait()
        usable = not gpu_context_failed()
        if usable:
            _active_cpu_inference += 1
    try:
        yield usable
    finally:
        if usable:
            with _native_condition:
                _active_cpu_inference -= 1
                _native_condition.notify_all()


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
# A previous process's saved verdict disables GPU selection, but its new Core can still
# compile CPU models. A native failure in this process forbids re-entering this Core.
_gpu_context_failed = False

#: Whether the durable marker has already been written in this process. Tracked apart from
#: ``_gpu_disabled_reason`` because a *transient* disable sets that first and would
#: otherwise make the real abort's durable request a no-op.
_gpu_failure_persisted: bool = False
_gpu_state_lock = threading.Lock()


def is_gpu_context_failure(exc: BaseException) -> bool:
    """Whether *exc* is the Intel driver saying its context is gone."""
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(marker in text for marker in _GPU_CONTEXT_FAILURE_MARKERS)


def gpu_backend_disabled() -> str | None:
    """Why the iGPU is no longer used for inference in this process, or None."""
    return _gpu_disabled_reason


def gpu_context_failed() -> bool:
    """Whether this process's OpenVINO context has suffered a native GPU failure."""
    return _gpu_context_failed


def disable_gpu_backend(reason: str, *, durable: bool = False) -> bool:
    """Take the iGPU out of service for inference. Returns True on the first caller.

    ``durable`` records the verdict on disk so restarts inherit it, and is for one thing
    only: the driver itself aborting. Everything else that takes the GPU out of service --
    an ffmpeg child that will not die, a media slot gone unhealthy -- is a condition of
    this run and must not condemn the chip for every future one.

    Deliberately one-way. The failure mode this exists for is not a bad request but a
    poisoned OpenCL context: the deployment logged one ``CL_OUT_OF_RESOURCES`` and then
    thirty-seven more for the same recording, every frame of which silently returned no
    detections because the model helper catches its own inference errors. Re-arming the GPU
    on the next job would simply reproduce that.
    """
    global _gpu_disabled_reason, _devices_cache, _device_cache, _gpu_failure_persisted
    global _gpu_context_failed
    with _gpu_state_lock:
        if durable:
            _gpu_context_failed = True
        first = _gpu_disabled_reason is None
        if first:
            _gpu_disabled_reason = reason
        # Decided under the same lock that owns the reason. This is reached from worker
        # threads, so an unsynchronised test-and-set lets two of them both conclude they
        # are the first to persist and both write the marker.
        should_persist = durable and not _gpu_failure_persisted
        if should_persist:
            _gpu_failure_persisted = True

    # Drop the GPU from the *cached* device list and re-resolve from that, in Python.
    #
    # Emphatically not by clearing the cache and asking OpenVINO again. The runtime has
    # just aborted; enumerating its devices means re-entering native code in a driver that
    # is in the middle of dying, and that call does not come back. It happened: the cache
    # was invalidated here, the next /health resolved the device, and the event loop
    # stopped for good -- the container stayed up, accepted connections and answered
    # nothing. The list from the last successful enumeration is all that is needed, and
    # the one thing that changed about it is known.
    remaining = [item for item in (_devices_cache or []) if not item.upper().startswith("GPU")]
    _devices_cache = remaining
    _device_cache = None

    if first:
        log.error(
            "the Intel GPU inference context has failed; inference moves to the CPU for "
            "the life of this process",
            reason=reason,
            remaining_devices=remaining,
            durable=durable,
        )

    # Persisted on its own terms, not on being the *first* disable.
    #
    # The two are different questions and conflating them lost the marker in the case it
    # exists for. `ensure_cpu` disables the backend non-durably for a transient condition --
    # a stuck ffmpeg child making the media slot unsafe -- so by the time the driver
    # genuinely aborts, `first` is already False and the durable call did nothing. The next
    # restart then re-armed the iGPU and walked into the same native abort, which is the
    # crash loop this marker was written to break.
    #
    # The file write stays outside the lock; only the decision is inside it.
    if should_persist:
        if not first:
            log.error(
                "the Intel GPU inference context has failed after an earlier, milder "
                "disable; recording it so the next start does not re-arm the chip",
                reason=reason,
            )
        if not _persist_gpu_failure(reason):
            with _gpu_state_lock:
                _gpu_failure_persisted = False
    return first


#: Marker recording that this machine's iGPU aborted, so a restart does not re-arm it.
#:
#: The disable above is process-local, and the abort kills the process -- so every restart
#: brought the GPU straight back and walked into the same abort. That is the crash loop,
#: and nothing in-process can break it, because the thing being recovered from is a native
#: ``abort()`` in the driver: by the time Python sees an exception the runtime has already
#: decided to take the process down. Surviving it has to be durable.
GPU_FAILURE_MARKER = "gpu-inference-failed.json"


def _gpu_failure_marker_path():
    from app.config import get_config

    return get_config().data_dir / GPU_FAILURE_MARKER


def _persist_gpu_failure(reason: str) -> bool:
    import json as _json
    from datetime import UTC, datetime

    temporary: Path | None = None
    try:
        path = _gpu_failure_marker_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = read_gpu_failure_marker() or {}
        payload = {
            "reason": reason[:500],
            "failures": int(existing.get("failures") or 0) + 1,
            "last_failed_at": datetime.now(UTC).isoformat(),
        }
        # Never truncate the previous verdict: a native abort can kill the process at
        # any instruction, and an incomplete marker must not re-arm a failed GPU.
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            temporary = Path(handle.name)
            _json.dump(payload, handle, indent=1)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        return True
    except Exception as exc:
        log.warning("could not record the GPU failure", error=f"{type(exc).__name__}: {exc}")
        return False
    finally:
        if temporary is not None:
            with contextlib.suppress(OSError):
                temporary.unlink(missing_ok=True)


def restore_gpu_failure_state() -> str | None:
    """Re-apply a previous run's GPU verdict before anything can use the chip.

    Called during start-up. Without it the disable dies with the process it was made in --
    and the process is being killed *by* the fault, so the next one re-arms the GPU and
    aborts again. That is the loop the deployment was stuck in.
    """
    global _gpu_disabled_reason
    data = read_gpu_failure_marker()
    if data is None:
        return None

    reason = str(data.get("reason") or "the iGPU aborted during a previous run")
    with _gpu_state_lock:
        _gpu_disabled_reason = reason
    log.error(
        "the iGPU is disabled for inference because it aborted before; delete the marker "
        "in the data directory to try it again",
        marker=GPU_FAILURE_MARKER,
        failures=data.get("failures"),
        reason=reason[:200],
    )
    return reason


def read_gpu_failure_marker() -> dict[str, object] | None:
    """The durable verdict as recorded on disk, or None when the chip is not condemned.

    Exposed because the marker is the only place that says *how often* the iGPU has
    aborted, and that is the difference between a one-off worth retrying and a chip that
    should stay off. Until this existed the answer lived in a file inside the container
    with no shell, so the honest operator action -- "has this happened once or thirty
    times?" -- could not be taken at all.
    """
    import json as _json

    invalid = {
        "reason": "The saved GPU failure marker is unreadable; retry after checking the driver.",
        "failures": None,
        "last_failed_at": None,
    }
    try:
        data = _json.loads(_gpu_failure_marker_path().read_text("utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return invalid
    if not isinstance(data, dict):
        return invalid
    try:
        failures = int(data.get("failures") or 0)
    except (TypeError, ValueError, OverflowError):
        failures = None
    return {
        "reason": str(data.get("reason") or "")[:500],
        "failures": max(0, failures) if failures is not None else None,
        "last_failed_at": data.get("last_failed_at"),
    }


def clear_gpu_failure_state() -> bool:
    """Forget the saved verdict while keeping this process's failed GPU disabled."""
    global _gpu_failure_persisted
    try:
        _gpu_failure_marker_path().unlink(missing_ok=True)
    except Exception:
        return False
    with _gpu_state_lock:
        # A late native error from an already-running request may still need to persist
        # a fresh verdict. Runtime state and device caches remain disabled until restart.
        _gpu_failure_persisted = False
    return True


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
    global _gpu_disabled_reason, _gpu_failure_persisted, _gpu_context_failed
    with _gpu_state_lock:
        _gpu_disabled_reason = None
        # Tracked separately from the reason, so it has to be cleared separately too --
        # otherwise one test's durable disable decides whether the next one's is written.
        _gpu_failure_persisted = False
        _gpu_context_failed = False
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


#: The last successful enumeration, so nothing ever has to ask the driver twice.
#:
#: Enumerating is a native call, and after the iGPU aborts it is a native call into a dying
#: driver that does not return. Remembering the answer is what lets every later decision --
#: the decode policy, the status page, the fallback to CPU -- be made in Python.
_devices_cache: list[str] | None = None


def available_devices() -> list[str]:
    """Devices reported by the installed OpenVINO runtime, enumerated at most once."""
    global _devices_cache
    if _devices_cache is not None:
        return list(_devices_cache)
    try:
        devices = list(_get_core().available_devices)
    except Exception as exc:
        log.debug("OpenVINO device discovery failed", error=f"{type(exc).__name__}: {exc}")
        return []
    _devices_cache = devices
    return list(devices)


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
    global _device_cache, _devices_cache
    _device_cache = None
    _devices_cache = None


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


def cpu_inference_threads() -> int:
    """How many threads one CPU inference session may use: half the logical CPUs, floor 2.

    Unbounded, an OpenVINO CPU session spreads across every core it can see, which on a
    host also running two ffmpeg decoders is a thread pool per runtime all claiming the
    whole machine. This is the bound for the inference half.

    The session is **process-wide**, not per worker, and it used to be sized as though it
    were per worker -- one worker's share of the CPUs, minus that worker's media budget.
    That had it backwards: the shared detector *shrank* as workers were added, going from
    four threads at one worker to the floor of two at two on an eight-thread host, while
    ffmpeg's aggregate grew with each one. Turning the concurrency up made the single thing
    doing the inference slower.

    Half is a deliberate over-subscription rather than a division of the machine.
    ``native_thread_budget`` already hands the workers' decoders roughly the whole CPU
    count between them, and subtracting that leaves nothing at all; but a decoder spends
    much of its life waiting on I/O, so sizing inference as though it did not would leave
    the CPU idle while the queue drains. The floor of two keeps a single-threaded detector
    off a large host, which would be a stranger failure than the contention.
    """
    import os

    cpus = max(1, os.cpu_count() or 1)
    # Half the machine, floored at two.
    #
    # Worker-count-independent on purpose, because the session is: `native_thread_budget`
    # already divides the machine between the workers' ffmpeg pools, so subtracting that
    # aggregate from the total leaves nothing at all and the floor takes over. On the
    # deployment this is written for -- eight threads, two workers, the iGPU out of service
    # so every detection runs here -- the old arithmetic gave the process's only inference
    # session two threads while ffmpeg held the rest, and gave it fewer the more workers
    # were added. Half is the ordinary answer for a shared CPU pool running beside decoders
    # that spend much of their time waiting on I/O.
    return max(2, cpus // 2)


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


def _runtime_version() -> str:
    """A filesystem-safe OpenVINO version, for keying the compiled-model cache."""
    try:
        import openvino as ov

        raw = str(getattr(ov, "__version__", "") or "unknown")
    except Exception:
        raw = "unknown"
    # Versions look like "2025.4.1-19140-..."; the leading release is the part that
    # decides blob compatibility, and the build suffix only makes the path unwieldy.
    head = raw.split("-", 1)[0].strip() or "unknown"
    return "".join(ch if ch.isalnum() or ch == "." else "_" for ch in head)


_INTEL_RUNTIME_CACHE_KEY = Path("/usr/local/share/dashcam/intel-runtime-cache-key")


def _model_cache_options(device: str) -> dict[str, str]:
    """Partition GPU blobs by the verified image's installed Intel runtime packages.

    Missing keys preserve non-Docker behavior. An unreadable or malformed present key
    disables disk caching, rather than reusing an unverified legacy GPU cache directory.
    This reads only the build-generated file; it never queries a native GPU property.
    """
    try:
        from app.config import get_config

        name = f"openvino_cache_{_runtime_version()}"
        if device.upper().startswith("GPU"):
            try:
                with _INTEL_RUNTIME_CACHE_KEY.open("rb") as stream:
                    fingerprint = stream.read(66)
            except FileNotFoundError:
                pass
            else:
                if len(fingerprint) == 65 and fingerprint.endswith(b"\n"):
                    fingerprint = fingerprint[:-1]
                if len(fingerprint) != 64 or any(
                    value not in b"0123456789abcdef" for value in fingerprint
                ):
                    raise ValueError("invalid Intel runtime cache fingerprint")
                name += f"_intel_{fingerprint.decode('ascii')}"
        directory = get_config().data_dir / name
        directory.mkdir(parents=True, exist_ok=True)
        return {"CACHE_DIR": str(directory)}
    except Exception as exc:
        log.debug("could not prepare OpenVINO cache", error=str(exc))
        return {}


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
        # Reading metadata and querying compiled-model properties are native calls too.
        # A model load queued behind a failed inference must stop before any of them.
        with _exclusive_native_lane():
            self._initialize(model_path, device=device)

    def _initialize(self, model_path: str | Path, *, device: str | None = None) -> None:
        if gpu_context_failed():
            raise RuntimeError("OpenVINO is disabled after a native GPU context failure")
        core = _get_core()
        requested = device or selected_device()
        if requested is None:
            raise RuntimeError("OpenVINO exposes no inference device")

        model_path = Path(model_path)
        model = core.read_model(str(model_path))
        target = requested
        performance_hint = selected_performance_hint(target)
        config: dict[str, str] = {"PERFORMANCE_HINT": performance_hint}
        if target.upper().startswith("CPU"):
            config["INFERENCE_NUM_THREADS"] = str(cpu_inference_threads())
        config.update(_model_cache_options(target))

        started = time.monotonic()
        with _gpu_inference_lock:
            if gpu_context_failed() or (
                target.upper().startswith("GPU") and gpu_backend_disabled() is not None
            ):
                raise RuntimeError("OpenVINO GPU is disabled; create a fresh CPU runtime")
            try:
                compiled = core.compile_model(model, target, config)
            except Exception as exc:
                if target.upper().startswith("GPU") and is_gpu_context_failure(exc):
                    disable_gpu_backend(f"{type(exc).__name__}: {exc}"[:500], durable=True)
                    raise
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
                config["INFERENCE_NUM_THREADS"] = str(cpu_inference_threads())
                config.pop("CACHE_DIR", None)
                config.update(_model_cache_options(target))
                compiled = core.compile_model(model, target, config)

        self.device = target
        self._compiled = compiled
        self._local = threading.local()
        self._cpu_session = None
        self._model_path = str(model_path)
        # Retain these objects after a GPU fault: destroying them can itself enter the
        # failed driver. Recovery uses a separate plain ONNX Runtime CPU session.
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
        except Exception as exc:
            if target.upper().startswith("GPU") and is_gpu_context_failure(exc):
                disable_gpu_backend(f"{type(exc).__name__}: {exc}"[:500], durable=True)
                raise
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
        if self._cpu_session is not None:
            return self._cpu_session.get_providers()
        return [f"OpenVINO:{self.device}"]

    def _request(self) -> Any:
        request = getattr(self._local, "request", None)
        if request is None:
            request = self._compiled.create_infer_request()
            self._local.request = request
        return request

    def _move_to_cpu(self, reason: str) -> None:
        """Recover through plain ONNX Runtime without touching the failed OpenVINO core."""
        with self._rebuild_lock:
            if getattr(self, "_cpu_session", None) is not None:
                return  # another thread rebuilt it while this one waited
            if not self.device.upper().startswith("GPU") and not gpu_context_failed():
                return
            import onnxruntime as ort

            from app.core.resources import onnx_session_options

            self._cpu_session = ort.InferenceSession(
                self._model_path,
                sess_options=onnx_session_options(),
                providers=["CPUExecutionProvider"],
            )
            self.device = "CPU"
        log.warning(
            "inference session recovered with ONNX Runtime CPU",
            model=self._model_name,
            reason=reason,
        )

    def ensure_cpu(self, reason: str) -> bool:
        """Move this session off the iGPU for good. Returns True if it moved.

        Called both when the driver has already failed and when the media layer says the
        chip is unsafe to touch -- an ffmpeg child that will not die holds exactly the
        resources OpenVINO is about to ask for.
        """
        if getattr(self, "_cpu_session", None) is not None:
            return False
        if not self.device.upper().startswith("GPU") and not gpu_context_failed():
            return False
        disable_gpu_backend(reason)
        self._move_to_cpu(reason)
        return True

    def run(
        self,
        output_names: Sequence[str] | None,
        input_feed: dict[str, np.ndarray],
    ) -> list[np.ndarray]:
        cpu_session = getattr(self, "_cpu_session", None)
        if cpu_session is not None:
            return cpu_session.run(output_names, input_feed)
        if self.device.upper().startswith("GPU"):
            with _exclusive_native_lane():
                # A caller may have queued before another request disabled the device.
                # Recheck under the same lock that publishes native-failure verdicts.
                cpu_session = getattr(self, "_cpu_session", None)
                if cpu_session is not None:
                    return cpu_session.run(output_names, input_feed)
                if gpu_backend_disabled() is not None:
                    raise RuntimeError("OpenVINO GPU is disabled after an earlier failure")
                try:
                    result = self._request().infer(input_feed)
                except Exception as exc:
                    if is_gpu_context_failure(exc):
                        # Persist while holding the inference lane, before a waiting
                        # request can enter native code. Recovery uses plain ORT later.
                        disable_gpu_backend(f"{type(exc).__name__}: {exc}"[:500], durable=True)
                    raise
        else:
            # The CPU fallback is published before `device` changes. Re-read it here:
            # another thread may have demoted this GPU session since our first snapshot.
            cpu_session = getattr(self, "_cpu_session", None)
            if cpu_session is not None:
                return cpu_session.run(output_names, input_feed)
            with _cpu_native_lane() as usable:
                if usable:
                    result = self._request().infer(input_feed)
            if not usable:
                # A model that was already on OpenVINO CPU/NPU shares the failed Core.
                # Continue through plain ORT instead of issuing another native OV call.
                self._move_to_cpu("another model failed in the shared OpenVINO context")
                return self._cpu_session.run(output_names, input_feed)
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
