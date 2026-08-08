"""Making dashcam footage playable in a browser.

The camera records MPEG-TS, and no browser can play it. Chrome, Firefox and Safari ship
demuxers for MP4, WebM and Ogg only -- there is no MPEG-TS support in any of them, so a
``<video src="...ts">`` element simply fails, however healthy the stream is.

The video inside is already H.264 and the audio already AAC, both natively playable. Only
the container is wrong, so this remuxes rather than transcodes: ``-c copy`` rewrites the
container and touches not a single frame. A two-minute segment converts in about a second
and the picture is bit-identical.

The result is cached rather than streamed on the fly, because a live remux cannot answer
HTTP range requests -- and without ranges the player cannot seek, which would break
clicking a detection to jump to its moment. Writing the MP4 once and serving that file
gives seeking for free.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from app.config import get_config
from app.core.logging import get_logger
from app.hardware.ffmpeg import DEFAULT_DECODE_TIMEOUT, FFmpegError, ffmpeg_path

log = get_logger(__name__)

#: Containers a browser can play directly. Anything else needs remuxing.
BROWSER_NATIVE_SUFFIXES = frozenset({".mp4", ".m4v", ".webm", ".ogg", ".ogv", ".mov"})

#: Per-recording locks so two viewers opening the same clip remux it once, not twice.
_locks: dict[int, asyncio.Lock] = {}
_locks_guard = asyncio.Lock()


@dataclass(slots=True)
class Playable:
    path: Path
    media_type: str
    #: True when the file was remuxed rather than served from the footage directory.
    from_cache: bool


def cache_dir() -> Path:
    path = get_config().cache_dir / "stream"
    path.mkdir(parents=True, exist_ok=True)
    return path


def is_browser_native(path: Path) -> bool:
    return path.suffix.lower() in BROWSER_NATIVE_SUFFIXES


async def _lock_for(recording_id: int) -> asyncio.Lock:
    async with _locks_guard:
        return _locks.setdefault(recording_id, asyncio.Lock())


async def _remux(source: Path, target: Path) -> bool:
    """Rewrite *source* into a faststart MP4 without re-encoding."""
    partial = target.with_suffix(".part.mp4")
    cmd = [
        ffmpeg_path(),
        "-hide_banner",
        "-v",
        "error",
        "-nostdin",
        "-y",
        # Dashcam segments carry a rolling PTS that can start well above zero and
        # occasionally wraps; without this the MP4 inherits a huge start offset and the
        # player shows a duration of hours for a two-minute clip.
        "-fflags",
        "+genpts+igndts",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-c",
        "copy",
        # H.264 in MPEG-TS is Annex-B; MP4 needs length-prefixed NALs.
        "-bsf:v",
        "h264_mp4toannexb=0",
        # Puts the index at the front so playback can start before the whole file has
        # been fetched, and lets the browser seek immediately.
        "-movflags",
        "+faststart",
        str(partial),
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=DEFAULT_DECODE_TIMEOUT)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        partial.unlink(missing_ok=True)
        log.warning("remux timed out", file=source.name)
        return False

    if proc.returncode != 0 or not partial.exists() or partial.stat().st_size == 0:
        detail = stderr.decode("utf-8", "replace")[-500:]
        # The h264_mp4toannexb filter is rejected outright when the stream is already in
        # MP4 form; retry without it before giving up.
        partial.unlink(missing_ok=True)
        if "h264_mp4toannexb" in detail:
            return await _remux_plain(source, target)
        log.warning("remux failed", file=source.name, error=detail)
        return False

    # Rename only once complete, so an interrupted remux never leaves a half file that
    # looks like a valid cache entry.
    partial.replace(target)
    return True


async def _remux_plain(source: Path, target: Path) -> bool:
    partial = target.with_suffix(".part.mp4")
    cmd = [
        ffmpeg_path(),
        "-hide_banner",
        "-v",
        "error",
        "-nostdin",
        "-y",
        "-fflags",
        "+genpts+igndts",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(partial),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0 or not partial.exists() or partial.stat().st_size == 0:
        partial.unlink(missing_ok=True)
        log.warning(
            "remux failed", file=source.name, error=stderr.decode("utf-8", "replace")[-500:]
        )
        return False
    partial.replace(target)
    return True


async def ensure_playable(source: Path, recording_id: int) -> Playable:
    """Return something the browser can actually play.

    Native containers are served straight from the footage directory. Anything else is
    remuxed into the cache once and reused; the footage directory is never written to.
    """
    if is_browser_native(source):
        return Playable(path=source, media_type="video/mp4", from_cache=False)

    target = cache_dir() / f"{recording_id:08d}.mp4"
    if target.is_file() and target.stat().st_size > 0:
        # Touch so the eviction sweep treats recently watched clips as hot.
        with contextlib.suppress(OSError):
            os.utime(target)
        return Playable(path=target, media_type="video/mp4", from_cache=True)

    lock = await _lock_for(recording_id)
    async with lock:
        # Another request may have finished while this one waited.
        if target.is_file() and target.stat().st_size > 0:
            return Playable(path=target, media_type="video/mp4", from_cache=True)

        try:
            ok = await _remux(source, target)
        except FFmpegError as exc:
            log.warning("remux unavailable", file=source.name, error=str(exc))
            ok = False

        if not ok:
            # Serving the original is honest: the browser will refuse it, but the API
            # still behaves and the failure is visible in the logs rather than a 500.
            return Playable(path=source, media_type="video/mp2t", from_cache=False)

    await asyncio.to_thread(prune_cache)
    return Playable(path=target, media_type="video/mp4", from_cache=True)


def cache_usage() -> tuple[int, int]:
    """``(bytes, files)`` currently held in the playback cache."""
    total = count = 0
    with contextlib.suppress(OSError):
        for entry in os.scandir(cache_dir()):
            if entry.is_file():
                total += entry.stat().st_size
                count += 1
    return total, count


def prune_cache(limit_bytes: int | None = None) -> int:
    """Evict least-recently-used clips until the cache fits.

    Remuxed copies are disposable -- the footage they came from is untouched -- so this
    only ever deletes inside ``/data/cache``. It never looks at the footage directory.
    """
    if limit_bytes is None:
        from app.core.settings_service import get_settings_service

        try:
            gb = float(get_settings_service().get_nowait("general.stream_cache_gb"))
        except Exception:
            gb = 5.0
        limit_bytes = int(gb * 1024**3)

    try:
        entries = [e for e in os.scandir(cache_dir()) if e.is_file()]
    except OSError:
        return 0

    total = sum(e.stat().st_size for e in entries)
    if total <= limit_bytes:
        return 0

    entries.sort(key=lambda e: e.stat().st_mtime)
    removed = 0
    for entry in entries:
        if total <= limit_bytes:
            break
        size = entry.stat().st_size
        try:
            os.unlink(entry.path)
        except OSError:
            continue
        total -= size
        removed += 1

    if removed:
        log.info("pruned playback cache", removed=removed, remaining_bytes=total)
    return removed


def clear_cache() -> None:
    with contextlib.suppress(OSError):
        shutil.rmtree(cache_dir())
