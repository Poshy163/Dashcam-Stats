"""Removing footage from drives where the car never moved.

The "sitting on my desk" case: a session the camera recorded while stationary, with nothing
in view — no ground covered, no vehicles or plates seen. It is pure noise in a library meant
to be about journeys, so this finds it and, when deletion is permitted, removes it.

Speed is the signal, and it is read off the burned-in overlay rather than derived from GPS
(:mod:`app.osd.parser` pulls the ``NN km/h`` field independently of position). That matters:
a unit on a desk has no GPS fix, so anything GPS-derived is null there — but the overlay
still says ``0 km/h``, so ``Recording.max_speed_kmh`` is ``0``, not unknown, and the footage
is identifiable.

Cautious by construction, four ways, because this deletes and deletion does not come back:

* **Whole idle drives only.** A recording is removed only when *every* live recording in the
  journey it belongs to is idle — so a red light or a pause inside a real drive, whose
  journey moved overall, is always kept. One segment that read a real speed spares the whole
  journey, which also means a single OCR misread of ``0`` cannot cause a deletion. A
  recording with no journey — what a desk session looks like, with no adjacency to group it —
  is judged on its own.
* **Never on absent evidence.** A recording whose telemetry has not been read, or that has no
  speed at all, cannot be called stationary and is left alone. Silence is not a speed of zero.
* **Never anything with something in it.** A detected vehicle or plate, or a protected/event
  flag, keeps a recording however still the car was — parking-mode activity is exactly what
  you would want to keep.
* **Same safety as size-based retention.** The footage root must be genuinely writable, the
  master "actually delete" switch on, and the single-run fraction cap honoured — all reused
  from :mod:`app.retention.planner`, not reimplemented.
"""

from __future__ import annotations

from sqlalchemy import and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.settings_service import get_settings_service
from app.db.models import Recording, RecordingState, StageState
from app.retention.planner import _GB, RetentionCandidate, RetentionPlan
from app.retention.safety import SafetyReport, evaluate_safety

log = get_logger(__name__)

#: Reason stamped on every idle candidate, shown in the retention report.
IDLE_REASON = "stationary — the car never moved"


def _idle_condition(threshold_kmh: float):
    """The per-recording test for 'the car was not moving and nothing was in view'.

    A SQL expression rather than a Python predicate so it can serve double duty: as a
    ``WHERE`` for the unassigned recordings and, wrapped in ``CASE``, as the count that
    decides whether a whole journey is idle. ``file_missing``/``DELETED`` are left out here
    because every caller already restricts to live rows.
    """
    return and_(
        Recording.telemetry_state == StageState.DONE,
        Recording.max_speed_kmh.isnot(None),
        Recording.max_speed_kmh < threshold_kmh,
        Recording.vehicle_count == 0,
        Recording.plate_count == 0,
        Recording.protected.is_(False),
        Recording.event_type.is_(None),
    )


def _live():
    return and_(
        Recording.file_missing.is_(False),
        Recording.state != RecordingState.DELETED,
    )


async def _candidates(session: AsyncSession, threshold_kmh: float) -> list[Recording]:
    idle = _idle_condition(threshold_kmh)

    # Journeys where every live recording is idle. Counting idle vs total in one grouped
    # pass is what enforces "whole drive only" without loading the library into memory.
    grouped = await session.execute(
        select(
            Recording.journey_id,
            func.count().label("live"),
            func.sum(case((idle, 1), else_=0)).label("idle"),
        )
        .where(_live(), Recording.journey_id.isnot(None))
        .group_by(Recording.journey_id)
    )
    fully_idle = [row.journey_id for row in grouped if row.live > 0 and row.live == row.idle]

    recordings: list[Recording] = []
    if fully_idle:
        recordings.extend(
            (
                await session.execute(
                    select(Recording).where(_live(), Recording.journey_id.in_(fully_idle))
                )
            )
            .scalars()
            .all()
        )
    # Recordings with no journey are judged individually — a desk session has no drive to
    # belong to, so "the whole drive is idle" collapses to "this recording is idle".
    recordings.extend(
        (
            await session.execute(
                select(Recording).where(_live(), Recording.journey_id.is_(None), idle)
            )
        )
        .scalars()
        .all()
    )
    return recordings


async def plan_idle(session: AsyncSession, safety: SafetyReport | None = None) -> RetentionPlan:
    """Which recordings would be removed for being stationary footage.

    Returns a :class:`RetentionPlan` so it can be handed straight to
    :func:`app.retention.planner.execute`, which carries the real deletion safety. Empty
    (and harmless) when the rule is switched off or the mount is not safe to write.

    ``safety`` may be passed in when the caller has already evaluated it — the scheduler
    runs the size-based plan first and hands its result straight here, so the footage tree
    is walked once a cycle rather than twice.
    """
    settings = get_settings_service()
    result = RetentionPlan()
    result.safety = safety if safety is not None else await evaluate_safety(session)
    result.deletion_enabled = bool(settings.get_nowait("storage.enable_deletion"))

    if not bool(settings.get_nowait("storage.delete_idle")):
        return result
    if not result.safety.ok:
        result.blocked = True
        result.blocked_reason = result.safety.blocked_reason
        result.bytes_before = result.safety.total_bytes
        return result

    threshold = float(settings.get_nowait("storage.idle_speed_kmh"))
    for recording in await _candidates(session, threshold):
        result.candidates.append(
            RetentionCandidate(
                recording_id=recording.id,
                rel_path=recording.rel_path,
                filename=recording.filename,
                size_bytes=recording.size_bytes,
                started_at=recording.started_at,
                reason=IDLE_REASON,
            )
        )

    # The same runaway guard the size-based plan has: a rule that suddenly wants to remove a
    # large fraction of the library is far likelier to be a telemetry regression than a real
    # intent, so it blocks rather than deletes.
    max_fraction = float(settings.get_nowait("storage.max_delete_fraction"))
    indexed = int(
        (
            await session.execute(
                select(func.coalesce(func.sum(Recording.size_bytes), 0)).where(
                    Recording.file_missing.is_(False)
                )
            )
        ).scalar()
        or 0
    )
    result.bytes_before = indexed
    if indexed and result.would_free_bytes > indexed * max_fraction:
        result.blocked = True
        result.blocked_reason = (
            f"idle cleanup would remove {result.would_free_bytes / _GB:.1f} GB, more than "
            f"the {max_fraction:.0%} single-run limit — check the telemetry before enabling"
        )
    return result
