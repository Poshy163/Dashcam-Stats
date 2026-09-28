"""Preview or apply conservative paired-camera recovery to existing OCR gaps.

Run with the normal DASHCAM_DATA_DIR configuration. Preview is the default; --apply
first creates an integrity-checked backup. Each recording is a short transaction,
with active jobs checked under the SQLite write lock. No footage is decoded, no
processing jobs are invalidated, and the partner recording remains read-only.
"""

from __future__ import annotations

import argparse
import asyncio
import json

from sqlalchemy import exists, select, text

from app.config import get_config
from app.db.backup import create_pre_migration_backup
from app.db.models import JobState, ProcessingJob, Recording, StageState
from app.db.session import dispose_engine, get_session_factory
from app.pipeline.revisions import CURRENT_REVISIONS
from app.pipeline.telemetry_quality import recover_from_paired_camera


def eligible():
    return (
        Recording.ignored.is_(False),
        Recording.file_missing.is_(False),
        Recording.telemetry_state == StageState.DONE,
        Recording.telemetry_revision == CURRENT_REVISIONS["telemetry"],
        Recording.gps_ocr_gap_count > 0,
        Recording.started_at.is_not(None),
        Recording.ended_at.is_not(None),
    )


async def repair(*, apply: bool, limit: int, after_id: int, recording_id: int | None) -> dict:
    config = get_config()
    if config.database_url:
        raise ValueError("Use DASHCAM_DATA_DIR with the normal SQLite database")
    if not config.db_path.is_file():
        raise FileNotFoundError(config.db_path)
    backup = (
        await asyncio.to_thread(create_pre_migration_backup, "telemetry-gaps", "paired-v1")
        if apply
        else None
    )
    factory = get_session_factory()
    async with factory() as session:
        query = select(Recording.id).where(*eligible(), Recording.id > after_id)
        if recording_id is not None:
            query = query.where(Recording.id == recording_id)
        ids = list((await session.scalars(query.order_by(Recording.id).limit(limit))).all())

    result = {
        "applied": apply,
        "backup": str(backup) if backup else None,
        "examined": 0,
        "changed_recordings": 0,
        "modified_recordings": 0,
        "recovered_points": 0,
        "skipped": 0,
        "last_id": after_id,
    }
    for rid in ids:
        async with factory() as session:
            # No worker can claim/rewrite a target between this check and the commit.
            # Preview also rolls back the complete real operation, including counters.
            await session.execute(text("BEGIN IMMEDIATE"))
            active = await session.scalar(
                select(
                    exists().where(
                        ProcessingJob.recording_id == rid,
                        ProcessingJob.state.in_([JobState.QUEUED, JobState.RUNNING]),
                    )
                )
            )
            recording = await session.scalar(
                select(Recording).where(Recording.id == rid, *eligible())
            )
            if active or recording is None:
                result["skipped"] += 1
                await session.rollback()
            else:
                before_changes = await session.scalar(text("SELECT total_changes()"))
                recovered = await recover_from_paired_camera(
                    session, recording, bidirectional=False, parse_failures_only=True
                )
                # Partner queries can autoflush earlier changes and clear session.dirty.
                # SQLite's per-connection counter includes those writes and also works
                # in preview, where the whole transaction is rolled back below.
                await session.flush()
                modified = await session.scalar(text("SELECT total_changes()")) > before_changes
                result["examined"] += 1
                result["changed_recordings"] += bool(recovered)
                result["modified_recordings"] += bool(modified)
                result["recovered_points"] += recovered
                if apply:
                    await session.commit()
                else:
                    await session.rollback()
            result["last_id"] = rid
    return result


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", help="Back up and commit; otherwise roll back"
    )
    parser.add_argument("--limit", type=int, default=100, help="Maximum recordings per invocation")
    parser.add_argument("--after-id", type=int, default=0, help="Resume after a returned last_id")
    parser.add_argument(
        "--recording-id", type=int, help="Verify one recording before a wider repair"
    )
    args = parser.parse_args()
    if not 1 <= args.limit <= 10000 or args.after_id < 0:
        parser.error("limit must be 1..10000 and after-id must be nonnegative")
    try:
        result = await repair(
            apply=args.apply,
            limit=args.limit,
            after_id=args.after_id,
            recording_id=args.recording_id,
        )
        print(json.dumps(result, sort_keys=True))
    finally:
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
