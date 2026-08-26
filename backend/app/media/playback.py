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


def export_dir() -> Path:
    """Where trimmed download clips are kept, beside the remuxed streams."""
    path = get_config().cache_dir / "exports"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _touch(path: Path) -> None:
    """Mark a cached file as recently used, so the LRU sweep treats it as hot.

    The stream cache has always done this on a hit; the export cache did not, and now that
    the two share one eviction pass it matters -- an export kept the mtime of the moment it
    was created, so the ones people actually download sorted oldest and were evicted first.
    """
    with contextlib.suppress(OSError):
        os.utime(path)


def _cache_dirs() -> tuple[Path, ...]:
    """Every directory the playback cache budget covers.

    ``exports`` used to be outside it entirely: ``cache_usage``, ``prune_cache`` and
    ``clear_cache`` all looked only at ``stream``, so one file per (recording, start, end)
    accumulated there for the life of the deployment with nothing anywhere deleting them,
    and ``general.stream_cache_gb`` silently applied to half the cache. Both are the same
    kind of thing -- a disposable copy of footage that is still on the share -- so they
    share one budget and one LRU sweep.
    """
    return (cache_dir(), export_dir())


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
            # Bounded, like every other wait here. An unbounded one on a child that will
            # not die is the same hang one line further down.
            await asyncio.wait_for(proc.wait(), timeout=5.0)
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
    stderr = await _communicate(proc, source, "remux")
    if stderr is None:
        partial.unlink(missing_ok=True)
        return False
    if proc.returncode != 0 or not partial.exists() or partial.stat().st_size == 0:
        partial.unlink(missing_ok=True)
        log.warning(
            "remux failed", file=source.name, error=stderr.decode("utf-8", "replace")[-500:]
        )
        return False
    partial.replace(target)
    return True


async def _communicate(proc, source: Path, what: str) -> bytes | None:
    """Drain a remux child under a timeout, killing it if it will not finish.

    Two of the three ffmpeg calls in this module awaited ``communicate()`` with no bound.
    An ffmpeg blocked in an uninterruptible read on a network mount never returns from
    that, and because these run while holding the per-recording lock, the request never
    completes *and* every later ``/stream``, ``/export.mp4`` and ``/osd-debug`` for the same
    recording blocks behind it for the life of the process. Returns the child's stderr, or
    ``None`` when it had to be killed.
    """
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=DEFAULT_DECODE_TIMEOUT)
        return stderr
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        log.warning(f"{what} timed out", file=source.name, timeout_s=DEFAULT_DECODE_TIMEOUT)
        return None


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

    await asyncio.to_thread(prune_cache, keep=target)
    return Playable(path=target, media_type="video/mp4", from_cache=True)


async def ensure_export_clip(
    source: Path, recording_id: int, *, start_s: float = 0.0, end_s: float | None = None
) -> Path:
    """Create a cached, lossless MP4 excerpt suitable for downloading."""
    playable = await ensure_playable(source, recording_id)
    exports = export_dir()
    end_key = "end" if end_s is None else f"{end_s:.2f}"
    target = exports / f"{recording_id:08d}-{start_s:.2f}-{end_key}.mp4"
    if target.is_file() and target.stat().st_size > 0:
        _touch(target)
        return target
    lock = await _lock_for(-recording_id)
    async with lock:
        if target.is_file() and target.stat().st_size > 0:
            _touch(target)
            return target
        partial = target.with_suffix(".part.mp4")
        cmd = [
            ffmpeg_path(),
            "-hide_banner",
            "-v",
            "error",
            "-nostdin",
            "-y",
            "-ss",
            str(start_s),
            "-i",
            str(playable.path),
        ]
        if end_s is not None:
            cmd.extend(["-t", str(max(0.01, end_s - start_s))])
        cmd.extend(
            [
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
        )
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        stderr = await _communicate(proc, source, "export")
        if stderr is None:
            partial.unlink(missing_ok=True)
            raise FFmpegError(f"export of {source.name} timed out")
        if proc.returncode != 0 or not partial.is_file() or partial.stat().st_size == 0:
            partial.unlink(missing_ok=True)
            raise FFmpegError(stderr.decode("utf-8", "replace")[-1000:])
        partial.replace(target)
    # Swept here as well as after a remux: an export is the same kind of disposable copy
    # and shares the same budget, and this is the only moment one is created.
    await asyncio.to_thread(prune_cache, keep=target)
    return target


def cache_usage() -> tuple[int, int]:
    """``(bytes, files)`` currently held in the playback cache."""
    total = count = 0
    for directory in _cache_dirs():
        with contextlib.suppress(OSError):
            for entry in os.scandir(directory):
                if entry.is_file():
                    total += entry.stat().st_size
                    count += 1
    return total, count


def prune_cache(limit_bytes: int | None = None, *, keep: Path | None = None) -> int:
    """Evict least-recently-used clips until the cache fits.

    Remuxed copies are disposable -- the footage they came from is untouched -- so this
    only ever deletes inside ``/data/cache``. It never looks at the footage directory.

    ``keep`` is the clip the caller is about to hand to a client, and it is exempt. Both
    callers sweep the moment they finish writing one, and a single remux of a long
    recording can be larger than the whole configured budget on its own -- at which point
    the loop below cannot reach the limit no matter what it deletes, and the newest file
    is deleted along with everything else. The request that paid for that remux would then
    serve a file that no longer exists. Skipping one entry cannot break the budget any
    further: the cache was already over it before this file was written, and the next
    sweep, once nobody is holding it, collects it like any other.
    """
    if limit_bytes is None:
        from app.core.settings_service import get_settings_service

        try:
            gb = float(get_settings_service().get_nowait("general.stream_cache_gb"))
        except Exception:
            gb = 5.0
        limit_bytes = int(gb * 1024**3)

    protected = None if keep is None else os.path.normcase(os.path.abspath(keep))
    entries = []
    for directory in _cache_dirs():
        try:
            # `.part.mp4` is a transfer in progress, not a cache entry: evicting one deletes
            # the file its own ffmpeg is still writing.
            entries.extend(
                e
                for e in os.scandir(directory)
                if e.is_file()
                and not e.name.endswith(".part.mp4")
                and os.path.normcase(os.path.abspath(e.path)) != protected
            )
        except OSError:
            continue
    if not entries:
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
    for directory in _cache_dirs():
        with contextlib.suppress(OSError):
            shutil.rmtree(directory)
