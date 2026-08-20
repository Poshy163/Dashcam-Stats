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

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.settings_service import get_settings_service
from app.db.models import Recording, RecordingState, StageState
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


def _static_condition(threshold_kmh: float):
    """The per-clip test: analysed, and proven to hold nothing.

    A SQL expression so it can be a ``WHERE`` directly. ``file_missing``/``DELETED`` are
    left out because callers restrict to live rows.

    Both stages must be ``DONE`` — silence is not evidence. A clip whose telemetry has not
    been read has an unknown speed, and one whose detection has not run has an unknown
    object count; either way it cannot be called empty, so it is left alone.
    """
    return and_(
        Recording.telemetry_state == StageState.DONE,
        Recording.detection_state == StageState.DONE,
        Recording.max_speed_kmh.isnot(None),
        Recording.max_speed_kmh < threshold_kmh,
        or_(Recording.distance_m.is_(None), Recording.distance_m < IDLE_DISTANCE_M),
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


async def plan_idle(session: AsyncSession, safety: SafetyReport | None = None) -> RetentionPlan:
    """Which clips would be removed for being static and empty.

    Returns a :class:`RetentionPlan` handed straight to :func:`app.retention.planner.execute`.
    ``deletion_enabled`` is set true by the rule itself — it authorises its own removals (see
    the module docstring) — so the scheduler runs it with ``dry_run=False`` and it acts
    without the master switch. ``safety`` may be passed in when the caller already evaluated
    it, so the footage tree is walked once a cycle.
    """
    settings = get_settings_service()
    result = RetentionPlan()
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
    recordings = (
        (await session.execute(select(Recording).where(_live(), _static_condition(threshold))))
        .scalars()
        .all()
    )
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
