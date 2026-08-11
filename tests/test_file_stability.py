"""Telling a file that is still being written from a file with nothing in it.

Both look identical from a single ``stat()`` at the wrong moment, and getting the
distinction wrong is expensive in opposite directions. Processing a growing file caches a
truncated duration and a partial telemetry track that nothing ever asks about again;
treating a permanently empty file as a transient failure spends every retry attempt on a
file that has no bytes in it.

The failure that prompted this: three zero-byte segments in the real library failed with
``is empty (0 bytes)`` on attempt 1 of 4, and then reappeared with a fresh attempt counter
after every bulk requeue -- 1/3, then 1/4 -- because the *recording* was left in the same
``failed`` state as a genuinely retryable one.
"""

from __future__ import annotations

import time

import pytest

from app.db.models import JobKind, JobState, ProcessingJob, Recording, RecordingState
from app.db.session import session_scope
from app.scanner.discovery import Scanner, queue_unprocessed
from app.scanner.stability import Readiness, assess

_SETTLE_NS = 90 * 1_000_000_000
_NOW = 1_800_000_000 * 1_000_000_000


def verdict(size: int, age_s: float, *, previous: tuple[int, int] | None = None):
    mtime = _NOW - int(age_s * 1_000_000_000)
    return assess(
        size=size,
        mtime_ns=mtime,
        now_ns=_NOW,
        settle_ns=_SETTLE_NS,
        previous_size=previous[0] if previous else None,
        previous_mtime_ns=previous[1] if previous else None,
    )


class TestTheVerdict:
    def test_a_file_written_seconds_ago_is_still_settling(self):
        assert verdict(50_000_000, age_s=5).readiness is Readiness.SETTLING

    def test_a_file_untouched_for_longer_than_the_window_is_ready(self):
        assert verdict(50_000_000, age_s=600).readiness is Readiness.READY

    def test_an_empty_file_that_has_just_appeared_is_not_condemned(self):
        """A copy that has created the file and not yet written to it looks exactly like
        a permanently empty one. Only time and stability separate them."""
        assert verdict(0, age_s=2).readiness is Readiness.SETTLING

    def test_a_stable_empty_file_is_invalid(self):
        result = verdict(0, age_s=600)
        assert result.readiness is Readiness.INVALID
        assert result.reason and "empty" in result.reason

    def test_a_file_that_changed_since_the_last_scan_waits_one_more(self):
        """Old mtime, different size: a writer that preserves timestamps, or a clock that
        disagrees. Either way one stable observation is cheap insurance."""
        result = verdict(50_000_000, age_s=600, previous=(10_000_000, _NOW - _SETTLE_NS * 2))
        assert result.readiness is Readiness.SETTLING

    def test_an_unchanged_file_seen_before_is_ready(self):
        mtime = _NOW - _SETTLE_NS * 2
        result = assess(
            size=50_000_000,
            mtime_ns=mtime,
            now_ns=_NOW,
            settle_ns=_SETTLE_NS,
            previous_size=50_000_000,
            previous_mtime_ns=mtime,
        )
        assert result.readiness is Readiness.READY

    def test_a_clock_ahead_of_ours_does_not_stall_forever(self):
        """A share whose mtimes are in the future would never leave the settle window if
        the age were tested as a magnitude. It falls through to the stability check, which
        needs no clocks to agree."""
        future = _NOW + _SETTLE_NS * 10
        first = assess(size=1024, mtime_ns=future, now_ns=_NOW, settle_ns=_SETTLE_NS)
        assert first.readiness is Readiness.READY

    def test_settling_is_disabled_by_a_zero_window(self):
        result = assess(size=1024, mtime_ns=_NOW, now_ns=_NOW, settle_ns=0)
        assert result.readiness is Readiness.READY

    def test_a_zero_window_still_condemns_an_empty_file(self):
        result = assess(size=0, mtime_ns=_NOW, now_ns=_NOW, settle_ns=0)
        assert result.readiness is Readiness.INVALID


@pytest.fixture
async def share(db_session, app_config):
    """A footage directory with one real segment and one zero-byte one, both settled."""
    root = app_config.footage_dir
    (root / "20260804111550_camera_0.ts").write_bytes(b"\x47" * (188 * 100))
    (root / "20260804111550_camera_1.ts").write_bytes(b"")
    old = time.time() - 3600
    for name in ("20260804111550_camera_0.ts", "20260804111550_camera_1.ts"):
        import os

        os.utime(root / name, (old, old))
    return root


async def _state(filename: str) -> RecordingState:
    from sqlalchemy import select

    async with session_scope() as session:
        return (
            await session.execute(select(Recording.state).where(Recording.filename == filename))
        ).scalar_one()


async def test_the_network_share_walk_runs_off_the_application_event_loop(share, monkeypatch):
    import threading

    scanner = Scanner(footage_dir=share)
    original_walk = scanner._walk
    event_loop_thread = threading.get_ident()
    walk_threads: set[int] = set()

    def observed_walk(*args, **kwargs):
        walk_threads.add(threading.get_ident())
        yield from original_walk(*args, **kwargs)

    monkeypatch.setattr(scanner, "_walk", observed_walk)

    await scanner.scan(trigger="test")

    assert walk_threads
    assert event_loop_thread not in walk_threads, (
        "os.scandir/stat against NFS ran on Uvicorn's event loop and can freeze every route"
    )


class TestAnEmptyRecordingLeavesTheQueue:
    async def test_it_is_marked_invalid_rather_than_failed(self, share):
        summary = await Scanner(footage_dir=share).scan(trigger="test")
        assert summary.invalid == 1

        assert await _state("20260804111550_camera_1.ts") is RecordingState.INVALID
        assert await _state("20260804111550_camera_0.ts") is RecordingState.DISCOVERED

    async def test_it_says_why(self, share):
        from sqlalchemy import select

        await Scanner(footage_dir=share).scan(trigger="test")
        async with session_scope() as session:
            message = (
                await session.execute(
                    select(Recording.error_message).where(
                        Recording.filename == "20260804111550_camera_1.ts"
                    )
                )
            ).scalar_one()
        assert message and "empty" in message, (
            "a recording that is being skipped forever has to explain itself in the UI"
        )

    async def test_it_is_never_queued(self, share):
        await Scanner(footage_dir=share).scan(trigger="test")
        async with session_scope() as session:
            queued = await queue_unprocessed(session)
        assert queued == 1, "the empty segment was handed a processing attempt"

    async def test_a_bulk_reprocess_leaves_it_alone(self, share, client):
        """The exact loop from the logs: reprocess-all handed it four fresh attempts."""
        await Scanner(footage_dir=share).scan(trigger="test")

        response = await client.post("/api/reprocess", json={"stages": ["everything"]})
        assert response.status_code == 200
        assert response.json()["queued"] == 0, (
            "bulk reprocessing queued a zero-byte file or stole a never-processed file "
            "from the higher-priority new-footage queue"
        )

    async def test_a_file_that_later_gains_content_is_picked_up(self, share):
        import os

        await Scanner(footage_dir=share).scan(trigger="test")
        assert await _state("20260804111550_camera_1.ts") is RecordingState.INVALID

        empty = share / "20260804111550_camera_1.ts"
        empty.write_bytes(b"\x47" * (188 * 100))
        old = time.time() - 3600
        os.utime(empty, (old, old))

        # First scan after the change sees a moved stat and holds it for one observation;
        # the next finds it stable and takes it.
        await Scanner(footage_dir=share).scan(trigger="test")
        await Scanner(footage_dir=share).scan(trigger="test")
        assert await _state("20260804111550_camera_1.ts") is RecordingState.DISCOVERED


class TestNewFootageKeepsPriority:
    async def test_an_old_bulk_job_is_repaired(self, db_session):
        recording = Recording(
            rel_path="new.ts",
            filename="new.ts",
            size_bytes=1024,
            fingerprint="stable",
            state=RecordingState.QUEUED,
            processed_at=None,
        )
        db_session.add(recording)
        await db_session.flush()
        job = ProcessingJob(
            recording_id=recording.id,
            kind=JobKind.REPROCESS,
            stages=["everything"],
            state=JobState.QUEUED,
            priority=200,
        )
        db_session.add(job)
        await db_session.flush()

        assert await queue_unprocessed(db_session) == 0
        await db_session.refresh(job)
        assert job.kind is JobKind.PROCESS
        assert job.stages is None
        assert job.priority == 100


class TestTheTwoKindsOfInvalid:
    """The scanner and the pipeline both condemn files, and only one may be reconsidered.

    The scanner's verdict is about the bytes on disk, so a later scan is entitled to
    overturn it -- an empty segment the camera came back and filled in is a real case. The
    pipeline's verdict is about the content it decoded, and those bytes have not changed,
    so re-examining them every scan would requeue the recording forever and watch it fail
    the same way each time. What separates them is the fingerprint: the scanner withholds
    it, the pipeline leaves it in place.
    """

    async def test_a_file_the_pipeline_condemned_is_not_resurrected(self, share):
        from sqlalchemy import select

        await Scanner(footage_dir=share).scan(trigger="test")

        # Stand in for the metadata stage failing permanently: a file with bytes in it,
        # a fingerprint, and no video stream.
        async with session_scope() as session:
            row = (
                await session.execute(
                    select(Recording).where(Recording.filename == "20260804111550_camera_0.ts")
                )
            ).scalar_one()
            row.state = RecordingState.INVALID
            row.error_message = "contains no video stream"

        await Scanner(footage_dir=share).scan(trigger="test")

        assert await _state("20260804111550_camera_0.ts") is RecordingState.INVALID, (
            "a file the pipeline found unusable was reset by the next scan; it will be "
            "requeued and fail again on every scan for the life of the index"
        )
        async with session_scope() as session:
            assert await queue_unprocessed(session) == 0

    async def test_a_recording_that_failed_because_the_share_vanished_recovers(self, share):
        """The opposite mistake: an absent file is very often an absent share, and the
        recording is fine. Nothing else notices, because the bytes never changed."""
        from sqlalchemy import select

        await Scanner(footage_dir=share).scan(trigger="test")
        async with session_scope() as session:
            row = (
                await session.execute(
                    select(Recording).where(Recording.filename == "20260804111550_camera_0.ts")
                )
            ).scalar_one()
            row.state = RecordingState.FAILED
            row.file_missing = True
            row.error_message = "is no longer on disk"

        await Scanner(footage_dir=share).scan(trigger="test")
        assert await _state("20260804111550_camera_0.ts") is RecordingState.DISCOVERED


class TestAFileHeldBackByTheSettleWindow:
    async def test_it_is_processed_once_it_stops_moving(self, db_session, app_config):
        """The regression this class exists for.

        A file first seen inside the settle window is stored with no fingerprint, which is
        the marker for "never read". If it is then never touched again its size and mtime
        match on every later scan -- and the cheap-path early return fired before anything
        noticed the fingerprint was still missing. The recording sat in the index forever,
        never queued, with no error and nothing in the UI to suggest anything was wrong.
        """
        root = app_config.footage_dir
        clip = root / "20260804120000_camera_0.ts"
        clip.write_bytes(b"\x47" * (188 * 50))

        first = await Scanner(footage_dir=root).scan(trigger="test")
        assert first.unsettled == 1
        assert await _state(clip.name) is RecordingState.SETTLING

        async with session_scope() as session:
            assert await queue_unprocessed(session) == 0, "a file still being written was queued"

        # Time passes and the file is never touched again, so its stat does not move --
        # which is precisely the case the early return used to swallow. Shortening the
        # window rather than back-dating the file keeps that true: rewriting the mtime
        # would look like the camera had written to it again.
        from app.core.settings_service import get_settings_service

        await get_settings_service().set("scanner.settle_seconds", 0)

        await Scanner(footage_dir=root).scan(trigger="test")
        assert await _state(clip.name) is RecordingState.DISCOVERED

        async with session_scope() as session:
            assert await queue_unprocessed(session) == 1, (
                "a file that left the settle window was never processed at all"
            )
