"""What the API offers has to be reachable, and has to mean what it says.

Every case here was found against a live deployment with a real backlog, and every one of
them looked fine on an empty database — which is why they survived the existing tests. The
running job was ordered off the end of a 2,030-row list, a journey's route was ordered by a
column that is null on a fifth of its fixes, a vehicle sighting withheld the recording it
came from, and "today" was somebody else's day.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.db.models import (
    Camera,
    CameraRole,
    JobState,
    Journey,
    ProcessingJob,
    Recording,
    RecordingState,
    TelemetryPoint,
    TrackedObject,
)


class TestTheRunningJobIsReachable:
    """The queue claims oldest-first; the list used to sort newest-first.

    Those two together put the running job as far from page one as it is possible to be —
    structurally, not by coincidence — so with any backlog the "Currently processing" panel
    rendered nothing while the Running tile directly above it said 2. The panel is also the
    only route to the overlay reader for work in flight.
    """

    @pytest.fixture
    async def backlog(self, db_session):
        base = datetime(2026, 8, 9, 6, 0, tzinfo=UTC)
        recording = Recording(
            rel_path="busy.ts", filename="busy.ts", size_bytes=1, state=RecordingState.PROCESSING
        )
        db_session.add(recording)
        await db_session.flush()

        # The running job was queued first, exactly as the claim order guarantees.
        running = ProcessingJob(
            recording_id=recording.id,
            state=JobState.RUNNING,
            queued_at=base,
            started_at=base + timedelta(seconds=1),
            stage_current="detection",
            progress=0.59,
        )
        db_session.add(running)
        for i in range(120):
            db_session.add(
                ProcessingJob(
                    recording_id=recording.id,
                    state=JobState.QUEUED,
                    queued_at=base + timedelta(seconds=10 + i),
                )
            )
            # A repeated bulk request creates exactly this shape: one replacement queued
            # job plus one cancelled predecessor per recording. The predecessors are
            # newer history, but must not crowd the active queue off the first page.
            db_session.add(
                ProcessingJob(
                    recording_id=recording.id,
                    state=JobState.CANCELLED,
                    queued_at=base + timedelta(seconds=500 + i),
                    finished_at=base + timedelta(seconds=500 + i),
                    error_message="superseded by a newer request",
                )
            )
        await db_session.flush()
        await db_session.commit()
        return running.id

    async def test_it_is_on_the_first_page(self, client, backlog):
        body = (await client.get("/api/jobs", params={"page_size": 50})).json()
        assert body["total"] > 50, "fixture did not build a backlog"
        ids = [item["id"] for item in body["items"]]
        assert backlog in ids, (
            "the running job is not on page 1 of the queue, so the Currently processing "
            "panel — progress, stage, decoder, device and the link to the overlay reader — "
            "renders nothing while the Running tile says otherwise"
        )
        assert ids[0] == backlog, "in-flight work should lead the list"
        assert {item["state"] for item in body["items"][1:]} == {"queued"}, (
            "cancelled bulk-request history crowded the real pending work off page 1"
        )

    async def test_the_rest_of_the_history_can_be_paged_to(self, client, backlog):
        first = (await client.get("/api/jobs", params={"page_size": 50})).json()
        assert first["pages"] > 1
        second = (await client.get("/api/jobs", params={"page_size": 50, "page": 2})).json()
        assert second["items"], "page 2 is empty even though pages > 1"
        assert {i["id"] for i in second["items"]}.isdisjoint({i["id"] for i in first["items"]})


class TestJourneyRouteOrdering:
    """A fix with no readable timestamp must not be hoisted to the front of the route.

    SQLite sorts NULLs first on ASC, and the overlay sometimes carries a good position with
    an unreadable clock. Ordering by ``captured_at`` therefore put every one of those at the
    start of the drive, ordered by offset alone — interleaving clips recorded minutes and
    kilometres apart into a scribble of false chords across the city.
    """

    @pytest.fixture
    async def journey(self, db_session):
        # The front camera is seeded by the migrations; reuse it rather than collide.
        camera = (
            await db_session.execute(select(Camera).where(Camera.role == CameraRole.FRONT).limit(1))
        ).scalar_one_or_none()
        if camera is None:
            camera = Camera(name="front", role=CameraRole.FRONT, key="camera_0")
            db_session.add(camera)
            await db_session.flush()

        base = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)
        j = Journey(started_at=base, ended_at=base + timedelta(minutes=8), has_gps=True)
        db_session.add(j)
        await db_session.flush()

        # Two clips, four minutes apart, driving steadily east. The second clip's fixes
        # have no timestamp — the case that used to sort them in front of the first clip.
        for clip, (offset_min, timestamped) in enumerate([(0, True), (4, False)]):
            recording = Recording(
                rel_path=f"route_{clip}.ts",
                filename=f"route_{clip}.ts",
                size_bytes=1,
                state=RecordingState.COMPLETED,
                camera_id=camera.id,
                journey_id=j.id,
                started_at=base + timedelta(minutes=offset_min),
                duration_s=60.0,
                has_gps=True,
            )
            db_session.add(recording)
            await db_session.flush()
            for step in range(30):
                db_session.add(
                    TelemetryPoint(
                        recording_id=recording.id,
                        journey_id=j.id,
                        t_offset_s=float(step),
                        captured_at=(
                            base + timedelta(minutes=offset_min, seconds=step)
                            if timestamped
                            else None
                        ),
                        lat=-34.90,
                        # Four minutes of driving separates the clips, so the second one
                        # starts a couple of kilometres further on. Joining the last fix of
                        # one to the first of the next would draw a road across all of it.
                        lon=138.60 + (clip * 30 + step) * 5e-4 + clip * 0.03,
                        has_fix=True,
                        speed_kmh=55.0,
                    )
                )
        await db_session.flush()
        await db_session.commit()
        return j.id

    async def test_the_route_runs_in_the_order_it_was_driven(self, client, journey):
        body = (await client.get(f"/api/journeys/{journey}")).json()
        points = [point for segment in body["route"] for point in segment]
        assert len(points) >= 2

        # The drive is monotonically eastward, so a correctly ordered route is too. Under
        # the old ordering the second clip came first and longitude went backwards.
        longitudes = [lon for _lat, lon, _rid, _off in points]
        assert longitudes == sorted(longitudes), (
            "the route doubles back on itself: fixes with no timestamp were sorted to the "
            f"front and the clips interleaved. {longitudes[:6]}"
        )

    async def test_a_gap_between_clips_is_not_drawn_as_a_road(self, client, journey):
        """Four minutes and a kilometre apart is not a straight line down a street."""
        body = (await client.get(f"/api/journeys/{journey}")).json()
        assert len(body["route"]) >= 2, (
            "the two clips were joined into one unbroken line across the gap between them"
        )

    async def test_each_point_carries_the_clip_it_came_from(self, client, journey):
        body = (await client.get(f"/api/journeys/{journey}")).json()
        ids = {r["id"] for r in body["recordings"]}
        for segment in body["route"]:
            for _lat, _lon, recording_id, offset in segment:
                assert recording_id in ids
                assert 0.0 <= offset <= 60.0


class TestVehicleSightingsLeadBackToTheFootage:
    """A crop with no route back to the recording is evidence and nothing else."""

    @pytest.fixture
    async def sighting(self, db_session):
        recording = Recording(
            rel_path="seen.ts",
            filename="seen.ts",
            size_bytes=1,
            state=RecordingState.COMPLETED,
            started_at=datetime(2026, 8, 5, 3, 0, tzinfo=UTC),
        )
        db_session.add(recording)
        await db_session.flush()
        track = TrackedObject(
            recording_id=recording.id,
            track_key=1,
            class_label="car",
            confidence_max=0.9,
            confidence_avg=0.8,
            first_seen_offset_s=42.5,
            last_seen_offset_s=48.0,
            duration_s=5.5,
            frame_count=20,
            first_seen_at=datetime(2026, 8, 5, 3, 0, 42, tzinfo=UTC),
            lat=-34.9,
            lon=138.6,
            crop_path="vehicles/1/1.jpg",
        )
        db_session.add(track)
        await db_session.flush()
        await db_session.commit()
        return recording.id

    async def test_a_sighting_says_where_and_when_it_was_seen(self, client, sighting):
        items = (await client.get("/api/vehicles")).json()["items"]
        assert items, "fixture produced no sighting"
        vehicle = items[0]
        assert vehicle["recording_id"] == sighting, (
            "the sighting does not say which recording it came from, so the card cannot "
            "link back to the footage"
        )
        assert vehicle["first_seen_offset_s"] == pytest.approx(42.5)
        assert vehicle["lat"] == pytest.approx(-34.9)

    async def test_the_filters_that_make_ten_thousand_sightings_navigable(self, client, sighting):
        assert (await client.get("/api/vehicles", params={"class_label": "car"})).json()["total"]
        assert not (await client.get("/api/vehicles", params={"class_label": "bus"})).json()[
            "total"
        ]
        assert not (await client.get("/api/vehicles", params={"has_plate": True})).json()["total"]
        assert (await client.get("/api/vehicles", params={"has_plate": False})).json()["total"]
        assert (await client.get("/api/vehicles", params={"date_from": "2026-08-05"})).json()[
            "total"
        ]


class TestTodayIsTheUsersToday:
    """UTC midnight is 09:30 in Adelaide, so a UTC boundary hides the whole morning."""

    def test_the_day_starts_where_the_configured_zone_says(self, monkeypatch):
        from app.core import settings_service

        monkeypatch.setattr(
            settings_service,
            "local_zone",
            lambda: __import__("zoneinfo").ZoneInfo("Australia/Adelaide"),
        )
        # 00:30 UTC on the 9th is already 10:00 on the 9th in Adelaide.
        midnight = settings_service.local_midnight_utc(datetime(2026, 8, 9, 0, 30, tzinfo=UTC))
        assert midnight == datetime(2026, 8, 8, 14, 30, tzinfo=UTC), (
            "the day boundary was taken in UTC, so everything driven before 09:30 local "
            "is filed under the wrong day"
        )

    def test_utc_is_left_alone(self, monkeypatch):
        from app.core import settings_service

        monkeypatch.setattr(settings_service, "local_zone", lambda: UTC)
        assert settings_service.local_midnight_utc(
            datetime(2026, 8, 9, 12, 0, tzinfo=UTC)
        ) == datetime(2026, 8, 9, 0, 0, tzinfo=UTC)
