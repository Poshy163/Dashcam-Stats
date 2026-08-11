"""Which ONNX Runtime execution providers to use, and in what order.

The upstream inference classes all take a ``providers`` sequence, so this is the one place
that decides where inference actually runs.

Naming the provider is not enough to reach the iGPU. ``OpenVINOExecutionProvider`` accepts
a ``device_type`` and, left unset, will happily compile for the CPU — the provider shows up
in diagnostics, everything looks accelerated, and the GPU sits idle. On a real deployment
that was measurable from the outside: ``intel_gpu_top`` showed the render engine mostly
carrying the desktop while a Python process pinned the CPU, and a 674-recording queue was
pacing at roughly three minutes each, about thirty-three hours of work.

The device is therefore chosen explicitly, from what OpenVINO reports it can actually see,
and compiled models are cached on the data volume. Without the cache every container
restart recompiles each network for the GPU, which costs seconds per model per start.
"""

from __future__ import annotations

from functools import cache
from typing import Any

from app.core.logging import get_logger

log = get_logger(__name__)

#: Most specific first — ONNX Runtime assigns each node to the first provider that will
#: take it, so ordering is what expresses the preference.
_PREFERRED = ("OpenVINOExecutionProvider", "CUDAExecutionProvider", "CPUExecutionProvider")

#: Where the OpenVINO EP keeps compiled model blobs. Under the data volume so it survives
#: a container restart, beside the weights it belongs to.
_CACHE_DIRNAME = "openvino_cache"


def _openvino_device() -> str | None:
    """The best OpenVINO EP device inferred from the host, or ``None`` if unavailable.

    The standalone ``openvino`` Python wheel must not be imported here. It ships a second,
    ABI-distinct copy of the same native libraries bundled by ``onnxruntime-openvino``;
    whichever core loads first poisons the other runtime's frontend. Hardware detection
    already knows whether this is an Intel render node and does not load either copy.
    """
    try:
        from app.core.settings_service import get_settings_service
        from app.hardware.detect import detect_hardware

        requested = str(get_settings_service().get_nowait("processing.inference_device"))
        info = detect_hardware()
    except Exception as exc:
        log.debug("could not infer OpenVINO device", error=str(exc))
        return None

    devices = list(info.openvino_devices)
    if requested != "auto":
        if requested in devices:
            return requested
        log.warning(
            "requested inference device is not available; falling back",
            requested=requested,
            available=devices,
        )
    return info.preferred_inference_device


@cache
def onnx_providers() -> tuple[Any, ...]:
    """Execution providers in order of preference, best available first.

    Entries are either a provider name or a ``(name, options)`` pair, which is the shape
    both upstream inference libraries accept.
    """
    try:
        import onnxruntime as ort
    except ImportError:
        log.warning("onnxruntime is not installed; AI features will be unavailable")
        return ()

    available = set(ort.get_available_providers())
    chosen: list[Any] = []
    for name in _PREFERRED:
        if name not in available:
            continue
        if name == "OpenVINOExecutionProvider":
            device = _openvino_device()
            if device is None:
                # The provider is built in but OpenVINO sees nothing to run on. Naming it
                # anyway would fail session creation instead of falling through to CPU.
                log.warning("OpenVINO provider is present but no device is visible")
                continue
            options: dict[str, str] = {"device_type": device}
            cache_dir = _cache_dir()
            if cache_dir:
                options["cache_dir"] = cache_dir
            chosen.append((name, options))
            log.info("using the OpenVINO execution provider", device=device)
            continue
        chosen.append(name)

    if not chosen:
        # An unfamiliar build: take whatever it offers rather than refusing to run.
        chosen = list(ort.get_available_providers())

    log.info("onnx runtime providers selected", providers=[_name_of(p) for p in chosen])
    return tuple(chosen)


def _cache_dir() -> str | None:
    try:
        from app.config import get_config

        path = get_config().data_dir / _CACHE_DIRNAME
        path.mkdir(parents=True, exist_ok=True)
        return str(path)
    except Exception as exc:
        log.debug("could not prepare the OpenVINO cache directory", error=str(exc))
        return None


def _name_of(provider: Any) -> str:
    return provider[0] if isinstance(provider, tuple) else str(provider)


def describe_runtime() -> dict[str, object]:
    """Provider state for the diagnostics page.

    Reports the device as well as the provider. "OpenVINOExecutionProvider" alone does not
    tell you whether the work is on the iGPU or back on the CPU, which is the only thing
    anyone actually wants to know from this.
    """
    try:
        import onnxruntime as ort
    except ImportError:
        return {"available": False, "providers": [], "using": None, "device": None}

    providers = onnx_providers()
    active = providers[0] if providers else None
    device = active[1].get("device_type") if isinstance(active, tuple) else None
    return {
        "available": bool(providers),
        "providers": list(ort.get_available_providers()),
        "using": _name_of(active) if active else None,
        "device": device,
        "accelerated": bool(device and device != "CPU"),
    }
