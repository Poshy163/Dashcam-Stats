"""Periodic database housekeeping: job history pruning, planner statistics, WAL checkpoint."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, text

from app.db.maintenance import (
    checkpoint_wal,
    prune_job_history,
    prune_scan_runs,
    refresh_planner_statistics,
    run_maintenance,
)
from app.db.models import (
    JobState,
    LogEntry,
    ProcessingJob,
    Recording,
    RecordingState,
    ScanRun,
)

NOW = datetime.now(UTC)
OLD = NOW - timedelta(days=120)
RECENT = NOW - timedelta(days=3)


async def make_recording(session, name: str) -> Recording:
    recording = Recording(
        rel_path=name,
        filename=name,
        size_bytes=1024,
        fingerprint=f"fp-{name}",
        started_at=NOW,
        state=RecordingState.COMPLETED,
    )
    session.add(recording)
    await session.flush()
    return recording


async def count(session, model) -> int:
    return int((await session.execute(select(func.count()).select_from(model))).scalar_one())


class TestJobHistoryPruning:
    async def test_only_old_terminal_jobs_are_removed(self, db_session):
        recording = await make_recording(db_session, "a.mp4")
        rid = recording.id
        db_session.add_all(
            [
                ProcessingJob(
                    recording_id=rid, state=JobState.COMPLETED, queued_at=OLD, finished_at=OLD
                ),
                ProcessingJob(
                    recording_id=rid, state=JobState.FAILED, queued_at=OLD, finished_at=OLD
                ),
                # Cancelled while still queued: never got a finished_at, so queued_at counts.
                ProcessingJob(recording_id=rid, state=JobState.CANCELLED, queued_at=OLD),
                # Recent, must stay.
                ProcessingJob(
                    recording_id=rid, state=JobState.COMPLETED, queued_at=RECENT, finished_at=RECENT
                ),
                # Old but not terminal: a stuck job is the queue's problem, not ours.
                ProcessingJob(recording_id=rid, state=JobState.QUEUED, queued_at=OLD),
                ProcessingJob(recording_id=rid, state=JobState.RUNNING, queued_at=OLD),
            ]
        )
        await db_session.commit()

        removed = await prune_job_history(db_session, 90)

        assert removed == 3
        remaining = {
            job.state for job in (await db_session.execute(select(ProcessingJob))).scalars()
        }
        assert remaining == {JobState.COMPLETED, JobState.QUEUED, JobState.RUNNING}
        assert await count(db_session, ProcessingJob) == 3

    async def test_log_lines_go_with_their_job(self, db_session):
        recording = await make_recording(db_session, "b.mp4")
        job = ProcessingJob(
            recording_id=recording.id, state=JobState.COMPLETED, queued_at=OLD, finished_at=OLD
        )
        db_session.add(job)
        await db_session.flush()
        db_session.add(LogEntry(ts=OLD, level="INFO", logger="t", message="hello", job_id=job.id))
        db_session.add(LogEntry(ts=OLD, level="INFO", logger="t", message="unrelated"))
        await db_session.commit()

        await prune_job_history(db_session, 90)

        messages = [e.message for e in (await db_session.execute(select(LogEntry))).scalars()]
        assert messages == ["unrelated"]

    async def test_scan_runs_are_pruned_by_start_time(self, db_session):
        db_session.add_all([ScanRun(started_at=OLD), ScanRun(started_at=RECENT)])
        await db_session.commit()

        assert await prune_scan_runs(db_session, 90) == 1
        assert await count(db_session, ScanRun) == 1


class TestPlannerStatistics:
    async def test_analyze_runs_once_then_optimize_takes_over(self, db_session):
        await db_session.execute(text("DROP TABLE IF EXISTS sqlite_stat1"))
        await db_session.commit()

        assert await refresh_planner_statistics(db_session) == (True, False)
        populated = (await db_session.execute(text("SELECT count(*) FROM sqlite_stat1"))).scalar()
        assert populated and populated > 0

        assert await refresh_planner_statistics(db_session) == (False, True)


class TestWalCheckpoint:
    async def test_checkpoint_does_not_raise(self, db_session):
        # In-memory test databases are not in WAL mode and report -1; on disk the count
        # is a non-negative page count. Either way the call must be harmless.
        result = await checkpoint_wal(db_session)
        assert result is None or result >= 0


class TestRunMaintenance:
    async def test_report_reflects_each_step(self, db_session):
        recording = await make_recording(db_session, "c.mp4")
        db_session.add(
            ProcessingJob(
                recording_id=recording.id,
                state=JobState.COMPLETED,
                queued_at=OLD,
                finished_at=OLD,
            )
        )
        db_session.add(ScanRun(started_at=OLD))
        await db_session.commit()

        report = await run_maintenance(db_session, job_history_days=30)

        assert report.jobs_pruned == 1
        assert report.scan_runs_pruned == 1
        assert report.analyzed or report.optimized
