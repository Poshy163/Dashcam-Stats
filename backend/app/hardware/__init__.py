"""Hardware discovery and FFmpeg interaction.

Everything the container can do with the iGPU is *probed* at runtime rather than assumed
from configuration, and every acceleration path degrades to CPU independently.
"""

from __future__ import annotations

from app.hardware.detect import HardwareInfo, detect_hardware, detect_hardware_async
from app.hardware.ffmpeg import (
    Crop,
    DecodeError,
    FFmpegError,
    ProbeError,
    ProbeResult,
    extract_frame,
    iter_frames,
    probe,
    select_hwaccel,
    write_thumbnail,
)

__all__ = [
    "Crop",
    "DecodeError",
    "FFmpegError",
    "HardwareInfo",
    "ProbeError",
    "ProbeResult",
    "detect_hardware",
    "detect_hardware_async",
    "extract_frame",
    "iter_frames",
    "probe",
    "select_hwaccel",
    "write_thumbnail",
]
