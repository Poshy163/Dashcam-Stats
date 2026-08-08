"""Retention planning and execution.

Default behaviour is **report-only**. The recommended deployment mounts footage read-only,
so the planner's normal job is to answer "what would be removed if deletion were enabled?"
rather than to remove anything. Deletion requires four independent conditions to hold at
once: the safety guards pass, the mount is genuinely writable, the user has explicitly
enabled deletion, and the caller asked for a real run.

Deleting a recording never deletes what was learned from it. The telemetry, tracks and
plate observations stay; only the video file goes.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.paths import PathTraversalError, directory_size, safe_join
from app.core.settings_service import get_settings_service
from app.db.models import (
    JobState,
    ProcessingJob,
    Recording,
    RecordingState,
    RetentionRun,
)
from app.retention.safety import SafetyReport, can_delete, evaluate_safety

log = get_logger(__name__)

_GB = 1024**3


@dataclass(slots=True)
class RetentionCandidate:
    recording_id: int
    rel_path: str
    filename: str
    size_bytes: int
    started_at: datetime | None
    reason: str = "oldest eligible"

    def as_dict(self) -> dict[str, object]:
        return {
            "recording_id": self.recording_id,
            "filename": self.filename,
            "rel_path": self.rel_path,
            "size_bytes": self.size_bytes,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "reason": self.reason,
        }


@dataclass(slots=True)
class RetentionPlan:
    bytes_before: int = 0
    bytes_limit: int = 0
    bytes_to_free: int = 0
    candidates: list[RetentionCandidate] = field(default_factory=list)
    blocked: bool = False
    blocked_reason: str | None = None
    deletion_enabled: bool = False
    safety: SafetyReport | None = None
    skipped_reasons: dict[str, int] = field(default_factory=dict)

    @property
    def would_delete_count(self) -> int:
        return len(self.candidates)

    @property
    def would_free_bytes(self) -> int:
        return sum(c.size_bytes for c in self.candidates)

    @property
    def over_limit(self) -> bool:
        return self.bytes_before > self.bytes_limit

    def as_dict(self) -> dict[str, object]:
        return {
            "bytes_before": self.bytes_before,
            "bytes_limit": self.bytes_limit,
            "bytes_to_free": self.bytes_to_free,
            "would_delete_count": self.would_delete_count,
            "would_free_bytes": self.would_free_bytes,
            "blocked": self.blocked,
            "blocked_reason": self.blocked_reason,
            "deletion_enabled": self.deletion_enabled,
            "over_limit": self.over_limit,
            "skipped": self.skipped_reasons,
            "candidates": [c.as_dict() for c in self.candidates],
            "safety": self.safety.as_dict() if self.safety else None,
        }


async def plan(session: AsyncSession) -> RetentionPlan:
    """Work out which recordings would be removed to get back under the limit."""
    settings = get_settings_service()
    result = RetentionPlan()

    safety = await evaluate_safety(session)
    result.safety = safety
    result.deletion_enabled = bool(settings.get_nowait("storage.enable_deletion"))

    limit_gb = int(settings.get_nowait("storage.max_footage_gb"))
    result.bytes_limit = limit_gb * _GB

    if not safety.ok:
        result.blocked = True
        result.blocked_reason = safety.blocked_reason
        # Still report the size when it is known, so the UI is not blank when blocked.
        result.bytes_before = safety.total_bytes
        return result

    result.bytes_before = safety.total_bytes
    result.bytes_to_free = max(0, result.bytes_before - result.bytes_limit)

    if result.bytes_to_free <= 0:
        return result

    if not bool(settings.get_nowait("storage.delete_oldest")):
        result.blocked = True
        result.blocked_reason = (
            "over the storage limit, but 'Delete oldest footage when limit exceeded' is off"
        )
        return result

    min_age_days = int(settings.get_nowait("storage.min_age_days"))
    keep_with_detections = bool(settings.get_nowait("storage.keep_with_detections"))
    keep_events = bool(settings.get_nowait("storage.keep_events"))
    cutoff = datetime.now(UTC) - timedelta(days=min_age_days)

    active_jobs = select(ProcessingJob.recording_id).where(
        ProcessingJob.state.in_([JobState.QUEUED, JobState.RUNNING]),
        ProcessingJob.recording_id.isnot(None),
    )

    stmt = (
        select(Recording)
        .where(
            Recording.file_missing.is_(False),
            Recording.state != RecordingState.DELETED,
            Recording.id.notin_(active_jobs),
        )
        # Oldest first. Recordings with no known start time sort last rather than being
        # treated as infinitely old — an unparsed filename must not make a file a
        # deletion candidate ahead of everything else.
        .order_by(Recording.started_at.asc().nullslast(), Recording.id.asc())
    )

    skipped: dict[str, int] = {}
    freed = 0

    for recording in (await session.execute(stmt)).scalars():
        if freed >= result.bytes_to_free:
            break

        if min_age_days > 0 and recording.started_at and recording.started_at > cutoff:
            skipped["too recent"] = skipped.get("too recent", 0) + 1
            continue
        if keep_with_detections and (recording.vehicle_count or recording.plate_count):
            skipped["contains detections"] = skipped.get("contains detections", 0) + 1
            continue
        if keep_events and _is_event(recording):
            skipped["event recording"] = skipped.get("event recording", 0) + 1
            continue
        if recording.state is RecordingState.PROCESSING:
            skipped["currently processing"] = skipped.get("currently processing", 0) + 1
            continue

        result.candidates.append(
            RetentionCandidate(
                recording_id=recording.id,
                rel_path=recording.rel_path,
                filename=recording.filename,
                size_bytes=recording.size_bytes,
                started_at=recording.started_at,
            )
        )
        freed += recording.size_bytes

    result.skipped_reasons = skipped

    # A plan that would wipe out a large fraction of the library in one pass is far more
    # likely to be a bug or a misconfiguration than a genuine intent.
    max_fraction = float(settings.get_nowait("storage.max_delete_fraction"))
    indexed_bytes = int(
        (
            await session.execute(
                select(func.coalesce(func.sum(Recording.size_bytes), 0)).where(
                    Recording.file_missing.is_(False)
                )
            )
        ).scalar()
        or 0
    )
    if indexed_bytes and result.would_free_bytes > indexed_bytes * max_fraction:
        result.blocked = True
        result.blocked_reason = (
            f"plan would remove {result.would_free_bytes / _GB:.1f} GB, more than the "
            f"{max_fraction:.0%} single-run limit ({indexed_bytes * max_fraction / _GB:.1f} GB)"
        )

    return result


def _is_event(recording: Recording) -> bool:
    """Whether a recording counts as an event.

    This dashcam does not mark events itself — there are no G-force or emergency flags in
    the footage — so nothing is an event unless something else has said so. The hook stays
    because the setting exists and a heuristic (harsh braking derived from OSD speed) can
    populate it later without changing this code.
    """
    return False


async def execute(
    session: AsyncSession,
    retention_plan: RetentionPlan | None = None,
    *,
    dry_run: bool = True,
    trigger: str = "scheduled",
) -> RetentionRun:
    """Record the plan and, only if everything permits it, carry it out."""
    settings = get_settings_service()
    if retention_plan is None:
        retention_plan = await plan(session)

    allowed, refusal = can_delete(
        retention_plan.safety or await evaluate_safety(session),
        retention_plan.deletion_enabled,
    )
    really_delete = allowed and not dry_run and not retention_plan.blocked

    run = RetentionRun(
        trigger=trigger,
        dry_run=not really_delete,
        blocked=retention_plan.blocked or not allowed,
        blocked_reason=retention_plan.blocked_reason or refusal,
        bytes_before=retention_plan.bytes_before,
        bytes_limit=retention_plan.bytes_limit,
        candidate_count=retention_plan.would_delete_count,
        candidate_bytes=retention_plan.would_free_bytes,
        plan=[c.as_dict() for c in retention_plan.candidates],
    )
    session.add(run)
    await session.flush()

    if not really_delete:
        run.finished_at = datetime.now(UTC)
        log.info(
            "retention evaluated (report only)",
            trigger=trigger,
            over_limit=retention_plan.over_limit,
            would_delete=retention_plan.would_delete_count,
            would_free_gb=round(retention_plan.would_free_bytes / _GB, 2),
            reason=run.blocked_reason,
        )
        return run

    root = await settings.footage_dir()
    deleted_count = 0
    deleted_bytes = 0

    for candidate in retention_plan.candidates:
        # Re-validate immediately before unlinking rather than trusting the path recorded
        # when the plan was built: the settings could have changed, and a symlink could
        # have appeared, between planning and execution.
        try:
            target = safe_join(root, candidate.rel_path)
        except PathTraversalError as exc:
            log.error(
                "refusing to delete a path outside the footage root",
                rel_path=candidate.rel_path,
                error=str(exc),
            )
            continue

        if not target.is_file():
            continue

        try:
            size = target.stat().st_size
            target.unlink()
        except OSError as exc:
            log.warning("could not delete recording", file=candidate.filename, error=str(exc))
            continue

        deleted_count += 1
        deleted_bytes += size

        recording = await session.get(Recording, candidate.recording_id)
        if recording is not None:
            # The file is gone but everything derived from it survives — the plates and
            # journeys remain searchable after cleanup.
            recording.file_missing = True
            recording.deleted_at = datetime.now(UTC)
            recording.state = RecordingState.DELETED

    run.deleted_count = deleted_count
    run.deleted_bytes = deleted_bytes
    run.finished_at = datetime.now(UTC)

    log.info(
        "retention deleted footage",
        trigger=trigger,
        deleted=deleted_count,
        freed_gb=round(deleted_bytes / _GB, 2),
    )
    return run


async def current_usage(session: AsyncSession) -> tuple[int, int, int]:
    """``(bytes_used, file_count, bytes_limit)`` for the dashboard's storage bar."""
    settings = get_settings_service()
    root = await settings.footage_dir()
    extensions = await settings.media_extensions()
    limit = int(settings.get_nowait("storage.max_footage_gb")) * _GB

    used, count = 0, 0
    with contextlib.suppress(OSError):
        used, count = directory_size(root, extensions)
    return used, count, limit
