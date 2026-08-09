"""Writes must persist, and timestamps must carry their zone.

Two faults that both looked like success.

*Nothing was saved.* The FastAPI session dependency never committed, so every write route
flushed inside the request and rolled back when the session closed. Queueing a reprocess,
flagging a plate, editing notes, merging journeys and retrying a job all returned 200 and
changed nothing -- which is why a bulk reprocess never appeared on the dashboard and never
ran.

*Every time was wrong.* SQLite has no timezone-aware column type, so aware datetimes came
back naive and serialised with no offset. ``new Date("2026-08-08T14:21:54")`` in a browser
is parsed as *local* time, so UTC values were rendered as local and the whole UI was out by
the local offset.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

import httpx
import pytest
from httpx import ASGITransport
from sqlalchemy import select

from app.db.models import JobState, Plate, ProcessingJob, Recording, RecordingState
from app.db.session import session_scope

#: An ISO-8601 timestamp a browser will read as UTC rather than local time.
ZONED = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$")


@pytest.fixture
async def client(db_session):
    from app.main import app

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
async def recording(db_session):
    async with session_scope() as session:
        rec = Recording(
            rel_path="20260804174353_camera_0.ts",
            filename="20260804174353_camera_0.ts",
            size_bytes=2048,
            state=RecordingState.COMPLETED,
            started_at=datetime(2026, 8, 4, 8, 13, 53, tzinfo=UTC),
        )
        session.add(rec)
        await session.flush()
        return rec.id


class TestWritesPersist:
    async def test_reprocess_actually_queues_a_job(self, client, recording):
        """The reported symptom: a reprocess that showed nothing on the dashboard."""
        response = await client.post("/api/reprocess", json={"stages": ["everything"]})
        assert response.status_code == 200
        assert response.json()["queued"] >= 1

        # The request has finished, so anything it wrote must be visible to a new session.
        async with session_scope() as session:
            jobs = (await session.execute(select(ProcessingJob))).scalars().all()
        assert jobs, "the reprocess reported success but no job survived the request"
        assert any(j.state == JobState.QUEUED for j in jobs)

    async def test_queued_work_reaches_the_dashboard(self, client, recording):
        await client.post("/api/reprocess", json={"stages": ["everything"]})

        stats = (await client.get("/api/jobs/stats")).json()
        assert stats["queued"] >= 1, "queued work is invisible to the queue view"

        status = (await client.get("/api/status")).json()
        assert status["processing"]["pending"] >= 1, "the dashboard shows nothing pending"

    async def test_single_recording_reprocess_persists(self, client, recording):
        response = await client.post(
            f"/api/recordings/{recording}/reprocess", json={"stages": ["telemetry"]}
        )
        assert response.status_code == 200
        async with session_scope() as session:
            count = len((await session.execute(select(ProcessingJob))).scalars().all())
        assert count >= 1

    async def test_plate_edits_persist(self, client, db_session):
        async with session_scope() as session:
            plate = Plate(normalised_text="S123ABC", display_text="S123ABC")
            session.add(plate)
            await session.flush()
            plate_id = plate.id

        response = await client.patch(
            f"/api/plates/{plate_id}", json={"flagged": True, "notes": "worth watching"}
        )
        assert response.status_code == 200

        async with session_scope() as session:
            stored = await session.get(Plate, plate_id)
            assert stored is not None
            assert stored.flagged is True, "flagging a plate did not survive the request"
            assert stored.notes == "worth watching"

    async def test_a_failing_handler_writes_nothing(self, client, db_session):
        """The rollback half still has to work."""
        before = (await client.get("/api/jobs/stats")).json()["queued"]
        assert (await client.post("/api/recordings/999999/reprocess", json={})).status_code == 404
        after = (await client.get("/api/jobs/stats")).json()["queued"]
        assert after == before


class TestTimestampsCarryTheirZone:
    @pytest.mark.parametrize(
        ("path", "pointer"),
        [
            ("/api/recordings", ("items", "started_at")),
            ("/api/logs", ("items", "ts")),
        ],
    )
    async def test_api_timestamps_are_zoned(self, client, recording, path, pointer):
        """Without an offset the browser reads UTC as local time."""
        payload = (await client.get(path)).json()
        collection, field = pointer
        items = payload[collection]
        if not items:
            pytest.skip(f"{path} returned nothing to check")

        for item in items[:5]:
            value = item.get(field)
            if value is None:
                continue
            assert ZONED.match(value), (
                f"{path}.{field} = {value!r} has no timezone; a browser will read it as local"
            )

    async def test_stored_datetimes_come_back_aware(self, db_session, recording):
        """SQLite drops tzinfo; the column type puts it back.

        Naive values leaking out of the database is what broke journey clustering with
        "can't compare offset-naive and offset-aware datetimes".
        """
        async with session_scope() as session:
            stored = await session.get(Recording, recording)
            assert stored is not None
            assert stored.started_at is not None
            assert stored.started_at.tzinfo is not None, "tzinfo was lost in the round trip"
            assert stored.started_at.utcoffset().total_seconds() == 0

    async def test_a_naive_value_is_stored_as_utc(self, db_session):
        """Anything written naive is taken to be UTC rather than guessed at."""
        async with session_scope() as session:
            rec = Recording(
                rel_path="naive.ts",
                filename="naive.ts",
                size_bytes=1,
                started_at=datetime(2026, 8, 4, 8, 13, 53),
            )
            session.add(rec)
            await session.flush()
            rec_id = rec.id

        async with session_scope() as session:
            stored = await session.get(Recording, rec_id)
            assert stored.started_at.tzinfo is not None
            assert stored.started_at.hour == 8, "the value was shifted, not just stamped"

    async def test_stored_and_computed_datetimes_compare(self, db_session, recording):
        """The comparison that used to raise TypeError."""
        async with session_scope() as session:
            stored = await session.get(Recording, recording)
            assert stored.started_at < datetime.now(UTC)


class TestAccessLogsAreNotPersisted:
    """The HTTP access log must not reach the database.

    It is one row per request and the UI polls — the Queue page alone asks for
    /api/jobs/stats every few seconds. On a real deployment that made 135 of every 200
    stored rows access noise, and it cost twice over: the Logs page became unreadable, and
    every request turned into an INSERT competing with the workers for SQLite's single
    write lock, surfacing as "database is locked" while a worker tried to claim its next
    job.

    Dedupe cannot help, because the key includes the message and every access line carries
    a different URL and source port.
    """

    def test_access_records_are_dropped_before_the_queue(self):
        from app.core.logging import DatabaseLogSink

        sink = DatabaseLogSink(session_factory=lambda: None)
        sink.emit_event(
            {
                "logger": "uvicorn.access",
                "level": "info",
                "event": '192.168.1.7:57529 - "GET /api/jobs/stats HTTP/1.1" 200',
            }
        )
        assert len(sink._queue) == 0, "an access log line was queued for the database"

    def test_application_records_are_still_stored(self):
        from app.core.logging import DatabaseLogSink

        sink = DatabaseLogSink(session_factory=lambda: None)
        sink.emit_event(
            {"logger": "app.workers.worker", "level": "error", "event": "worker loop error"}
        )
        assert len(sink._queue) == 1, "a real application event was dropped"
