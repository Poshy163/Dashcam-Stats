"""Inference runtime selection and diagnostics.

Models execute through the standalone OpenVINO runtime, not ONNX Runtime's OpenVINO
execution provider.  The provider wheel trails the current runtime and bundles native
libraries that cannot safely coexist with a newer standalone OpenVINO build.
"""

from __future__ import annotations

from functools import cache

from app.ai.openvino_session import (
    available_devices,
    gpu_backend_disabled,
    selected_device,
    selected_performance_hint,
)
from app.core.logging import get_logger

log = get_logger(__name__)


def describe_media_policy() -> dict[str, object]:
    """How decode and inference are currently sharing the iGPU, and why.

    Surfaced rather than left implicit because the answer is usually "decoding in
    software", and without a reason beside it that reads on the queue page as a hardware
    fault. It is the opposite: it is the policy that keeps the hardware working, since this
    chip will not run VAAPI and OpenVINO at once.
    """
    from app.core.settings_service import get_settings_service
    from app.hardware.ffmpeg import media_health, select_hwaccel, software_decode_reason

    # Ask the thing that actually decides, with the preference the pipeline actually
    # passes it.
    #
    # Two goes at this were wrong in the same way. It first reported whatever the *policy*
    # permitted, which said "hardware" while every clip went through software once VAAPI
    # stopped existing. Then it asked `select_hwaccel` -- but hard-coded "auto", which is
    # not what the stages pass: they send "cpu" when hardware acceleration is switched off.
    # So turning that setting off halved the wall-clock time per recording, visibly, while
    # this went on reporting "vaapi".
    preference = "auto"
    reason = software_decode_reason()
    try:
        settings = get_settings_service()
        if not bool(settings.get_nowait("processing.hardware_acceleration")):
            preference = "cpu"
            reason = reason or "hardware acceleration is switched off in Settings"
        else:
            preference = str(settings.get_nowait("processing.decoder_preference"))
    except Exception as exc:
        log.debug("could not read the decode preference", error=str(exc))

    _, effective = select_hwaccel(preference, None)
    if reason is None and effective == "software":
        reason = "no hardware decoder is available on this machine"
    return {
        "decode": effective,
        "decode_reason": reason,
        "gpu_inference_disabled": gpu_backend_disabled(),
        "media_slot": media_health(),
    }


def _openvino_device() -> str | None:
    """Compatibility shim for callers and tests around device selection."""
    return selected_device()


@cache
def onnx_providers() -> tuple[str, ...]:
    """Providers for the emergency ONNX Runtime fallback.

    OpenVINO is deliberately absent here: inference uses it directly.  Keeping the plain
    CPU runtime lets one unsupported graph degrade gracefully without disabling the rest
    of the analysis pipeline.
    """
    try:
        import onnxruntime as ort
    except ImportError:
        return ()
    available = list(ort.get_available_providers())
    if "CPUExecutionProvider" in available:
        return ("CPUExecutionProvider",)
    return tuple(available)


def describe_runtime() -> dict[str, object]:
    """The engine and physical device that new model sessions will use."""
    devices = available_devices()
    device = _openvino_device()
    if device is not None:
        try:
            import openvino as ov

            version = getattr(ov, "__version__", None)
        except Exception:
            version = None
        return {
            "available": True,
            "engine": "OpenVINO",
            "version": version,
            "providers": devices,
            "using": "OpenVINO",
            "device": device,
            "accelerated": device.startswith(("GPU", "NPU")),
            "performance_hint": selected_performance_hint(),
        }

    fallback = onnx_providers()
    return {
        "available": bool(fallback),
        "engine": "ONNX Runtime" if fallback else None,
        "version": None,
        "providers": list(fallback),
        "using": fallback[0] if fallback else None,
        "device": "CPU" if fallback else None,
        "accelerated": False,
        "performance_hint": None,
    }
