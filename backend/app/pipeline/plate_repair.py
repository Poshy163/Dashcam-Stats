"""Bounded, durable plate revalidation after processing updates or profile changes."""

from __future__ import annotations

import hashlib
import json

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.settings_service import get_settings_service
from app.db.models import (
    BULK_PRIORITY,
    JobKind,
    JobState,
    PlateObservation,
    ProcessingJob,
    Recording,
    RecordingState,
    StageState,
)
from app.pipeline.revisions import CURRENT_REVISIONS

log = get_logger(__name__)
ORIENTATION_METHOD = "per-crop-dual-ocr-v1"


def validation_key() -> str:
    settings = get_settings_service()
    profile = {
        key: settings.get_nowait(f"plates.{key}")
        for key in (
            "region",
            "detection_confidence",
            "min_store_confidence",
            "store_unmatched",
            "max_ocr_per_track",
            "min_plate_width",
        )
    }
    fingerprint = hashlib.sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest()[:16]
    return f"{CURRENT_REVISIONS['plates']}:{fingerprint}"


def stamp_validation(recording: Recording, key: str) -> None:
    recording.probe_json = {**(recording.probe_json or {}), "plate_validation_key": key}


async def queue_plate_repairs(session: AsyncSession, *, limit: int = 200) -> int:
    """Check provenance and invariants, then enqueue only affected historical recordings.

    Workers perform OCR, not the startup path. Requests are stamped with the target
    revision/profile so a permanently failed job cannot be recreated forever on restart.
    Existing jobs retain ownership; subsequent sweeps pick up anything they did not fix.
    """
    from app.pipeline.orchestrator import invalidate_recordings
    from app.workers import queue

    settings = get_settings_service()
    if not settings.get_nowait("plates.enabled") or not settings.get_nowait(
        "plates.auto_revalidate"
    ):
        return 0
    target = validation_key()
    active = (
        select(ProcessingJob.id)
        .where(
            ProcessingJob.recording_id == Recording.id,
            ProcessingJob.state.in_([JobState.QUEUED, JobState.RUNNING]),
        )
        .exists()
    )
    metadata = PlateObservation.bbox
    inconsistent = (
        select(PlateObservation.id)
        .where(
            PlateObservation.recording_id == Recording.id,
            or_(
                metadata.is_(None),
                metadata["orientation_method"].as_string().is_(None),
                metadata["orientation_method"].as_string() != ORIENTATION_METHOD,
                metadata["mirrored"].as_boolean().is_(None),
                metadata["orientation_margin"].as_float().is_(None),
                PlateObservation.raw_text == "",
                PlateObservation.normalised_text == "",
                PlateObservation.ocr_confidence
                < float(settings.get_nowait("plates.min_store_confidence")),
                PlateObservation.ocr_confidence > 1.0,
            ),
        )
        .exists()
    )
    profile_key = Recording.probe_json["plate_validation_key"].as_string()
    requested_key = Recording.probe_json["plate_repair_requested"].as_string()
    recordings = list(
        (
            await session.execute(
                select(Recording)
                .where(
                    Recording.ignored.is_(False),
                    Recording.file_missing.is_(False),
                    Recording.state != RecordingState.INVALID,
                    Recording.processed_at.is_not(None),
                    Recording.detection_state == StageState.DONE,
                    ~active,
                    or_(requested_key.is_(None), requested_key != target),
                    or_(
                        Recording.plate_revision.is_(None),
                        Recording.plate_revision != CURRENT_REVISIONS["plates"],
                        profile_key.is_(None),
                        profile_key != target,
                        inconsistent,
                    ),
                )
                .order_by(Recording.id)
                .limit(limit)
            )
        ).scalars()
    )
    if not recordings:
        return 0
    await invalidate_recordings(session, [r.id for r in recordings], ["plates"])
    for recording in recordings:
        recording.probe_json = {**(recording.probe_json or {}), "plate_repair_requested": target}
        await queue.enqueue(
            session,
            recording.id,
            kind=JobKind.REPROCESS,
            stages=["plates"],
            priority=BULK_PRIORITY,
            force=True,
        )
    log.info("queued automatic plate revalidation", recordings=len(recordings), target=target)
    return len(recordings)
