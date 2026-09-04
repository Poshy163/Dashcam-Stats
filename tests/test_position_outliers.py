"""Discarding positions that cannot belong to the drive they were recorded in.

Every case here was taken from a real 674-recording library, where 65 of 45,898 stored
fixes sat in the wrong place and put an impossible distance on 21 of 45 journeys — one
reported 154,701 km covered in eighteen minutes.

The hard case is the first one. Per-point range checks and consecutive-step checks both
ask only whether a reading is internally consistent, and a recording whose minus sign was
dropped for the whole clip is *perfectly* consistent: every fix agrees with every other, at
0.95 confidence, 7,700 km from where the car was. Only the rest of the journey disagrees.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import pairwise

import pytest
from sqlalchemy import func, select, update

from app.db.models import Journey, Recording, RecordingState, TelemetryPoint
from app.db.session import session_scope
from app.journeys.builder import JourneyBuilder
from app.osd.engine import haversine_m
from app.osd.outliers import (
    plausible_radius_m,
    robust_centre,
    spatial_outliers,
)

# Adelaide, where the corpus was recorded.
LAT, LON = -34.8088, 138.6769


class TestSpatialOutliers:
    def test_a_wholly_sign_flipped_recording_is_caught_by_its_journey(self):
        """The failure that motivated this module.

        Five consecutive fixes, all flipped, all agreeing with each other. Nothing within
        that run is anomalous — which is exactly why the comparison has to be against the
        drive as a whole rather than against the neighbouring sample.
        """
        good = [(LAT + i * 1e-4, LON + i * 1e-4) for i in range(40)]
        flipped = [(-LAT + i * 1e-4, LON + i * 1e-4) for i in range(5)]
        points = good[:20] + flipped + good[20:]

        outliers = spatial_outliers(points, span_s=1200.0)
        assert outliers == {20, 21, 22, 23, 24}

    def test_consecutive_step_checking_would_miss_that_run(self):
        """Shows why the cheaper check is not enough, rather than asserting it in prose."""
        flipped = [(-LAT + i * 1e-4, LON + i * 1e-4) for i in range(5)]
        steps = [haversine_m(a[0], a[1], b[0], b[1]) for a, b in pairwise(flipped)]
        # Inside the flipped run every step is a few metres: utterly plausible.
        assert max(steps) < 50

    def test_a_single_stray_coordinate_is_caught(self):
        points = [(LAT, LON)] * 30
        points[7] = (LAT, 13.8769)  # 138.6769 with a digit lost
        assert spatial_outliers(points, span_s=600.0) == {7}

    def test_a_genuine_long_drive_is_left_alone(self):
        """Adelaide to Melbourne over nine hours is real travel, not corruption."""
        points = [
            (LAT + (37.8136 - LAT) * i / 200, LON + (144.9631 - LON) * i / 200) for i in range(200)
        ]
        assert spatial_outliers(points, span_s=9 * 3600.0) == set()

    def test_too_few_points_to_judge_rejects_nothing(self):
        # Two fixes cannot vote on which of them is wrong, and a wrongly discarded fix is
        # a hole in a route that nobody notices.
        assert spatial_outliers([(LAT, LON), (-LAT, LON)], span_s=60.0) == set()

    def test_a_majority_of_outliers_is_refused_rather_than_acted_on(self):
        """An evenly split set has no trustworthy centre, so nothing is discarded.

        Half the fixes at Adelaide and half flipped puts the median on the equator, midway
        between two clusters and near neither. Every point is then thousands of kilometres
        from it and every point looks wrong. Acting on that would delete an entire
        journey's positions on the strength of a statistic that has already failed; the
        only safe answer is to leave it for a human to notice.
        """
        points = [(LAT, LON)] * 10 + [(-LAT, LON)] * 10
        centre = robust_centre(points)
        assert centre is not None and abs(centre[0]) < 1.0, "median should land on the equator"
        assert spatial_outliers(points, span_s=600.0) == set()

    def test_a_clear_minority_is_still_discarded(self):
        # The guard must not be so cautious that it stops doing its job: a lopsided split
        # has a trustworthy centre and the minority really is wrong.
        points = [(LAT, LON)] * 20 + [(-LAT, LON)] * 3
        assert spatial_outliers(points, span_s=600.0) == {20, 21, 22}

    def test_copies_outnumbering_the_readings_do_not_move_the_centre(self):
        """Journey 270, reduced to its arithmetic.

        Six fixes the camera actually printed, forty carried in from the other camera and
        all sign-flipped. By count the flipped ones are the drive; by evidence they are one
        misread photocopied forty times. Told which is which, the pass keeps the six.
        """
        read = [(LAT + i * 1e-4, LON) for i in range(6)]
        copied = [(-LAT + i * 1e-4, LON) for i in range(40)]
        points = read + copied
        direct = [True] * 6 + [False] * 40

        outliers = spatial_outliers(points, span_s=9295.0, direct=direct)

        assert outliers == set(range(6, 46))

    def test_without_that_distinction_the_copies_win(self):
        """Why the flag had to exist -- the same input, judged by headcount.

        This is not a hypothetical: it is what the live library did. The drive kept 1,167
        seconds of Japan and cleared every genuine reading, and because each rebuild asked
        the survivors where the car was, the verdict was stable and reprocessing it changed
        nothing.
        """
        read = [(LAT + i * 1e-4, LON) for i in range(6)]
        copied = [(-LAT + i * 1e-4, LON) for i in range(40)]
        points = read + copied

        assert spatial_outliers(points, span_s=9295.0) == set(range(6))

    def test_too_few_readings_to_anchor_on_falls_back_to_the_whole_set(self):
        """Two readings cannot outvote anything, and pretending otherwise is a guess."""
        read = [(LAT, LON), (LAT + 1e-4, LON)]
        copied = [(-LAT + i * 1e-4, LON) for i in range(40)]
        points = read + copied
        direct = [True] * 2 + [False] * 40

        assert spatial_outliers(points, span_s=9295.0, direct=direct) == spatial_outliers(
            points, span_s=9295.0
        )

    def test_readings_that_disagree_among_themselves_still_call_the_pass_off(self):
        """The guard survives the change: an unanchored anchor is no anchor.

        Half the readings flipped puts their own median on the equator, so every point is
        thousands of kilometres from it. Nothing here is trustworthy enough to delete
        anything with.
        """
        read = [(LAT, LON)] * 5 + [(-LAT, LON)] * 5
        copied = [(-LAT, LON)] * 20
        points = read + copied
        direct = [True] * 10 + [False] * 20

        assert spatial_outliers(points, span_s=9295.0, direct=direct) == set()

    def test_the_centre_is_not_dragged_by_the_outliers(self):
        # A mean of twenty good fixes and one 7,700 km away lands 370 km from the truth,
        # which is far enough to start rejecting the good ones instead.
        points = [(LAT, LON)] * 20 + [(-LAT, LON)]
        centre = robust_centre(points)
        assert centre is not None
        assert haversine_m(centre[0], centre[1], LAT, LON) < 1_000

    @pytest.mark.parametrize(
        ("span_s", "at_least_km"),
        [(60.0, 5.0), (3600.0, 60.0), (9 * 3600.0, 580.0)],
    )
    def test_the_radius_scales_with_how_long_the_drive_lasted(self, span_s, at_least_km):
        assert plausible_radius_m(span_s) >= at_least_km * 1000.0


class TestNoFixPlaceholder:
    """``00.0000`` read with one digit wrong is still ``00.0000``."""

    @pytest.mark.parametrize(
        "line",
        [
            # Every one of these was stored as a real position on the live library.
            "2026-08-07 20:31:21 E:00.0001 N:00.0000 0 km/h",
            "2026-08-07 12:30:29 E:00.0010 N:00.0000 0 km/h",
            "2026-08-06 10:15:15 E:00.0000 N:00.0010 0 km/h",
            "2026-08-04 11:29:38 E:00.0000 N:00.0100 77 km/h",
            # The boundary itself. A strict ``<`` against a 0.1 epsilon missed these by
            # exactly nothing, and they are the *commonest* corruption of the placeholder:
            # one wrong digit in the first decimal place. Two parked clips put 24 fixes in
            # the Gulf of Guinea this way while ``00.0900`` from the same footage was
            # caught, which is what gave the comparison away.
            "2026-08-20 16:08:36 E:00.0000 N:00.1000 0 km/h",
            "2026-08-20 16:12:41 E:00.1000 N:00.0000 0 km/h",
        ],
    )
    def test_a_near_zero_placeholder_is_not_a_coordinate(self, line):
        from app.osd.parser import parse_osd_text

        reading = parse_osd_text(line)
        assert reading.has_position is False, (
            "a placeholder read with one digit wrong was stored as a position in the "
            "Gulf of Guinea, adding ~14,000 km to its journey"
        )

    def test_a_real_coordinate_is_still_accepted(self):
        from app.osd.parser import parse_osd_text

        reading = parse_osd_text("2026-08-04 17:44:39 E:138.6769 N:-34.8088 68 km/h")
        assert reading.has_position is True
        assert reading.lat == pytest.approx(-34.8088)


class TestJourneyMetrics:
    """End to end: a journey containing a flipped recording must still measure correctly."""

    @pytest.fixture
    async def journey_with_a_flipped_recording(self, db_session):
        async with session_scope() as session:
            journey = Journey(
                started_at=datetime(2026, 8, 8, 5, 33, tzinfo=UTC),
                ended_at=datetime(2026, 8, 8, 5, 51, tzinfo=UTC),
                duration_s=1080.0,
            )
            session.add(journey)
            await session.flush()

            base = datetime(2026, 8, 8, 5, 33, tzinfo=UTC)
            for index in range(3):
                rec = Recording(
                    rel_path=f"j-{index}.ts",
                    filename=f"j-{index}.ts",
                    size_bytes=1,
                    state=RecordingState.COMPLETED,
                    journey_id=journey.id,
                    started_at=base + timedelta(seconds=index * 60),
                    ended_at=base + timedelta(seconds=(index + 1) * 60),
                )
                session.add(rec)
                await session.flush()

                # Recording 1 is the one whose minus sign was lost, for every fix.
                flipped = index == 1
                for step in range(30):
                    lat = LAT + step * 1e-4
                    session.add(
                        TelemetryPoint(
                            recording_id=rec.id,
                            journey_id=journey.id,
                            t_offset_s=float(step),
                            captured_at=base + timedelta(seconds=index * 60 + step),
                            lat=-lat if flipped else lat,
                            lon=LON + step * 1e-4,
                            has_fix=True,
                            speed_kmh=60.0,
                        )
                    )
            await session.flush()
            return journey.id

    async def test_the_flipped_positions_are_discarded(self, journey_with_a_flipped_recording):
        journey_id = journey_with_a_flipped_recording
        async with session_scope() as session:
            journey = (
                await session.execute(select(Journey).where(Journey.id == journey_id))
            ).scalar_one()
            await JourneyBuilder().refresh(session, journey)
            await session.commit()

        async with session_scope() as session:
            fixes = (
                (
                    await session.execute(
                        select(TelemetryPoint.lat).where(
                            TelemetryPoint.journey_id == journey_id,
                            TelemetryPoint.has_fix.is_(True),
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert fixes, "the good fixes must survive"
        assert all(lat < 0 for lat in fixes), "a sign-flipped position was kept"
        assert len(fixes) == 60, "only the flipped recording's 30 fixes should have gone"

    async def test_the_distance_becomes_physically_possible(self, journey_with_a_flipped_recording):
        journey_id = journey_with_a_flipped_recording
        async with session_scope() as session:
            journey = (
                await session.execute(select(Journey).where(Journey.id == journey_id))
            ).scalar_one()
            await JourneyBuilder().refresh(session, journey)
            await session.commit()

        async with session_scope() as session:
            journey = (
                await session.execute(select(Journey).where(Journey.id == journey_id))
            ).scalar_one()
            distance_km = (journey.distance_m or 0) / 1000.0
            implied_kmh = distance_km / (journey.duration_s / 3600.0)

        # Live data had journeys reporting 514,921 km/h. Anything a car cannot do is wrong.
        assert implied_kmh < 200, f"{distance_km:.0f} km in {journey.duration_s}s"

    async def test_the_bounds_do_not_span_the_planet(self, journey_with_a_flipped_recording):
        journey_id = journey_with_a_flipped_recording
        async with session_scope() as session:
            journey = (
                await session.execute(select(Journey).where(Journey.id == journey_id))
            ).scalar_one()
            await JourneyBuilder().refresh(session, journey)
            await session.commit()

        async with session_scope() as session:
            journey = (
                await session.execute(select(Journey).where(Journey.id == journey_id))
            ).scalar_one()

        # Bounds set the map's initial zoom. One flipped fix opened it to half the globe.
        assert journey.min_lat is not None and journey.max_lat is not None
        assert journey.max_lat - journey.min_lat < 1.0
        assert journey.max_lat < 0, "bounds still reach into the northern hemisphere"


class TestAParkedSessionWithNoLockInventsNoPlace:
    """The one shape the relative checks are structurally unable to judge.

    Every pass above asks whether a fix disagrees with its neighbours. A car parked in a
    garage for two hours has no neighbours to disagree with: the camera reported no lock
    for all but a handful of samples, and that handful is the same corrupted placeholder
    repeated to the last printed digit. The median centre lands on the corruption, the
    check reports a clean drive, and the map draws a journey in the Gulf of Guinea.

    Two live journeys looked exactly like this — 24 fixes at ``0.1, 0.0`` and one at
    ``0.0, 161.0`` — and each was reported as a journey whose entire bounds were the stray
    point.
    """

    async def _parked_journey(
        self, *, lat: float, lon: float, no_fix_per_recording: int, fixes_per_recording: int = 12
    ) -> int:
        async with session_scope() as session:
            journey = Journey(
                started_at=datetime(2026, 8, 20, 4, 34, tzinfo=UTC),
                ended_at=datetime(2026, 8, 20, 6, 59, tzinfo=UTC),
                duration_s=8734.0,
            )
            session.add(journey)
            await session.flush()

            base = datetime(2026, 8, 20, 4, 34, tzinfo=UTC)
            for index in range(2):
                rec = Recording(
                    rel_path=f"parked-{index}.ts",
                    filename=f"parked-{index}.ts",
                    size_bytes=1,
                    state=RecordingState.COMPLETED,
                    journey_id=journey.id,
                    started_at=base + timedelta(seconds=index * 60),
                    ended_at=base + timedelta(seconds=(index + 1) * 60),
                    telemetry_point_count=no_fix_per_recording + fixes_per_recording,
                    gps_point_count=fixes_per_recording,
                    gps_no_fix_count=no_fix_per_recording,
                )
                session.add(rec)
                await session.flush()
                for step in range(fixes_per_recording):
                    session.add(
                        TelemetryPoint(
                            recording_id=rec.id,
                            journey_id=journey.id,
                            t_offset_s=float(step),
                            captured_at=base + timedelta(seconds=index * 60 + step),
                            # Identical every time, which no real receiver manages.
                            lat=lat,
                            lon=lon,
                            has_fix=True,
                            speed_kmh=0.0,
                        )
                    )
            await session.flush()
            return journey.id

    async def _refresh(self, journey_id: int) -> Journey:
        async with session_scope() as session:
            journey = (
                await session.execute(select(Journey).where(Journey.id == journey_id))
            ).scalar_one()
            await JourneyBuilder().refresh(session, journey)
            await session.commit()
        async with session_scope() as session:
            return (
                await session.execute(select(Journey).where(Journey.id == journey_id))
            ).scalar_one()

    async def _surviving_fixes(self, journey_id: int) -> int:
        async with session_scope() as session:
            return (
                await session.execute(
                    select(func.count(TelemetryPoint.id)).where(
                        TelemetryPoint.journey_id == journey_id,
                        TelemetryPoint.has_fix.is_(True),
                    )
                )
            ).scalar_one()

    async def test_the_placeholder_positions_are_discarded(self, db_session):
        journey_id = await self._parked_journey(lat=0.1, lon=0.0, no_fix_per_recording=250)

        journey = await self._refresh(journey_id)

        assert await self._surviving_fixes(journey_id) == 0
        assert journey.has_gps is False

    async def test_the_journey_stops_claiming_bounds_it_never_had(self, db_session):
        journey_id = await self._parked_journey(lat=0.1, lon=0.0, no_fix_per_recording=250)

        journey = await self._refresh(journey_id)

        # Bounds are what the heat map and the coverage list read. While these were set the
        # map fitted itself to half the planet and every real drive collapsed into a dot.
        assert (journey.min_lat, journey.max_lat) == (None, None)
        assert (journey.min_lon, journey.max_lon) == (None, None)
        assert (journey.start_lat, journey.end_lat) == (None, None)

    async def test_a_corruption_that_is_not_near_null_island_goes_too(self, db_session):
        # The rule is about the *session*, not about proximity to (0, 0). This one is a
        # longitude in the Pacific, produced by the date being read as a coordinate.
        journey_id = await self._parked_journey(
            lat=0.0, lon=161.0, no_fix_per_recording=300, fixes_per_recording=1
        )

        await self._refresh(journey_id)

        assert await self._surviving_fixes(journey_id) == 0

    async def test_the_cleared_rows_stop_calling_themselves_valid(self, db_session):
        """The quality columns have to move with the coordinate.

        Clearing a position used to leave ``gps_quality`` reading ``valid`` on a row with
        no coordinate at all. Nothing drew it, because every map query tests the coordinate
        too — but :mod:`app.osd.reasons` exists so that "which check rejected this" is a
        query, and a rejected row reporting ``valid`` is the one answer it must never give.
        """
        journey_id = await self._parked_journey(lat=0.1, lon=0.0, no_fix_per_recording=250)

        await self._refresh(journey_id)

        async with session_scope() as session:
            rows = (
                (
                    await session.execute(
                        select(TelemetryPoint.gps_quality, TelemetryPoint.gps_reason).where(
                            TelemetryPoint.journey_id == journey_id
                        )
                    )
                )
                .tuples()
                .all()
            )
        assert rows
        # No lock is what the camera actually reported, so that is what the rows say -- not
        # "rejected", which would imply a reading we disbelieved.
        assert {quality for quality, _ in rows} == {"no_fix"}
        assert {reason for _, reason in rows} == {"no_fix"}

    async def test_an_ordinary_outlier_is_marked_rejected_instead(self, db_session):
        # The other verdict: these positions were read and disbelieved, which is a
        # different thing from the camera never having had a lock.
        journey_id = await self._parked_journey(lat=LAT, lon=LON, no_fix_per_recording=0)
        async with session_scope() as session:
            rec_id = (
                await session.execute(
                    select(Recording.id).where(Recording.journey_id == journey_id).limit(1)
                )
            ).scalar_one()
            base = datetime(2026, 8, 20, 4, 34, tzinfo=UTC)
            # One fix on the far side of the planet, in a journey that otherwise agrees.
            session.add(
                TelemetryPoint(
                    recording_id=rec_id,
                    journey_id=journey_id,
                    t_offset_s=99.0,
                    captured_at=base + timedelta(seconds=99),
                    lat=-LAT,
                    lon=LON,
                    has_fix=True,
                    speed_kmh=0.0,
                    gps_quality="valid",
                )
            )
            await session.commit()

        await self._refresh(journey_id)

        async with session_scope() as session:
            row = (
                await session.execute(
                    select(TelemetryPoint.gps_quality, TelemetryPoint.gps_reason).where(
                        TelemetryPoint.journey_id == journey_id,
                        TelemetryPoint.t_offset_s == 99.0,
                    )
                )
            ).one()
        assert row.gps_quality == "rejected"
        assert row.gps_reason == "isolated_position_outlier"

    async def test_a_genuinely_stationary_drive_with_a_lock_is_left_alone(self, db_session):
        """The guard that keeps this from eating real data.

        Idling at a level crossing also produces identical coordinates. What it does not
        produce is a camera reporting no satellite lock, so the ratio is what tells the two
        apart — and it has to, because discarding a real position is the worse error.
        """
        journey_id = await self._parked_journey(lat=LAT, lon=LON, no_fix_per_recording=0)

        journey = await self._refresh(journey_id)

        assert await self._surviving_fixes(journey_id) == 24
        assert journey.has_gps is True
        assert journey.min_lat == pytest.approx(LAT)

    async def test_a_lock_that_is_merely_patchy_is_left_alone(self, db_session):
        # No lock for most of the session, but the positions it did report are distinct --
        # the car was moving. Only the combination is damning.
        async with session_scope() as session:
            journey = Journey(
                started_at=datetime(2026, 8, 20, 4, 34, tzinfo=UTC),
                ended_at=datetime(2026, 8, 20, 4, 44, tzinfo=UTC),
                duration_s=600.0,
            )
            session.add(journey)
            await session.flush()
            base = datetime(2026, 8, 20, 4, 34, tzinfo=UTC)
            rec = Recording(
                rel_path="patchy.ts",
                filename="patchy.ts",
                size_bytes=1,
                state=RecordingState.COMPLETED,
                journey_id=journey.id,
                started_at=base,
                ended_at=base + timedelta(seconds=600),
                telemetry_point_count=600,
                gps_point_count=10,
                gps_no_fix_count=590,
            )
            session.add(rec)
            await session.flush()
            for step in range(10):
                session.add(
                    TelemetryPoint(
                        recording_id=rec.id,
                        journey_id=journey.id,
                        t_offset_s=float(step),
                        captured_at=base + timedelta(seconds=step),
                        lat=LAT + step * 1e-4,
                        lon=LON + step * 1e-4,
                        has_fix=True,
                        speed_kmh=40.0,
                    )
                )
            await session.flush()
            journey_id = journey.id

        await self._refresh(journey_id)

        assert await self._surviving_fixes(journey_id) == 10


class TestRebuildPreservesTheIndex:
    """Rebuilding journeys must not empty the library, and must say so if it does.

    The failure this guards was reported by every signal as a success. Deleting the stale
    journeys cascades through ``ON DELETE SET NULL`` and nulls every
    ``recordings.journey_id`` in the database; SQLite then reissues the row id that was
    just freed, so the replacement journey is id 1 exactly as the old one was. Assigning
    ``recording.journey_id = journey.id`` writes 1 over an in-memory 1, the unit of work
    sees no change and emits no UPDATE, the database keeps its NULL, and ``_drop_empty``
    removes the "empty" new journey. The log line read "rebuilt journeys" and the return
    value was a positive count.

    On the live library one press of Scan now would have emptied all 45 journeys.
    """

    @pytest.fixture
    async def clustered(self, db_session):
        base = datetime(2026, 8, 4, 8, 0, tzinfo=UTC)
        async with session_scope() as session:
            for index in range(6):
                # Two clusters of three, an hour apart — well beyond the gap threshold.
                start = base + timedelta(hours=index // 3, minutes=(index % 3) * 2)
                rec = Recording(
                    rel_path=f"r{index}.ts",
                    filename=f"r{index}.ts",
                    size_bytes=1,
                    state=RecordingState.COMPLETED,
                    started_at=start,
                    ended_at=start + timedelta(minutes=1),
                )
                session.add(rec)
                await session.flush()
                session.add(
                    TelemetryPoint(
                        recording_id=rec.id,
                        t_offset_s=0.0,
                        captured_at=start,
                        lat=LAT,
                        lon=LON,
                        has_fix=True,
                        speed_kmh=60.0,
                    )
                )
            await session.flush()

        async with session_scope() as session:
            await JourneyBuilder().rebuild(session)

    async def test_a_second_rebuild_keeps_every_journey(self, clustered):
        """The first rebuild creates journeys; the second must not destroy them.

        The second is where row ids get reused, which is the whole mechanism.
        """
        async with session_scope() as session:
            before = list((await session.execute(select(Journey.id))).scalars())
        assert before, "fixture produced no journeys to begin with"

        async with session_scope() as session:
            await JourneyBuilder().rebuild(session)

        async with session_scope() as session:
            after = list((await session.execute(select(Journey.id))).scalars())
            linked = list(
                (
                    await session.execute(
                        select(func.count(Recording.id)).where(Recording.journey_id.is_not(None))
                    )
                ).scalars()
            )
        assert after, "rebuilding deleted every journey and reported success"
        assert len(after) == len(before)
        assert linked[0] == 6, f"{linked[0]} of 6 recordings are still attached to a journey"

    async def test_denormalised_journey_ids_follow_the_rebuild(self, clustered):
        """Telemetry carries its own copy of journey_id, and the map reads that copy.

        Leaving it behind outlives the rebuild that orphaned it: the per-journey route and
        the heat map's journey filter stay empty even once journeys exist again.
        """
        async with session_scope() as session:
            await JourneyBuilder().rebuild(session)

        async with session_scope() as session:
            orphaned = (
                await session.execute(
                    select(func.count(TelemetryPoint.id)).where(TelemetryPoint.journey_id.is_(None))
                )
            ).scalar()
            journey_ids = set((await session.execute(select(Journey.id))).scalars())
            pointed = set(
                (
                    await session.execute(
                        select(TelemetryPoint.journey_id).where(
                            TelemetryPoint.journey_id.is_not(None)
                        )
                    )
                ).scalars()
            )
        assert orphaned == 0, f"{orphaned} telemetry points lost their journey"
        assert pointed <= journey_ids, "telemetry points at a journey that no longer exists"


class TestJourneyRollupsStayTrue:
    """A journey that claims GPS must be able to say how far it went.

    Both failures below were live at the same time: 123 of 128 journeys reporting
    `has_gps=True` with `distance_m=None`, one of them holding 1,654 perfectly good fixes,
    and every incrementally built journey showing an empty route on the map.
    """

    @pytest.fixture
    async def journey_with_telemetry(self, db_session):
        base = datetime(2026, 8, 4, 17, 43, tzinfo=UTC)
        async with session_scope() as session:
            rec = Recording(
                rel_path="inc.ts",
                filename="inc.ts",
                size_bytes=1,
                state=RecordingState.COMPLETED,
                started_at=base,
                ended_at=base + timedelta(minutes=2),
            )
            session.add(rec)
            await session.flush()
            for step in range(60):
                session.add(
                    TelemetryPoint(
                        recording_id=rec.id,
                        t_offset_s=float(step),
                        captured_at=base + timedelta(seconds=step),
                        lat=LAT + step * 2e-4,
                        lon=LON + step * 2e-4,
                        has_fix=True,
                        speed_kmh=55.0,
                    )
                )
            await session.flush()
            return rec.id

    async def test_incremental_attach_carries_the_telemetry(self, journey_with_telemetry):
        """The route map reads telemetry_points.journey_id, not recordings.journey_id.

        Setting only the latter attached the recording and orphaned its telemetry, so the
        journey listed its recordings correctly and drew nothing on the map.
        """
        async with session_scope() as session:
            recording = await session.get(Recording, journey_with_telemetry)
            journey = await JourneyBuilder().assign_recording(session, recording)
            assert journey is not None
            journey_id = journey.id
            await session.commit()

        async with session_scope() as session:
            attached = (
                await session.execute(
                    select(func.count(TelemetryPoint.id)).where(
                        TelemetryPoint.journey_id == journey_id
                    )
                )
            ).scalar()
        assert attached == 60, (
            f"{attached} of 60 telemetry points were attached to the journey; the route "
            "map would be empty"
        )

    async def test_a_stale_rollup_is_recomputed(self, journey_with_telemetry):
        """ "Claims GPS, has no distance" is a state no refreshed journey can be in.

        `refresh` sets both from the same rows: either there are fixes and both are
        populated, or there are none and neither is. Anything in between means the rollup
        was invalidated and never recomputed.
        """
        async with session_scope() as session:
            recording = await session.get(Recording, journey_with_telemetry)
            journey = await JourneyBuilder().assign_recording(session, recording)
            journey_id = journey.id
            # Exactly what migration 0004 left behind.
            journey.distance_m = None
            journey.has_gps = True
            await session.commit()

        async with session_scope() as session:
            repaired = await JourneyBuilder().repair_stale(session)
            await session.commit()
        assert repaired == 1

        async with session_scope() as session:
            journey = (
                await session.execute(select(Journey).where(Journey.id == journey_id))
            ).scalar_one()
            assert journey.distance_m is not None and journey.distance_m > 0, (
                "a journey with 60 good fixes still reports no distance"
            )

    async def test_repair_leaves_a_genuinely_gps_less_journey_alone(self, db_session):
        # No fixes at all is a real state -- a parked car with no lock -- and must not be
        # mistaken for a stale rollup.
        base = datetime(2026, 8, 4, 17, 43, tzinfo=UTC)
        async with session_scope() as session:
            journey = Journey(
                started_at=base, ended_at=base + timedelta(minutes=2), duration_s=120.0
            )
            session.add(journey)
            await session.flush()
            await session.commit()

        async with session_scope() as session:
            assert await JourneyBuilder().repair_stale(session) == 0


class TestTwoFixesCannotInventADistance:
    """A journey with only two fixes, one of them wrong.

    Taken from the live library: journey 52 held a single 120-second recording with exactly
    two usable fixes, at longitudes 138.6403 and 139.0422 — one misread digit, 36 km apart.
    It reported 36.7 km covered in 120 seconds, an implied 1,100 km/h.

    Neither existing guard could see it. `spatial_outliers` declines below three points on
    purpose, because two fixes cannot vote on which of them is wrong. And a flat leg cap has
    to be loose enough for a genuine leg across a long dropout, which leaves it far too
    loose for two fixes moments apart.

    Elapsed time is what separates the two cases, and it is available on the rows already.
    """

    @pytest.fixture
    async def two_fix_journey(self, db_session):
        base = datetime(2026, 8, 4, 8, 51, tzinfo=UTC)
        async with session_scope() as session:
            journey = Journey(
                started_at=base, ended_at=base + timedelta(seconds=120), duration_s=120.0
            )
            session.add(journey)
            await session.flush()
            rec = Recording(
                rel_path="two.ts",
                filename="two.ts",
                size_bytes=1,
                state=RecordingState.COMPLETED,
                journey_id=journey.id,
                started_at=base,
                ended_at=base + timedelta(seconds=120),
            )
            session.add(rec)
            await session.flush()
            for offset, lon in ((0.0, 138.6403), (1.0, 139.0422)):
                session.add(
                    TelemetryPoint(
                        recording_id=rec.id,
                        journey_id=journey.id,
                        t_offset_s=offset,
                        captured_at=base + timedelta(seconds=offset),
                        lat=-34.8147,
                        lon=lon,
                        has_fix=True,
                        speed_kmh=30.0,
                    )
                )
            await session.flush()
            return journey.id

    async def test_the_impossible_leg_is_not_counted(self, two_fix_journey):
        async with session_scope() as session:
            journey = (
                await session.execute(select(Journey).where(Journey.id == two_fix_journey))
            ).scalar_one()
            await JourneyBuilder().refresh(session, journey)
            await session.commit()

        async with session_scope() as session:
            journey = (
                await session.execute(select(Journey).where(Journey.id == two_fix_journey))
            ).scalar_one()
            km = (journey.distance_m or 0) / 1000
            implied = km / (journey.duration_s / 3600)
        assert implied < 200, f"{km:.1f} km in {journey.duration_s:.0f}s is {implied:.0f} km/h"

    async def test_a_real_leg_over_a_long_gap_still_counts(self, db_session):
        """The cap must scale with elapsed time, not forbid distance outright.

        Two fixes four minutes apart across a tunnel are 4 km apart at road speed, and that
        is real travel that must survive.
        """
        base = datetime(2026, 8, 4, 8, 51, tzinfo=UTC)
        async with session_scope() as session:
            journey = Journey(
                started_at=base, ended_at=base + timedelta(minutes=5), duration_s=300.0
            )
            session.add(journey)
            await session.flush()
            rec = Recording(
                rel_path="gap.ts",
                filename="gap.ts",
                size_bytes=1,
                state=RecordingState.COMPLETED,
                journey_id=journey.id,
                started_at=base,
                ended_at=base + timedelta(minutes=5),
            )
            session.add(rec)
            await session.flush()
            for offset, lat in ((0.0, LAT), (240.0, LAT + 0.036)):  # ~4 km in 4 minutes
                session.add(
                    TelemetryPoint(
                        recording_id=rec.id,
                        journey_id=journey.id,
                        t_offset_s=offset,
                        captured_at=base + timedelta(seconds=offset),
                        lat=lat,
                        lon=LON,
                        has_fix=True,
                        speed_kmh=60.0,
                    )
                )
            await session.flush()
            journey_id = journey.id
            await session.commit()

        async with session_scope() as session:
            journey = (
                await session.execute(select(Journey).where(Journey.id == journey_id))
            ).scalar_one()
            await JourneyBuilder().refresh(session, journey)
            await session.commit()

        async with session_scope() as session:
            journey = (
                await session.execute(select(Journey).where(Journey.id == journey_id))
            ).scalar_one()
        assert (journey.distance_m or 0) > 3_000, "a real 4 km leg was thrown away"

    async def test_refresh_repoints_the_telemetry(self, two_fix_journey):
        """Routes heal wherever refresh runs, not only on first attach."""
        async with session_scope() as session:
            await session.execute(
                update(TelemetryPoint)
                .where(TelemetryPoint.journey_id == two_fix_journey)
                .values(journey_id=None)
            )
            await session.commit()

        async with session_scope() as session:
            journey = (
                await session.execute(select(Journey).where(Journey.id == two_fix_journey))
            ).scalar_one()
            await JourneyBuilder().refresh(session, journey)
            await session.commit()

        async with session_scope() as session:
            attached = (
                await session.execute(
                    select(func.count(TelemetryPoint.id)).where(
                        TelemetryPoint.journey_id == two_fix_journey
                    )
                )
            ).scalar()
        assert attached == 2, "refresh left the telemetry orphaned; the route map stays empty"


class TestRepairRevisitsBadDistances:
    """A wrong number already in the database has to be reachable again.

    Recomputing on "never computed" alone is not enough: a journey that already holds an
    impossible distance is not null, so nothing would ever ask it to try again, and the
    fix for the bug that produced it would never reach the row it produced.
    """

    @pytest.fixture
    async def journey_with_a_bad_distance(self, db_session):
        base = datetime(2026, 8, 4, 8, 51, tzinfo=UTC)
        async with session_scope() as session:
            journey = Journey(
                started_at=base,
                ended_at=base + timedelta(seconds=120),
                duration_s=120.0,
                # What the live library actually held: 36.7 km in two minutes.
                distance_m=36_700.0,
                has_gps=True,
            )
            session.add(journey)
            await session.flush()
            rec = Recording(
                rel_path="bad.ts",
                filename="bad.ts",
                size_bytes=1,
                state=RecordingState.COMPLETED,
                journey_id=journey.id,
                started_at=base,
                ended_at=base + timedelta(seconds=120),
            )
            session.add(rec)
            await session.flush()
            for offset, lon in ((0.0, 138.6403), (1.0, 139.0422)):
                session.add(
                    TelemetryPoint(
                        recording_id=rec.id,
                        journey_id=journey.id,
                        t_offset_s=offset,
                        captured_at=base + timedelta(seconds=offset),
                        lat=-34.8147,
                        lon=lon,
                        has_fix=True,
                        speed_kmh=30.0,
                    )
                )
            await session.flush()
            return journey.id

    async def test_an_impossible_distance_is_recomputed(self, journey_with_a_bad_distance):
        async with session_scope() as session:
            assert await JourneyBuilder().repair_stale(session) == 1
            await session.commit()

        async with session_scope() as session:
            journey = (
                await session.execute(
                    select(Journey).where(Journey.id == journey_with_a_bad_distance)
                )
            ).scalar_one()
            implied = ((journey.distance_m or 0) / 1000) / (journey.duration_s / 3600)
        assert implied < 200, f"still reporting {implied:.0f} km/h"

    async def test_a_believable_distance_is_left_alone(self, db_session):
        base = datetime(2026, 8, 4, 8, 51, tzinfo=UTC)
        async with session_scope() as session:
            session.add(
                Journey(
                    started_at=base,
                    ended_at=base + timedelta(minutes=20),
                    duration_s=1200.0,
                    distance_m=16_000.0,  # 48 km/h
                    has_gps=True,
                )
            )
            await session.flush()
            await session.commit()

        async with session_scope() as session:
            assert await JourneyBuilder().repair_stale(session) == 0


class TestJourneyBoundariesRecluster:
    """Journeys built one recording at a time must not stay fragmented.

    Two paths build journeys. A rebuild clusters the whole library and gets the boundaries
    right; `assign_recording` attaches one recording as it finishes and creates a journey
    when it finds no neighbour. The queue does not run recordings in chronological order,
    so a segment routinely finishes before the ones either side of it and starts a journey
    of its own. Once the import is done the scanner reports no new files, the rebuild never
    runs, and those fragments become permanent.

    On the live library that produced 129 journeys of which 65 held a single recording, 77
    adjacent pairs inside the five-minute threshold meant to join them, and journeys that
    overlapped in time. One pair was split by a gap of one second.
    """

    @pytest.fixture
    async def fragments(self, db_session):
        """Six consecutive recordings, attached in shuffled order — as the queue does."""
        base = datetime(2026, 8, 4, 8, 0, tzinfo=UTC)
        async with session_scope() as session:
            ids = []
            for index in range(6):
                start = base + timedelta(seconds=index * 121)
                rec = Recording(
                    rel_path=f"f{index}.ts",
                    filename=f"f{index}.ts",
                    size_bytes=1,
                    state=RecordingState.COMPLETED,
                    started_at=start,
                    ended_at=start + timedelta(seconds=120),
                )
                session.add(rec)
                await session.flush()
                ids.append(rec.id)
            await session.flush()

        builder = JourneyBuilder()
        # Out of order on purpose: this is what makes each one create its own journey.
        for index in (3, 0, 5, 1, 4, 2):
            async with session_scope() as session:
                recording = await session.get(Recording, ids[index])
                await builder.assign_recording(session, recording)
                await session.commit()

    async def test_the_fragments_are_detected(self, fragments):
        async with session_scope() as session:
            journeys = list((await session.execute(select(Journey.id))).scalars())
            assert len(journeys) > 1, "fixture did not fragment; nothing to detect"
            assert await JourneyBuilder().needs_recluster(session) is True

    async def test_reclustering_joins_them(self, fragments):
        async with session_scope() as session:
            await JourneyBuilder().rebuild(session)
            await session.commit()

        async with session_scope() as session:
            journeys = list((await session.execute(select(Journey.id))).scalars())
            attached = (
                await session.execute(
                    select(func.count(Recording.id)).where(Recording.journey_id.is_not(None))
                )
            ).scalar()
        assert len(journeys) == 1, (
            f"six consecutive recordings two minutes apart became {len(journeys)} journeys"
        )
        assert attached == 6

    async def test_a_scan_that_finds_nothing_new_still_reclusters(self, fragments, app_config):
        """The wiring, not just the detection.

        This is where the bug actually lived: the rebuild was gated on the scanner finding
        a new or changed file, and a library that has finished importing never reports one.
        Running the scan over an empty footage directory reproduces exactly that -- nothing
        new, nothing changed -- and the fragments must still be put back together.
        """
        from app.scanner.discovery import Scanner
        from app.workers.scheduler import Scheduler

        scheduler = Scheduler()
        # Points at the configured (empty) footage dir, so the scan finds nothing at all.
        scheduler._scanner = Scanner(footage_dir=app_config.footage_dir)
        await scheduler._run_scan()

        async with session_scope() as session:
            journeys = list((await session.execute(select(Journey.id))).scalars())
        assert len(journeys) == 1, (
            f"a scan finding no new files left {len(journeys)} fragmented journeys"
        )

    async def test_a_settled_library_is_left_alone(self, fragments):
        """Having reclustered once, it must not keep doing so.

        The check is a property of the data, so it has to go false as soon as the shape is
        right — otherwise every scan would delete and recreate every journey, renumbering
        them and churning the ids the UI links to.
        """
        async with session_scope() as session:
            await JourneyBuilder().rebuild(session)
            await session.commit()

        async with session_scope() as session:
            assert await JourneyBuilder().needs_recluster(session) is False


class TestACopiedPositionCannotDecideWhereTheDriveWas:
    """Journey 270 of the live library, end to end.

    Two and a half hours parked at home. The rear camera printed its latitude with the
    minus sign decoded as a second colon -- ``N::34.7971`` -- and paired-camera recovery
    copied the resulting position into every hole in the drive. The copies came to
    outnumber the readings, so the median moved to Shizuoka, and the fixes the front camera
    had decoded cleanly at 0.96 confidence were cleared as the outliers. What survived was
    1,167 seconds of Japan and nothing else.

    The parser stopped producing that reading, but no pass over the stored rows could undo
    it: every rebuild asked the survivors where the car had been, and the survivors were
    the copies.
    """

    async def _journey(self) -> int:
        async with session_scope() as session:
            started = datetime(2026, 9, 3, 5, 59, 49, tzinfo=UTC)
            journey = Journey(
                started_at=started,
                ended_at=started + timedelta(seconds=9295),
                duration_s=9295.0,
            )
            session.add(journey)
            await session.flush()

            rec = Recording(
                rel_path="j270.ts",
                filename="j270.ts",
                size_bytes=1,
                state=RecordingState.COMPLETED,
                journey_id=journey.id,
                started_at=started,
                ended_at=started + timedelta(seconds=9295),
                telemetry_point_count=46,
                gps_point_count=46,
                # Not a no-lock session: the camera had a fix throughout. This drive's
                # problem was never the sky.
                gps_no_fix_count=0,
            )
            session.add(rec)
            await session.flush()

            def add(offset: float, lat: float, quality: str) -> None:
                session.add(
                    TelemetryPoint(
                        recording_id=rec.id,
                        journey_id=journey.id,
                        t_offset_s=offset,
                        captured_at=started + timedelta(seconds=offset),
                        lat=lat,
                        lon=LON,
                        has_fix=True,
                        speed_kmh=0.0,
                        gps_quality=quality,
                    )
                )

            # What the front camera read, spread across the session.
            for i in range(6):
                add(float(i * 1500), LAT + i * 1e-4, "valid")
            # What recovery copied in from the rear clip, in every gap between them.
            for i in range(40):
                add(float(i * 220 + 10), -LAT + i * 1e-4, "interpolated")

            await session.flush()
            return journey.id

    async def _rebuild(self, journey_id: int) -> None:
        async with session_scope() as session:
            journey = (
                await session.execute(select(Journey).where(Journey.id == journey_id))
            ).scalar_one()
            await JourneyBuilder().refresh(session, journey)
            await session.commit()

    async def _surviving_latitudes(self, journey_id: int) -> list[float]:
        async with session_scope() as session:
            return list(
                (
                    await session.execute(
                        select(TelemetryPoint.lat).where(
                            TelemetryPoint.journey_id == journey_id,
                            TelemetryPoint.lat.is_not(None),
                        )
                    )
                ).scalars()
            )

    async def test_the_readings_survive_and_the_copies_are_cleared(self, db_session):
        journey_id = await self._journey()

        await self._rebuild(journey_id)

        latitudes = await self._surviving_latitudes(journey_id)
        assert len(latitudes) == 6, "the six the camera actually printed"
        assert all(lat < 0 for lat in latitudes), "and none of the forty it did not"

    async def test_the_journey_no_longer_claims_to_be_in_japan(self, db_session):
        journey_id = await self._journey()

        await self._rebuild(journey_id)

        async with session_scope() as session:
            journey = (
                await session.execute(select(Journey).where(Journey.id == journey_id))
            ).scalar_one()
        assert journey.max_lat is not None and journey.max_lat < 0
        assert haversine_m(journey.min_lat, journey.min_lon, LAT, LON) < 5_000
