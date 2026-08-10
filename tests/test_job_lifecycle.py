"""A recording's trip through the queue, and the four ways it used to go wrong.

Each of these turned an ordinary situation into a permanently failed or permanently stuck
recording, and each looked like something else in the logs:

* **Contention counted as failure.** ``OperationalError: database is locked`` on the first
  ``DELETE`` of a stage spent an attempt. Four collisions with a busy database and a
  perfectly good recording was marked failed 4/4, for a reason with nothing to do with the
  footage.
* **Two workers, one recording.** ``enqueue(force=True)`` -- what a bulk reprocess uses --
  created a second job for a recording another worker was already running, and the claim
  had no opinion about that.
* **Jobs stacked.** The same call also left one queued job per press, so two presses of
  "reprocess everything" decoded the whole library twice.
* **Stranded mid-processing.** A hard restart left the recording ``processing`` with no job
  to finish it, and nothing anywhere looked for that pairing.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.db.models import JobState, ProcessingJob, Recording, RecordingState
from app.db.session import session_scope
from app.workers import queue


async def make_recording(session, name: str = "job.ts", **kwargs) -> int:
    recording = Recording(
        rel_path=name,
        filename=name,
        size_bytes=1024,
        state=kwargs.pop("state", RecordingState.QUEUED),
        **kwargs,
    )
    session.add(recording)
    await session.flush()
    await session.commit()
    return recording.id


@pytest.fixture
async def recording_id(db_session) -> int:
    return await make_recording(db_session)


class TestTransientFailuresDoNotSpendAttempts:
    async def test_a_database_lock_is_refunded(self, recording_id):
        async with session_scope() as session:
            await queue.enqueue(session, recording_id)

        for _ in range(5):
            async with session_scope() as session:
                job = await queue.claim_next(session, "w1")
                assert job is not None, "a contended job was not requeued for another try"
                await queue.fail(
                    session,
                    job,
                    "(sqlite3.OperationalError) database is locked",
                    transient=True,
                )
                job.not_before = None  # skip the backoff for the test's sake
                job_id = job.id

        async with session_scope() as session:
            stored = await session.get(ProcessingJob, job_id)
            assert stored.state is JobState.QUEUED, (
                "five collisions with a busy database permanently failed a valid recording"
            )
            assert stored.attempts == 0, "contention was charged against the retry budget"

    async def test_the_free_retries_are_bounded(self, recording_id):
        """A database that is locked forever must still surface as a failure eventually,
        or the job requeues itself every twenty seconds until the container restarts."""
        async with session_scope() as session:
            await queue.enqueue(session, recording_id)

        # Enough rounds to exhaust the free retries and then the ordinary attempt budget
        # behind them; the loop stops as soon as the queue gives up, which is the point.
        job_id = None
        for _ in range(40):
            async with session_scope() as session:
                job = await queue.claim_next(session, "w1")
                if job is None:
                    break
                job_id = job.id
                await queue.fail(session, job, "database is locked", transient=True)
                job.not_before = None

        async with session_scope() as session:
            stored = await session.get(ProcessingJob, job_id)
        assert stored.state is JobState.FAILED, (
            "a database that never unlocks would requeue this job forever"
        )

    async def test_an_ordinary_failure_still_costs_an_attempt(self, recording_id):
        async with session_scope() as session:
            await queue.enqueue(session, recording_id)
        async with session_scope() as session:
            job = await queue.claim_next(session, "w1")
            await queue.fail(session, job, "decode failed")
            job_id, attempts = job.id, job.attempts

        assert attempts == 1
        async with session_scope() as session:
            assert (await session.get(ProcessingJob, job_id)).attempts == 1


class TestOneRecordingAtATime:
    async def test_a_second_job_cannot_start_while_the_first_runs(self, recording_id):
        """A bulk reprocess queues everything, including whatever is running right now."""
        async with session_scope() as session:
            await queue.enqueue(session, recording_id)
        async with session_scope() as session:
            first = await queue.claim_next(session, "w1")
            assert first is not None

        # The reprocess arrives while worker 1 is still decoding.
        async with session_scope() as session:
            await queue.enqueue(session, recording_id, force=True)

        async with session_scope() as session:
            second = await queue.claim_next(session, "w2")
        assert second is None, (
            "two workers claimed the same recording; both will delete and rewrite its "
            "rows, and whichever delete lands mid-way takes the other's inserts with it"
        )

    async def test_it_runs_once_the_first_has_finished(self, recording_id):
        async with session_scope() as session:
            await queue.enqueue(session, recording_id)
        async with session_scope() as session:
            first = await queue.claim_next(session, "w1")
            await queue.complete(session, first, {"ok": True})
        async with session_scope() as session:
            await queue.enqueue(session, recording_id, force=True)
        async with session_scope() as session:
            assert await queue.claim_next(session, "w2") is not None

    async def test_other_recordings_are_not_blocked(self, db_session, recording_id):
        other = await make_recording(db_session, "other.ts")
        async with session_scope() as session:
            await queue.enqueue(session, recording_id)
            await queue.enqueue(session, other)
        async with session_scope() as session:
            first = await queue.claim_next(session, "w1")
        async with session_scope() as session:
            second = await queue.claim_next(session, "w2")
        assert first is not None and second is not None
        assert first.recording_id != second.recording_id


class TestForcingDoesNotStack:
    async def test_repeated_requests_leave_one_queued_job(self, recording_id):
        for _ in range(3):
            async with session_scope() as session:
                await queue.enqueue(session, recording_id, force=True)

        async with session_scope() as session:
            queued = int(
                (
                    await session.execute(
                        select(func.count(ProcessingJob.id)).where(
                            ProcessingJob.recording_id == recording_id,
                            ProcessingJob.state == JobState.QUEUED,
                        )
                    )
                ).scalar()
            )
        assert queued == 1, (
            "three reprocess requests left three queued jobs; the library would be "
            "decoded three times over"
        )


class TestNothingIsLeftStranded:
    async def test_a_recording_left_processing_is_released(self, db_session):
        """The pairing nothing looked for: a killed process leaves the recording
        ``processing`` and its job gone, and ``queue_unprocessed`` looks at neither."""
        recording_id = await make_recording(
            db_session, "stranded.ts", state=RecordingState.PROCESSING
        )

        async with session_scope() as session:
            released = await queue.release_stranded_recordings(session)
        assert released == 1

        async with session_scope() as session:
            assert (await session.get(Recording, recording_id)).state is RecordingState.DISCOVERED

    async def test_a_recording_with_a_live_job_is_left_alone(self, recording_id):
        async with session_scope() as session:
            await queue.enqueue(session, recording_id)
            recording = await session.get(Recording, recording_id)
            recording.state = RecordingState.PROCESSING

        async with session_scope() as session:
            assert await queue.release_stranded_recordings(session) == 0

    async def test_reclaiming_a_stale_job_brings_its_recording_back_too(self, recording_id):
        from app.core.settings_service import get_settings_service

        async with session_scope() as session:
            await queue.enqueue(session, recording_id)
        async with session_scope() as session:
            job = await queue.claim_next(session, "dead-worker")
            job.heartbeat_at = None
            recording = await session.get(Recording, recording_id)
            recording.state = RecordingState.PROCESSING

        await get_settings_service().set("advanced.job_heartbeat_timeout_s", 1)
        async with session_scope() as session:
            assert await queue.reclaim_stale(session) == 1

        async with session_scope() as session:
            assert (await session.get(Recording, recording_id)).state is RecordingState.QUEUED


class TestPermanentFailuresLeaveTheQueue:
    async def test_an_unusable_source_marks_the_recording_invalid(self, db_session):
        """``failed`` means "try again"; a file with no video stream never can."""
        from app.pipeline import orchestrator
        from app.pipeline.stages import StageError

        recording_id = await make_recording(db_session, "nostream.ts", duration_s=60.0)

        async def broken_stage(session, recording, *, progress=None):
            raise StageError("nostream.ts contains no video stream", permanent=True)

        original = dict(orchestrator.STAGES)
        try:
            orchestrator.STAGES["metadata"] = broken_stage
            async with session_scope() as session:
                recording = await session.get(Recording, recording_id)
                report = await orchestrator.run_stages(session, recording, ["metadata"])
            assert report.permanent
        finally:
            orchestrator.STAGES.clear()
            orchestrator.STAGES.update(original)

        async with session_scope() as session:
            stored = await session.get(Recording, recording_id)
        assert stored.state is RecordingState.INVALID, (
            "a permanently unprocessable file stayed in the retryable population and will "
            "be handed fresh attempts by the next bulk reprocess"
        )
        assert stored.error_message
