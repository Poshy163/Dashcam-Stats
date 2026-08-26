"""Invariants a journey must satisfy however it was built.

A journey is assembled by two paths that run at different times -- ``assign_recording`` as
each clip finishes, and ``rebuild`` over the whole library -- and reshaped by a third
whenever telemetry changes underneath it. The properties below are the ones that have
actually been violated on the live library, so each is pinned rather than assumed.

The headline case: one drive on Aug 3 appeared as **five** journeys, three of them holding
a single recording. It was not reprocessing, and it was not the front/rear pair. Two clips
in the middle of the drive had exactly one surviving GPS fix each, and that fix was a
misread -- so ``recordings.start_lat`` held a coordinate thousands of kilometres away, and
the GPS-continuity check cut the drive either side of each of them. One damaged clip makes
two cuts, which is where "3 for 1" comes from.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.db.models import Camera, CameraRole, Journey, Recording, RecordingState, TelemetryPoint
from app.journeys.builder import JourneyBuilder

BASE = datetime(2026, 8, 3, 3, 15, 27, tzinfo=UTC)
LAT, LON = -34.8080, 138.6179


@pytest.fixture
async def cameras(db_session):
    """The seeded camera_0/camera_1 pair -- the mapping is data, not code."""
    rows = {camera.key: camera for camera in (await db_session.execute(select(Camera))).scalars()}
    front = rows["camera_0"]
    rear = rows["camera_1"]
    assert front.role is CameraRole.FRONT and rear.role is CameraRole.REAR
    return front, rear


async def drive(session, cameras, *, clips: int = 12, damaged: set[int] = frozenset()):
    """A continuous drive: `clips` two-minute segments, each recorded by both cameras.

    A clip listed in *damaged* keeps a single misread fix, exactly as recordings 265 and
    268 do on the live library -- one surviving position, in the wrong hemisphere.
    """
    front, rear = cameras
    made = []
    for index in range(clips):
        start = BASE + timedelta(seconds=120 * index)
        for camera, offset in ((front, 0), (rear, 1)):
            rec = Recording(
                rel_path=f"{index}_{camera.key}.ts",
                filename=f"2026080313{index:02d}{offset:02d}_{camera.key}.ts",
                camera_id=camera.id,
                size_bytes=1024,
                started_at=start + timedelta(seconds=offset),
                ended_at=start + timedelta(seconds=offset + 120),
                duration_s=120.0,
                state=RecordingState.COMPLETED,
            )
            session.add(rec)
            await session.flush()
            if index in damaged:
                # The one fix that survived its own outlier pass, and it is wrong.
                session.add(
                    TelemetryPoint(
                        recording_id=rec.id,
                        t_offset_s=0.0,
                        captured_at=start,
                        lat=-LAT,  # sign lost: the other hemisphere
                        lon=LON,
                        has_fix=True,
                        speed_kmh=50.0,
                    )
                )
                rec.start_lat, rec.start_lon = -LAT, LON
            else:
                for second in range(0, 120, 10):
                    session.add(
                        TelemetryPoint(
                            recording_id=rec.id,
                            t_offset_s=float(second),
                            captured_at=start + timedelta(seconds=second),
                            lat=LAT + (120 * index + second) * 0.0001,
                            lon=LON,
                            has_fix=True,
                            speed_kmh=50.0,
                        )
                    )
                rec.start_lat, rec.start_lon = LAT + 120 * index * 0.0001, LON
            made.append(rec)
    await session.flush()
    return made


class TestOneDriveIsOneJourney:
    async def test_a_clean_drive_is_not_fragmented(self, db_session, cameras):
        await drive(db_session, cameras)
        await JourneyBuilder().rebuild(db_session)
        journeys = list((await db_session.execute(select(Journey))).scalars())
        assert len(journeys) == 1, f"a continuous drive became {len(journeys)} journeys"
        assert journeys[0].recording_count == 24

    async def test_a_damaged_clip_does_not_cut_the_drive_in_three(self, db_session, cameras):
        """The Aug 3 case. One clip with a single misread fix used to cut the drive either
        side of itself, turning one drive into three journeys."""
        await drive(db_session, cameras, damaged={5})
        await JourneyBuilder().rebuild(db_session)
        journeys = list((await db_session.execute(select(Journey))).scalars())
        assert len(journeys) == 1, (
            f"one damaged clip split the drive into {len(journeys)} journeys; a misread "
            "coordinate must not decide where a drive begins and ends"
        )

    async def test_two_damaged_clips_do_not_cut_it_into_five(self, db_session, cameras):
        """Aug 3 had two of them -- clips 265 and 268 -- and produced five journeys."""
        await drive(db_session, cameras, damaged={5, 6})
        await JourneyBuilder().rebuild(db_session)
        assert len((await db_session.execute(select(Journey))).scalars().all()) == 1

    async def test_a_clip_with_no_gps_at_all_stays_in_the_drive(self, db_session, cameras):
        """A satellite outage is not the end of a drive. GPS-less clips have no start
        position, so the continuity check has nothing to compare and time alone decides."""
        recs = await drive(db_session, cameras, clips=8)
        for rec in recs:
            if rec.filename.startswith("20260803130400") or "04" in rec.filename[12:14]:
                pass
        # Strip clip 4 of every fix, the way a tunnel would.
        middle = [r for r in recs if r.started_at == BASE + timedelta(seconds=480)]
        for rec in middle:
            await db_session.execute(
                TelemetryPoint.__table__.delete().where(TelemetryPoint.recording_id == rec.id)
            )
            rec.start_lat = rec.start_lon = None
        await db_session.flush()

        await JourneyBuilder().rebuild(db_session)
        journeys = list((await db_session.execute(select(Journey))).scalars())
        assert len(journeys) == 1, "a GPS dropout split the drive"
        assert journeys[0].recording_count == 16


class TestReprocessingDoesNotMultiplyJourneys:
    async def test_rebuilding_repeatedly_is_idempotent(self, db_session, cameras):
        builder = JourneyBuilder()
        await drive(db_session, cameras, damaged={5})
        await builder.rebuild(db_session)
        first = list((await db_session.execute(select(Journey.id))).scalars())

        for _ in range(3):
            await builder.rebuild(db_session)
        after = list((await db_session.execute(select(Journey.id))).scalars())
        assert len(after) == len(first) == 1, "rebuilding created extra journeys"
        assert await builder.needs_recluster(db_session) is False

    async def test_a_recording_belongs_to_exactly_one_journey(self, db_session, cameras):
        await drive(db_session, cameras, damaged={5})
        await JourneyBuilder().rebuild(db_session)
        rows = (
            await db_session.execute(
                select(Recording.journey_id, func.count(Recording.id))
                .where(Recording.journey_id.is_not(None))
                .group_by(Recording.journey_id)
            )
        ).all()
        assert rows, "nothing was attached to a journey at all"
        # The column is single-valued, so the real risk is the opposite: a journey left
        # behind holding nothing, which is what a partial rebuild used to leave.
        empty = (
            await db_session.execute(
                select(func.count(Journey.id)).where(
                    ~Journey.id.in_(
                        select(Recording.journey_id).where(Recording.journey_id.is_not(None))
                    )
                )
            )
        ).scalar()
        assert empty == 0, f"{empty} journeys hold no recordings"

    async def test_reprocessing_one_recording_does_not_add_a_journey(self, db_session, cameras):
        from app.pipeline.stages import stage_summarise

        recs = await drive(db_session, cameras)
        await JourneyBuilder().rebuild(db_session)
        before = list((await db_session.execute(select(Journey.id))).scalars())

        # What the summarise stage does at the end of every reprocess.
        await stage_summarise(db_session, recs[0])
        await db_session.flush()

        after = list((await db_session.execute(select(Journey.id))).scalars())
        assert len(after) == len(before), (
            f"reprocessing one recording took the library from {len(before)} journeys "
            f"to {len(after)}"
        )


class TestTheNumbersDescribeTheDrive:
    async def test_start_and_end_come_from_the_drive_itself(self, db_session, cameras):
        await drive(db_session, cameras)
        await JourneyBuilder().rebuild(db_session)
        journey = (await db_session.execute(select(Journey))).scalar_one()

        assert journey.start_lat == pytest.approx(LAT, abs=1e-4)
        # The drive runs north; the end must be the far end of it, not a middle sample.
        assert journey.end_lat > journey.start_lat
        assert journey.started_at <= journey.ended_at

    async def test_the_markers_are_the_ends_of_the_drawn_route(self, db_session, cameras, client):
        """The line, the pins and the distance are one answer or they are three."""
        await drive(db_session, cameras)
        await JourneyBuilder().rebuild(db_session)
        journey = (await db_session.execute(select(Journey))).scalar_one()
        await db_session.commit()

        body = (await client.get(f"/api/journeys/{journey.id}")).json()
        points = [p for segment in body["route"] for p in segment]
        assert points, "the journey drew no route at all"
        assert points[0][0] == pytest.approx(body["start_lat"], abs=1e-4)
        assert points[-1][0] == pytest.approx(body["end_lat"], abs=1e-4)

    async def test_distance_is_not_doubled_by_the_second_camera(self, db_session, cameras):
        """Both cameras record the same drive. Measured across 59 live journeys, counting
        every stored fix gave a median of 2.05x the distance the drive can have covered."""
        await drive(db_session, cameras, clips=6)
        await JourneyBuilder().rebuild(db_session)
        journey = (await db_session.execute(select(Journey))).scalar_one()

        # 6 clips x 120 s at 0.0001 deg/s of latitude ~= 8.0 km travelled once.
        travelled = 720 * 0.0001 * 111_320
        assert journey.distance_m == pytest.approx(travelled, rel=0.15), (
            f"distance {journey.distance_m:.0f} m against {travelled:.0f} m of road; "
            "a ratio near two means both cameras were measured"
        )

    async def test_a_journey_with_no_valid_fix_has_no_location(self, db_session, cameras):
        recs = await drive(db_session, cameras, clips=3)
        await db_session.execute(TelemetryPoint.__table__.delete())
        for rec in recs:
            rec.start_lat = rec.start_lon = None
        await db_session.flush()

        await JourneyBuilder().rebuild(db_session)
        journey = (await db_session.execute(select(Journey))).scalar_one()
        assert journey.has_gps is False
        assert journey.start_lat is None and journey.end_lat is None, (
            "a journey with no valid fix must show its location as unknown, not guess one"
        )


class TestTheClipStartSentinelDoesNotCrossRecordings:
    """`breaks_segment` on the first fix of a clip is bookkeeping, not a real break.

    ``track_quality.classify`` sets it on the first drawable fix of *every* recording,
    unconditionally, because its forward walk has no anchor yet within that clip. Every
    per-recording consumer sets its anchor from that same row before testing the flag, so
    inside one recording it means nothing. Concatenated into a journey-wide track it would
    mean something quite wrong: a journey of thirty clips reporting thirty breaks, losing
    the real leg across each clip boundary from its distance and drawing the route cut at
    every one.
    """

    @staticmethod
    def _row(recording_id, offset, lat, lon, breaks):
        from datetime import UTC, datetime

        return SimpleNamespace(
            recording_id=recording_id,
            t_offset_s=offset,
            lat=lat,
            lon=lon,
            speed_kmh=50.0,
            captured_at=datetime(2026, 8, 4, 17, 44, int(offset), tzinfo=UTC),
            breaks_segment=breaks,
        )

    def test_the_first_fix_of_a_later_clip_does_not_break_the_track(self):
        from app.journeys.track import single_track

        rows = [
            self._row(1, 0, -34.80, 138.70, True),  # clip 1 starts
            self._row(1, 1, -34.8002, 138.7002, False),
            self._row(2, 2, -34.8004, 138.7004, True),  # clip 2 starts
            self._row(2, 3, -34.8006, 138.7006, False),
        ]
        track = single_track(rows, {1: None, 2: None}, front_ids={1, 2})

        assert [p.recording_id for p in track] == [1, 1, 2, 2]
        assert [p.breaks_segment for p in track] == [True, False, False, False], (
            "the second clip's start sentinel survived into the journey-wide track"
        )

    def test_a_genuine_break_within_a_clip_is_kept(self):
        from app.journeys.track import single_track

        rows = [
            self._row(1, 0, -34.80, 138.70, True),
            self._row(1, 1, -34.8002, 138.7002, False),
            self._row(1, 2, -34.8004, 138.7004, True),  # a real dropout mid-clip
        ]
        track = single_track(rows, {1: None}, front_ids={1})

        assert [p.breaks_segment for p in track] == [True, False, True]
