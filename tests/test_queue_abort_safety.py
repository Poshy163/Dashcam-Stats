"""A queue reset cannot replace work that has not actually stopped."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.db.models import JobState, ProcessingJob, Recording, RecordingState, StageState
from app.db.session import get_session_factory
from app.pipeline.revisions import CURRENT_REVISIONS
from app.workers import queue, reset
from app.workers.worker import ActiveJob, WorkerPool


@pytest.fixture(autouse=True)
def isolated_reset_epoch():
    original = queue._reset_at
    yield
    queue._reset_at = original


async def _library(session):
    recording = Recording(
        filename="held.ts",
        rel_path="held.ts",
        fingerprint="stable-synthetic-source",
        state=RecordingState.PROCESSING,
        processed_at=datetime.now(UTC),
        metadata_state=StageState.DONE,
        telemetry_state=StageState.DONE,
        metadata_revision=CURRENT_REVISIONS["metadata"],
        telemetry_revision=CURRENT_REVISIONS["telemetry"],
    )
    session.add(recording)
    await session.flush()
    job = ProcessingJob(recording_id=recording.id, state=JobState.RUNNING)
    session.add(job)
    await session.commit()
    return recording.id, job.id


async def test_reset_refuses_replacements_while_cancelled_work_is_still_stopping(
    client, db_session, app_config, monkeypatch
):
    recording_id, job_id = await _library(db_session)
    source = app_config.footage_dir / "held.ts"
    source.write_bytes(b"preserved synthetic footage")
    original_epoch = queue.reset_epoch()
    pool = WorkerPool()
    pool._active[job_id] = ActiveJob(job_id, recording_id, "held.ts")
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def slow_cancellation():
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()

    task = asyncio.create_task(slow_cancellation())
    pool._work[job_id] = task
    monkeypatch.setattr(reset, "get_worker_pool", lambda: pool)
    monkeypatch.setattr("app.workers.worker._ABORT_GRACE_S", 0.01)
    await entered.wait()
    try:
        response = await client.post("/api/reprocess", json={"stages": ["everything"]})

        assert response.status_code == 409, response.text
        assert "still stopping" in response.json()["detail"]
        assert cancelled.is_set()
        assert not task.done()
        assert queue.reset_epoch() == original_epoch
        async with get_session_factory()() as reader:
            stored = await reader.get(Recording, recording_id)
            assert stored.metadata_state is StageState.DONE
            assert stored.telemetry_state is StageState.DONE
            assert stored.metadata_revision == CURRENT_REVISIONS["metadata"]
            assert stored.telemetry_revision == CURRENT_REVISIONS["telemetry"]
            jobs = list((await reader.scalars(select(ProcessingJob))).all())
            assert len(jobs) == 1
            assert jobs[0].id == job_id
            assert jobs[0].state is JobState.CANCELLED
        assert source.read_bytes() == b"preserved synthetic footage"
    finally:
        release.set()
        await asyncio.wait_for(task, timeout=1)


async def test_unexpected_abort_failure_also_prevents_replacement(db_session, monkeypatch):
    recording_id, job_id = await _library(db_session)

    async def cannot_abort():
        raise RuntimeError("abort did not complete")

    monkeypatch.setattr(
        reset,
        "get_worker_pool",
        lambda: SimpleNamespace(
            abort_active=cannot_abort, reset_barrier=WorkerPool().reset_barrier
        ),
    )
    with pytest.raises(RuntimeError, match="abort did not complete"):
        await reset.reset_and_rebuild(db_session, stages=["everything"])

    async with get_session_factory()() as reader:
        stored = await reader.get(Recording, recording_id)
        assert stored.telemetry_revision == CURRENT_REVISIONS["telemetry"]
        jobs = list((await reader.scalars(select(ProcessingJob))).all())
        assert len(jobs) == 1
        assert jobs[0].id == job_id
        assert jobs[0].state is JobState.CANCELLED


async def test_worker_does_not_claim_recording_with_cancelled_work_still_settling(
    db_session, monkeypatch
):
    recording_id, job_id = await _library(db_session)
    original = await db_session.get(ProcessingJob, job_id)
    original.state = JobState.CANCELLED
    replacement = ProcessingJob(recording_id=recording_id, state=JobState.QUEUED)
    other = Recording(filename="other.ts", rel_path="other.ts")
    db_session.add_all([replacement, other])
    await db_session.flush()
    available = ProcessingJob(recording_id=other.id, state=JobState.QUEUED)
    db_session.add(available)
    await db_session.commit()
    replacement_id, available_id = replacement.id, available.id
    pool = WorkerPool()
    pool._active[job_id] = ActiveJob(job_id, recording_id, "held.ts", cancelled=True)
    pool._running = True
    claimed = []

    async def run_next(job_id, *args):
        claimed.append(job_id)
        pool._running = False

    monkeypatch.setattr(pool, "_run_job", run_next)
    await asyncio.wait_for(pool._worker(0), timeout=2)

    assert claimed == [available_id]
    async with get_session_factory()() as reader:
        assert (await reader.get(ProcessingJob, replacement_id)).state is JobState.QUEUED
    pool._active.clear()
    async with get_session_factory()() as next_worker:
        assert (await queue.claim_next(next_worker, "after-settled")).id == replacement_id
