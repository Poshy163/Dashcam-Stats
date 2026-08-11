"""Inference runtime selection and diagnostics.

Models execute through the standalone OpenVINO runtime, not ONNX Runtime's OpenVINO
execution provider.  The provider wheel trails the current runtime and bundles native
libraries that cannot safely coexist with a newer standalone OpenVINO build.
"""

from __future__ import annotations

from functools import cache

from app.ai.openvino_session import (
    available_devices,
    selected_device,
    selected_performance_hint,
)
from app.core.logging import get_logger

log = get_logger(__name__)


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
