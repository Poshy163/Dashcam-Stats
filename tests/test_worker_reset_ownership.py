"""Queue resets must own claimed work before probes and through outcome cleanup."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.db.models import JobKind, JobState, ProcessingJob, Recording, RecordingState, StageState
from app.db.session import get_session_factory
from app.pipeline.orchestrator import RunReport
from app.workers import queue, reset, worker


@pytest.fixture(autouse=True)
def isolated_queue_state(monkeypatch):
    original_epoch = queue._reset_at
    monkeypatch.setattr(queue, "_paused", False)
    yield
    queue._reset_at = original_epoch


async def queued_recording(session):
    recording = Recording(
        filename="owned.ts",
        rel_path="owned.ts",
        fingerprint="synthetic-owned-source",
        state=RecordingState.QUEUED,
        processed_at=datetime.now(UTC),
        metadata_state=StageState.DONE,
        telemetry_state=StageState.DONE,
    )
    session.add(recording)
    await session.flush()
    job = ProcessingJob(recording_id=recording.id, kind=JobKind.PROCESS, state=JobState.QUEUED)
    session.add(job)
    await session.commit()
    return recording.id, job.id


async def settle_worker(pool, task):
    pool._running = False
    if not task.done():
        task.cancel()
    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=2)


async def test_claim_is_visible_and_abortable_while_hardware_probe_is_still_waiting(
    db_session, monkeypatch
):
    recording_id, job_id = await queued_recording(db_session)
    pool = worker.WorkerPool()
    pool._running = True
    probing, probe_cancelled = asyncio.Event(), asyncio.Event()
    processed = []

    async def hardware_probe():
        probing.set()
        try:
            await asyncio.Event().wait()
        finally:
            probe_cancelled.set()

    async def process(*args):
        processed.append(args)

    monkeypatch.setattr(worker, "detect_hardware_async", hardware_probe)
    monkeypatch.setattr(pool, "_process", process)
    task = asyncio.create_task(pool._worker(0))
    try:
        await asyncio.wait_for(probing.wait(), timeout=1)
        assert job_id in pool._active, (
            "a committed claim cannot disappear during hardware discovery"
        )
        assert pool._active[job_id].recording_id == recording_id
        pool._running = False
        assert await asyncio.wait_for(pool.abort_active(), timeout=1) == 1
        await asyncio.wait_for(task, timeout=1)
        assert probe_cancelled.is_set()
        assert not processed, "cancelled hardware discovery must never fall through to processing"
        assert not pool._active
    finally:
        await settle_worker(pool, task)


async def test_reset_waits_for_committed_claim_registration_before_cancelling_and_rebuilding(
    db_session, app_config, monkeypatch
):
    recording_id, old_job_id = await queued_recording(db_session)
    source = app_config.footage_dir / "owned.ts"
    source.write_bytes(b"synthetic source must remain unchanged")
    pool = worker.WorkerPool()
    pool._running = True
    committed, register = asyncio.Event(), asyncio.Event()
    original_scope = worker.session_scope

    @asynccontextmanager
    async def pause_after_claim_commit():
        async with original_scope() as session:
            yield session
        if not committed.is_set():
            committed.set()
            await register.wait()

    async def process(*args):
        await asyncio.Event().wait()

    async def invalidate(*args):
        return {"metadata"}

    monkeypatch.setattr(worker, "session_scope", pause_after_claim_commit)
    monkeypatch.setattr(worker, "detect_hardware_async", _hardware)
    monkeypatch.setattr(pool, "_process", process)
    monkeypatch.setattr(reset, "get_worker_pool", lambda: pool)
    monkeypatch.setattr(reset, "_needs_thumbnail", lambda rows: set())
    monkeypatch.setattr(reset, "invalidate_recordings", invalidate)
    task = asyncio.create_task(pool._worker(0))
    reset_task = None
    try:
        await asyncio.wait_for(committed.wait(), timeout=1)
        pool._running = False
        reset_task = asyncio.create_task(reset.reset_and_rebuild(db_session, stages=["metadata"]))
        done, _ = await asyncio.wait((reset_task,), timeout=0.05)
        assert not done, "reset bypassed a committed claim whose owner had not registered yet"
        async with get_session_factory()() as reader:
            assert (await reader.get(ProcessingJob, old_job_id)).state is JobState.RUNNING
        register.set()
        summary = await asyncio.wait_for(reset_task, timeout=2)
        await asyncio.wait_for(task, timeout=1)
        assert summary.aborted == 1
        assert summary.recordings == 1
        assert source.read_bytes() == b"synthetic source must remain unchanged"
        async with get_session_factory()() as reader:
            assert (await reader.get(ProcessingJob, old_job_id)).state is JobState.CANCELLED
            assert (await reader.get(Recording, recording_id)).filename == "owned.ts"
    finally:
        register.set()
        if reset_task is not None:
            if not reset_task.done():
                reset_task.cancel()
            await asyncio.gather(reset_task, return_exceptions=True)
        await settle_worker(pool, task)


async def _hardware():
    return SimpleNamespace(vaapi_available=False)


async def test_abort_does_not_claim_success_while_completed_work_owner_is_still_finishing(
    db_session, monkeypatch
):
    recording_id, job_id = await queued_recording(db_session)
    pool = worker.WorkerPool()
    pool._running = True
    finishing, finish_cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def process(*args):
        return RunReport(recording_id=recording_id)

    async def finish(*args, **kwargs):
        finishing.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            finish_cancelled.set()
            await release.wait()
        return True

    monkeypatch.setattr(worker, "detect_hardware_async", _hardware)
    monkeypatch.setattr(worker, "_ABORT_GRACE_S", 0.02)
    monkeypatch.setattr(pool, "_process", process)
    monkeypatch.setattr(pool, "_finish", finish)
    task = asyncio.create_task(pool._worker(0))
    try:
        await asyncio.wait_for(finishing.wait(), timeout=1)
        pool._running = False
        assert pool._work[job_id].done(), "fixture must exercise a finished processing child"
        with pytest.raises(worker.ProcessingStillRunningError, match="still stopping"):
            await pool.abort_active()
        assert finish_cancelled.is_set()
        assert not task.done()
        assert job_id in pool._active
        release.set()
        await asyncio.wait_for(task, timeout=1)
        assert not pool._active
    finally:
        release.set()
        await settle_worker(pool, task)
