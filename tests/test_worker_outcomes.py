"""How a job ends, and what happens when recording that fails.

Three failures lived here, and they compounded. The bookkeeping that records a finished
job shared a session and an error handler with the run itself, so a lock held elsewhere
turned a completed recording into a stranded RUNNING row — five idle minutes waiting for
the heartbeat timeout, a burned attempt, then a full reprocess from the first frame. Cancel
changed a badge and stopped nothing, and the run then wrote its result over the top and
un-cancelled itself. And a heartbeat failing its way towards exactly that outcome said so
only at debug level, where nobody saw it.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.db.models import JobState, ProcessingJob, Recording, RecordingState
from app.db.session import session_scope
from app.pipeline.orchestrator import RunReport
from app.workers import queue
from app.workers.worker import ActiveJob, WorkerPool


def test_reported_decoder_prefers_any_software_fallback():
    report = SimpleNamespace(
        stages=[
            SimpleNamespace(stats={"decoder": "vaapi"}),
            SimpleNamespace(stats={"decoder": "software"}),
        ]
    )

    assert WorkerPool._decoder_from(report) == "software"


class RecordingLog:
    """Captures what the worker logged.

    Not caplog: logging here goes through structlog's own pipeline, so the stdlib handler
    caplog installs never sees these records — an assertion against caplog passes whatever
    the code does, which is worse than no test.
    """

    def __init__(self) -> None:
        self.entries: list[tuple[str, str]] = []

    def _record(self, level):
        def emit(message, **kwargs):
            self.entries.append((level, message))

        return emit

    def __getattr__(self, level):
        return self._record(level)

    def messages(self, level: str) -> list[str]:
        return [message for lvl, message in self.entries if lvl == level]


@pytest.fixture
async def job(db_session):
    recording = Recording(
        rel_path="outcome.ts",
        filename="outcome.ts",
        size_bytes=1024,
        state=RecordingState.QUEUED,
        duration_s=60.0,
    )
    db_session.add(recording)
    await db_session.flush()
    await db_session.commit()

    async with session_scope() as session:
        created = await queue.enqueue(session, recording.id)
        claimed = await queue.claim_next(session, "test-worker")
        assert claimed is not None and claimed.id == created.id
        return claimed.id


def _report(ok: bool = True) -> RunReport:
    report = RunReport(recording_id=1)
    report.ok = ok
    report.elapsed_s = 1.0
    if not ok:
        report.error = "something went wrong"
    return report


class TestRecordingTheOutcome:
    async def test_a_completed_analysis_notifies_the_cleanup_scheduler(self, job, monkeypatch):
        requested: list[int] = []
        monkeypatch.setattr("app.workers.worker._request_idle_cleanup", requested.append)
        pool = WorkerPool()
        active = ActiveJob(job_id=job, recording_id=1, filename="outcome.ts")

        assert await pool._finish(job, active, report=_report())
        assert requested == [1]

    async def test_a_transient_failure_does_not_lose_the_run(self, job, monkeypatch):
        """The whole point: a lock at the wrong moment must not discard finished work."""
        pool = WorkerPool()
        active = ActiveJob(job_id=job, recording_id=1, filename="outcome.ts")

        attempts = {"count": 0}
        real_complete = queue.complete

        async def flaky_complete(session, j, result, **kwargs):
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise OSError("database is locked")
            return await real_complete(session, j, result, **kwargs)

        monkeypatch.setattr(queue, "complete", flaky_complete)
        monkeypatch.setattr("app.workers.worker._FINISH_RETRY_S", 0.01)

        await pool._finish(job, active, report=_report())

        assert attempts["count"] == 2, "the failed write was not retried"
        async with session_scope() as session:
            assert (await session.get(ProcessingJob, job)).state is JobState.COMPLETED

    async def test_a_job_that_stays_locked_out_says_so_loudly(self, job, monkeypatch):
        """Out of retries, the row is stranded. That must be explained, not swallowed."""
        pool = WorkerPool()
        active = ActiveJob(job_id=job, recording_id=1, filename="outcome.ts")

        async def always_locked(session, j, result, **kwargs):
            raise OSError("database is locked")

        monkeypatch.setattr(queue, "complete", always_locked)
        monkeypatch.setattr("app.workers.worker._FINISH_RETRY_S", 0.001)
        recorded = RecordingLog()
        monkeypatch.setattr("app.workers.worker.log", recorded)

        await pool._finish(job, active, report=_report())

        assert any("could not record job outcome" in m for m in recorded.messages("error")), (
            "a job whose outcome could never be written left no error in the log"
        )
        async with session_scope() as session:
            assert (await session.get(ProcessingJob, job)).state is JobState.RUNNING

    async def test_a_cancelled_job_is_not_resurrected(self, job):
        """Cancel used to be undone by the run finishing a few minutes later."""
        async with session_scope() as session:
            assert await queue.cancel(session, job)

        pool = WorkerPool()
        active = ActiveJob(job_id=job, recording_id=1, filename="outcome.ts")
        await pool._finish(job, active, report=_report())

        async with session_scope() as session:
            assert (await session.get(ProcessingJob, job)).state is JobState.CANCELLED, (
                "a cancelled job was written back as completed"
            )

    async def test_a_cancelled_job_is_not_requeued_by_a_failure(self, job):
        """The other half: queue.fail sets state back to QUEUED with a retry pending."""
        async with session_scope() as session:
            assert await queue.cancel(session, job)

        pool = WorkerPool()
        active = ActiveJob(job_id=job, recording_id=1, filename="outcome.ts")
        await pool._finish(job, active, report=_report(ok=False))

        async with session_scope() as session:
            assert (await session.get(ProcessingJob, job)).state is JobState.CANCELLED


class TestCancellingRunningWork:
    async def test_the_heartbeat_stops_a_run_that_has_been_cancelled(self, job, monkeypatch):
        """Cancel has to reach work already inside a model or an ffmpeg read.

        The heartbeat is the pool's only regular tick, so it is where the request is
        noticed. Before this, nothing anywhere read ``job.state`` during a run.
        """
        monkeypatch.setattr("app.workers.worker._HEARTBEAT_INTERVAL_S", 0.02)
        pool = WorkerPool()
        active = ActiveJob(job_id=job, recording_id=1, filename="outcome.ts")

        started = asyncio.Event()

        async def never_finishes():
            started.set()
            await asyncio.sleep(60)

        work = asyncio.create_task(never_finishes())
        pool._work[job] = work
        beat = asyncio.create_task(pool._heartbeat(active))
        await started.wait()

        async with session_scope() as session:
            assert await queue.cancel(session, job)

        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(work, timeout=5)
        assert active.cancelled
        beat.cancel()

    async def test_a_running_job_can_be_cancelled_at_all(self, job):
        """queue.cancel accepts RUNNING, and clears the worker so nothing looks stuck."""
        async with session_scope() as session:
            assert (await session.get(ProcessingJob, job)).state is JobState.RUNNING
            assert await queue.cancel(session, job)

        async with session_scope() as session:
            row = await session.get(ProcessingJob, job)
            assert row.state is JobState.CANCELLED
            assert row.worker_id is None


class TestHeartbeatVisibility:
    async def test_repeated_failures_are_reported_at_warning(self, job, monkeypatch):
        """The only warning before a job is thrown away was invisible at the default level."""
        monkeypatch.setattr("app.workers.worker._HEARTBEAT_INTERVAL_S", 0.01)
        monkeypatch.setattr("app.workers.worker._HEARTBEAT_WARN_AFTER", 3)

        async def always_fails(*args, **kwargs):
            raise OSError("database is locked")

        monkeypatch.setattr(queue, "heartbeat", always_fails)
        recorded = RecordingLog()
        monkeypatch.setattr("app.workers.worker.log", recorded)

        pool = WorkerPool()
        active = ActiveJob(job_id=job, recording_id=1, filename="outcome.ts")
        beat = asyncio.create_task(pool._heartbeat(active))
        await asyncio.sleep(0.2)
        beat.cancel()
        try:
            await beat
        except asyncio.CancelledError:
            pass

        assert recorded.messages("warning"), "a heartbeat failing repeatedly never rose above debug"
        # The first blip stays quiet; a run of them does not.
        assert recorded.messages("debug"), "every single failure was escalated"


class TestResizingThePool:
    """Lowering the worker count must not throw away the work in flight.

    The shrink used to cancel the highest-indexed worker outright. Nothing set
    ``active.cancelled``, so the run re-raised instead of recording an outcome: the job row
    stayed RUNNING with a dead heartbeat, showed as a phantom running job for the whole
    reclaim timeout, and was then requeued by ``reclaim_stale`` — which, unlike
    ``reclaim_interrupted``, does not refund the attempt. Turning the setting down in the UI
    therefore cost a retry attempt and a full re-decode of whatever was being processed.
    """

    #: The supervisor ticks every two seconds and an idle worker sleeps for two more
    #: before it re-reads its own loop condition, so a resize is observable within about
    #: five. Polled rather than slept through, so a fast machine does not pay for it.
    _SETTLE_BUDGET_S = 12.0

    @classmethod
    async def _settle(cls, pool: WorkerPool, expected: int) -> int:
        for _ in range(int(cls._SETTLE_BUDGET_S / 0.05)):
            await asyncio.sleep(0.05)
            if pool.worker_count == expected:
                break
        return pool.worker_count

    async def test_it_grows_and_shrinks_to_the_setting(self, db_session, app_config):
        from app.core.settings_service import get_settings_service

        pool = WorkerPool()
        await pool.start()
        try:
            await get_settings_service().set("processing.max_workers", 3)
            assert await self._settle(pool, 3) == 3
            await get_settings_service().set("processing.max_workers", 1)
            assert await self._settle(pool, 1) == 1
        finally:
            await pool.stop()

    async def test_a_busy_worker_is_retired_rather_than_cancelled(
        self, db_session, app_config, monkeypatch
    ):
        """Driven through a real claim, because `_busy` is owned by the worker loop.

        A worker marks itself busy from the moment it starts claiming -- there are several
        awaits between `claim_next` flipping a row to RUNNING and the run beginning -- and
        clears it again when a claim finds nothing. Setting the flag from outside would
        therefore be undone by the worker's own next idle cycle and prove nothing.
        """
        import asyncio as _asyncio

        from app.core.settings_service import get_settings_service
        from app.db.models import Recording, RecordingState

        running = _asyncio.Event()
        release = _asyncio.Event()

        async def blocking_run(self, *args, **kwargs):
            running.set()
            await release.wait()

        monkeypatch.setattr(WorkerPool, "_run_job", blocking_run, raising=True)

        async with session_scope() as session:
            recording = Recording(
                rel_path="busy.ts", filename="busy.ts", state=RecordingState.QUEUED
            )
            session.add(recording)
            await session.flush()
            await queue.enqueue(session, recording.id)

        pool = WorkerPool()
        await pool.start()
        try:
            await get_settings_service().set("processing.max_workers", 2)
            assert await self._settle(pool, 2) == 2
            await _asyncio.wait_for(running.wait(), timeout=self._SETTLE_BUDGET_S)

            busy = next(iter(pool._busy))
            task = pool._workers[busy]

            await get_settings_service().set("processing.max_workers", 1)
            # The shrink prefers an idle worker and only retires when the one it would pick
            # is busy, so which of the two happens is not the invariant. The invariant is
            # that the worker owning the run is never cancelled: a cancellation lands
            # *inside* the run, unwinding past the code that records its outcome and
            # leaving the job RUNNING with a dead heartbeat.
            for _ in range(int(self._SETTLE_BUDGET_S / 0.05)):
                await _asyncio.sleep(0.05)
                if pool.worker_count == 1 or busy in pool._retiring:
                    break
            assert not task.cancelled(), "a worker owning a running job was cancelled"
            assert not task.done(), "a worker owning a running job was stopped"

            # It leaves of its own accord once the run finishes, if it was the one chosen.
            release.set()
            assert await self._settle(pool, 1) == 1
            assert not task.cancelled(), "a busy worker was cancelled rather than retired"
        finally:
            release.set()
            await pool.stop()
