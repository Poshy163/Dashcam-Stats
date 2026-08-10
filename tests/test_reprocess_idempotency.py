"""Running a recording again must leave the database as if it had only ever run once.

Every stage deletes its own rows before writing, so duplication was never the risk. What
was left behind was the other half: rows derived from the rows that had just been deleted.

* ``tracked_objects`` are recreated by the detection stage, and ``plate_observations``
  point at them with ``ON DELETE SET NULL``. Reprocessing detection alone therefore left
  every observation pointing at nothing, still carrying the coordinates and offsets of
  tracks that no longer existed -- and the plate pages went on showing them.
* Plate rollups were refreshed only for plates the recording *still* sees, so a plate whose
  reading a reprocess corrected kept a count including the observation just deleted, and a
  plate left with no observations anywhere stayed in the catalogue forever.
* Positions copied onto sightings survive the telemetry that produced them. Clearing a
  coordinate out of ``telemetry_points`` never touched them, which is how a coordinate
  recognised as impossible carried on being drawn on the map as a detection.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from sqlalchemy import func, select

from app.db.models import (
    Journey,
    Plate,
    PlateObservation,
    Recording,
    RecordingState,
    StageState,
    TelemetryPoint,
    TrackedObject,
)
from app.pipeline.orchestrator import expand_stages, pending_stages

BASE = datetime(2026, 8, 3, 13, 5, 28, tzinfo=UTC)
LAT, LON = -34.8040, 138.6845


class TestStagesPullTheirDependantsWithThem:
    def test_reprocessing_detection_also_reruns_plates(self):
        """Plate observations are built out of tracked objects. Recreating the tracks
        without recreating the observations orphans every one of them."""
        assert "plates" in expand_stages(["detection"])

    def test_reprocessing_telemetry_reruns_both(self):
        """Every position on a track or an observation is chosen from the telemetry."""
        stages = expand_stages(["telemetry"])
        assert "detection" in stages and "plates" in stages

    def test_reprocessing_plates_alone_does_not_redo_detection(self):
        """The dependency runs one way; re-reading plates says nothing about the tracks."""
        assert "detection" not in expand_stages(["plates"])

    def test_a_stage_left_running_is_run_again(self):
        """A worker killed mid-stage leaves it RUNNING, which is neither done nor pending.
        Counting it as complete let a reclaimed recording finish with that stage's results
        missing and its state reported as completed."""
        recording = Recording(rel_path="x.ts", filename="x.ts")
        recording.metadata_state = StageState.DONE
        recording.telemetry_state = StageState.DONE
        recording.detection_state = StageState.RUNNING
        recording.plate_state = StageState.DONE
        assert "detection" in pending_stages(recording)

    def test_a_finished_recording_needs_nothing(self):
        recording = Recording(rel_path="x.ts", filename="x.ts")
        for attr in ("metadata_state", "telemetry_state", "detection_state", "plate_state"):
            setattr(recording, attr, StageState.DONE)
        assert pending_stages(recording) == ()


@pytest.fixture
async def read_twice(db_session, monkeypatch):
    """Run the plates stage over one recording twice, with a different reading each time."""
    from app.pipeline import stages

    recording = Recording(
        rel_path="twice.ts",
        filename="20260803130528_camera_0.ts",
        size_bytes=1024,
        state=RecordingState.PROCESSING,
        duration_s=60.0,
        started_at=BASE,
    )
    db_session.add(recording)
    await db_session.flush()
    db_session.add(
        TrackedObject(
            recording_id=recording.id,
            track_key=1,
            class_label="car",
            confidence_max=0.9,
            confidence_avg=0.85,
            first_seen_offset_s=1.0,
            last_seen_offset_s=3.0,
            duration_s=2.0,
            frame_count=8,
            best_frame_offset_s=2.0,
            best_bbox=[0.2, 0.2, 0.5, 0.5],
        )
    )
    await db_session.flush()

    reading = {"text": "S192DKX"}

    class FakeDetector:
        async def detect(self, image, *, min_width_px=0):
            return [((0.1, 0.6, 0.4, 0.8), 0.95)]

    class FakeOCR:
        async def read(self, image):
            return reading["text"], 0.97

    async def fake_models():
        return FakeDetector(), FakeOCR()

    async def fake_iter_frames(path, **kwargs):
        yield 0.0, np.full((480, 640, 3), 120, dtype=np.uint8)

    monkeypatch.setattr(stages, "_shared_plate_models", fake_models)
    monkeypatch.setattr(stages, "iter_frames", fake_iter_frames)
    monkeypatch.setattr(stages, "resolve_footage_path", lambda *a, **k: "twice.ts")
    return db_session, recording, reading


class TestRunningThePlatesStageAgain:
    async def test_it_does_not_duplicate_the_observation(self, read_twice):
        from app.pipeline import stages

        session, recording, _ = read_twice
        await stages.stage_plates(session, recording)
        await stages.stage_plates(session, recording)

        count = int(
            (
                await session.execute(
                    select(func.count(PlateObservation.id)).where(
                        PlateObservation.recording_id == recording.id
                    )
                )
            ).scalar()
        )
        assert count == 1

    async def test_a_corrected_reading_does_not_leave_the_old_plate_behind(self, read_twice):
        from app.pipeline import stages

        session, recording, reading = read_twice
        await stages.stage_plates(session, recording)

        reading["text"] = "S945CDX"
        await stages.stage_plates(session, recording)

        plates = list((await session.execute(select(Plate.normalised_text))).scalars())
        assert plates == ["S945CDX"], (
            f"the superseded plate survived with a count that includes a deleted "
            f"observation: {plates}"
        )

    async def test_a_plate_a_user_flagged_is_never_removed(self, read_twice):
        from app.pipeline import stages

        session, recording, reading = read_twice
        await stages.stage_plates(session, recording)
        plate = (await session.execute(select(Plate))).scalar_one()
        plate.flagged = True
        await session.flush()

        reading["text"] = "S945CDX"
        await stages.stage_plates(session, recording)

        remaining = set((await session.execute(select(Plate.normalised_text))).scalars())
        assert "S192DKX" in remaining, "a flagged plate was deleted by a reprocess"
        assert (await session.get(Plate, plate.id)).observation_count == 0

    async def test_rollups_match_reality(self, read_twice):
        from app.pipeline import stages

        session, recording, _ = read_twice
        await stages.stage_plates(session, recording)
        await stages.stage_plates(session, recording)

        plate = (await session.execute(select(Plate))).scalar_one()
        actual = int(
            (
                await session.execute(
                    select(func.count(PlateObservation.id)).where(
                        PlateObservation.plate_id == plate.id
                    )
                )
            ).scalar()
        )
        assert plate.observation_count == actual == 1


class TestSightingPositionsAreCleanedWithTheirTelemetry:
    async def test_an_impossible_coordinate_stops_being_a_map_pin(self, db_session):
        """The ``-34.8040, -8.6845`` case, one layer down from where it was first fixed.

        Migration 0004 cleaned ``telemetry_points`` and stopped there; the copies on the
        sighting rows were never examined, so the dot stayed on the map.
        """
        from app.journeys.builder import JourneyBuilder

        journey = Journey(started_at=BASE, ended_at=BASE + timedelta(seconds=120))
        db_session.add(journey)
        await db_session.flush()

        recording = Recording(
            rel_path="stale.ts",
            filename="20260803130528_camera_0.ts",
            journey_id=journey.id,
            started_at=BASE,
            ended_at=BASE + timedelta(seconds=120),
            duration_s=120.0,
            state=RecordingState.COMPLETED,
        )
        db_session.add(recording)
        await db_session.flush()

        for index in range(20):
            db_session.add(
                TelemetryPoint(
                    recording_id=recording.id,
                    journey_id=journey.id,
                    t_offset_s=float(index),
                    captured_at=BASE + timedelta(seconds=index),
                    lat=LAT + index * 0.0001,
                    lon=LON + index * 0.0001,
                    has_fix=True,
                    speed_kmh=40.0,
                )
            )

        good = TrackedObject(
            recording_id=recording.id,
            journey_id=journey.id,
            track_key=1,
            class_label="car",
            confidence_max=0.85,
            confidence_avg=0.8,
            first_seen_offset_s=1.0,
            last_seen_offset_s=2.0,
            duration_s=1.0,
            frame_count=4,
            lat=LAT,
            lon=LON,
        )
        # Stamped from a telemetry point that has since been recognised as a misread.
        stale = TrackedObject(
            recording_id=recording.id,
            journey_id=journey.id,
            track_key=2,
            class_label="car",
            confidence_max=0.85,
            confidence_avg=0.8,
            first_seen_offset_s=5.0,
            last_seen_offset_s=6.0,
            duration_s=1.0,
            frame_count=4,
            lat=LAT,
            lon=-8.6845,
        )
        db_session.add_all([good, stale])
        await db_session.flush()

        plate = Plate(normalised_text="S192DKX", display_text="S192DKX")
        db_session.add(plate)
        await db_session.flush()
        db_session.add(
            PlateObservation(
                plate_id=plate.id,
                recording_id=recording.id,
                journey_id=journey.id,
                tracked_object_id=stale.id,
                t_offset_s=5.0,
                captured_at=BASE + timedelta(seconds=5),
                raw_text="S192DKX",
                normalised_text="S192DKX",
                ocr_confidence=0.97,
                detection_confidence=0.95,
                lat=LAT,
                lon=-8.6845,
            )
        )
        await db_session.flush()

        await JourneyBuilder().refresh(db_session, journey)

        # Read the columns rather than the mapped objects. The clearing statement runs
        # with `synchronize_session=False` -- deliberately, as `_attach` explains -- so the
        # identity map still holds what the rows used to say, and asserting against it
        # would test the ORM's memory rather than the database.
        async def position(table, row_id):
            return (
                await db_session.execute(select(table.lat, table.lon).where(table.id == row_id))
            ).one()

        assert await position(TrackedObject, stale.id) == (None, None)
        assert (await position(TrackedObject, good.id))[1] == pytest.approx(LON), (
            "a sighting in the right place was cleared along with the wrong one"
        )

        observation_id = (await db_session.execute(select(PlateObservation.id))).scalar_one()
        assert (await position(PlateObservation, observation_id))[1] is None, (
            "the plate sighting kept a longitude 11,000 km from the drive it belongs to"
        )
        # The sighting itself survives: only the claim about where it was is withdrawn.
        row = (
            await db_session.execute(
                select(PlateObservation.normalised_text, PlateObservation.ocr_confidence).where(
                    PlateObservation.id == observation_id
                )
            )
        ).one()
        assert row.normalised_text == "S192DKX"
        assert row.ocr_confidence == pytest.approx(0.97)

    async def test_journey_bounds_exclude_what_was_rejected(self, db_session):
        from app.journeys.builder import JourneyBuilder

        journey = Journey(started_at=BASE, ended_at=BASE + timedelta(seconds=120))
        db_session.add(journey)
        await db_session.flush()
        recording = Recording(
            rel_path="bounds.ts",
            filename="20260803130528_camera_0.ts",
            journey_id=journey.id,
            started_at=BASE,
            ended_at=BASE + timedelta(seconds=120),
            duration_s=120.0,
        )
        db_session.add(recording)
        await db_session.flush()

        for index in range(20):
            db_session.add(
                TelemetryPoint(
                    recording_id=recording.id,
                    t_offset_s=float(index),
                    captured_at=BASE + timedelta(seconds=index),
                    lat=LAT + index * 0.0001,
                    lon=LON + index * 0.0001,
                    has_fix=True,
                )
            )
        db_session.add(
            TelemetryPoint(
                recording_id=recording.id,
                t_offset_s=25.0,
                captured_at=BASE + timedelta(seconds=25),
                lat=LAT,
                lon=-8.6845,
                has_fix=True,
            )
        )
        await db_session.flush()

        await JourneyBuilder().refresh(db_session, journey)
        assert journey.min_lon == pytest.approx(LON, abs=0.01), (
            "one misread longitude dragged the journey's bounds across the planet, which "
            "opens the map zoomed out to most of it"
        )
