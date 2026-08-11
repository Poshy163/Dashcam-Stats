"""Shared native thread budgets for media and inference libraries.

FFmpeg, OpenCV and ONNX Runtime each assume they own the machine and default to a thread
pool sized to every CPU core. That is reasonable for one command-line process, but this
application deliberately runs several recording workers at once. Leaving all three at
their defaults multiplies the host's core count by the worker count and spends more time
context-switching than doing useful work.
"""

from __future__ import annotations

import os

from app.core.settings_service import get_settings_service

_AUTO_CAP = 4


def native_thread_budget() -> int:
    """Return the per-job native thread budget.

    A positive ``advanced.ffmpeg_threads`` value is an explicit operator choice and is
    honoured for every native runtime. Zero means bounded automatic sizing: divide the
    logical CPUs between workers, but never give one job more than four threads. The cap
    matters on large hosts where two 20-thread FFmpeg filter pools were saturating all
    cores while OpenVINO waited for its next frame.
    """
    try:
        settings = get_settings_service()
    except RuntimeError:
        # Media helpers are also used by one-off scripts and standalone tests that do
        # not boot the FastAPI lifespan. Keep those callers bounded without requiring
        # the application settings service.
        configured = 0
        workers = 1
    else:
        configured = int(settings.get_nowait("advanced.ffmpeg_threads"))
        workers = max(1, int(settings.get_nowait("processing.max_workers")))
    if configured > 0:
        return configured

    cpus = max(1, os.cpu_count() or 1)
    return max(1, min(_AUTO_CAP, cpus // workers))


def configure_opencv_threads() -> int:
    """Apply the shared budget to OpenCV and return the chosen value."""
    budget = native_thread_budget()
    try:
        import cv2

        cv2.setNumThreads(budget)
    except (ImportError, AttributeError):
        pass
    return budget


def onnx_session_options():
    """Build conservative options for the emergency ONNX Runtime CPU fallback.

    Direct OpenVINO sessions ignore these options. They are passed through to the upstream
    model helpers so a graph that OpenVINO cannot compile can still run on ONNX Runtime
    without its worker threads spinning between inference calls.
    """
    import onnxruntime as ort

    options = ort.SessionOptions()
    options.intra_op_num_threads = native_thread_budget()
    options.inter_op_num_threads = 1
    options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    options.add_session_config_entry("session.intra_op.allow_spinning", "0")
    options.add_session_config_entry("session.inter_op.allow_spinning", "0")
    return options
