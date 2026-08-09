"""All FFmpeg interaction.

Two things here exist specifically because of what the real footage turned out to be:

* **Frame rate is recovered, not read.** The corpus reports ``r_frame_rate`` values like
  ``90000/1`` and ``299/12`` on short or damaged segments, because ffprobe is estimating
  from PTS deltas. Anything implausible is rejected and the rate is counted instead.
* **PTS wraparound is clamped.** MPEG-TS timestamps are 33 bits at 90 kHz, so they wrap
  every ~95443 s. Two files in the corpus report ~95377 s durations for 9 MB of data.
  Taking that at face value would corrupt every downstream duration and seek.

``iter_frames`` applies crop and scale *inside* the filter graph. That matters: the
telemetry pass needs a 1920x50 strip at 1 fps, and pulling full 1080p frames across the
pipe to crop them in Python would be roughly forty times the data for no benefit.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import shutil
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from app.core.logging import get_logger
from app.hardware.detect import detect_hardware

log = get_logger(__name__)

#: 2**33 / 90000 — the period of a 33-bit PTS counter at 90 kHz.
PTS_WRAP_SECONDS = 95443.717

#: Above this a reported duration is a wrapped timestamp rather than a real recording.
#:
#: Deliberately *below* the wrap period. A wrap makes the last timestamp smaller than the
#: first, so the container reports ``wrap - elapsed`` — a minute-long clip comes back as
#: 95,376 s. Testing for a duration at or beyond the wrap point therefore never fires, and
#: on a real library it never did: both affected files sat a minute under it and their
#: durations went straight into the dashboard's footage total.
#:
#: The margin is one hour, which no dashcam segment approaches and every wrapped one clears.
PTS_WRAP_THRESHOLD_S = PTS_WRAP_SECONDS - 3600.0

#: Floor on a bitrate before it is trusted to reconstruct a duration.
#:
#: ffprobe computes the bitrate from the duration, so on a file with a wrapped timestamp
#: the two are wrong together and reconstructing one from the other returns the same wrong
#: answer. 802 bps for 1080p video, as one of these files reports, is the tell.
MIN_PLAUSIBLE_BITRATE = 100_000

#: Beyond this a "frame rate" is ffprobe's estimator misfiring, not a real camera.
MAX_PLAUSIBLE_FPS = 120.0
MIN_PLAUSIBLE_FPS = 1.0

DEFAULT_PROBE_TIMEOUT = 60.0
DEFAULT_DECODE_TIMEOUT = 900.0


class FFmpegError(RuntimeError):
    """FFmpeg or ffprobe failed. Carries the tail of stderr for the job log."""

    def __init__(self, message: str, *, stderr: str = "", returncode: int | None = None) -> None:
        super().__init__(message)
        self.stderr = stderr
        self.returncode = returncode


class DecodeError(FFmpegError):
    """The stream could not be decoded — a corrupt or truncated recording."""


class ProbeError(FFmpegError):
    """The file could not be inspected at all — empty, missing, or not media."""


def _binary(name: str) -> str:
    found = shutil.which(name)
    if not found:
        raise FFmpegError(f"{name} not found on PATH")
    return found


def ffmpeg_path() -> str:
    return _binary("ffmpeg")


def ffprobe_path() -> str:
    return _binary("ffprobe")


# --------------------------------------------------------------------------------------
# Probing
# --------------------------------------------------------------------------------------


def _parse_rate(value: Any) -> float | None:
    """Parse ffprobe's ``num/den`` rate strings, rejecting the implausible ones."""
    if not value or not isinstance(value, str) or "/" not in value:
        return None
    num_s, _, den_s = value.partition("/")
    try:
        num, den = float(num_s), float(den_s)
    except ValueError:
        return None
    if den == 0 or num == 0:
        return None
    rate = num / den
    if not math.isfinite(rate) or not (MIN_PLAUSIBLE_FPS <= rate <= MAX_PLAUSIBLE_FPS):
        return None
    return rate


@dataclass(slots=True)
class ProbeResult:
    """Normalised ffprobe output, with this corpus's quirks already accounted for."""

    path: str
    size_bytes: int = 0
    container: str | None = None
    duration_s: float | None = None
    bitrate: int | None = None
    start_time: float = 0.0

    video_codec: str | None = None
    video_profile: str | None = None
    width: int | None = None
    height: int | None = None
    pix_fmt: str | None = None
    #: Rate actually used downstream, after validation and possible recounting.
    fps: float | None = None
    #: What the container claimed, kept for diagnostics because it is often wrong.
    fps_container: float | None = None

    has_audio: bool = False
    audio_codec: str | None = None
    audio_sample_rate: int | None = None
    audio_channels: int | None = None

    #: True when the reported duration was a wrapped 33-bit PTS and had to be recomputed.
    pts_wrapped: bool = False
    #: Non-fatal oddities worth recording against the recording row.
    warnings: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def has_video(self) -> bool:
        return self.video_codec is not None

    @property
    def resolution(self) -> str | None:
        return f"{self.width}x{self.height}" if self.width and self.height else None


async def _run(
    cmd: list[str], timeout: float, stdin: bytes | None = None
) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin), timeout=timeout)
    except TimeoutError:
        # A hung ffmpeg holds a decoder and a file handle; kill it rather than leak it.
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        raise FFmpegError(f"timed out after {timeout:.0f}s: {' '.join(cmd[:3])}") from None
    return proc.returncode or 0, out, err


async def ffprobe_raw(
    path: Path | str, *, timeout: float = DEFAULT_PROBE_TIMEOUT
) -> dict[str, Any]:
    cmd = [
        ffprobe_path(),
        "-hide_banner",
        "-v",
        "error",
        "-show_format",
        "-show_streams",
        "-print_format",
        "json",
        str(path),
    ]
    code, out, err = await _run(cmd, timeout)
    stderr = err.decode("utf-8", "replace").strip()
    if not out.strip():
        raise ProbeError(
            f"ffprobe produced no output for {Path(path).name}",
            stderr=stderr[-2000:],
            returncode=code,
        )
    try:
        return json.loads(out.decode("utf-8", "replace"))
    except json.JSONDecodeError as exc:
        raise ProbeError(
            f"ffprobe output was not valid JSON for {Path(path).name}",
            stderr=stderr[-2000:],
            returncode=code,
        ) from exc


async def _count_fps(path: Path | str, *, sample_s: float = 10.0) -> float | None:
    """Recover the real frame rate by counting decoded frames over a bounded window."""
    cmd = [
        ffprobe_path(),
        "-hide_banner",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-count_packets",
        "-read_intervals",
        f"%+{sample_s:g}",
        "-show_entries",
        "stream=nb_read_packets",
        "-print_format",
        "json",
        str(path),
    ]
    try:
        code, out, _ = await _run(cmd, DEFAULT_PROBE_TIMEOUT)
        if code != 0:
            return None
        streams = json.loads(out.decode("utf-8", "replace")).get("streams") or []
        packets = int(streams[0].get("nb_read_packets", 0)) if streams else 0
    except (FFmpegError, json.JSONDecodeError, ValueError, IndexError, KeyError):
        return None
    if packets <= 1:
        return None
    rate = packets / sample_s
    return rate if MIN_PLAUSIBLE_FPS <= rate <= MAX_PLAUSIBLE_FPS else None


async def probe(path: Path | str, *, timeout: float = DEFAULT_PROBE_TIMEOUT) -> ProbeResult:
    """Inspect a recording, correcting the container's unreliable claims."""
    p = Path(path)
    result = ProbeResult(path=str(p))

    try:
        result.size_bytes = p.stat().st_size
    except OSError as exc:
        raise ProbeError(f"cannot stat {p.name}: {exc}") from exc

    # Zero-byte segments genuinely occur in this corpus; there is nothing to probe.
    if result.size_bytes == 0:
        raise ProbeError(f"{p.name} is empty (0 bytes)")

    data = await ffprobe_raw(p, timeout=timeout)
    result.raw = data

    fmt = data.get("format") or {}
    result.container = fmt.get("format_name")
    result.bitrate = _int_or_none(fmt.get("bit_rate"))
    result.start_time = _float_or_zero(fmt.get("start_time"))
    duration = _float_or_none(fmt.get("duration"))

    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    if video is None:
        raise ProbeError(f"{p.name} contains no video stream")

    result.video_codec = video.get("codec_name")
    result.video_profile = video.get("profile")
    result.width = _int_or_none(video.get("width"))
    result.height = _int_or_none(video.get("height"))
    result.pix_fmt = video.get("pix_fmt")

    if audio is not None:
        result.has_audio = True
        result.audio_codec = audio.get("codec_name")
        result.audio_sample_rate = _int_or_none(audio.get("sample_rate"))
        result.audio_channels = _int_or_none(audio.get("channels"))

    # --- frame rate ---------------------------------------------------------------
    container_rate = _parse_rate(video.get("r_frame_rate")) or _parse_rate(
        video.get("avg_frame_rate")
    )
    result.fps_container = container_rate
    if container_rate is None:
        # Rejected as implausible, so count instead. This is the 90000/1 case.
        counted = await _count_fps(p)
        if counted is not None:
            result.fps = counted
            result.warnings.append(
                f"container reported an implausible frame rate "
                f"({video.get('r_frame_rate')}); measured {counted:.2f} fps"
            )
        else:
            result.warnings.append("frame rate could not be determined")
    else:
        result.fps = container_rate

    # --- duration -----------------------------------------------------------------
    stream_duration = _float_or_none(video.get("duration"))
    if duration is None:
        duration = stream_duration

    if duration is not None and duration >= PTS_WRAP_THRESHOLD_S:
        # A wrapped 33-bit PTS lands just *below* the wrap period, not above it. When the
        # last timestamp has wrapped past the first, the container reports
        # ``wrap - elapsed``, so a one-minute clip comes back as 95,376 s rather than
        # 95,444. The original test required the duration to exceed the wrap point, which
        # nothing ever does, so the clamp never fired for either of the two files it was
        # written for. Their bogus durations were stored, and between them they accounted
        # for 190,753 of the 260,817 s the dashboard reported as the library's footage --
        # 72 hours claimed against about 19 real ones.
        result.pts_wrapped = True
        recovered = None

        # The bitrate is not independent evidence here and must not be trusted as if it
        # were: ffprobe derives it from the same broken duration. One of these files
        # reports 802 bps for 1080p video, and ``size * 8 / 802`` comes back as 95,441 s --
        # the wrap period again, wearing a different hat and comfortably inside any naive
        # sanity check.
        if result.bitrate and result.bitrate >= MIN_PLAUSIBLE_BITRATE:
            recovered = result.size_bytes * 8 / result.bitrate
        if recovered is None or not (0 < recovered < PTS_WRAP_THRESHOLD_S):
            recovered = await _measure_duration(p)
        if recovered is not None and not (0 < recovered < PTS_WRAP_THRESHOLD_S):
            # Decoding did not help either. Better to admit the duration is unknown than
            # to store a number that inflates every rollup built on top of it.
            recovered = None
        result.warnings.append(
            f"container duration {duration:.0f}s exceeds the 33-bit PTS wrap point; "
            f"using {recovered:.1f}s"
            if recovered
            else f"container duration {duration:.0f}s is a wrapped PTS and could not be recovered"
        )
        duration = recovered

    result.duration_s = duration
    if duration is not None and duration <= 0:
        result.warnings.append("duration is zero or negative")

    return result


async def _measure_duration(path: Path | str) -> float | None:
    """Last resort: decode the stream and report where it ended."""
    cmd = [
        ffmpeg_path(),
        "-hide_banner",
        "-v",
        "error",
        "-i",
        str(path),
        "-map",
        "0:v:0",
        "-f",
        "null",
        "-",
        "-progress",
        "pipe:1",
        "-nostats",
    ]
    try:
        code, out, _ = await _run(cmd, DEFAULT_DECODE_TIMEOUT)
    except FFmpegError:
        return None
    if code != 0:
        return None
    best = None
    for line in out.decode("utf-8", "replace").splitlines():
        if line.startswith("out_time_ms="):
            with contextlib.suppress(ValueError):
                best = int(line.split("=", 1)[1]) / 1_000_000
    return best


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _float_or_zero(value: Any) -> float:
    return _float_or_none(value) or 0.0


# --------------------------------------------------------------------------------------
# Decoding
# --------------------------------------------------------------------------------------


def select_hwaccel(preference: str, codec: str | None) -> tuple[list[str], str]:
    """Choose input-side hardware acceleration flags.

    Returns ``(args, label)`` where the label is what the UI displays as the decoder in
    use. Falls back to software whenever the requested path is not actually available.
    """
    if preference == "cpu":
        return [], "software"

    hw = detect_hardware()
    if preference in ("auto", "vaapi", "qsv"):
        if hw.vaapi_available and hw.vaapi_device:
            if codec and hw.vaapi_decode_codecs and codec not in hw.vaapi_decode_codecs:
                return [], "software"
            # Output is downloaded back to system memory: every consumer here is numpy or
            # an ffmpeg filter, so keeping frames on the GPU would only add a copy.
            return (
                [
                    "-hwaccel",
                    "vaapi",
                    "-vaapi_device",
                    hw.vaapi_device,
                    "-hwaccel_output_format",
                    "nv12",
                ],
                "vaapi",
            )
    return [], "software"


@dataclass(slots=True)
class Crop:
    """Pixel crop applied inside the filter graph."""

    x: int
    y: int
    width: int
    height: int

    def to_filter(self) -> str:
        return f"crop={self.width}:{self.height}:{self.x}:{self.y}"


def build_filter_chain(
    *,
    fps: float | None = None,
    crop: Crop | None = None,
    scale: tuple[int, int] | None = None,
    hwaccel_label: str = "software",
    pix_fmt: str = "bgr24",
) -> str:
    filters: list[str] = []
    # Deliberately no `hwdownload` here. `select_hwaccel` passes
    # `-hwaccel_output_format nv12`, which already hands frames back in system memory, so
    # adding hwdownload gives that filter software input it cannot accept and the graph
    # fails to configure -- turning every hardware-accelerated decode into an error. The
    # two settings have to agree: either output vaapi surfaces and download in the graph,
    # or download at the decoder and filter normally. Everything downstream here is numpy
    # or a CPU filter, so downloading at the decoder is the cheaper half of that choice.
    if fps:
        filters.append(f"fps={fps:g}")
    if crop:
        filters.append(crop.to_filter())
    if scale:
        filters.append(f"scale={scale[0]}:{scale[1]}")
    filters.append(f"format={pix_fmt}")
    return ",".join(filters)


async def _decode_frames(
    path: Path | str,
    *,
    fps: float | None = None,
    crop: Crop | None = None,
    scale: tuple[int, int] | None = None,
    start: float | None = None,
    duration: float | None = None,
    hwaccel: str = "auto",
    codec: str | None = None,
    grayscale: bool = False,
    timeout: float = DEFAULT_DECODE_TIMEOUT,
) -> AsyncIterator[tuple[float, np.ndarray]]:
    """One decode attempt. Callers should use :func:`iter_frames`, which adds fallback."""
    hw_args, label = select_hwaccel(hwaccel, codec)
    pix_fmt = "gray" if grayscale else "bgr24"
    channels = 1 if grayscale else 3

    # Frame geometry has to be known up front to slice the raw stream, so resolve it
    # from the crop/scale that will actually be applied.
    if scale:
        width, height = scale
    elif crop:
        width, height = crop.width, crop.height
    else:
        info = await probe(path)
        if not info.width or not info.height:
            raise DecodeError(f"cannot determine frame size for {Path(path).name}")
        width, height = info.width, info.height

    chain = build_filter_chain(
        fps=fps, crop=crop, scale=scale, hwaccel_label=label, pix_fmt=pix_fmt
    )

    cmd = [ffmpeg_path(), "-hide_banner", "-v", "error", "-nostdin"]
    if start:
        # Before -i so the seek is done by demuxing rather than decode-and-discard.
        cmd += ["-ss", f"{start:g}"]
    cmd += hw_args
    cmd += ["-i", str(path)]
    if duration:
        cmd += ["-t", f"{duration:g}"]
    cmd += ["-map", "0:v:0", "-vf", chain, "-f", "rawvideo", "-pix_fmt", pix_fmt, "pipe:1"]

    frame_bytes = width * height * channels
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stdout is not None

    stderr_chunks: list[bytes] = []

    async def _drain_stderr() -> None:
        # Left unread, a decoder that logs heavily on damaged files fills the pipe
        # buffer and deadlocks the process we are reading frames from.
        assert proc.stderr is not None
        while chunk := await proc.stderr.read(8192):
            stderr_chunks.append(chunk)

    drainer = asyncio.create_task(_drain_stderr())
    index = 0
    step = 1.0 / fps if fps else None
    base = start or 0.0

    try:
        while True:
            try:
                buf = await asyncio.wait_for(proc.stdout.readexactly(frame_bytes), timeout=timeout)
            except asyncio.IncompleteReadError:
                break
            except TimeoutError:
                raise DecodeError(
                    f"decode of {Path(path).name} stalled for {timeout:.0f}s"
                ) from None

            frame = np.frombuffer(buf, dtype=np.uint8)
            frame = frame.reshape((height, width) if grayscale else (height, width, channels))
            offset = base + (index * step if step else 0.0)
            yield offset, frame
            index += 1
    finally:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        with contextlib.suppress(Exception):
            await drainer

    stderr = b"".join(stderr_chunks).decode("utf-8", "replace")
    if index == 0:
        raise DecodeError(
            f"no frames decoded from {Path(path).name}",
            stderr=stderr[-2000:],
            returncode=proc.returncode,
        )
    if stderr.strip():
        # Damaged-but-partially-readable files are normal here; the frames already
        # yielded are still usable, so this is a warning rather than a failure.
        log.debug("decoder reported errors", file=Path(path).name, stderr=stderr[-500:])


async def iter_frames(
    path: Path | str,
    *,
    fps: float | None = None,
    crop: Crop | None = None,
    scale: tuple[int, int] | None = None,
    start: float | None = None,
    duration: float | None = None,
    hwaccel: str = "auto",
    codec: str | None = None,
    grayscale: bool = False,
    timeout: float = DEFAULT_DECODE_TIMEOUT,
) -> AsyncIterator[tuple[float, np.ndarray]]:
    """Yield ``(offset_seconds, frame)`` decoded from *path*, falling back to software.

    Crop and scale are applied in the filter graph, so only the pixels actually wanted
    cross the pipe. The offset is derived from the output frame index and the requested
    rate, which is exact because ``fps=`` resamples to a constant rate.

    A hardware decode that fails is retried in software. Acceleration depends on the
    driver, the codec and the specific file, and a stage that silently yields nothing is
    far worse than one that runs slower -- a misconfigured filter graph once produced zero
    telemetry across an entire library while every other stage reported success.
    """
    kwargs = {
        "fps": fps,
        "crop": crop,
        "scale": scale,
        "start": start,
        "duration": duration,
        "codec": codec,
        "grayscale": grayscale,
        "timeout": timeout,
    }
    _, label = select_hwaccel(hwaccel, codec)
    yielded = 0

    try:
        async for item in _decode_frames(path, hwaccel=hwaccel, **kwargs):
            yielded += 1
            yield item
        return
    except FFmpegError as exc:
        # Retrying is only safe before anything reached the caller; mid-stream the
        # consumer has already seen frames and would receive them twice.
        if label == "software" or yielded:
            raise
        log.warning(
            "hardware decode failed; retrying in software",
            file=Path(path).name,
            decoder=label,
            error=str(exc),
        )

    async for item in _decode_frames(path, hwaccel="cpu", **kwargs):
        yield item


async def extract_frame(
    path: Path | str, t: float, *, hwaccel: str = "auto", codec: str | None = None
) -> np.ndarray | None:
    """Single BGR frame at *t*, or None when it cannot be decoded."""
    try:
        async for _, frame in iter_frames(
            path, start=t, duration=0.5, fps=None, hwaccel=hwaccel, codec=codec
        ):
            return frame
    except (DecodeError, FFmpegError) as exc:
        log.debug("frame extraction failed", file=Path(path).name, t=t, error=str(exc))
    return None


async def write_thumbnail(
    path: Path | str,
    out: Path,
    *,
    t: float = 1.0,
    width: int = 480,
    quality: int = 80,
    hwaccel: str = "auto",
) -> bool:
    """Write a JPEG thumbnail. Returns False rather than raising on unreadable input."""
    out.parent.mkdir(parents=True, exist_ok=True)
    hw_args, label = select_hwaccel(hwaccel, None)
    chain = build_filter_chain(scale=(width, -2), hwaccel_label=label, pix_fmt="yuvj420p")

    cmd = [
        ffmpeg_path(),
        "-hide_banner",
        "-v",
        "error",
        "-nostdin",
        "-y",
        "-ss",
        f"{t:g}",
        *hw_args,
        "-i",
        str(path),
        "-frames:v",
        "1",
        "-vf",
        chain,
        "-q:v",
        str(max(2, min(31, int(31 - (quality / 100) * 29)))),
        str(out),
    ]
    try:
        code, _, err = await _run(cmd, DEFAULT_PROBE_TIMEOUT)
    except FFmpegError:
        return False
    if code != 0 or not out.exists() or out.stat().st_size == 0:
        log.debug("thumbnail failed", file=Path(path).name, stderr=err.decode()[-300:])
        with contextlib.suppress(OSError):
            out.unlink()
        return False
    return True
