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
from sqlalchemy import func, select

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
