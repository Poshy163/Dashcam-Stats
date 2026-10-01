"""Slow footage storage must not monopolise SQLite's writer during cleanup."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.core.settings_service import get_settings_service
from app.db.models import JobState, ProcessingJob, Recording, RecordingState
from app.retention.planner import RetentionCandidate, RetentionPlan, execute
from app.retention.safety import SafetyReport


async def test_retention_releases_writer_before_unlink_and_between_passes(
    db_session, app_config, temp_dirs, monkeypatch
):
    _, footage = temp_dirs
    await get_settings_service().set("general.footage_dir", str(footage))
    target = footage / "old.ts"
    target.write_bytes(b"recording")
    recording = Recording(
        rel_path=target.name,
        filename=target.name,
        size_bytes=target.stat().st_size,
        state=RecordingState.COMPLETED,
    )
    db_session.add(recording)
    await db_session.commit()
    recording_id = recording.id
    safety = SafetyReport(ok=True, writable=True)

    # Scheduled/manual cleanup uses this same session for several consecutive passes.
    await execute(db_session, RetentionPlan(safety=safety), trigger="report-first")
    candidate = RetentionCandidate(recording_id, target.name, target.name, 9, None)
    plan = RetentionPlan(candidates=[candidate], deletion_enabled=True, safety=safety)
    original_unlink = Path.unlink
    observed_intents = []

    def unlink_with_another_writer(path, *args, **kwargs):
        if path == target:
            # Real independent SQLite connection, as used by a backup heartbeat.
            with sqlite3.connect(app_config.db_path, timeout=0.05) as other:
                other.execute(
                    "UPDATE recordings SET size_bytes = size_bytes WHERE id = ?",
                    (recording_id,),
                )
                observed_intents.extend(
                    other.execute(
                        "SELECT id, finished_at FROM retention_runs WHERE trigger = ?",
                        ("slow-unlink",),
                    ).fetchall()
                )
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink_with_another_writer)
    run = await execute(db_session, plan, dry_run=False, trigger="slow-unlink")

    assert run.deleted_count == 1
    assert not target.exists()
    assert observed_intents == [(run.id, None)], "persist intent before irreversible IO"
    # Final bookkeeping also releases its writer before the next pass starts planning.
    with sqlite3.connect(app_config.db_path, timeout=0.05) as other:
        other.execute("UPDATE recordings SET size_bytes = size_bytes WHERE id = ?", (recording_id,))
        assert (
            other.execute(
                "SELECT deleted_count, finished_at FROM retention_runs WHERE id = ?", (run.id,)
            ).fetchone()[0]
            == 1
        )


async def test_failed_unlink_keeps_a_durable_unfinished_audit(
    db_session, app_config, temp_dirs, monkeypatch
):
    _, footage = temp_dirs
    await get_settings_service().set("general.footage_dir", str(footage))
    target = footage / "old.ts"
    target.write_bytes(b"recording")
    recording = Recording(
        rel_path=target.name, filename=target.name, size_bytes=9, state=RecordingState.COMPLETED
    )
    db_session.add(recording)
    await db_session.commit()
    plan = RetentionPlan(
        candidates=[RetentionCandidate(recording.id, target.name, target.name, 9, None)],
        deletion_enabled=True,
        safety=SafetyReport(ok=True, writable=True),
    )

    def unexpected_failure(*_args, **_kwargs):
        raise RuntimeError("storage worker interrupted")

    monkeypatch.setattr(Path, "unlink", unexpected_failure)
    with pytest.raises(RuntimeError, match="storage worker interrupted"):
        await execute(db_session, plan, dry_run=False, trigger="interrupted-unlink")
    await db_session.rollback()
    with sqlite3.connect(app_config.db_path) as other:
        assert other.execute(
            "SELECT finished_at FROM retention_runs WHERE trigger = ?",
            ("interrupted-unlink",),
        ).fetchall() == [(None,)]
    assert target.exists()


@pytest.mark.parametrize("change", ["protect", "queue"])
async def test_later_candidate_is_rechecked_after_slow_storage(
    db_session, app_config, temp_dirs, monkeypatch, change
):
    _, footage = temp_dirs
    await get_settings_service().set("general.footage_dir", str(footage))
    recordings = []
    for name in ("first.ts", "second.ts"):
        (footage / name).write_bytes(b"recording")
        recording = Recording(
            rel_path=name, filename=name, size_bytes=9, state=RecordingState.COMPLETED
        )
        db_session.add(recording)
        recordings.append(recording)
    await db_session.flush()
    second_id = recordings[1].id
    db_session.add(ProcessingJob(recording_id=second_id, state=JobState.COMPLETED))
    await db_session.commit()
    plan = RetentionPlan(
        candidates=[RetentionCandidate(r.id, r.rel_path, r.filename, 9, None) for r in recordings],
        deletion_enabled=True,
        exclude_from_stats=True,
        safety=SafetyReport(ok=True, writable=True),
    )
    original_unlink = Path.unlink

    def change_later_candidate(path, *args, **kwargs):
        if path == footage / "first.ts":
            with sqlite3.connect(app_config.db_path, timeout=0.05) as other:
                if change == "protect":
                    other.execute("UPDATE recordings SET protected = 1 WHERE id = ?", (second_id,))
                else:
                    other.execute(
                        "UPDATE processing_jobs SET state = ? WHERE recording_id = ?",
                        (JobState.QUEUED.name, second_id),
                    )
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", change_later_candidate)
    run = await execute(db_session, plan, dry_run=False, trigger="recheck-test")
    assert run.deleted_count == 1
    assert not (footage / "first.ts").exists()
    assert (footage / "second.ts").exists()
