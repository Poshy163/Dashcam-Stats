"""Pressing "reprocess all footage" starts again, rather than adding to what was there.

The old behaviour was additive, and every one of these tests is a way that showed:

* the queue kept whatever was already in it, so a run that had failed half the library
  started the new one already half failed;
* a job the pool was decoding stayed running, and could still write its outcome minutes
  after the queue it belonged to had been replaced;
* the counters on the Queue page went on reporting the previous run's arithmetic;
* a recording imported this morning waited behind eight hundred older ones before it was
  given so much as a thumbnail.

The rebuild answers the last one with a pass of its own: every missing thumbnail is made
first, at a priority nothing else can share, and the full analysis then works through the
library oldest first. A recording holds exactly one job the whole way through -- the
analysis job is created when the thumbnail job finishes, which is what keeps a
thumbnails-first queue from being a queue with everything in it twice.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.core.paths import media_root
from app.db.models import (
    BULK_PRIORITY,
    NEW_FOOTAGE_PRIORITY,
    JobKind,
    JobState,
    ProcessingJob,
    Recording,
    RecordingState,
    StageState,
    TelemetryPoint,
)
from app.db.session import session_scope
from app.workers import queue
from app.workers.reset import reset_and_rebuild

BASE = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def isolated_reset_epoch():
    """The epoch is process-wide durable state, so it must not leak between tests."""
    original = queue._reset_at
    yield
    queue._reset_at = original


def write_thumbnail(name: str) -> str:
    """A real file on the media volume, because "has a thumbnail" is asked of the disk."""
    path = media_root() / "thumbnails" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xd8\xff\xdb")
    return f"thumbnails/{name}"


async def make_recording(
    session,
    name: str,
    *,
    hours: float = 0.0,
    thumbnail: str | None = None,
    analysed: bool = True,
    **kwargs,
) -> Recording:
    recording = Recording(
        rel_path=name,
        filename=name,
        size_bytes=1024,
        # Withheld from a file still inside the settle window, so the rebuild reads its
        # presence as "the camera has finished writing this".
        fingerprint=f"fp-{name}",
        started_at=BASE + timedelta(hours=hours),
        thumbnail_path=thumbnail,
        processed_at=BASE + timedelta(hours=hours) if analysed else None,
        state=kwargs.pop("state", RecordingState.COMPLETED if analysed else RecordingState.QUEUED),
        **kwargs,
    )
    session.add(recording)
    await session.flush()
    return recording


async def jobs_for(session, recording_id: int, *states: JobState) -> list[ProcessingJob]:
    stmt = select(ProcessingJob).where(ProcessingJob.recording_id == recording_id)
    if states:
        stmt = stmt.where(ProcessingJob.state.in_(states))
    return list((await session.execute(stmt.order_by(ProcessingJob.id))).scalars())


async def drain(limit: int) -> list[tuple[JobKind, int | None]]:
    """Claim and finish jobs one at a time, recording the order the queue hands them over.

    One at a time on purpose: the claim refuses a recording that is already running, so
    running them concurrently would test the interleaving rather than the ordering.
    """
    order: list[tuple[JobKind, int | None]] = []
    for index in range(limit):
        async with session_scope() as session:
            job = await queue.claim_next(session, f"worker-{index}")
            if job is None:
                break
            order.append((job.kind, job.recording_id))
            await queue.complete(session, job, {"ok": True})
    return order


class TestTheQueueIsEmptiedFirst:
    async def test_waiting_running_and_failed_jobs_are_all_retired(self, db_session):
        waiting = await make_recording(db_session, "waiting.ts", hours=1, thumbnail=None)
        busy = await make_recording(db_session, "busy.ts", hours=2, thumbnail=None)
        broken = await make_recording(db_session, "broken.ts", hours=3, thumbnail=None)
        db_session.add_all(
            [
                ProcessingJob(recording_id=waiting.id, state=JobState.QUEUED),
                ProcessingJob(
                    recording_id=busy.id,
                    state=JobState.RUNNING,
                    worker_id="w1",
                    progress=0.4,
                    stage_current="detection",
                ),
                ProcessingJob(
                    recording_id=broken.id,
                    state=JobState.FAILED,
                    attempts=4,
                    error_message="decoder gave up",
                ),
            ]
        )
        await db_session.flush()

        summary = await reset_and_rebuild(db_session, stages=["everything"])

        assert summary.cleared == {"queued": 1, "running": 1, "failed": 1}
        stale = list(
            (
                await db_session.execute(
                    select(ProcessingJob).where(ProcessingJob.queued_at < summary.started_at)
                )
            ).scalars()
        )
        assert [job.state for job in stale] == [JobState.CANCELLED] * 3
        assert all(job.worker_id is None and job.progress == 0.0 for job in stale)
        assert all(job.stage_current is None for job in stale)

    async def test_every_counter_starts_from_zero(self, db_session):
        old = await make_recording(
            db_session, "old.ts", hours=1, thumbnail=write_thumbnail("a.jpg")
        )
        db_session.add_all(
            [
                ProcessingJob(recording_id=old.id, state=JobState.FAILED, attempts=4),
                ProcessingJob(recording_id=old.id, state=JobState.COMPLETED, finished_at=BASE),
            ]
        )
        await db_session.flush()

        before = await queue.stats(db_session)
        assert before["failed"] == 1 and before["completed"] == 1

        await reset_and_rebuild(db_session, stages=["everything"])
        after = await queue.stats(db_session)

        assert after["failed"] == 0, "a failure from the previous run survived the reset"
        assert after["running"] == 0
        assert after["completed"] == 0
        assert after["completed_today"] == 0
        assert after["cancelled"] == 0, "the jobs the reset itself retired were counted as its own"
        assert after["queued"] == 1, "the rebuilt queue should hold exactly the eligible footage"

    async def test_nothing_the_reset_is_not_about_is_deleted(self, db_session):
        """Footage, analysis and the log trail of the previous run all survive."""
        recording = await make_recording(db_session, "keep.ts", hours=1)
        db_session.add(TelemetryPoint(recording_id=recording.id, t_offset_s=1.0, has_fix=True))
        db_session.add(ProcessingJob(recording_id=recording.id, state=JobState.COMPLETED))
        await db_session.flush()

        await reset_and_rebuild(db_session, stages=["everything"])

        assert await db_session.get(Recording, recording.id) is not None
        assert (
            int(
                (
                    await db_session.execute(
                        select(func.count(TelemetryPoint.id)).where(
                            TelemetryPoint.recording_id == recording.id
                        )
                    )
                ).scalar()
            )
            == 1
        )
        # Retired, not removed: `log_entries` cascades from this table, and deleting the
        # rows would take the record of what the previous run did with them.
        assert int((await db_session.execute(select(func.count(ProcessingJob.id)))).scalar()) == 2


class TestTheQueueIsRebuiltFromTheFootage:
    async def test_each_recording_is_queued_exactly_once(self, db_session):
        for index in range(5):
            await make_recording(db_session, f"clip{index}.ts", hours=index)
        # Two rows for one recording, which is the state a stacking bulk requeue left.
        extra = await make_recording(db_session, "twice.ts", hours=9)
        db_session.add_all(
            [
                ProcessingJob(recording_id=extra.id, state=JobState.QUEUED),
                ProcessingJob(recording_id=extra.id, state=JobState.QUEUED),
            ]
        )
        await db_session.flush()

        summary = await reset_and_rebuild(db_session, stages=["everything"])

        assert summary.recordings == 6
        live = list(
            (
                await db_session.execute(
                    select(ProcessingJob.recording_id).where(
                        ProcessingJob.state.in_([JobState.QUEUED, JobState.RUNNING])
                    )
                )
            ).scalars()
        )
        assert len(live) == len(set(live)) == 6, "a recording was queued twice"

    async def test_footage_that_cannot_be_processed_is_left_out(self, db_session):
        await make_recording(db_session, "good.ts", hours=1)
        await make_recording(db_session, "hidden.ts", hours=2, ignored=True)
        await make_recording(db_session, "gone.ts", hours=3, file_missing=True)
        await make_recording(db_session, "empty.ts", hours=4, state=RecordingState.INVALID)
        await make_recording(db_session, "writing.ts", hours=5, state=RecordingState.SETTLING)
        await db_session.flush()

        summary = await reset_and_rebuild(db_session, stages=["everything"])

        assert summary.recordings == 1, (
            "blacklisted, missing, unprocessable or still-being-written footage was queued"
        )

    async def test_new_footage_is_swept_in_rather_than_dropped(self, db_session):
        """The rebuild owns the whole queue, so anything it skips loses its job entirely.

        The old endpoint deliberately ignored never-analysed recordings, which was right
        while it was only adding to a queue that already held their jobs. It is exactly
        wrong once the queue has been emptied first.
        """
        fresh = await make_recording(db_session, "fresh.ts", hours=6, analysed=False)
        await db_session.flush()

        await reset_and_rebuild(db_session, stages=["telemetry"])

        job = (await jobs_for(db_session, fresh.id, JobState.QUEUED))[0]
        assert job.stages is None, (
            "a recording that has never been analysed was given a partial stage selection; "
            "'telemetry only' cannot mean anything to a file whose metadata is unread"
        )

    async def test_recordings_come_back_to_a_clean_waiting_state(self, db_session):
        stuck = await make_recording(
            db_session,
            "stuck.ts",
            hours=1,
            state=RecordingState.PROCESSING,
            error_message="ffprobe produced no output",
        )
        await db_session.flush()

        await reset_and_rebuild(db_session, stages=["everything"])

        await db_session.refresh(stuck)
        assert stuck.state is RecordingState.QUEUED, (
            "a recording left processing by a stopped worker went on reporting a run that "
            "was not happening"
        )
        assert stuck.error_message is None


class TestThumbnailsAreMadeFirst:
    async def test_only_recordings_without_one_are_queued_for_it(self, db_session):
        has = await make_recording(
            db_session, "has.ts", hours=1, thumbnail=write_thumbnail("has.jpg")
        )
        missing = await make_recording(db_session, "missing.ts", hours=2, thumbnail=None)
        await db_session.flush()

        summary = await reset_and_rebuild(db_session, stages=["everything"])

        assert (summary.thumbnails, summary.analysis) == (1, 1)
        assert (await jobs_for(db_session, missing.id))[0].kind is JobKind.THUMBNAIL
        assert (await jobs_for(db_session, has.id))[0].kind is JobKind.REPROCESS, (
            "a recording that already had a picture was sent round the thumbnail pass again"
        )

    async def test_a_recorded_path_with_no_file_behind_it_counts_as_missing(self, db_session):
        """The column says there is a thumbnail; the media volume disagrees.

        A restored backup, a pruned volume or a write that never landed all produce this,
        and the check that only looked at the column left those recordings unable to get a
        picture ever again.
        """
        ghost = await make_recording(
            db_session, "ghost.ts", hours=1, thumbnail="thumbnails/never-written.jpg"
        )
        await db_session.flush()

        await reset_and_rebuild(db_session, stages=["everything"])

        assert (await jobs_for(db_session, ghost.id))[0].kind is JobKind.THUMBNAIL

    async def test_they_run_before_every_analysis_however_old_the_footage(self, db_session):
        """The whole point: a clip imported today is not behind the library's backlog."""
        await make_recording(
            db_session, "ancient.ts", hours=0, thumbnail=write_thumbnail("ancient.jpg")
        )
        await make_recording(
            db_session, "older.ts", hours=1, thumbnail=write_thumbnail("older.jpg")
        )
        newest = await make_recording(db_session, "newest.ts", hours=99, thumbnail=None)
        await db_session.flush()
        await reset_and_rebuild(db_session, stages=["everything"])
        await db_session.commit()

        first = (await drain(1))[0]
        assert first == (JobKind.THUMBNAIL, newest.id), (
            "the newest recording waited behind the analysis of every older one for a "
            "picture that takes a couple of seconds to make"
        )

    async def test_the_run_reports_which_pass_it_is_in(self, db_session):
        await make_recording(db_session, "one.ts", hours=1, thumbnail=None)
        await make_recording(db_session, "two.ts", hours=2, thumbnail=write_thumbnail("two.jpg"))
        await db_session.flush()
        await reset_and_rebuild(db_session, stages=["everything"])

        stats = await queue.stats(db_session)
        assert stats["thumbnails_pending"] == 1
        assert stats["phase"] == "thumbnails"


class TestTheQueuePageShowsTheOrderItWillRunIn:
    async def test_a_thumbnail_follow_up_is_listed_where_it_will_run(self, client, db_session):
        """Found on the live library: ten recordings listed at the bottom of a nine-hundred
        row queue while the claim was about to run them from the middle of it.

        Ordering the waiting list by ``queued_at`` agrees with the claim only while nothing
        is added after the queue is built, and the thumbnail pass adds constantly -- every
        thumbnail job creates its recording's analysis job as it finishes, minutes after the
        rebuild stamped the rest.
        """
        async with session_scope() as session:
            await make_recording(session, "old.ts", hours=1, thumbnail=write_thumbnail("old.jpg"))
            # No thumbnail, and old: its analysis job is created last and belongs first.
            await make_recording(session, "oldest.ts", hours=0, thumbnail=None)
            await make_recording(session, "new.ts", hours=9, thumbnail=write_thumbnail("new.jpg"))
            await reset_and_rebuild(session, stages=["everything"])
        await db_session.commit()

        async with session_scope() as session:
            job = await queue.claim_next(session, "w1")
            assert job.kind is JobKind.THUMBNAIL
            await queue.complete(session, job, {"ok": True})

        listed = (await client.get("/api/jobs?state=queued&page_size=50")).json()["items"]
        assert [j["recording_filename"] for j in listed] == ["oldest.ts", "old.ts", "new.ts"], (
            "the queue page listed the waiting work in a different order from the one the "
            "claim will take it in"
        )


class TestThenTheAnalysisRunsOldestFirst:
    async def test_the_whole_library_comes_out_in_order(self, db_session):
        """Thumbnails first, then every recording chronologically -- including the ones the
        thumbnail pass handled, which rejoin in their proper place rather than at the front.
        """
        clips = {}
        for hours, thumbnail in ((1, "a.jpg"), (2, None), (3, "c.jpg"), (4, None)):
            name = f"clip{hours}.ts"
            clips[hours] = await make_recording(
                db_session,
                name,
                hours=hours,
                thumbnail=write_thumbnail(thumbnail) if thumbnail else None,
            )
        await db_session.flush()
        await reset_and_rebuild(db_session, stages=["everything"])
        await db_session.commit()

        order = await drain(10)

        assert order == [
            (JobKind.THUMBNAIL, clips[2].id),
            (JobKind.THUMBNAIL, clips[4].id),
            (JobKind.REPROCESS, clips[1].id),
            (JobKind.REPROCESS, clips[2].id),
            (JobKind.REPROCESS, clips[3].id),
            (JobKind.REPROCESS, clips[4].id),
        ], "the queue did not run thumbnails first and then the footage oldest first"

    async def test_a_recording_with_no_known_time_still_sorts_last(self, db_session):
        dated = await make_recording(
            db_session, "dated.ts", hours=5, thumbnail=write_thumbnail("dated.jpg")
        )
        undated = await make_recording(
            db_session, "undated.ts", thumbnail=write_thumbnail("undated.jpg")
        )
        undated.started_at = None
        await db_session.flush()
        await reset_and_rebuild(db_session, stages=["everything"])
        await db_session.commit()

        assert [rid for _, rid in await drain(5)] == [dated.id, undated.id]


class TestTheThumbnailPassHandsRecordingsOn:
    async def test_finishing_one_queues_the_analysis_exactly_once(self, db_session):
        recording = await make_recording(db_session, "hand-on.ts", hours=1, thumbnail=None)
        await db_session.flush()
        await reset_and_rebuild(db_session, stages=["telemetry"])
        await db_session.commit()

        async with session_scope() as session:
            job = await queue.claim_next(session, "w1")
            assert job.kind is JobKind.THUMBNAIL
            await queue.complete(session, job, {"ok": True})

        async with session_scope() as session:
            live = await jobs_for(session, recording.id, JobState.QUEUED, JobState.RUNNING)
            assert len(live) == 1, "the thumbnail pass left the recording queued twice"
            assert live[0].kind is JobKind.REPROCESS
            assert live[0].stages == ["telemetry"], "the stage selection was lost on the way"
            assert live[0].priority == BULK_PRIORITY

    async def test_a_repeated_outcome_write_does_not_queue_it_twice(self, db_session):
        """`_finish` retries the outcome write, and a commit can fail after the row landed."""
        recording = await make_recording(db_session, "retry.ts", hours=1, thumbnail=None)
        await db_session.flush()
        await reset_and_rebuild(db_session, stages=["everything"])
        await db_session.commit()

        async with session_scope() as session:
            job = await queue.claim_next(session, "w1")
            await queue.complete(session, job, {"ok": True})
            await queue.complete(session, job, {"ok": True})

        async with session_scope() as session:
            assert len(await jobs_for(session, recording.id, JobState.QUEUED)) == 1

    async def test_a_thumbnail_that_cannot_be_made_still_gets_analysed(self, db_session):
        """The analysis pass probes the file properly, so it is also what reaches a real
        verdict about footage this pass could only fail on."""
        recording = await make_recording(db_session, "no-frame.ts", hours=1, thumbnail=None)
        await db_session.flush()
        await reset_and_rebuild(db_session, stages=["everything"])
        await db_session.commit()

        async with session_scope() as session:
            job = await queue.claim_next(session, "w1")
            await queue.fail(session, job, "no usable frame", permanent=True)

        async with session_scope() as session:
            live = await jobs_for(session, recording.id, JobState.QUEUED)
            assert len(live) == 1 and live[0].kind is JobKind.REPROCESS, (
                "a recording whose thumbnail failed was dropped from the run entirely"
            )

    async def test_a_retry_does_not_hand_it_on_early(self, db_session):
        recording = await make_recording(db_session, "retryable.ts", hours=1, thumbnail=None)
        await db_session.flush()
        await reset_and_rebuild(db_session, stages=["everything"])
        await db_session.commit()

        async with session_scope() as session:
            job = await queue.claim_next(session, "w1")
            await queue.fail(session, job, "the media stack is failing")

        async with session_scope() as session:
            live = await jobs_for(session, recording.id, JobState.QUEUED)
            assert len(live) == 1 and live[0].kind is JobKind.THUMBNAIL


@pytest.mark.needs_ffmpeg
@pytest.mark.slow
class TestTheThumbnailPassAgainstRealFootage:
    """The pass itself, decoding an actual file rather than a mocked one.

    Everything above tests the queue's arithmetic, which would go on being correct if the
    pass produced no picture at all.
    """

    @pytest.fixture
    async def clip(self, db_session, app_config, front_clip):
        shutil.copy2(front_clip, app_config.footage_dir / front_clip.name)
        # Deliberately without a duration: this is what an unprobed recording looks like,
        # and the pass has to cope rather than probe for one.
        recording = await make_recording(db_session, front_clip.name, hours=1, thumbnail=None)
        await db_session.commit()
        return recording

    async def test_it_writes_a_picture_and_records_where(self, db_session, clip):
        from app.core.paths import resolve_media_path
        from app.pipeline.stages import ensure_thumbnail

        outcome = await ensure_thumbnail(clip)

        assert outcome.written is True
        assert clip.thumbnail_path
        written = resolve_media_path(clip.thumbnail_path)
        assert written.is_file() and written.stat().st_size > 0

    async def test_a_second_pass_does_not_decode_it_again(self, db_session, clip):
        from app.core.paths import resolve_media_path
        from app.pipeline.stages import ensure_thumbnail

        await ensure_thumbnail(clip)
        written = resolve_media_path(clip.thumbnail_path)
        stamp = written.stat().st_mtime_ns

        again = await ensure_thumbnail(clip)

        assert (again.written, again.present) == (False, True)
        assert written.stat().st_mtime_ns == stamp, "an existing thumbnail was regenerated"

    async def test_the_worker_runs_it_as_a_job(self, db_session, clip):
        from app.core.paths import resolve_media_path
        from app.workers.worker import ActiveJob, WorkerPool

        await reset_and_rebuild(db_session, stages=["everything"])
        await db_session.commit()

        async with session_scope() as session:
            job = await queue.claim_next(session, "w1")
        assert job.kind is JobKind.THUMBNAIL

        active = ActiveJob(job_id=job.id, recording_id=clip.id, filename=clip.filename)
        report = await WorkerPool()._make_thumbnail(clip.id, active)

        assert report.ok and [s.name for s in report.stages] == ["thumbnail"]
        assert active.stage == "thumbnail"
        async with session_scope() as session:
            stored = await session.get(Recording, clip.id)
            assert stored.thumbnail_path
            assert resolve_media_path(stored.thumbnail_path).is_file()
            # The pass makes a picture and claims nothing else about the recording.
            assert stored.state is RecordingState.QUEUED
            assert stored.processed_at is None or stored.processed_at == clip.processed_at
            assert stored.metadata_state is StageState.PENDING

    async def test_a_file_that_has_gone_is_a_retryable_failure(self, db_session, clip, app_config):
        """Not a verdict about the recording: an absent file is very often an absent share."""
        from app.pipeline.stages import StageError, ensure_thumbnail

        (app_config.footage_dir / clip.filename).unlink()

        with pytest.raises(StageError) as raised:
            await ensure_thumbnail(clip)
        assert raised.value.permanent is False


class TestStaleWorkCannotWriteBack:
    async def test_a_cancelled_run_does_not_record_its_outcome(self, db_session):
        """The run the reset interrupted finishes a minute later and tries to report.

        Left to it, the job came back as completed -- or as queued with a retry pending --
        inside a queue that had already been rebuilt without it.
        """
        from app.workers.worker import ActiveJob, WorkerPool

        recording = await make_recording(db_session, "in-flight.ts", hours=1)
        job = ProcessingJob(recording_id=recording.id, state=JobState.RUNNING, worker_id="w1")
        db_session.add(job)
        await db_session.flush()
        job_id = job.id

        await reset_and_rebuild(db_session, stages=["everything"])
        await db_session.commit()

        pool = WorkerPool()
        await pool._finish(
            job_id,
            ActiveJob(job_id=job_id, recording_id=recording.id, filename="in-flight.ts"),
            note="finished after the reset",
        )

        async with session_scope() as session:
            stale = await session.get(ProcessingJob, job_id)
            assert stale.state is JobState.CANCELLED, "a stale worker un-cancelled its job"
            assert stale.result is None
            stats = await queue.stats(session)
        assert stats["completed"] == 0 and stats["completed_today"] == 0

    async def test_the_epoch_survives_a_restart(self, db_session):
        await make_recording(db_session, "epoch.ts", hours=1)
        await db_session.flush()
        summary = await reset_and_rebuild(db_session, stages=["everything"])

        # A fresh process reads it back off the data volume before reporting any count.
        queue._reset_at = None
        assert queue.restore_reset_epoch() == summary.started_at
        assert queue.reset_epoch() == summary.started_at

    async def test_a_scan_does_not_reorder_the_rebuilt_queue(self, db_session):
        """`queue_unprocessed` pulls never-analysed recordings to the front of a bulk
        requeue, which is right for a queue that was added to and wrong for one that was
        rebuilt around them."""
        from app.scanner.discovery import queue_unprocessed

        fresh = await make_recording(db_session, "brand-new.ts", hours=8, analysed=False)
        await db_session.flush()
        await reset_and_rebuild(db_session, stages=["everything"])

        await queue_unprocessed(db_session)

        live = await jobs_for(db_session, fresh.id, JobState.QUEUED, JobState.RUNNING)
        assert len(live) == 1, "the scan queued a recording the rebuild had already queued"
        assert live[0].priority != NEW_FOOTAGE_PRIORITY, (
            "the scan promoted a rebuilt job out of its chronological place"
        )


class TestTheEndpoint:
    async def test_it_resets_and_reports_what_it_did(self, client, db_session):
        async with session_scope() as session:
            await make_recording(session, "api-old.ts", hours=1, thumbnail=write_thumbnail("o.jpg"))
            await make_recording(session, "api-new.ts", hours=2, thumbnail=None)
            failed = await make_recording(session, "api-bad.ts", hours=3, thumbnail=None)
            session.add(ProcessingJob(recording_id=failed.id, state=JobState.FAILED, attempts=4))
        await db_session.commit()

        body = (await client.post("/api/reprocess", json={"stages": ["everything"]})).json()

        assert body["reset"] is True
        assert body["queued"] == 3
        assert body["thumbnails_queued"] == 2
        assert body["cleared"] == {"failed": 1}

        stats = (await client.get("/api/jobs/stats")).json()
        assert stats["failed"] == 0
        assert stats["running"] == 0
        assert stats["queued"] == 3
        # Serialised snake_case; the browser client converts on the way in.
        assert stats["thumbnails_pending"] == 2
        assert stats["phase"] == "thumbnails"

    async def test_the_targeted_reruns_still_leave_the_queue_alone(self, client, db_session):
        """ "Failed only" and "outdated only" repair a queue the user wants to keep."""
        async with session_scope() as session:
            waiting = await make_recording(session, "api-waiting.ts", hours=1)
            session.add(ProcessingJob(recording_id=waiting.id, state=JobState.QUEUED))
        await db_session.commit()

        body = (
            await client.post(
                # snake_case on the wire: the browser client converts request bodies too.
                "/api/reprocess",
                json={"stages": ["everything"], "only_failed": True},
            )
        ).json()

        assert body.get("reset") is None
        async with session_scope() as session:
            live = await jobs_for(session, waiting.id, JobState.QUEUED)
        assert len(live) == 1, "a targeted rerun discarded work the user had not asked about"
