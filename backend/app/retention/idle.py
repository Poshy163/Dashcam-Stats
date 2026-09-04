"""Removing clips that recorded nothing: the car never moved and nothing was seen.

The "sitting on my desk" footage, and its cousin the long idle park: a clip where, for its
whole duration, the position did not change, the speed stayed at nothing, and neither the
object detector nor the plate reader found a thing. It holds no information, so it goes.

Speed is read off the burned-in overlay, not derived from GPS (:mod:`app.osd.parser` pulls
the ``NN km/h`` field independently of position), so a unit on a desk with no fix still
reports ``0 km/h`` and the clip is identifiable. Confirmed on the live library: the desk
clips read ``max_speed_kmh = 0``, ``distance_m = null`` (no GPS), ``vehicle_count = 0``,
``plate_count = 0`` once analysed.

**Per clip, not per drive.** An earlier version spared a whole journey if any one clip in
it held a detection — which meant a two-hour desk session was kept in full because six
frames tripped a false vehicle. Judging each clip on its own removes the ~139 empty ones
and keeps only the handful that actually caught something. A red light inside a real drive
is still safe: at a light there are almost always other cars in view (detected → kept), and
a moving approach to it covers ground (``distance_m`` > 0 → kept).

**It deletes on its own, and that is deliberate.** Unlike the size-based cleanup this does
not wait on the master "actually delete" switch: the operator asked for empty static footage
to be removed without a per-run step, and the rule only ever touches a clip it has *proven*
worthless — analysed, still, and empty. The load-bearing guards remain: the footage root
must be genuinely writable (the guard against deleting into a share that failed to mount),
the single-run fraction cap still applies (a telemetry regression that suddenly called half
the library static blocks rather than deletes), and anything protected, flagged as an event,
or not yet fully analysed is never eligible. ``storage.delete_idle`` turns the whole rule
off for anyone who does not want it.
"""

from __future__ import annotations

from collections.abc import Collection

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.settings_service import get_settings_service
from app.db.models import JobState, ProcessingJob, Recording, RecordingState, StageState
from app.pipeline.revisions import CURRENT_REVISIONS
from app.retention.planner import _GB, RetentionCandidate, RetentionPlan
from app.retention.safety import SafetyReport, evaluate_safety

log = get_logger(__name__)

#: Reason stamped on every candidate, shown in the retention report.
IDLE_REASON = "static — the car never moved and nothing was seen"

#: A clip that covered less than this went nowhere. Mostly redundant with the speed test
#: (below ~3 km/h a clip cannot cover much in a minute), which is the point: it is the
#: second, independent witness the operator asked for — "the GPS coordinates did not
#: change" as well as "the speed did not change" — so a single misread does not delete a
#: clip on its own. A clip with no GPS at all (``distance_m`` null) satisfies it by having
#: no position to have changed.
IDLE_DISTANCE_M = 50.0


def settled_condition():
    """Analysis is finished, current, and final enough to delete on.

    A SQL expression, so it can be a ``WHERE`` directly. ``file_missing``/``DELETED`` are
    left out because callers restrict to live rows.

    Silence is not evidence. A stage that has not run yet reports no vehicles exactly as
    convincingly as one that ran and found none, so every stage has to be ``DONE`` and
    every persisted result has to belong to the current pipeline revision. That matters
    after a partial reprocess: detection may have finished empty before the plate stage
    failed, leaving apparently persuasive zeroes on a recording whose overall verdict is
    ``FAILED``. Only ``COMPLETED`` is final enough to delete on, and an ignored recording
    is deliberately outside all automatic processing.

    Shared with :mod:`app.retention.parked`, which has to demand the same thing and would
    otherwise restate it -- and a second copy of this list is a copy that can fall behind
    a new stage.
    """
    return and_(
        Recording.state == RecordingState.COMPLETED,
        Recording.ignored.is_(False),
        Recording.processed_at.isnot(None),
        Recording.metadata_state == StageState.DONE,
        Recording.telemetry_state == StageState.DONE,
        Recording.detection_state == StageState.DONE,
        Recording.plate_state.in_([StageState.DONE, StageState.SKIPPED]),
        Recording.metadata_revision == CURRENT_REVISIONS["metadata"],
        Recording.telemetry_revision == CURRENT_REVISIONS["telemetry"],
        Recording.detection_revision == CURRENT_REVISIONS["detection"],
        Recording.plate_revision == CURRENT_REVISIONS["plates"],
    )


def spared_condition():
    """Footage no automatic rule may remove, whatever else it concludes."""
    return and_(
        Recording.protected.is_(False),
        Recording.event_type.is_(None),
    )


def _static_condition(threshold_kmh: float):
    """The per-clip test: settled analysis that proved there is nothing here.

    Settled and unprotected are :func:`settled_condition` and :func:`spared_condition`;
    what this rule adds is the evidence of emptiness -- no speed, no distance, nothing
    seen. All three are needed, and the middle one is why a clip parked where other cars
    pass is not caught by this rule at all: it saw them.
    """
    return and_(
        settled_condition(),
        Recording.max_speed_kmh.isnot(None),
        Recording.max_speed_kmh < threshold_kmh,
        or_(Recording.distance_m.is_(None), Recording.distance_m < IDLE_DISTANCE_M),
        Recording.vehicle_count == 0,
        Recording.plate_count == 0,
        spared_condition(),
    )


def live_condition():
    return and_(
        Recording.file_missing.is_(False),
        Recording.state != RecordingState.DELETED,
    )


#: Kept as the module's own short name for it; shared under the public one above.
_live = live_condition


async def plan_idle(
    session: AsyncSession,
    safety: SafetyReport | None = None,
    *,
    recording_ids: Collection[int] | None = None,
) -> RetentionPlan:
    """Which clips would be removed for being static and empty.

    Returns a :class:`RetentionPlan` handed straight to :func:`app.retention.planner.execute`.
    ``deletion_enabled`` is set true by the rule itself — it authorises its own removals (see
    the module docstring) — so the scheduler runs it with ``dry_run=False`` and it acts
    without the master switch. ``safety`` may be passed in when the caller already evaluated
    it, so the footage tree is walked once a cycle. ``recording_ids`` narrows a
    post-processing pass to the bounded batch that just finished; the periodic pass omits
    it and remains the full-library safety net.
    """
    settings = get_settings_service()
    result = RetentionPlan()
    # Unlike ordinary retention, this policy is a discard verdict.  The recording row is
    # retained as a tombstone, but execute() hides it from every statistics view as soon
    # as its source file has actually been removed.
    result.exclude_from_stats = True
    result.safety = safety if safety is not None else await evaluate_safety(session)

    if not bool(settings.get_nowait("storage.delete_idle")):
        # Rule off: nothing planned, and nothing authorised.
        result.deletion_enabled = False
        return result

    # The rule authorises its own deletion. The mount-writable guard and the fraction cap
    # below are what keep that safe; the master 'actually delete' switch is not consulted.
    result.deletion_enabled = True

    if not result.safety.ok:
        result.blocked = True
        result.blocked_reason = result.safety.blocked_reason
        result.bytes_before = result.safety.total_bytes
        return result

    threshold = float(settings.get_nowait("storage.idle_speed_kmh"))
    limited_to = tuple(set(recording_ids)) if recording_ids is not None else None
    if limited_to == ():
        return result
    # Never a recording something is working on, which the size-based planner has always
    # excluded and this rule did not.
    #
    # The asymmetry mattered because this is the only rule that deletes without the master
    # switch. `_static_condition` asks about telemetry and detection, so a recording whose
    # *plate* stage is still pending -- or one a user has just queued for reprocessing --
    # satisfied every test while its job sat in the queue, and the file could be unlinked
    # out from under the worker. The recording was then marked failed for a missing file,
    # for a reason that had nothing to do with it, and the deletion was irreversible.
    active_jobs = select(ProcessingJob.recording_id).where(
        ProcessingJob.state.in_([JobState.QUEUED, JobState.RUNNING]),
        ProcessingJob.recording_id.is_not(None),
    )
    predicates = [
        _live(),
        _static_condition(threshold),
        # Kept even though COMPLETED is now required: a queued reprocess normally demotes
        # the recording, but the job row is the authoritative ownership marker and this
        # guard protects future queue paths that may intentionally leave the final state.
        Recording.state != RecordingState.PROCESSING,
        Recording.id.notin_(active_jobs),
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
                reason=IDLE_REASON,
            )
        )

    # The runaway guard, kept even though this deletes automatically: a rule that suddenly
    # wants to remove a large fraction of the library is far likelier to be a telemetry or
    # detection regression that called everything empty than a real intent, so it blocks.
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
            f"static cleanup would remove {result.would_free_bytes / _GB:.1f} GB, more than "
            f"the {max_fraction:.0%} single-run limit — check the telemetry and detection "
            f"stages before letting it run"
        )
    return result
