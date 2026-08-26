"""Re-deriving stored metadata that a later fix would now compute differently.

A probe is run once, when a recording is first discovered, and its answers are written to
the row. That makes every correction to the probing logic retroactive only in intent: the
files processed before the fix keep the numbers the broken version gave them, because
nothing ever asks them again.

That is not hypothetical. The 33-bit PTS wrap clamp was written for two specific files and
then fixed so it actually fires -- and those two files still carried 95,376 s and 95,377 s
afterwards, 53 hours between them, because the clamp only runs inside ``probe`` and neither
file was ever probed again. The dashboard went on reporting 72.5 hours of footage for a
library holding about 19.5.

The same shape as ``JourneyBuilder.repair_stale``, and for the same reason: a derived value
that is wrong by an old rule will stay wrong forever unless something notices and asks for
it to be computed again.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.paths import resolve_footage_path
from app.db.models import Recording, carry_probe_markers
from app.hardware.ffmpeg import (
    MAX_PLAUSIBLE_BITRATE,
    MIN_PLAUSIBLE_BITRATE,
    FFmpegError,
    probe,
)

log = get_logger(__name__)


def implausible_duration():
    """Rows whose stored duration cannot be true for a file that size.

    Bytes are the one piece of evidence that is not derived from the duration, so they are
    what the duration is checked against. Measured across the live library: 675 recordings
    with both values span 2.86 to 43.0 Mbps, and the three broken rows sit at 0.0008,
    0.0009 and 1,879 Mbps. The band below therefore separates them by more than three
    orders of magnitude in both directions -- the threshold is nowhere near delicate, which
    is the only reason it is safe to act on automatically.
    """
    rate = Recording.size_bytes * 8.0 / Recording.duration_s
    return and_(
        Recording.duration_s.is_not(None),
        Recording.duration_s > 0,
        Recording.size_bytes > 0,
        Recording.file_missing.is_(False),
        or_(rate < MIN_PLAUSIBLE_BITRATE, rate > MAX_PLAUSIBLE_BITRATE),
    )


def _is_plausible(duration_s: float | None, size_bytes: int) -> bool:
    """The Python twin of :func:`implausible_duration`, for checking a fresh probe.

    ``None`` counts as plausible: it means the probe declined to guess, which is a valid
    and useful answer, and strictly better than the number it replaces.
    """
    if duration_s is None:
        return True
    if duration_s <= 0 or size_bytes <= 0:
        return False
    return MIN_PLAUSIBLE_BITRATE <= size_bytes * 8 / duration_s <= MAX_PLAUSIBLE_BITRATE


async def repair_durations(session: AsyncSession, *, limit: int = 25) -> int:
    """Re-probe recordings whose stored duration is impossible, and correct them.

    Returns how many rows changed. Bounded per run because each repair decodes a file that
    has already proved it cannot be trusted, and there is no hurry: this is a background
    correction, not a user-facing operation.
    """
    stale = list(
        (
            await session.execute(select(Recording).where(implausible_duration()).limit(limit))
        ).scalars()
    )
    if not stale:
        return 0

    repaired = 0
    for recording in stale:
        before = recording.duration_s
        try:
            path = resolve_footage_path(recording.rel_path, must_exist=True)
            info = await probe(path)
        except (FFmpegError, FileNotFoundError, ValueError) as exc:
            # A file that cannot be probed is not a duration problem, and re-probing it
            # every cycle would be pointless. Say so once and leave the row alone.
            log.warning(
                "could not re-probe a recording with an impossible duration",
                file=recording.filename,
                error=str(exc),
            )
            continue

        if not _is_plausible(info.duration_s, info.size_bytes):
            # The new answer has to survive the same test the old one failed, or this
            # replaces one impossible number with another and flags the row again next
            # cycle -- a repair loop that reports success every time it runs. Some files
            # genuinely have no recoverable duration; saying so once is the honest end.
            log.warning(
                "a recording's duration is impossible and re-probing does not recover it",
                file=recording.filename,
                stored_s=before,
                probed_s=info.duration_s,
                size_bytes=recording.size_bytes,
            )
            continue

        recording.duration_s = info.duration_s
        recording.bitrate = info.bitrate
        recording.fps = info.fps
        recording.fps_container = info.fps_container
        recording.size_bytes = info.size_bytes
        # Merged, not assigned. Other subsystems keep their own keys in this column and a
        # duration repair has no business dropping them -- see ``DURABLE_PROBE_KEYS``.
        recording.probe_json = carry_probe_markers(
            recording.probe_json,
            {
                "warnings": info.warnings,
                "pts_wrapped": info.pts_wrapped,
                "source_damaged": bool(info.warnings),
            },
        )
        # ended_at is derived from the duration, so it inherited the same lie -- one of
        # these two files claimed to have finished 26 hours after it started.
        if recording.started_at is not None:
            recording.ended_at = (
                recording.started_at + timedelta(seconds=info.duration_s)
                if info.duration_s
                else None
            )
        repaired += 1
        log.info(
            "corrected an impossible duration",
            file=recording.filename,
            was_s=before,
            now_s=info.duration_s,
        )

    if repaired:
        await session.flush()
    return repaired
