"""Which ONNX Runtime execution providers to use, and in what order.

The upstream inference classes all take a ``providers`` sequence, so this is the one place
that decides where inference actually runs.

The image ships plain ``onnxruntime``, which offers only the CPU provider. That is a
deliberate choice rather than an oversight: ``onnxruntime-openvino`` installs under the
same ``onnxruntime`` module name *and* bundles its own build of the OpenVINO runtime,
which the ``openvino`` package used for hardware probing also loads. Two builds of the
same native library in one process is not a risk worth taking for a workload where plate
detection runs on a handful of crops per vehicle track.

Nothing here is conditional on that staying true. If a future image installs
``onnxruntime-openvino``, the provider simply appears in ``get_available_providers()`` and
gets picked up, with the iGPU used through the same ``/dev/dri`` render node as VAAPI. No
code change, no setting to flip.
"""

from __future__ import annotations

from functools import cache

from app.core.logging import get_logger

log = get_logger(__name__)

#: Most specific first — ONNX Runtime assigns each node to the first provider that will
#: take it, so ordering is what expresses the preference.
_PREFERRED = ("OpenVINOExecutionProvider", "CUDAExecutionProvider", "CPUExecutionProvider")


@cache
def onnx_providers() -> tuple[str, ...]:
    """Execution providers in order of preference, best available first."""
    try:
        import onnxruntime as ort
    except ImportError:
        log.warning("onnxruntime is not installed; AI features will be unavailable")
        return ()

    available = set(ort.get_available_providers())
    chosen = tuple(p for p in _PREFERRED if p in available)
    if not chosen:
        # An unfamiliar build: take whatever it offers rather than refusing to run.
        chosen = tuple(ort.get_available_providers())

    log.info("onnx runtime providers selected", providers=list(chosen))
    return chosen


def describe_runtime() -> dict[str, object]:
    """Provider state for the diagnostics page."""
    try:
        import onnxruntime as ort
    except ImportError:
        return {"available": False, "providers": [], "using": None}

    providers = onnx_providers()
    return {
        "available": bool(providers),
        "providers": list(ort.get_available_providers()),
        "using": providers[0] if providers else None,
    }
