"""Removing the footage behind journeys the car never actually drove.

The static rule next door asks whether a *clip* recorded nothing: no speed, no distance,
nothing detected. That is the right question for a camera left on a desk and the wrong one
for a car left in a street, because a parked car watches traffic all day. Journey 280 of
the live library is twenty-five clips over nineteen minutes covering forty-seven metres --
and it holds eighty-six vehicle detections, so the static rule spares every second of it.
Twenty-two such journeys had accumulated: 716 recordings, thirteen hours of footage of the
car not moving, none of it eligible for deletion under any existing rule.

So this one asks about the *journey* instead. If the drive never happened -- if the group
of recordings clustered together never got above walking pace -- then the footage inside it
is footage of a parked car, whatever wandered through the frame. The thresholds are the
ones the journeys list uses (:func:`app.api.visibility.is_a_drive`), deliberately shared so
that what the disk keeps and what the page shows cannot drift apart.

**It deletes clips that saw things, so the guards matter more here, not less.**

*Positive evidence, never absence.* A journey qualifies only if it recorded a speed and
that speed was too low. One whose speed was never established at all -- no telemetry, no
overlay, nothing -- fails the *display* test and is hidden, and is never touched here. "We
do not know what this was" is a reason to keep footage, not to delete it.

*The whole journey has to be settled.* Not just the clip in hand: rollups are computed from
every recording in the group, so a journey with one clip still in the queue has an average
speed that is not yet the answer. The membership test is a count -- every live recording in
the journey settled, or the journey is skipped -- which is null-safe in a way that negating
the per-row condition is not.

*Nothing queued against it.* A journey with a reprocess in flight is left alone entirely.
This is the rule that would otherwise have deleted the footage under a job that was
re-reading it, and marked the recording failed for a missing file it had removed itself.

*Protected and event clips are never candidates*, the footage root must be genuinely
writable, and the single-run fraction cap applies unchanged -- a telemetry regression that
suddenly reported the whole library stationary blocks rather than deletes.
"""

from __future__ import annotations

from collections.abc import Collection

from sqlalchemy import case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.settings_service import get_settings_service
from app.db.models import JobState, Journey, ProcessingJob, Recording, RecordingState
from app.retention.idle import live_condition, settled_condition, spared_condition
from app.retention.planner import _GB, RetentionCandidate, RetentionPlan
from app.retention.safety import SafetyReport, evaluate_safety

log = get_logger(__name__)

#: Reason stamped on every candidate, shown in the retention report.
PARKED_REASON = "parked session -- the journey never became a drive"


def parked_journey_ids(min_avg_speed_kmh: float, min_top_speed_kmh: float):
    """Journeys that recorded a speed and it was too low for the car to have been driven.

    The inverse of :func:`app.api.visibility.is_a_drive` in intent, but not its negation in
    SQL, and the difference is the whole safety argument. ``NOT (avg >= 5 AND max >= 10)``
    is true for a journey with no speed at all only by accident of how nulls fall out; here
    both figures must be present *and* below the line. Hiding a journey we know nothing
    about is free. Deleting one is not.

    Either threshold failing is enough -- an average under five and a top speed under ten
    are two ways of describing a car that stayed put, and a journey needs both to count as
    a drive, so failing either means it was not one.
    """
    tests = []
    if min_avg_speed_kmh > 0:
        tests.append(Journey.avg_speed_kmh < min_avg_speed_kmh)
    if min_top_speed_kmh > 0:
        tests.append(Journey.max_speed_kmh < min_top_speed_kmh)
    if not tests:
        # Both thresholds off means the rule has been asked to identify nothing.
        return select(Journey.id).where(Journey.id.is_(None))
    return select(Journey.id).where(
        Journey.avg_speed_kmh.is_not(None),
        Journey.max_speed_kmh.is_not(None),
        or_(*tests),
    )


def _fully_settled_journey_ids():
    """Journeys in which every live recording has finished analysis.

    Counted rather than expressed as "has no unsettled recording", because negating
    :func:`settled_condition` is not null-safe: a recording whose revision column is null
    makes the comparison null, the negation null, and the row would drop out of the
    very subquery meant to catch it. ``CASE`` sends null to the ``else_`` branch, so an
    unsettled row is counted as unsettled whatever shape its nulls are.
    """
    totals = (
        select(
            Recording.journey_id.label("journey_id"),
            func.count(Recording.id).label("total"),
            func.sum(case((settled_condition(), 1), else_=0)).label("settled"),
        )
        .where(Recording.journey_id.is_not(None), live_condition())
        .group_by(Recording.journey_id)
        .subquery()
    )
    return select(totals.c.journey_id).where(totals.c.total == totals.c.settled)


def _busy_journey_ids():
    """Journeys with any recording queued or running -- left alone in full."""
    return (
        select(Recording.journey_id)
        .where(
            Recording.journey_id.is_not(None),
            Recording.id.in_(
                select(ProcessingJob.recording_id).where(
                    ProcessingJob.state.in_([JobState.QUEUED, JobState.RUNNING]),
                    ProcessingJob.recording_id.is_not(None),
                )
            ),
        )
        .distinct()
    )


async def plan_parked(
    session: AsyncSession,
    safety: SafetyReport | None = None,
    *,
    recording_ids: Collection[int] | None = None,
) -> RetentionPlan:
    """Which clips would be removed for belonging to a journey that was never a drive.

    Shaped like :func:`app.retention.idle.plan_idle`, and for the same reason: the rule
    authorises its own removals, so the scheduler runs it for real while the mount-writable
    and fraction guards inside still decide whether it may act. ``safety`` may be passed in
    when the caller has already evaluated it, so the footage tree is walked once a cycle.
    """
    settings = get_settings_service()
    result = RetentionPlan()
    # A discard verdict, like the static rule: the recording row survives as a tombstone
    # and execute() drops it from every statistics view once the file is really gone.
    result.exclude_from_stats = True
    result.safety = safety if safety is not None else await evaluate_safety(session)

    if not bool(settings.get_nowait("storage.delete_parked_journeys")):
        result.deletion_enabled = False
        return result

    result.deletion_enabled = True

    if not result.safety.ok:
        result.blocked = True
        result.blocked_reason = result.safety.blocked_reason
        result.bytes_before = result.safety.total_bytes
        return result

    min_avg = float(settings.get_nowait("journeys.min_avg_speed_kmh"))
    min_top = float(settings.get_nowait("journeys.min_top_speed_kmh"))
    if min_avg <= 0 and min_top <= 0:
        # Nothing is a parked session when nothing is being tested for.
        return result

    limited_to = tuple(set(recording_ids)) if recording_ids is not None else None
    if limited_to == ():
        return result

    predicates = [
        live_condition(),
        settled_condition(),
        spared_condition(),
        Recording.state != RecordingState.PROCESSING,
        Recording.journey_id.in_(parked_journey_ids(min_avg, min_top)),
        Recording.journey_id.in_(_fully_settled_journey_ids()),
        Recording.journey_id.notin_(_busy_journey_ids()),
    ]
    if limited_to is not None:
        predicates.append(Recording.id.in_(limited_to))

    recordings = (await session.execute(select(Recording).where(*predicates))).scalars().all()
    for recording in recordings:
        result.candidates.append(
            RetentionCandidate(
                recording_id=recording.id,
                rel_path=recording.rel_path,
                filename=recording.filename,
                size_bytes=recording.size_bytes,
                started_at=recording.started_at,
                reason=PARKED_REASON,
            )
        )

    # The runaway guard. A rule that suddenly wants to remove a large fraction of the
    # library is far likelier to be a telemetry regression that called every drive
    # stationary than a real intent, so it blocks and says so.
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
            f"parked-session cleanup would remove {result.would_free_bytes / _GB:.1f} GB, "
            f"more than the {max_fraction:.0%} single-run safety limit"
        )
        return result

    if result.candidates:
        log.info(
            "parked sessions hold footage of a car that never moved",
            recordings=len(result.candidates),
            would_free_gb=round(result.would_free_bytes / _GB, 2),
            min_avg_speed_kmh=min_avg,
            min_top_speed_kmh=min_top,
        )
    return result


__all__ = ["PARKED_REASON", "parked_journey_ids", "plan_parked"]
