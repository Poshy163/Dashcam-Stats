"""Journeys must settle, and a stale coordinate must not decide where they split.

All four cases here were found by reading a live server rather than the code, and they
compound into each other:

* ``recordings.start_lat`` is written once, from the recording's first fix, and nothing
  ever revisits it. When that first fix is a misread the value is frozen -- and clustering
  reads it, so a rear segment whose latitude lost its minus sign sits 7,700 km from the
  front segment recorded at the same instant and the drive is split in half.
* ``needs_recluster`` asked a *time-only* question while ``_cluster`` splits on time **and**
  GPS continuity, so a pair the rebuild had deliberately separated looked, to the test,
  like a pair that should be joined. The rebuild therefore ran on every single scan: 12,626
  log entries, 85 journeys where clustering says 44, and constant write-lock pressure that
  is where the workers' "database is locked" failures came from.
* Rebuilding deletes every automatic journey while workers are mid-recording, so a worker's
  in-memory ``recording.journey_id`` names a journey that no longer exists and its inserts
  fail on the foreign key.
* And a stage that dies inside a flush leaves the session refusing everything, so the code
  that records the failure is itself discarded.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.db.models import Journey, Recording, RecordingState, TelemetryPoint, TrackedObject
from app.journeys.builder import JourneyBuilder

BASE = datetime(2026, 7, 30, 1, 58, 47, tzinfo=UTC)
LAT, LON = -34.7903, 138.6952


def clip(session, index: int, camera: str, start: datetime, *, seconds: float = 120.0):
    rec = Recording(
        rel_path=f"{index}_{camera}.ts",
        filename=f"2026073011{index:02d}_{camera}.ts",
        size_bytes=1024,
        started_at=start,
        ended_at=start + timedelta(seconds=seconds),
        duration_s=seconds,
        state=RecordingState.COMPLETED,
    )
    session.add(rec)
    return rec


class TestAFrozenStartPositionSplitsADrive:
    async def test_a_sign_flipped_start_does_not_survive_a_refresh(self, db_session):
        """Recording 23 on the live library: its 230 sightings and its stored start
        position all carry ``+34.79`` where the drive is at ``-34.79``."""
        journey = Journey(started_at=BASE, ended_at=BASE + timedelta(seconds=120))
        db_session.add(journey)
        await db_session.flush()

        rec = clip(db_session, 23, "camera_1", BASE)
        rec.journey_id = journey.id
        rec.start_lat, rec.start_lon = 34.7921, LON  # the frozen misread
        await db_session.flush()

        for i in range(10):
            db_session.add(
                TelemetryPoint(
                    recording_id=rec.id,
                    t_offset_s=float(i),
                    captured_at=BASE + timedelta(seconds=i),
                    lat=LAT + i * 0.0001,
                    lon=LON,
                    has_fix=True,
                )
            )
        await db_session.flush()

        await JourneyBuilder().refresh(db_session, journey)

        stored = (
            await db_session.execute(
                select(Recording.start_lat, Recording.start_lon).where(Recording.id == rec.id)
            )
        ).one()
        assert stored.start_lat == pytest.approx(LAT, abs=1e-4), (
            "the recording kept a start position 7,700 km from its own telemetry, which is "
            "what clustering reads when it decides whether to split the drive"
        )

    async def test_a_recording_with_no_surviving_fix_has_no_start_position(self, db_session):
        journey = Journey(started_at=BASE, ended_at=BASE + timedelta(seconds=120))
        db_session.add(journey)
        await db_session.flush()
        rec = clip(db_session, 24, "camera_1", BASE)
        rec.journey_id = journey.id
        rec.start_lat, rec.start_lon = 34.7921, LON
        await db_session.flush()

        await JourneyBuilder().refresh(db_session, journey)
        stored = (
            await db_session.execute(select(Recording.start_lat).where(Recording.id == rec.id))
        ).scalar_one()
        assert stored is None, "a stale position outlived the telemetry that justified it"


class TestTheRebuildSettles:
    async def _library(self, session, *, split_on_gps: bool = False):
        """Three front/rear pairs, each overlapping the next in time.

        With ``split_on_gps`` the middle pair is genuinely 55 km away -- backed by its own
        telemetry, so it survives the start-position recompute and clustering separates it
        for a real reason. That leaves journeys *seconds* apart in time that a rebuild will
        never join, which is exactly the arrangement a time-only staleness test can never be
        satisfied by.
        """
        recs = []
        for i in range(3):
            start = BASE + timedelta(seconds=120 * i)
            lat = LAT + (0.5 if split_on_gps and i == 1 else 0.0)
            for camera, offset in (("camera_0", 0), ("camera_1", 8)):
                rec = clip(
                    session, 2 * i + (offset and 1), camera, start + timedelta(seconds=offset)
                )
                await session.flush()
                for n in range(3):
                    session.add(
                        TelemetryPoint(
                            recording_id=rec.id,
                            t_offset_s=float(n),
                            captured_at=start + timedelta(seconds=offset + n),
                            lat=lat,
                            lon=LON,
                            has_fix=True,
                        )
                    )
                recs.append(rec)
        await session.flush()
        return recs

    async def test_a_rebuild_leaves_nothing_to_recluster(self, db_session):
        """The property the previous test could not have: after a rebuild, the thing that
        triggers rebuilds must be satisfied.

        Built so the answer is *not* trivially true: clustering splits this library on GPS
        continuity, so several journeys end up seconds apart in time. A staleness test that
        only looks at time calls that stale, rebuilds, gets the same split, and does it all
        again on the next scan -- 12,626 log entries' worth on the live server.
        """
        builder = JourneyBuilder()
        await self._library(db_session, split_on_gps=True)

        assert await builder.needs_recluster(db_session) is True, "nothing was clustered yet"
        await builder.rebuild(db_session)

        journeys = list((await db_session.execute(select(Journey))).scalars())
        overlapping = sum(
            1
            for a in journeys
            for b in journeys
            if a.id != b.id and abs((a.ended_at - b.started_at).total_seconds()) < 300
        )
        assert overlapping, (
            "this library was meant to end up with journeys close together in time; "
            "without that the assertion below would pass for the wrong reason"
        )
        assert await builder.needs_recluster(db_session) is False, (
            "a rebuild left the library still looking stale, so the scheduler will rebuild "
            "again on the next scan, and the next, holding the write lock every time"
        )

    async def test_it_is_still_idempotent_a_second_time(self, db_session):
        builder = JourneyBuilder()
        await self._library(db_session)
        await builder.rebuild(db_session)
        first = list((await db_session.execute(select(Journey.id))).scalars())
        await builder.rebuild(db_session)
        assert await builder.needs_recluster(db_session) is False
        second = list((await db_session.execute(select(Journey.id))).scalars())
        assert len(first) == len(second) == 1, "one drive should be one journey"

    async def test_a_genuine_fragment_is_still_noticed(self, db_session):
        """The check must not become blind: a drive split across two journeys is exactly
        what it exists to catch."""
        builder = JourneyBuilder()
        recs = await self._library(db_session)
        await builder.rebuild(db_session)

        # Tear one recording out into a journey of its own, as assign_recording would.
        stray = Journey(started_at=recs[0].started_at, ended_at=recs[0].ended_at)
        db_session.add(stray)
        await db_session.flush()
        recs[0].journey_id = stray.id
        await db_session.flush()

        assert await builder.needs_recluster(db_session) is True


class TestNoStageStampsAJourneyItCannotSee:
    def test_the_stages_never_write_journey_id(self):
        """A rebuild deletes journeys while a worker is mid-recording, so the worker's
        in-memory value names a journey that may already be gone -- and the insert fails
        on the foreign key, costing a full reprocess."""
        import inspect

        from app.pipeline import stages

        for fn in (stages.stage_telemetry, stages.stage_detect, stages.stage_plates):
            source = inspect.getsource(fn)
            assert "journey_id=recording.journey_id" not in source, (
                f"{fn.__name__} stamps journey_id from an in-memory value that a "
                "concurrent rebuild may already have invalidated"
            )
            assert '"journey_id": recording.journey_id' not in source

    async def test_summarise_still_backfills_it(self, db_session):
        """Leaving it null at insert time is only safe because one writer owns it."""
        from app.pipeline.stages import stage_summarise

        rec = clip(db_session, 30, "camera_0", BASE)
        await db_session.flush()
        db_session.add(
            TrackedObject(
                recording_id=rec.id,
                track_key=1,
                class_label="car",
                confidence_max=0.9,
                confidence_avg=0.8,
                first_seen_offset_s=1.0,
                last_seen_offset_s=2.0,
                duration_s=1.0,
                frame_count=4,
            )
        )
        db_session.add(
            TelemetryPoint(
                recording_id=rec.id,
                t_offset_s=0.0,
                captured_at=BASE,
                lat=LAT,
                lon=LON,
                has_fix=True,
            )
        )
        await db_session.flush()

        await stage_summarise(db_session, rec)
        await db_session.flush()

        journey_id = (
            await db_session.execute(select(Recording.journey_id).where(Recording.id == rec.id))
        ).scalar_one()
        assert journey_id is not None
        track_journey = (
            await db_session.execute(
                select(TrackedObject.journey_id).where(TrackedObject.recording_id == rec.id)
            )
        ).scalar_one()
        assert track_journey == journey_id, "the derived pointer was never backfilled"


class TestAFailedStageStillRecordsItsFailure:
    async def test_a_poisoned_session_does_not_swallow_the_outcome(self, db_session):
        """A stage whose statement fails leaves the session refusing everything until it is
        rolled back -- so ``recording.state = FAILED`` was discarded, the commit after it
        raised, and the job was never marked failed at all. It stayed RUNNING with no
        worker and was reclaimed five minutes later to fail the same way.

        The live trigger was a foreign key violation inside a flush. A failing statement is
        used here instead: it leaves the session in the same unusable state, deterministically
        and without depending on how a particular driver unwinds a failed flush.
        """
        from sqlalchemy import text

        from app.db.session import session_scope
        from app.pipeline import orchestrator

        rec = clip(db_session, 40, "camera_0", BASE)
        rec.state = RecordingState.QUEUED
        await db_session.flush()
        await db_session.commit()
        recording_id = rec.id

        async def poisons_the_session(session, recording, *, progress=None):
            # Dirty the recording first: this is the state that used to be discarded.
            recording.vehicle_count = 99
            await session.execute(text("SELECT * FROM a_table_that_does_not_exist"))

        async def noop(session, recording, *, progress=None):
            from app.pipeline.orchestrator import StageResult

            return StageResult("summarise", True)

        original = dict(orchestrator.STAGES)
        try:
            orchestrator.STAGES["detection"] = poisons_the_session
            orchestrator.STAGES["summarise"] = noop
            async with session_scope() as session:
                recording = await session.get(Recording, recording_id)
                report = await orchestrator.run_stages(session, recording, ["detection"])
        except Exception as exc:
            pytest.fail(f"the failure escaped run_stages instead of being recorded: {exc!r}")
        finally:
            orchestrator.STAGES.clear()
            orchestrator.STAGES.update(original)

        assert report.ok is False and report.error

        async with session_scope() as session:
            stored = await session.get(Recording, recording_id)
        assert stored.state is RecordingState.FAILED, (
            "the recording's failure was written on a session that had already been "
            "invalidated, so it was silently discarded"
        )
        assert stored.error_message
