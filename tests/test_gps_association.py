"""Which GPS reading belongs to a detection, and when the honest answer is none.

Three separate defects put sightings in the wrong place, and all three produced a
confident coordinate rather than an error:

* ``ORDER BY abs(t_offset_s - ?) LIMIT 1`` has no tolerance, so a recording whose lock
  died ten seconds in still had a "nearest" fix for a detection two minutes later.
* Positions were copied off the track, which holds the coordinate of the frame the vehicle
  was *first* seen in -- up to twenty seconds and several hundred metres from the frame
  its plate was actually read in.
* ``_derive``'s anchor was the first fix, unchecked. When that one was the misread, every
  good fix after it was rejected for disagreeing with it and the recording kept the single
  wrong position -- the shape of the ``-34.8040, -8.6845`` sighting.

The module under test is deliberately free of the database, the decoder and the queue, so
each of those is reproducible from a list of numbers.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest

from app.osd.locate import FixSample, FixTrack

BASE = datetime(2026, 8, 3, 13, 5, 28, tzinfo=UTC)
LAT, LON = -34.8040, 138.6845


def fix(offset: float, lat: float | None = None, lon: float | None = None) -> FixSample:
    """One overlay sample, moving roughly north-east at about 40 km/h."""
    return FixSample(
        t_offset_s=offset,
        lat=LAT + offset * 0.0001 if lat is None else lat,
        lon=LON + offset * 0.0001 if lon is None else lon,
        captured_at=BASE + timedelta(seconds=offset),
    )


def track(*samples: FixSample, **kwargs) -> FixTrack:
    return FixTrack(list(samples), **kwargs)


class TestChoosingAFix:
    def test_a_detection_on_a_sample_takes_it(self):
        located = track(fix(0), fix(1), fix(2)).at(1.0)
        assert located is not None
        assert located.source == "exact"
        assert located.lat == pytest.approx(LAT + 0.0001)

    def test_a_detection_between_two_samples_is_interpolated(self):
        located = track(fix(10), fix(11)).at(10.5)
        assert located is not None
        assert located.source == "interpolated"
        # Exactly halfway, not one end or the other.
        assert located.lat == pytest.approx(LAT + 0.00105, abs=1e-9)
        assert located.captured_at == BASE + timedelta(seconds=10.5)

    def test_interpolation_reports_the_gap_it_crossed(self):
        located = track(fix(0), fix(6)).at(3.0)
        assert located is not None
        assert located.span_s == pytest.approx(6.0)
        assert located.dt_s == pytest.approx(3.0)

    def test_a_detection_just_outside_the_track_uses_the_nearest_fix(self):
        located = track(fix(0), fix(1)).at(2.5)
        assert located is not None
        assert located.source == "nearest"
        assert located.dt_s == pytest.approx(1.5)


class TestRefusingToGuess:
    def test_a_detection_far_from_any_fix_has_no_position(self):
        """The stale-fix bug. GPS locked for the first two seconds of a two-minute clip."""
        located = track(fix(0), fix(1), fix(2)).at(110.0)
        assert located is None, (
            "a detection 108 seconds after the last fix was given that fix as its location"
        )

    def test_a_dropout_wider_than_the_bracket_is_not_interpolated_across(self):
        """Losing lock mid-recording must leave a hole, not a straight line through it."""
        located = track(fix(0), fix(60), max_bracket_s=15.0).at(30.0)
        assert located is None

    def test_a_short_dropout_is_interpolated(self):
        located = track(fix(0), fix(10), max_bracket_s=15.0).at(5.0)
        assert located is not None and located.source == "interpolated"

    def test_a_recording_with_no_fixes_locates_nothing(self):
        assert track().at(0.0) is None

    def test_two_fixes_that_contradict_each_other_are_not_averaged(self):
        """One of the pair is a misread. Halfway between a real place and a wrong one is
        a third wrong place, and nothing downstream could tell it was invented."""
        contradictory = track(
            fix(0),
            FixSample(
                t_offset_s=1.0, lat=LAT, lon=13.8845, captured_at=BASE + timedelta(seconds=1)
            ),
            max_fix_age_s=0.0,
        )
        assert contradictory.at(0.5) is None

    @pytest.mark.parametrize(
        "lat,lon",
        [
            (float("nan"), LON),
            (LAT, float("inf")),
            (95.0, LON),
            (LAT, 200.0),
            (0.0, 0.0),
            (0.0001, 0.0009),
        ],
    )
    def test_a_coordinate_that_is_not_a_place_is_never_offered(self, lat, lon):
        """Including the two nobody checked for: NaN passes every magnitude comparison
        ever written, and the camera's 00.0000 no-fix marker is 600 km off Africa."""
        only_bad = track(FixSample(t_offset_s=0.0, lat=lat, lon=lon, captured_at=BASE))
        assert len(only_bad) == 0
        assert only_bad.at(0.0) is None

    def test_tolerance_is_configurable_and_respected(self):
        loose = track(fix(0), max_fix_age_s=30.0)
        tight = track(fix(0), max_fix_age_s=1.0)
        assert loose.at(20.0) is not None
        assert tight.at(20.0) is None


class TestTheTrace:
    def test_every_answer_carries_how_it_was_reached(self):
        """A suspicious coordinate has to be traceable to the decision that produced it,
        not guessed at from the value alone."""
        located = track(fix(0), fix(2)).at(1.0)
        assert located is not None
        logged = located.as_log()
        assert logged["gps_source"] == "interpolated"
        assert logged["gps_dt_s"] == pytest.approx(1.0)
        assert logged["gps_before_offset_s"] == 0.0
        assert logged["gps_after_offset_s"] == 2.0
        assert math.isclose(logged["gps_lat"], LAT + 0.0001, abs_tol=1e-6)


class TestTheLiveLibraryCase:
    """Recording 268 of the real library, reduced to the numbers that matter.

    Measured on the running server: ``20260803130528_camera_0.ts`` holds 110 tracked
    objects and one plate observation, *all* at ``-34.8040, -8.6845``, while the recording
    itself stores zero telemetry fixes. Re-decoding its 121 frames yields 92 good readings
    of ``138.6845`` and a handful where the leading digits are lost -- at t=2 exactly
    ``-8.6845``.

    So the failure was never that a bad reading exists; the overlay on this clip is simply
    hard to read. It is that one surviving fix was stamped on every sighting in the clip
    regardless of how far away in time it was, and that clearing it from the telemetry left
    all 110 copies behind.
    """

    def test_one_surviving_fix_does_not_place_a_whole_recording(self):
        lone = track(fix(2, lat=-34.8040, lon=-8.6845), max_fix_age_s=3.0)
        # The offsets the real tracked objects sit at, spread over the whole two minutes.
        placed = [lone.at(t) for t in (0.0, 13.75, 40.0, 90.0, 119.0)]
        assert placed[0] is not None, "a detection two seconds from the fix may use it"
        assert all(p is None for p in placed[1:]), (
            "one fix was stamped on sightings up to two minutes away from it, which is how "
            "110 detections in one clip ended up sharing a single wrong coordinate"
        )

    def test_the_plate_lands_on_the_road_once_the_good_fixes_survive(self):
        """t=13.75 is where S192DKX's plate was actually read, and 138.6845 is what the
        frames either side of it decode to."""
        from app.osd.geo import haversine_m

        good = track(*(fix(t, lat=-34.8040, lon=138.6845) for t in (10.0, 12.0, 13.0, 15.0)))
        located = good.at(13.75)
        assert located is not None
        assert located.lon == pytest.approx(138.6845, abs=1e-4)
        assert located.lat == pytest.approx(-34.8040, abs=1e-4)
        assert haversine_m(-34.8040, -8.6845, located.lat, located.lon) > 11_000_000, (
            "the sighting should move roughly 11,500 km, from the South Atlantic to Adelaide"
        )


class TestAgainstTheDatabase:
    """The same rules, reached through the stage's own loader."""

    async def test_the_loader_applies_the_configured_tolerance(self, db_session):
        from app.db.models import Recording, TelemetryPoint
        from app.pipeline.stages import _fix_track

        recording = Recording(rel_path="gps.ts", filename="20260803130528_camera_0.ts")
        db_session.add(recording)
        await db_session.flush()

        for offset in (0.0, 1.0, 2.0):
            db_session.add(
                TelemetryPoint(
                    recording_id=recording.id,
                    t_offset_s=offset,
                    captured_at=BASE + timedelta(seconds=offset),
                    lat=LAT + offset * 0.0001,
                    lon=LON + offset * 0.0001,
                    has_fix=True,
                    speed_kmh=40.0,
                )
            )
        await db_session.flush()

        fixes = await _fix_track(db_session, recording.id)
        assert len(fixes) == 3
        assert fixes.at(1.5) is not None, "a detection between two fixes should be placed"
        assert fixes.at(110.0) is None, (
            "a detection long after the last fix was given a stale position"
        )

    async def test_a_sighting_is_placed_where_its_plate_was_read(self, db_session, monkeypatch):
        """The ``S192DKX`` case, end to end.

        The plate is read from the vehicle's *best* frame, 40 seconds into the clip. The
        observation used to copy the track's coordinate, which belongs to the frame the
        vehicle was *first* seen in -- 10 seconds and, at 40 km/h, a third of a kilometre
        earlier.
        """
        import numpy as np
        from sqlalchemy import select

        from app.db.models import (
            PlateObservation,
            Recording,
            RecordingState,
            TelemetryPoint,
            TrackedObject,
        )
        from app.pipeline import stages

        recording = Recording(
            rel_path="s192dkx.ts",
            filename="20260803130528_camera_0.ts",
            size_bytes=1024,
            state=RecordingState.PROCESSING,
            duration_s=120.0,
            started_at=BASE,
        )
        db_session.add(recording)
        await db_session.flush()

        for offset in range(0, 120):
            db_session.add(
                TelemetryPoint(
                    recording_id=recording.id,
                    t_offset_s=float(offset),
                    captured_at=BASE + timedelta(seconds=offset),
                    lat=LAT + offset * 0.0001,
                    lon=LON + offset * 0.0001,
                    has_fix=True,
                    speed_kmh=40.0,
                )
            )
        db_session.add(
            TrackedObject(
                recording_id=recording.id,
                track_key=1,
                class_label="car",
                confidence_max=0.9,
                confidence_avg=0.85,
                first_seen_offset_s=30.0,
                last_seen_offset_s=45.0,
                duration_s=15.0,
                frame_count=60,
                best_frame_offset_s=40.0,
                best_bbox=[0.2, 0.2, 0.5, 0.5],
            )
        )
        await db_session.flush()

        class FakeDetector:
            async def detect(self, image, *, min_width_px=0):
                return [((0.1, 0.6, 0.4, 0.8), 0.95)]

        class FakeOCR:
            async def read(self, image):
                return "S192DKX", 0.97

        async def fake_models():
            return FakeDetector(), FakeOCR()

        async def fake_iter_frames(path, **kwargs):
            yield 0.0, np.full((480, 640, 3), 120, dtype=np.uint8)

        monkeypatch.setattr(stages, "_shared_plate_models", fake_models)
        monkeypatch.setattr(stages, "iter_frames", fake_iter_frames)
        monkeypatch.setattr(stages, "resolve_footage_path", lambda *a, **k: "s192dkx.ts")

        assert (await stages.stage_plates(db_session, recording)).ok
        observation = (await db_session.execute(select(PlateObservation))).scalar_one()

        assert observation.normalised_text == "S192DKX"
        assert observation.lat == pytest.approx(LAT + 40 * 0.0001, abs=1e-9), (
            "the sighting was placed where the vehicle first appeared, not where its plate was read"
        )
        assert observation.captured_at == BASE + timedelta(seconds=40)
        assert observation.t_offset_s == pytest.approx(40.0)

    async def test_a_sighting_with_no_lock_is_not_mapped(self, db_session, monkeypatch):
        """No fix anywhere near the detection means no location, not the nearest one."""
        import numpy as np
        from sqlalchemy import select

        from app.db.models import (
            PlateObservation,
            Recording,
            RecordingState,
            TelemetryPoint,
            TrackedObject,
        )
        from app.pipeline import stages

        recording = Recording(
            rel_path="nolock.ts",
            filename="20260803130528_camera_0.ts",
            size_bytes=1024,
            state=RecordingState.PROCESSING,
            duration_s=120.0,
            started_at=BASE,
        )
        db_session.add(recording)
        await db_session.flush()

        # The camera acquired a lock for the first three seconds and then lost it.
        for offset in range(3):
            db_session.add(
                TelemetryPoint(
                    recording_id=recording.id,
                    t_offset_s=float(offset),
                    captured_at=BASE + timedelta(seconds=offset),
                    lat=LAT,
                    lon=LON,
                    has_fix=True,
                )
            )
        # ...and went on reporting the no-fix placeholder for the rest of the clip.
        for offset in range(3, 120):
            db_session.add(
                TelemetryPoint(
                    recording_id=recording.id,
                    t_offset_s=float(offset),
                    captured_at=BASE + timedelta(seconds=offset),
                    lat=None,
                    lon=None,
                    has_fix=False,
                )
            )
        db_session.add(
            TrackedObject(
                recording_id=recording.id,
                track_key=1,
                class_label="car",
                confidence_max=0.9,
                confidence_avg=0.85,
                first_seen_offset_s=90.0,
                last_seen_offset_s=95.0,
                duration_s=5.0,
                frame_count=20,
                best_frame_offset_s=92.0,
                best_bbox=[0.2, 0.2, 0.5, 0.5],
            )
        )
        await db_session.flush()

        class FakeDetector:
            async def detect(self, image, *, min_width_px=0):
                return [((0.1, 0.6, 0.4, 0.8), 0.95)]

        class FakeOCR:
            async def read(self, image):
                return "S945CDX", 0.97

        async def fake_models():
            return FakeDetector(), FakeOCR()

        async def fake_iter_frames(path, **kwargs):
            yield 0.0, np.full((480, 640, 3), 120, dtype=np.uint8)

        monkeypatch.setattr(stages, "_shared_plate_models", fake_models)
        monkeypatch.setattr(stages, "iter_frames", fake_iter_frames)
        monkeypatch.setattr(stages, "resolve_footage_path", lambda *a, **k: "nolock.ts")

        result = await stages.stage_plates(db_session, recording)
        assert result.ok
        assert result.stats["without_position"] == 1, (
            "a sighting recorded during a satellite outage was not reported as unlocated"
        )

        observation = (await db_session.execute(select(PlateObservation))).scalar_one()
        assert observation.lat is None and observation.lon is None, (
            "a sighting 89 seconds after the last fix was given that fix's coordinates"
        )
        # The sighting still exists and still has a time -- it simply has no place.
        assert observation.normalised_text == "S945CDX"
        assert observation.captured_at == BASE + timedelta(seconds=92)

    async def test_telemetry_never_crosses_between_recordings(self, db_session):
        """Front and rear are separate files with separate overlays; a fix from one is not
        evidence about the other, whatever the filenames suggest."""
        from app.db.models import Recording, TelemetryPoint
        from app.pipeline.stages import _fix_track

        front = Recording(rel_path="f.ts", filename="20260803130528_camera_0.ts")
        rear = Recording(rel_path="r.ts", filename="20260803130527_camera_1.ts")
        db_session.add_all([front, rear])
        await db_session.flush()

        db_session.add(
            TelemetryPoint(
                recording_id=front.id,
                t_offset_s=0.0,
                captured_at=BASE,
                lat=LAT,
                lon=LON,
                has_fix=True,
            )
        )
        await db_session.flush()

        assert len(await _fix_track(db_session, front.id)) == 1
        assert len(await _fix_track(db_session, rear.id)) == 0
