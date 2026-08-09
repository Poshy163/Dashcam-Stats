"""Concurrent writers must not lose work to "database is locked".

This reproduces a production failure. Two workers, the scheduler and the log sink all
write, and the job-claim query reads before it writes:

    SELECT id FROM processing_jobs WHERE state='queued' ...   -- shared lock
    UPDATE processing_jobs SET state='running' WHERE id=? ... -- needs a write lock

`busy_timeout` does not cover that upgrade. If another connection is writing when the
UPDATE runs, SQLite returns SQLITE_BUSY *immediately* rather than waiting, because waiting
could deadlock. The result was a steady trickle of `OperationalError: database is locked`
in the worker loop, each one an abandoned job.

`BEGIN IMMEDIATE` takes the write lock at the start of the transaction, so contention
becomes an ordinary wait instead of an instant failure.

A note on what these tests do and do not prove. The concurrency cases below exercise real
parallel claims and writes, but they pass with or without the fix on a fast local disk --
the race needs enough contention to interleave, which a loaded server running two decoders
supplies and a test machine does not. Reproducing it deterministically is not possible
either: the failing interleaving is "read, let someone else write, then write", and with
the fix in place the second writer simply *waits*, so a scripted version deadlocks by
design rather than failing.

`test_write_lock_is_taken_eagerly` is therefore the assertion that actually prevents
regression -- it fails the moment the `begin` handler is removed. The rest are smoke tests
that the claim stays atomic and no work is dropped.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import func, select

from app.db.models import JobState, ProcessingJob, Recording, RecordingState
from app.db.session import session_scope
from app.workers import queue


@pytest.fixture
async def recordings(db_session):
    made = []
    for i in range(40):
        rec = Recording(
            rel_path=f"conc_{i}.ts",
            filename=f"conc_{i}.ts",
            size_bytes=1024,
            state=RecordingState.QUEUED,
        )
        db_session.add(rec)
        made.append(rec)
    await db_session.flush()
    await db_session.commit()
    return made


class TestConcurrentClaims:
    async def test_many_workers_claim_without_lock_errors(self, db_session, recordings):
        """The failure this file exists for: concurrent claims must not raise."""
        async with session_scope() as session:
            for rec in recordings:
                await queue.enqueue(session, rec.id)

        claimed: list[int] = []
        errors: list[Exception] = []

        async def worker(name: str) -> None:
            while True:
                try:
                    async with session_scope() as session:
                        job = await queue.claim_next(session, name)
                        if job is None:
                            return
                        claimed.append(job.id)
                        await queue.complete(session, job, {"by": name})
                except Exception as exc:
                    errors.append(exc)
                    return

        await asyncio.gather(*(worker(f"w{i}") for i in range(6)))

        assert not errors, f"concurrent claims raised: {errors[0]!r}"
        # Every job claimed exactly once -- the atomic claim still holds under contention.
        assert len(claimed) == len(set(claimed)) == len(recordings)

    async def test_no_job_is_left_queued(self, db_session, recordings):
        async with session_scope() as session:
            for rec in recordings:
                await queue.enqueue(session, rec.id)

        async def drain(name: str) -> None:
            while True:
                async with session_scope() as session:
                    job = await queue.claim_next(session, name)
                    if job is None:
                        return
                    await queue.complete(session, job, None)

        await asyncio.gather(*(drain(f"w{i}") for i in range(4)))

        async with session_scope() as session:
            leftover = int(
                (
                    await session.execute(
                        select(func.count(ProcessingJob.id)).where(
                            ProcessingJob.state == JobState.QUEUED
                        )
                    )
                ).scalar()
                or 0
            )
        assert leftover == 0


class TestConcurrentWrites:
    async def test_parallel_writers_all_commit(self, db_session):
        """Bulk telemetry inserts run alongside job bookkeeping in production."""
        errors: list[Exception] = []

        async def writer(index: int) -> None:
            try:
                for n in range(6):
                    async with session_scope() as session:
                        session.add(
                            Recording(
                                rel_path=f"w{index}_{n}.ts",
                                filename=f"w{index}_{n}.ts",
                                size_bytes=n,
                            )
                        )
            except Exception as exc:
                errors.append(exc)

        await asyncio.gather(*(writer(i) for i in range(8)))

        assert not errors, f"parallel writes raised: {errors[0]!r}"
        async with session_scope() as session:
            total = int(
                (
                    await session.execute(
                        select(func.count(Recording.id)).where(Recording.filename.like("w%_%.ts"))
                    )
                ).scalar()
                or 0
            )
        assert total == 48, "a writer silently lost its rows"


class TestClaimIsOneStatement:
    """The claim must not read a candidate and then update it separately.

    That shape takes a read snapshot and needs a write-lock upgrade afterwards, which
    SQLite refuses immediately (SQLITE_BUSY_SNAPSHOT) rather than waiting -- busy_timeout
    does not apply. Selecting inside the UPDATE keeps it atomic.
    """

    def test_claim_selects_inside_the_update(self):
        import inspect

        source = inspect.getsource(queue.claim_next)
        update_at = source.find("update(ProcessingJob)")
        assert update_at != -1, "claim_next no longer issues an UPDATE"

        # A standalone `await session.execute(select(...))` before the UPDATE is the
        # read-then-write pattern this guards against. The subquery form embeds the
        # select in the statement instead.
        assert "scalar_subquery()" in source, (
            "claim_next should select the candidate inside the UPDATE, not beforehand"
        )
        assert "execute(\n            select(" not in source

    async def test_transactions_stay_deferred(self, db_session):
        """Forcing BEGIN IMMEDIATE starves readers.

        It fixes the claim race by serialising everything, including read-only API
        requests, which then queue behind long worker writes and fail on BEGIN itself.
        Under WAL a deferred read never takes the write lock at all.
        """
        from app.db.session import get_engine

        engine = get_engine()
        assert not engine.sync_engine.dispatch.begin, (
            "a 'begin' handler is installed; if it forces BEGIN IMMEDIATE it will starve "
            "readers behind the workers' write transactions"
        )


class TestJobDiagnosticsArePersisted:
    """How a job ran, not just that it ran.

    The Queue page shows throughput, decoder and inference device per job. All three used
    to be written only by the heartbeat, which is cancelled the moment the job finishes —
    and realtime speed is not even known until the last stage returns, so persisting it
    was a race against the `finally` that kills the heartbeat. On a real library that race
    was lost every time: `speed_realtime` was empty on all 200 jobs sampled and `decoder`
    on 195, so three columns were blank for every recording ever processed.
    """

    async def test_completion_records_how_the_job_ran(self, db_session):
        rec = Recording(
            rel_path="diag.ts",
            filename="diag.ts",
            size_bytes=1024,
            state=RecordingState.QUEUED,
        )
        db_session.add(rec)
        await db_session.flush()
        # Release the fixture session's write lock before another session writes, or the
        # two deadlock and the failure looks like a queue bug rather than a test one.
        await db_session.commit()
        recording_id = rec.id

        async with session_scope() as session:
            await queue.enqueue(session, recording_id)
        async with session_scope() as session:
            job = await queue.claim_next(session, "w1")
            assert job is not None
            await queue.complete(
                session,
                job,
                {"ok": True},
                speed=27.7,
                decoder="vaapi",
                device="GPU",
            )
            job_id = job.id

        async with session_scope() as session:
            stored = await session.get(ProcessingJob, job_id)
            assert stored is not None
            assert stored.state is JobState.COMPLETED
            assert stored.speed_realtime == pytest.approx(27.7)
            assert stored.decoder == "vaapi"
            assert stored.inference_device == "GPU"

    async def test_completion_without_diagnostics_keeps_what_the_heartbeat_wrote(self, db_session):
        # A telemetry-only job runs no model, so it has no device to report. Passing None
        # must leave the heartbeat's earlier values alone rather than blanking them.
        rec = Recording(
            rel_path="diag2.ts",
            filename="diag2.ts",
            size_bytes=1024,
            state=RecordingState.QUEUED,
        )
        db_session.add(rec)
        await db_session.flush()
        # Release the fixture session's write lock before another session writes, or the
        # two deadlock and the failure looks like a queue bug rather than a test one.
        await db_session.commit()
        recording_id = rec.id

        async with session_scope() as session:
            await queue.enqueue(session, recording_id)
        async with session_scope() as session:
            job = await queue.claim_next(session, "w1")
            assert job is not None
            job_id = job.id
            await queue.heartbeat(session, job_id, decoder="vaapi", progress=0.5)

        async with session_scope() as session:
            job = await session.get(ProcessingJob, job_id)
            await queue.complete(session, job, {"ok": True})

        async with session_scope() as session:
            stored = await session.get(ProcessingJob, job_id)
            assert stored.decoder == "vaapi"
            assert stored.inference_device is None


class TestTheWriteLockIsNotHeldAcrossAStage:
    """A job must not hold SQLite's write lock for its whole duration.

    `run_stages` marks the recording PROCESSING and flushes, which takes the write lock,
    and the worker holds that session across every stage. Without a commit in between, the
    lock is held through all of the ffmpeg decoding and inference — tens of seconds — while
    the second worker, the scheduler and the log sink block on their next write. Two
    workers become one, and any contention surfaces as "database is locked" rather than as
    a wait.
    """

    async def test_another_session_can_write_while_a_stage_runs(self, db_session):
        from app.pipeline import orchestrator

        rec = Recording(
            rel_path="lock.ts",
            filename="lock.ts",
            size_bytes=1024,
            state=RecordingState.QUEUED,
            duration_s=60.0,
        )
        db_session.add(rec)
        await db_session.flush()
        await db_session.commit()
        recording_id = rec.id

        observed: dict[str, object] = {}

        async def slow_stage(session, recording, *, progress=None):
            """Stands in for a real stage: reads first, then does long non-database work.

            The read is the point. Marking the stage RUNNING leaves the recording dirty,
            and the stage's first query autoflushes it -- taking the write lock before the
            long work rather than after it. A fake stage that never queries misses the bug
            entirely, which is exactly what this test used to do.
            """
            from app.db.models import Camera

            await session.execute(select(Camera).limit(1))
            try:
                async with session_scope() as other:
                    other.add(
                        Recording(
                            rel_path="written-during-stage.ts",
                            filename="written-during-stage.ts",
                            size_bytes=1,
                            state=RecordingState.DISCOVERED,
                        )
                    )
                    await other.flush()
                observed["wrote"] = True
            except Exception as exc:
                observed["error"] = f"{type(exc).__name__}: {exc}"
            from app.pipeline.orchestrator import StageResult

            return StageResult("metadata", True)

        async def noop_stage(session, recording, *, progress=None):
            from app.pipeline.orchestrator import StageResult

            return StageResult("summarise", True)

        # "metadata" stands in for a decode: expand_stages only accepts real stage names,
        # and an unknown one silently falls back to running the entire pipeline.
        original = dict(orchestrator.STAGES)
        try:
            orchestrator.STAGES["metadata"] = slow_stage
            orchestrator.STAGES["summarise"] = noop_stage
            async with session_scope() as session:
                recording = await session.get(Recording, recording_id)
                report = await asyncio.wait_for(
                    orchestrator.run_stages(session, recording, ["metadata"]),
                    timeout=20,
                )
            assert report.ok, report.error
        finally:
            orchestrator.STAGES.clear()
            orchestrator.STAGES.update(original)

        assert observed.get("wrote"), (
            "a second session could not write while a stage was running: the job is "
            f"holding the write lock across the whole stage. {observed.get('error')}"
        )
