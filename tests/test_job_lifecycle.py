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

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.db.models import JobState, ProcessingJob, Recording, RecordingState, StageState
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


def test_an_operator_pause_survives_a_process_restart(tmp_path, monkeypatch):
    marker = tmp_path / ".queue-paused"
    original = queue._paused
    monkeypatch.setattr(queue, "_pause_marker", lambda: marker)
    try:
        queue.pause()
        assert marker.exists()

        # Simulate a fresh module/process before startup restores durable runtime state.
        queue._paused = False
        assert queue.restore_pause_state()
        assert queue.is_paused()

        queue.resume()
        assert not marker.exists()
        assert not queue.is_paused()
    finally:
        queue._paused = original


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

    async def test_a_running_retry_does_not_display_the_previous_error(self, recording_id):
        async with session_scope() as session:
            await queue.enqueue(session, recording_id)
        async with session_scope() as session:
            job = await queue.claim_next(session, "w1")
            await queue.fail(session, job, "ffprobe produced no output (return code -6)")
            job.not_before = None
            recording = await session.get(Recording, recording_id)
            recording.error_message = job.error_message

        async with session_scope() as session:
            retry = await queue.claim_next(session, "w2")
            assert retry.state is JobState.RUNNING
            assert retry.error_message is None
            assert retry.finished_at is None
            recording = await session.get(Recording, recording_id)
            await session.refresh(recording)
            assert recording.error_message is None


class TestLegacyContentionFailures:
    async def test_old_lock_failures_are_reconciled_without_duplicating_new_work(self, db_session):
        superseded_recording = Recording(
            rel_path="superseded.ts",
            filename="superseded.ts",
            size_bytes=1024,
            state=RecordingState.QUEUED,
            error_message="OperationalError: database is locked",
        )
        retry_recording = Recording(
            rel_path="retry.ts",
            filename="retry.ts",
            size_bytes=1024,
            state=RecordingState.FAILED,
            error_message="OperationalError: database is locked",
        )
        ordinary_recording = Recording(
            rel_path="ordinary.ts",
            filename="ordinary.ts",
            size_bytes=1024,
            state=RecordingState.FAILED,
        )
        db_session.add_all([superseded_recording, retry_recording, ordinary_recording])
        await db_session.flush()

        old_superseded = ProcessingJob(
            recording_id=superseded_recording.id,
            state=JobState.FAILED,
            attempts=4,
            error_message="OperationalError: database is locked",
        )
        old_retry = ProcessingJob(
            recording_id=retry_recording.id,
            state=JobState.FAILED,
            attempts=4,
            progress=0.7,
            error_message="OperationalError: database is locked",
        )
        ordinary_failure = ProcessingJob(
            recording_id=ordinary_recording.id,
            state=JobState.FAILED,
            attempts=3,
            error_message="decoder failed",
        )
        db_session.add_all([old_superseded, old_retry, ordinary_failure])
        await db_session.flush()
        newer = ProcessingJob(
            recording_id=superseded_recording.id,
            state=JobState.QUEUED,
        )
        db_session.add(newer)
        await db_session.flush()

        requeued, superseded = await queue.reconcile_legacy_contention_failures(db_session)

        assert (requeued, superseded) == (1, 1)
        assert old_superseded.state is JobState.CANCELLED
        assert "superseded" in (old_superseded.error_message or "")
        assert old_retry.state is JobState.QUEUED
        assert old_retry.attempts == 0
        assert old_retry.progress == 0.0
        assert old_retry.error_message is None
        assert ordinary_failure.state is JobState.FAILED
        assert superseded_recording.error_message is None
        assert retry_recording.state is RecordingState.QUEUED

    async def test_current_bounded_lock_failure_stays_failed(self, db_session):
        recording_id = await make_recording(db_session, "bounded.ts")
        job = ProcessingJob(
            recording_id=recording_id,
            state=JobState.FAILED,
            attempts=3,
            result={"contention_retries": queue.MAX_CONTENTION_RETRIES},
            error_message="database is locked",
        )
        db_session.add(job)
        await db_session.flush()

        assert await queue.reconcile_legacy_contention_failures(db_session) == (0, 0)
        assert job.state is JobState.FAILED


class TestMisclassifiedProbeCrashes:
    async def test_legacy_abort_is_unhidden_and_requeued_once(self, db_session):
        legacy = Recording(
            rel_path="legacy.ts",
            filename="legacy.ts",
            size_bytes=1024,
            state=RecordingState.INVALID,
            metadata_state=StageState.FAILED,
            ignored=True,
            error_message="ffprobe produced no output for legacy.ts",
            probe_json={
                "source_damaged": True,
                "damaged_policy": {
                    "action": "hide",
                    "reason": "ffprobe produced no output for legacy.ts",
                },
            },
        )
        conclusive = Recording(
            rel_path="bad.ts",
            filename="bad.ts",
            size_bytes=1024,
            state=RecordingState.INVALID,
            metadata_state=StageState.FAILED,
            ignored=True,
            error_message="ffprobe produced no output for bad.ts",
            probe_json={
                "source_damaged": True,
                "probe_failure": {
                    "permanent": True,
                    "returncode": 1,
                    "stderr": "Invalid data found when processing input",
                },
            },
        )
        db_session.add_all([legacy, conclusive])
        await db_session.flush()
        legacy_job = ProcessingJob(
            recording_id=legacy.id,
            state=JobState.FAILED,
            attempts=1,
            error_message="ffprobe produced no output for legacy.ts",
        )
        db_session.add(legacy_job)
        await db_session.flush()

        restored = await queue.reconcile_misclassified_probe_crashes(db_session)

        assert restored == 1
        assert legacy.ignored is False
        assert legacy.state is RecordingState.QUEUED
        assert legacy.metadata_state is StageState.PENDING
        assert legacy.error_message is None
        assert "damaged_policy" not in legacy.probe_json
        assert legacy_job.state is JobState.QUEUED
        assert legacy_job.attempts == 0
        assert conclusive.ignored is True
        assert conclusive.state is RecordingState.INVALID

        assert await queue.reconcile_misclassified_probe_crashes(db_session) == 0


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

    async def test_restart_immediately_reclaims_recent_running_jobs(self, recording_id):
        async with session_scope() as session:
            await queue.enqueue(session, recording_id)
        async with session_scope() as session:
            job = await queue.claim_next(session, "dead-process")
            job_id = job.id
            recording = await session.get(Recording, recording_id)
            recording.state = RecordingState.PROCESSING

        async with session_scope() as session:
            assert await queue.reclaim_interrupted(session) == 1

        async with session_scope() as session:
            job = await session.get(ProcessingJob, job_id)
            recording = await session.get(Recording, recording_id)
            assert job.state is JobState.QUEUED
            assert job.attempts == 0, "an application crash spent a footage retry attempt"
            assert job.worker_id is None
            assert recording.state is RecordingState.QUEUED


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


class TestOldestFootageIsProcessedFirst:
    """A bulk reprocess should work through the library chronologically.

    ``queued_at`` cannot express that. A bulk reprocess stamps every job inside the same
    second, so ordering by it meant the queue ran in whatever order the rows happened to be
    inserted -- and progress through a nine-hundred-recording backlog was unreadable,
    because the date reached told you nothing about how far along it was.
    """

    async def test_the_queue_runs_in_recording_order(self, db_session):
        base = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)
        # Inserted newest-first on purpose: if the claim honoured insertion order, this is
        # the arrangement that would show it.
        wanted = []
        async with session_scope() as session:
            for offset in (5, 1, 3, 0, 4, 2):
                recording = Recording(
                    rel_path=f"clip{offset}.ts",
                    filename=f"clip{offset}.ts",
                    started_at=base + timedelta(hours=offset),
                )
                session.add(recording)
                await session.flush()
                await queue.enqueue(session, recording.id)
                wanted.append((offset, recording.id))

        expected = [rid for _, rid in sorted(wanted)]

        claimed = []
        for index in range(len(expected)):
            async with session_scope() as session:
                job = await queue.claim_next(session, f"worker-{index}")
                assert job is not None
                claimed.append(job.recording_id)
                # Finish it, or the next claim refuses a recording already running.
                await queue.complete(session, job)

        assert claimed == expected, "the queue did not work through the footage oldest first"

    async def test_a_recording_with_no_known_time_sorts_last(self, db_session):
        """An unparsed filename is not evidence of age.

        Sorting unknowns first would let a handful of oddities delay every real recording
        behind them.
        """
        async with session_scope() as session:
            undated = Recording(rel_path="mystery.ts", filename="mystery.ts", started_at=None)
            dated = Recording(
                rel_path="known.ts",
                filename="known.ts",
                started_at=datetime(2026, 8, 9, 12, 0, tzinfo=UTC),
            )
            session.add(undated)
            await session.flush()
            await queue.enqueue(session, undated.id)
            session.add(dated)
            await session.flush()
            await queue.enqueue(session, dated.id)
            dated_id, undated_id = dated.id, undated.id

        order = []
        for index in range(2):
            async with session_scope() as session:
                job = await queue.claim_next(session, f"worker-{index}")
                assert job is not None
                order.append(job.recording_id)
                await queue.complete(session, job)

        assert order == [dated_id, undated_id]
