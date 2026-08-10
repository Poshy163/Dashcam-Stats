"""Surviving contention for SQLite's single write lock.

Two recordings in the real library were marked failed 4/4 with

    OperationalError: (sqlite3.OperationalError) database is locked
    [SQL: DELETE FROM tracked_objects WHERE tracked_objects.recording_id = ?]
    [SQL: DELETE FROM telemetry_points WHERE telemetry_points.recording_id = ?]

Both of those statements are the *first write* of a stage, i.e. the moment its transaction
opens. ``busy_timeout`` is thirty seconds, so something was holding the lock for longer
than that -- and the only thing in the process that can is a journey rebuild, which
refreshed forty-five journeys inside one transaction while reading every telemetry point
of each. It took the lock with its first ``UPDATE`` and then spent minutes reading.

So there are two things to hold in place, and this file covers both:

* ``refresh`` must do its reading before its first write, so the lock is held for the
  length of a write rather than the length of a pass.
* A writer that loses anyway must be retried rather than turned into a failed recording.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError

from app.db.models import Journey, Recording, TelemetryPoint
from app.db.retry import commit_with_retry, is_locked_error, retry_on_locked
from app.db.session import session_scope

BASE = datetime(2026, 8, 3, 13, 5, 28, tzinfo=UTC)
LAT, LON = -34.8040, 138.6845


class TestRecognisingContention:
    @pytest.mark.parametrize(
        "message",
        [
            "(sqlite3.OperationalError) database is locked",
            "database table is locked: telemetry_points",
            "SQLITE_BUSY",
        ],
    )
    def test_a_lock_is_recognised_however_it_is_worded(self, message):
        assert is_locked_error(RuntimeError(message))

    def test_it_is_found_through_the_wrapping(self):
        """SQLAlchemy wraps the DBAPI error, and the caller often wraps that again."""
        inner = RuntimeError("(sqlite3.OperationalError) database is locked")
        try:
            try:
                raise inner
            except RuntimeError as exc:
                raise ValueError("stage failed") from exc
        except ValueError as outer:
            assert is_locked_error(outer)

    def test_a_real_defect_is_not_mistaken_for_contention(self):
        """The distinction decides whether an attempt is spent, so a false positive here
        would give a genuinely broken recording unlimited free retries."""
        assert not is_locked_error(ValueError("no such column: telemetry_points.lat"))
        assert not is_locked_error(None)


class TestRetrying:
    async def test_it_gives_up_the_lock_and_tries_again(self):
        attempts = {"count": 0}

        async def flaky():
            attempts["count"] += 1
            if attempts["count"] < 3:
                raise RuntimeError("(sqlite3.OperationalError) database is locked")
            return "written"

        result = await retry_on_locked(flaky, what="test", base_delay_s=0.001)
        assert result == "written"
        assert attempts["count"] == 3

    async def test_a_real_error_is_raised_immediately(self):
        attempts = {"count": 0}

        async def broken():
            attempts["count"] += 1
            raise ValueError("that column does not exist")

        with pytest.raises(ValueError):
            await retry_on_locked(broken, what="test", base_delay_s=0.001)
        assert attempts["count"] == 1, "a genuine error was retried, hiding it for seconds"

    async def test_the_last_failure_still_propagates(self):
        """Swallowing it would be worse than the lock: a write that silently did not
        happen is the failure this codebase is most prone to."""

        async def always_locked():
            raise RuntimeError("database is locked")

        with pytest.raises(RuntimeError, match="locked"):
            await retry_on_locked(always_locked, what="test", attempts=2, base_delay_s=0.001)

    async def test_a_commit_never_discards_what_it_was_asked_to_persist(
        self, db_session, monkeypatch
    ):
        """The trap in retrying a commit, and why this one does not roll back.

        Rolling back between attempts and then committing again is the obvious shape and
        it is catastrophic: the rollback throws away the work, the retry commits an empty
        transaction, and the stage reports success having written nothing. That is the
        exact class of defect this codebase has been bitten by most often, and it would be
        strictly worse than the lock it was working around.
        """
        calls = {"count": 0}
        real_commit = type(db_session).commit

        async def flaky_commit(self):
            calls["count"] += 1
            if calls["count"] == 1:
                raise OperationalError("DELETE", {}, Exception("database is locked"))
            return await real_commit(self)

        monkeypatch.setattr(type(db_session), "commit", flaky_commit)
        db_session.add(Recording(rel_path="retried.ts", filename="retried.ts"))
        await commit_with_retry(db_session, what="test")
        monkeypatch.undo()

        async with session_scope() as session:
            found = (
                await session.execute(
                    select(Recording.id).where(Recording.filename == "retried.ts")
                )
            ).scalar_one_or_none()
        assert found is not None, (
            "the retried commit wrote nothing: the pending work was rolled back and an "
            "empty transaction was committed in its place, reporting success"
        )

    async def test_a_retried_write_phase_produces_the_same_rows(self, db_session, monkeypatch):
        """``write_with_retry`` is the form that *may* roll back, because it puts the work
        back afterwards -- a delete followed by inserts from data already in memory."""
        from app.db.retry import write_with_retry

        calls = {"count": 0}
        real_commit = type(db_session).commit

        async def flaky_commit(self):
            calls["count"] += 1
            if calls["count"] == 1:
                raise OperationalError("DELETE", {}, Exception("database is locked"))
            return await real_commit(self)

        async def write() -> None:
            db_session.add(Recording(rel_path="phase.ts", filename="phase.ts"))
            await db_session.flush()

        monkeypatch.setattr(type(db_session), "commit", flaky_commit)
        await write_with_retry(db_session, write, what="test")
        monkeypatch.undo()

        async with session_scope() as session:
            rows = list(
                (
                    await session.execute(
                        select(Recording.id).where(Recording.filename == "phase.ts")
                    )
                ).scalars()
            )
        assert len(rows) == 1, f"the retried write phase wrote {len(rows)} rows, not one"


class TestTheJourneyRefreshDoesNotHoldTheLockAcrossItsReads:
    """The root cause of the two failures quoted at the top of this file.

    Asserted on the *shape* of what ``refresh`` issues rather than on timing. A race needs
    contention to reproduce, and a test machine with a fast local disk does not supply it --
    the same reason the concurrency suite next door leans on a structural assertion. What
    can be pinned exactly is the order: no write may be issued before the last read, because
    the write is what opens the transaction that then holds the lock.
    """

    async def test_every_read_happens_before_the_first_write(self, db_session):
        from app.journeys.builder import JourneyBuilder

        journey = Journey(started_at=BASE, ended_at=BASE + timedelta(seconds=120))
        db_session.add(journey)
        await db_session.flush()
        recording = Recording(
            rel_path="lockorder.ts",
            filename="20260803130528_camera_0.ts",
            journey_id=journey.id,
            started_at=BASE,
            ended_at=BASE + timedelta(seconds=120),
            duration_s=120.0,
        )
        db_session.add(recording)
        await db_session.flush()
        for index in range(30):
            db_session.add(
                TelemetryPoint(
                    recording_id=recording.id,
                    t_offset_s=float(index),
                    captured_at=BASE + timedelta(seconds=index),
                    lat=LAT + index * 0.0001,
                    lon=LON + index * 0.0001,
                    has_fix=True,
                    speed_kmh=40.0,
                )
            )
        await db_session.flush()

        issued: list[str] = []
        real_execute = db_session.execute

        async def recording_execute(statement, *args, **kwargs):
            issued.append(type(statement).__name__)
            return await real_execute(statement, *args, **kwargs)

        db_session.execute = recording_execute  # type: ignore[method-assign]
        try:
            await JourneyBuilder().refresh(db_session, journey)
        finally:
            db_session.execute = real_execute  # type: ignore[method-assign]

        writes = [i for i, kind in enumerate(issued) if kind in ("Update", "Delete")]
        reads = [i for i, kind in enumerate(issued) if kind == "Select"]
        assert reads and writes, f"refresh issued nothing recognisable: {issued}"
        assert max(reads) < min(writes), (
            "a read is issued after the first write, so the write transaction -- and "
            f"SQLite's single write lock -- is held across it: {issued}"
        )

    async def test_a_rebuild_commits_between_journeys(self, db_session):
        """Forty-five journeys refreshed inside one transaction is minutes of held lock.
        Committing per journey also means an interrupted rebuild keeps what it finished."""
        import inspect

        from app.journeys.builder import JourneyBuilder

        source = inspect.getsource(JourneyBuilder.rebuild)
        assert "commit_with_retry" in source, (
            "the rebuild loop no longer commits per journey; it will hold the write lock "
            "for the whole pass and every other writer will fail on its busy timeout"
        )


class TestConcurrentWritersUnderAJourneyRebuild:
    async def test_a_worker_can_still_write_while_journeys_rebuild(self, db_session):
        """End to end, at whatever contention this machine can produce: the two things
        that collided in production must both complete."""
        from app.journeys.builder import JourneyBuilder

        for index in range(12):
            db_session.add(
                Recording(
                    rel_path=f"rb_{index}.ts",
                    filename=f"2026080313{index:02d}00_camera_0.ts",
                    size_bytes=1024,
                    started_at=BASE + timedelta(minutes=index * 30),
                    ended_at=BASE + timedelta(minutes=index * 30, seconds=120),
                    duration_s=120.0,
                )
            )
        await db_session.flush()
        await db_session.commit()

        errors: list[Exception] = []

        async def rebuild() -> None:
            try:
                async with session_scope() as session:
                    await JourneyBuilder().rebuild(session)
            except Exception as exc:  # pragma: no cover - the assertion below reports it
                errors.append(exc)

        async def worker() -> None:
            try:
                for index in range(10):
                    async with session_scope() as session:
                        await session.execute(
                            update(Recording)
                            .where(Recording.rel_path == f"rb_{index}.ts")
                            .values(vehicle_count=index)
                        )
            except Exception as exc:
                errors.append(exc)

        await asyncio.gather(rebuild(), worker(), worker())
        assert not errors, f"a writer was locked out during a journey rebuild: {errors[0]!r}"
