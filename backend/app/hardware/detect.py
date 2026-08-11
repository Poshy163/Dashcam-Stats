"""Runtime discovery of the video and inference hardware actually available.

Everything here is *probed*, not assumed. A capability list that says VAAPI exists is not
the same as VAAPI working — a render node can be present but unusable because the driver
is missing, the container account is not in the owning group, or the iGPU does not support
the codec. So the decode probe genuinely decodes something.

Each probe is independently fault-isolated. A failing VAAPI probe must not disable
OpenVINO inference and vice versa; both are optional and both degrade to CPU alone.
"""

from __future__ import annotations

import asyncio
import os
import platform
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from app.core.logging import get_logger

log = get_logger(__name__)

DRI_PATH = Path("/dev/dri")
PROBE_TIMEOUT_S = 20.0

# Vendor IDs seen on the PCI bus for anything that can present a DRM render node.
# Only the ones that matter here are named; anything else is reported by raw ID rather
# than guessed at.
_PCI_VENDORS = {
    0x8086: "Intel",
    0x1002: "AMD",
    0x1022: "AMD",
    0x10DE: "NVIDIA",
    0x15AD: "VMware",
    0x1AF4: "Red Hat (virtio)",
}

# Enough to name the common integrated parts. An unmatched device still reports its
# vendor and hex ID, which is more useful than a wrong marketing name.
_INTEL_FAMILIES: tuple[tuple[range, str], ...] = (
    (range(0xA780, 0xA7B0), "Intel Raptor Lake-P/H (Iris Xe)"),
    (range(0xA720, 0xA75F), "Intel Raptor Lake (UHD)"),
    (range(0x4680, 0x46FF), "Intel Alder Lake (UHD/Iris Xe)"),
    (range(0x4C80, 0x4CFF), "Intel Rocket Lake (UHD)"),
    (range(0x9A40, 0x9A80), "Intel Tiger Lake (Iris Xe)"),
    (range(0x8A50, 0x8A60), "Intel Ice Lake (Iris Plus)"),
    (range(0x5900, 0x5930), "Intel Kaby Lake (HD/UHD)"),
    (range(0x3E90, 0x3EA0), "Intel Coffee Lake (UHD)"),
    (range(0x1900, 0x1930), "Intel Skylake (HD)"),
    (range(0x4E50, 0x4E80), "Intel Jasper Lake (UHD)"),
    (range(0x4900, 0x4910), "Intel DG1"),
    (range(0x5690, 0x56C1), "Intel Arc"),
)


@dataclass(slots=True)
class HardwareInfo:
    """A snapshot of what this container can actually use."""

    drm_devices: list[str] = field(default_factory=list)
    render_nodes: list[str] = field(default_factory=list)
    gpu_name: str | None = None
    gpu_vendor: str | None = None
    gpu_pci_id: str | None = None
    driver: str | None = None

    vaapi_available: bool = False
    vaapi_device: str | None = None
    vaapi_driver: str | None = None
    vaapi_decode_codecs: list[str] = field(default_factory=list)
    qsv_available: bool = False

    openvino_available: bool = False
    openvino_version: str | None = None
    openvino_devices: list[str] = field(default_factory=list)
    openvino_device_names: dict[str, str] = field(default_factory=dict)
    onnxruntime_available: bool = False
    onnxruntime_providers: list[str] = field(default_factory=list)

    cpu_model: str | None = None
    cpu_cores: int = 0

    ffmpeg_path: str | None = None
    ffmpeg_version: str | None = None
    ffmpeg_hwaccels: list[str] = field(default_factory=list)

    #: Human-readable diagnostics surfaced verbatim on the Settings > Advanced page.
    #: This is where "you mapped /dev/dri but the account cannot open it" gets said.
    notes: list[str] = field(default_factory=list)

    @property
    def hardware_decode(self) -> bool:
        return self.vaapi_available or self.qsv_available

    @property
    def preferred_inference_device(self) -> str:
        if self.openvino_available:
            for kind in ("GPU", "NPU", "CPU"):
                exact = next((device for device in self.openvino_devices if device == kind), None)
                if exact:
                    return exact
                variant = next(
                    (device for device in self.openvino_devices if device.startswith(f"{kind}.")),
                    None,
                )
                if variant:
                    return variant
        return "CPU"

    def as_dict(self) -> dict[str, object]:
        return {
            "gpu": {
                "name": self.gpu_name,
                "vendor": self.gpu_vendor,
                "pci_id": self.gpu_pci_id,
                "driver": self.driver,
                "drm_devices": self.drm_devices,
                "render_nodes": self.render_nodes,
            },
            "decode": {
                "vaapi_available": self.vaapi_available,
                "vaapi_device": self.vaapi_device,
                "vaapi_driver": self.vaapi_driver,
                "vaapi_codecs": self.vaapi_decode_codecs,
                "qsv_available": self.qsv_available,
                "hardware_decode": self.hardware_decode,
            },
            "inference": {
                "openvino_available": self.openvino_available,
                "openvino_version": self.openvino_version,
                "openvino_devices": self.openvino_devices,
                "openvino_device_names": self.openvino_device_names,
                "onnxruntime_available": self.onnxruntime_available,
                "onnxruntime_providers": self.onnxruntime_providers,
                "preferred_device": self.preferred_inference_device,
            },
            "cpu": {"model": self.cpu_model, "cores": self.cpu_cores},
            "ffmpeg": {
                "path": self.ffmpeg_path,
                "version": self.ffmpeg_version,
                "hwaccels": self.ffmpeg_hwaccels,
            },
            "notes": self.notes,
        }


# --------------------------------------------------------------------------------------
# Individual probes. Each one swallows its own failures.
# --------------------------------------------------------------------------------------


def _run(cmd: list[str], timeout: float = PROBE_TIMEOUT_S) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)


def _read_hex(path: Path) -> int | None:
    try:
        return int(path.read_text().strip(), 16)
    except (OSError, ValueError):
        return None


def _describe_gpu(info: HardwareInfo) -> None:
    """Name the GPU from sysfs PCI identifiers."""
    for card in sorted(Path("/sys/class/drm").glob("card[0-9]*")):
        device = card / "device"
        vendor_id = _read_hex(device / "vendor")
        device_id = _read_hex(device / "device")
        if vendor_id is None or device_id is None:
            continue

        info.gpu_vendor = _PCI_VENDORS.get(vendor_id, f"0x{vendor_id:04x}")
        info.gpu_pci_id = f"{vendor_id:04x}:{device_id:04x}"

        name = None
        if vendor_id == 0x8086:
            for id_range, family in _INTEL_FAMILIES:
                if device_id in id_range:
                    name = family
                    break
            name = name or f"Intel iGPU (device 0x{device_id:04x})"
        elif vendor_id in (0x1002, 0x1022):
            name = f"AMD GPU (device 0x{device_id:04x})"
        else:
            name = f"{info.gpu_vendor} GPU (device 0x{device_id:04x})"
        info.gpu_name = name

        try:
            driver_link = (device / "driver").resolve()
            info.driver = driver_link.name
        except OSError:
            pass
        return


def _enumerate_dri(info: HardwareInfo) -> None:
    if not DRI_PATH.exists():
        info.notes.append(
            "/dev/dri is not present — running CPU-only. Add 'devices: [/dev/dri:/dev/dri]' "
            "to docker-compose.yml to enable hardware acceleration."
        )
        return

    try:
        entries = sorted(p.name for p in DRI_PATH.iterdir())
    except OSError as exc:
        info.notes.append(f"/dev/dri exists but could not be listed: {exc}")
        return

    info.drm_devices = entries
    info.render_nodes = [str(DRI_PATH / n) for n in entries if n.startswith("render")]

    if not info.render_nodes:
        info.notes.append("/dev/dri contains no render node; hardware decode is unavailable.")
        return

    # A mapped-but-unreadable render node is the single most common reason hardware
    # acceleration silently does not work, so say so explicitly rather than just
    # reporting 'unavailable'.
    unreadable = [n for n in info.render_nodes if not os.access(n, os.R_OK | os.W_OK)]
    if unreadable:
        info.notes.append(
            f"render node(s) {', '.join(unreadable)} are not accessible to uid {os.getuid()}. "
            "The container account must belong to the group that owns the device."
        )


def _probe_ffmpeg(info: HardwareInfo) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        info.notes.append("ffmpeg not found on PATH — video processing is unavailable.")
        return
    info.ffmpeg_path = ffmpeg

    try:
        out = _run([ffmpeg, "-hide_banner", "-version"]).stdout.decode("utf-8", "replace")
        first = out.splitlines()[0] if out else ""
        m = re.search(r"ffmpeg version (\S+)", first)
        info.ffmpeg_version = m.group(1) if m else first.strip() or None
    except (OSError, subprocess.SubprocessError, IndexError):
        pass

    try:
        out = _run([ffmpeg, "-hide_banner", "-hwaccels"]).stdout.decode("utf-8", "replace")
        info.ffmpeg_hwaccels = [line.strip() for line in out.splitlines()[1:] if line.strip()]
    except (OSError, subprocess.SubprocessError):
        pass


def _probe_vaapi(info: HardwareInfo) -> None:
    """Decode a real clip on each render node rather than trusting -hwaccels.

    `ffmpeg -hwaccels` lists what the binary was *built* with, which says nothing about
    whether this machine's driver and permissions let it work.
    """
    if not info.ffmpeg_path or not info.render_nodes:
        return
    if "vaapi" not in info.ffmpeg_hwaccels:
        info.notes.append("this ffmpeg build has no VAAPI support")
        return

    for node in info.render_nodes:
        working: list[str] = []
        for codec, encoder in (("h264", "libx264"), ("hevc", "libx265")):
            try:
                # Generate a tiny clip and immediately decode it back through VAAPI. If
                # the encoder is unavailable the probe is inconclusive, not a failure.
                make = _run(
                    [
                        info.ffmpeg_path,
                        "-hide_banner",
                        "-v",
                        "error",
                        "-y",
                        "-f",
                        "lavfi",
                        "-i",
                        "testsrc2=size=320x240:rate=10:duration=0.5",
                        "-c:v",
                        encoder,
                        "-pix_fmt",
                        "yuv420p",
                        "-f",
                        "mpegts",
                        "-",
                    ]
                )
                if make.returncode != 0 or not make.stdout:
                    continue

                probe = subprocess.run(
                    [
                        info.ffmpeg_path,
                        "-hide_banner",
                        "-v",
                        "error",
                        "-hwaccel",
                        "vaapi",
                        "-vaapi_device",
                        node,
                        "-f",
                        "mpegts",
                        "-i",
                        "pipe:0",
                        "-f",
                        "null",
                        "-",
                    ],
                    input=make.stdout,
                    capture_output=True,
                    timeout=PROBE_TIMEOUT_S,
                    check=False,
                )
                if probe.returncode == 0:
                    working.append(codec)
            except (OSError, subprocess.SubprocessError):
                continue

        if working:
            info.vaapi_available = True
            info.vaapi_device = node
            info.vaapi_decode_codecs = working
            info.vaapi_driver = os.environ.get("LIBVA_DRIVER_NAME")
            return

    info.notes.append(
        "a render node is present but no VAAPI decode probe succeeded; "
        "falling back to software decoding."
    )


def _probe_qsv(info: HardwareInfo) -> None:
    # QSV shares the same device as VAAPI on Intel and adds little over it here, so it is
    # only reported, never preferred over a working VAAPI path.
    info.qsv_available = (
        "qsv" in info.ffmpeg_hwaccels and info.vaapi_available and (info.gpu_vendor == "Intel")
    )


def _probe_openvino(info: HardwareInfo) -> None:
    """Probe the standalone OpenVINO runtime and the devices it can actually open."""
    try:
        import openvino as ov
    except Exception as exc:
        info.notes.append(f"OpenVINO not available ({type(exc).__name__}); inference will use CPU")
        return

    try:
        core = ov.Core()
        info.openvino_version = getattr(ov, "__version__", None)
        info.openvino_devices = list(core.available_devices)
        info.openvino_available = bool(info.openvino_devices)
        for device in info.openvino_devices:
            try:
                info.openvino_device_names[device] = str(
                    core.get_property(device, "FULL_DEVICE_NAME")
                )
            except Exception:
                info.openvino_device_names[device] = device
    except Exception as exc:
        info.notes.append(f"OpenVINO present but failed to initialise: {exc}")
        return

    if not any(device == "GPU" or device.startswith("GPU.") for device in info.openvino_devices):
        info.notes.append(
            "OpenVINO could not open an Intel GPU; inference will run on CPU. "
            "Check the OpenCL runtime and /dev/dri permissions."
        )


def _probe_onnxruntime(info: HardwareInfo) -> None:
    try:
        import onnxruntime as ort

        info.onnxruntime_available = True
        info.onnxruntime_providers = list(ort.get_available_providers())
    except Exception:
        pass


def _probe_cpu(info: HardwareInfo) -> None:
    info.cpu_cores = os.cpu_count() or 0
    try:
        text = Path("/proc/cpuinfo").read_text()
        m = re.search(r"^model name\s*:\s*(.+)$", text, re.MULTILINE)
        if m:
            info.cpu_model = m.group(1).strip()
    except OSError:
        info.cpu_model = platform.processor() or None


# --------------------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------------------


def _detect() -> HardwareInfo:
    info = HardwareInfo()
    for probe in (
        _enumerate_dri,
        _describe_gpu,
        _probe_ffmpeg,
        _probe_vaapi,
        _probe_qsv,
        _probe_onnxruntime,
        _probe_cpu,
        _probe_openvino,
    ):
        try:
            probe(info)
        except Exception as exc:
            # One probe blowing up must never take the others with it.
            info.notes.append(f"{probe.__name__} failed: {type(exc).__name__}: {exc}")
            log.warning("hardware probe failed", probe=probe.__name__, error=str(exc))
    return info


@lru_cache(maxsize=1)
def _cached_detect() -> HardwareInfo:
    return _detect()


def detect_hardware(*, force: bool = False) -> HardwareInfo:
    """Detected hardware, cached because probing spawns several subprocesses."""
    if force:
        _cached_detect.cache_clear()
    return _cached_detect()


async def detect_hardware_async(*, force: bool = False) -> HardwareInfo:
    """Detection off the event loop — the VAAPI probes are blocking and slow."""
    return await asyncio.to_thread(detect_hardware, force=force)
