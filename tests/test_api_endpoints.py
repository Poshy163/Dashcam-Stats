"""Every read endpoint must actually return 200.

Generating the OpenAPI schema proves the routes *register*; it does not prove they
respond. `/api/settings` registered perfectly and then returned 500 to every single
request, because the serialiser emitted ``choices: None`` for settings that are not
select fields while the response model types that field as a list. Pydantic rejected it
on the way out -- 60 validation errors per call -- and the Settings page was unreachable.

Nothing caught it because no test had ever called the endpoint. These do.
"""

from __future__ import annotations

from itertools import pairwise

import pytest
from sqlalchemy import select, update

from app.db.models import Plate, Recording, RecordingState
from app.db.session import session_scope

# The `client` fixture lives in conftest.py; several test modules now need it.


@pytest.fixture
async def sample_data(db_session):
    async with session_scope() as session:
        rec = Recording(
            rel_path="20260804174353_camera_0.ts",
            filename="20260804174353_camera_0.ts",
            size_bytes=1024,
            state=RecordingState.COMPLETED,
        )
        session.add(rec)
        session.add(Plate(normalised_text="S123ABC", display_text="S123ABC"))
        await session.flush()


READ_ENDPOINTS = [
    "/health",
    "/api/status",
    "/api/settings",
    "/api/recordings",
    "/api/journeys",
    "/api/plates",
    "/api/vehicles",
    "/api/jobs",
    "/api/jobs/stats",
    "/api/logs",
    "/api/system/hardware",
    "/api/system/info",
    "/api/system/database",
    "/api/retention/safety",
    "/api/search?q=ABC",
    "/api/map/heatmap",
    "/api/map/coverage",
    "/api/map/routes",
]


class TestReadEndpoints:
    @pytest.mark.parametrize("path", READ_ENDPOINTS)
    async def test_returns_200(self, client, sample_data, path):
        response = await client.get(path)
        assert response.status_code == 200, (
            f"{path} returned {response.status_code}: {response.text[:400]}"
        )
        assert response.json() is not None


class TestSettingsPayload:
    """The exact shape the Settings page consumes."""

    async def test_choices_is_always_a_list(self, client):
        """The bug: `None` here made the whole response fail validation."""
        categories = (await client.get("/api/settings")).json()
        assert categories, "no setting categories returned"

        for category in categories:
            for setting in category["settings"]:
                assert isinstance(setting["choices"], list), (
                    f"{setting['key']}: choices is {setting['choices']!r}, expected a list"
                )

    async def test_select_settings_offer_their_options(self, client):
        categories = (await client.get("/api/settings")).json()
        selects = [s for c in categories for s in c["settings"] if s["type"] == "select"]
        assert selects, "no select settings found"
        for setting in selects:
            assert setting["choices"], f"{setting['key']} is a select with no choices"
            for choice in setting["choices"]:
                assert {"value", "label"} <= set(choice)

    async def test_every_setting_carries_what_the_ui_renders(self, client):
        categories = (await client.get("/api/settings")).json()
        for category in categories:
            assert {"key", "label", "settings"} <= set(category)
            for setting in category["settings"]:
                assert {"key", "label", "type", "value", "default", "is_default"} <= set(setting)

    async def test_categories_cover_the_documented_areas(self, client):
        keys = {c["key"] for c in (await client.get("/api/settings")).json()}
        assert {"general", "scanner", "processing", "storage", "maps", "advanced"} <= keys


class TestWriteEndpoints:
    async def test_settings_round_trip(self, client):
        """Update then read back, which is what the Save button does."""
        response = await client.put(
            "/api/settings", json={"values": {"scanner.interval_minutes": 42}}
        )
        assert response.status_code == 200, response.text

        categories = response.json()
        found = [
            s for c in categories for s in c["settings"] if s["key"] == "scanner.interval_minutes"
        ]
        assert found and found[0]["value"] == 42
        assert found[0]["is_default"] is False

    async def test_invalid_setting_is_rejected_with_the_key(self, client):
        response = await client.put(
            "/api/settings", json={"values": {"scanner.interval_minutes": "not a number"}}
        )
        assert response.status_code == 400
        # The UI marks the offending field, so the key has to be in the message.
        assert "scanner.interval_minutes" in response.text

    async def test_unknown_setting_is_rejected(self, client):
        response = await client.put("/api/settings", json={"values": {"nope.not_real": 1}})
        assert response.status_code == 400

    async def test_retention_plan_is_reportable(self, client):
        response = await client.post("/api/retention/plan")
        assert response.status_code == 200, response.text
        plan = response.json()
        assert plan["deletion_enabled"] is False, "deletion must default to off"
        assert "safety" in plan


class TestFeatureStatus:
    """A zero has to say which kind of zero it is.

    On the live library "0 vehicles seen" across 674 processed recordings was accurate and
    completely misleading: the detection weights were fetched from a URL that did not
    exist, every attempt 404'd, and the stage produced nothing while the recordings were
    still marked completed. Nothing in the API distinguished that from quiet roads.
    """

    async def test_status_reports_why_a_feature_produced_nothing(self, client):
        body = (await client.get("/api/status")).json()
        features = {f["key"]: f for f in body["features"]}
        assert {"detection", "plates"} <= set(features)

        for feature in features.values():
            assert {"enabled", "ready", "blocked_reason", "results", "label"} <= set(feature)
            # The invariant that makes the tile trustworthy: a zero is either explained or
            # genuinely means nothing was found.
            if feature["results"] == 0:
                assert feature["blocked_reason"], (
                    f"{feature['key']} reports 0 results with no explanation, which is "
                    "indistinguishable from having genuinely found nothing"
                )

    async def test_a_feature_with_missing_models_is_not_ready(self, client):
        # Models are downloaded on first use and no test downloads them, so this is the
        # unavailable case — exactly the state the live deployment was in.
        features = {f["key"]: f for f in (await client.get("/api/status")).json()["features"]}
        detection = features["detection"]
        assert detection["ready"] is False
        assert detection["blocked_reason"]

    async def test_switching_a_feature_off_is_reported_as_such(self, client):
        await client.put("/api/settings", json={"values": {"plates.enabled": False}})
        features = {f["key"]: f for f in (await client.get("/api/status")).json()["features"]}
        assert features["plates"]["enabled"] is False
        assert "settings" in features["plates"]["blocked_reason"].lower()


class TestDetailEndpoints:
    """Detail routes, which the list routes link to and no test previously opened.

    `/api/journeys` was covered and passed; `/api/journeys/{id}` was not, and returned 400
    for every journey on the live deployment. `JourneyDetailOut` declares a `recordings`
    field, so validating an ORM Journey against it made Pydantic read the lazy relationship
    during attribute extraction, where the async engine has no greenlet to load it in. The
    Journeys page listed 45 drives and every one of them led to an error.

    A list endpoint returning 200 says nothing about the page it links to.
    """

    @pytest.fixture
    async def journey(self, db_session):
        from datetime import UTC, datetime, timedelta

        from app.db.models import Journey, TelemetryPoint

        async with session_scope() as session:
            base = datetime(2026, 8, 4, 17, 43, tzinfo=UTC)
            j = Journey(started_at=base, ended_at=base + timedelta(minutes=18), duration_s=1080.0)
            session.add(j)
            await session.flush()
            rec = Recording(
                rel_path="detail.ts",
                filename="detail.ts",
                size_bytes=1024,
                state=RecordingState.COMPLETED,
                journey_id=j.id,
                started_at=base,
                ended_at=base + timedelta(minutes=1),
            )
            session.add(rec)
            await session.flush()
            for step in range(5):
                session.add(
                    TelemetryPoint(
                        recording_id=rec.id,
                        journey_id=j.id,
                        t_offset_s=float(step),
                        captured_at=base + timedelta(seconds=step),
                        lat=-34.8088 + step * 1e-3,
                        lon=138.6769 + step * 1e-3,
                        has_fix=True,
                        speed_kmh=60.0,
                    )
                )
            await session.flush()
            return j.id

    async def test_journey_detail_loads(self, client, journey):
        response = await client.get(f"/api/journeys/{journey}")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["id"] == journey
        assert len(body["recordings"]) == 1
        assert body["route"], "the map needs the route geometry"
        # Segments of points, not one flat line: the breaks between them are real.
        assert sum(len(segment) for segment in body["route"]) >= 2

    async def test_every_route_point_says_where_it_came_from(self, client, journey):
        """The map's only interactive control depends on this.

        Clicking the route opens the moment that was clicked, which is only possible if
        each point still knows its recording and offset after simplification. Without it
        the handler had to guess from the point's position in the array, and guessed the
        first recording at *index* seconds every time.
        """
        body = (await client.get(f"/api/journeys/{journey}")).json()
        recording_ids = {r["id"] for r in body["recordings"]}
        assert recording_ids

        points = [point for segment in body["route"] for point in segment]
        assert points
        for lat, lon, recording_id, offset in points:
            assert isinstance(lat, float) and isinstance(lon, float)
            assert recording_id in recording_ids, (
                f"route point claims recording {recording_id}, which is not in this journey"
            )
            assert offset >= 0.0

    async def test_every_listed_journey_can_be_opened(self, client, journey):
        """Whatever the list offers, the detail route must serve — that is the link."""
        listed = (await client.get("/api/journeys")).json()["items"]
        assert listed, "fixture did not produce a journey"
        for item in listed:
            response = await client.get(f"/api/journeys/{item['id']}")
            assert response.status_code == 200, (
                f"journey {item['id']} is listed but its page returns "
                f"{response.status_code}: {response.text[:200]}"
            )

    async def test_reanalysis_hides_then_repopulates_the_journey(self, client, journey):
        """Retained journey rows must not keep the old count visible during a rebuild."""
        from app.pipeline.revisions import CURRENT_REVISIONS, INVALIDATED_REVISION

        async with session_scope() as session:
            await session.execute(
                update(Recording)
                .where(Recording.journey_id == journey)
                .values(telemetry_revision=INVALIDATED_REVISION)
            )

        listing = (await client.get("/api/journeys")).json()
        status = (await client.get("/api/status")).json()
        assert listing["total"] == 0
        assert status["totals"]["journeys"] == 0
        assert status["latest_journey"] is None
        assert (await client.get(f"/api/journeys/{journey}")).status_code == 404

        async with session_scope() as session:
            await session.execute(
                update(Recording)
                .where(Recording.journey_id == journey)
                .values(telemetry_revision=CURRENT_REVISIONS["telemetry"])
            )

        listing = (await client.get("/api/journeys")).json()
        status = (await client.get("/api/status")).json()
        assert listing["total"] == 1
        assert status["totals"]["journeys"] == 1
        assert status["latest_journey"]["id"] == journey

    async def test_recording_detail_and_its_sub_resources_load(self, client, journey):
        listed = (await client.get("/api/recordings")).json()["items"]
        assert listed
        rid = listed[0]["id"]
        for path in (
            f"/api/recordings/{rid}",
            f"/api/recordings/{rid}/telemetry",
            f"/api/recordings/{rid}/detections",
            f"/api/recordings/{rid}/plates",
        ):
            response = await client.get(path)
            assert response.status_code == 200, f"{path}: {response.text[:200]}"

        telemetry = (await client.get(f"/api/recordings/{rid}/telemetry")).json()
        assert telemetry
        assert all("raw_text" in point and "quality" in point for point in telemetry)
        assert all(point["quality"].get("gps_status") for point in telemetry)


class TestHeatmap:
    """The heat map aggregates in SQL, so the aggregation itself needs checking.

    A 200 proves nothing here: an endpoint that groups wrongly, or that lets the no-fix
    placeholder through, returns 200 with a map that is quietly wrong — a hot spot in the
    Gulf of Guinea, or every cell weighted 1 because the grouping never collapsed anything.
    """

    @pytest.fixture
    async def telemetry(self, db_session):
        from app.db.models import TelemetryPoint

        async with session_scope() as session:
            rec = Recording(
                rel_path="20260804174353_camera_0.ts",
                filename="20260804174353_camera_0.ts",
                size_bytes=1024,
                state=RecordingState.COMPLETED,
            )
            session.add(rec)
            await session.flush()

            points = [
                # Six fixes inside one ~110 m cell at precision 3, from three distinct
                # coordinates. They must collapse to a single cell of weight 6.
                *[(-34.8088, 138.6769) for _ in range(3)],
                *[(-34.80881, 138.67691) for _ in range(2)],
                (-34.80884, 138.67694),
                # A second cell, clearly elsewhere.
                (-34.7956, 138.7031),
            ]
            for index, (lat, lon) in enumerate(points):
                session.add(
                    TelemetryPoint(
                        recording_id=rec.id,
                        t_offset_s=float(index),
                        lat=lat,
                        lon=lon,
                        has_fix=True,
                        speed_kmh=60.0,
                    )
                )
            # No-fix rows, which must never reach the map. The flagged-but-null case is the
            # dangerous one: it would round to (0, 0) and put a hot spot off West Africa.
            session.add(
                TelemetryPoint(
                    recording_id=rec.id, t_offset_s=90.0, lat=None, lon=None, has_fix=False
                )
            )
            session.add(
                TelemetryPoint(
                    recording_id=rec.id, t_offset_s=91.0, lat=None, lon=None, has_fix=True
                )
            )
            await session.flush()

    async def test_fixes_collapse_into_weighted_cells(self, client, telemetry):
        response = await client.get("/api/map/heatmap?precision=3")
        assert response.status_code == 200, response.text
        body = response.json()

        assert body["cells"] == 2, f"expected two cells, got {body['points']}"
        assert body["total_points"] == 7
        assert body["max_weight"] == 6
        assert body["truncated"] is False

        heaviest = max(body["points"], key=lambda p: p[2])
        assert heaviest[2] == 6
        assert heaviest[0] == pytest.approx(-34.809, abs=1e-3)
        assert heaviest[1] == pytest.approx(138.677, abs=1e-3)

    async def test_no_fix_rows_never_reach_the_map(self, client, telemetry):
        body = (await client.get("/api/map/heatmap?precision=4")).json()
        for lat, lon, _weight in body["points"]:
            assert (lat, lon) != (0.0, 0.0), "the no-fix placeholder was plotted as a coordinate"
            assert -90 <= lat <= 90 and -180 <= lon <= 180

    async def test_finer_precision_splits_cells_that_coarser_merges(self, client, telemetry):
        coarse = (await client.get("/api/map/heatmap?precision=2")).json()
        fine = (await client.get("/api/map/heatmap?precision=4")).json()
        assert fine["cells"] >= coarse["cells"]
        # Whatever the grid, every fix is still accounted for exactly once.
        assert fine["total_points"] == coarse["total_points"] == 7

    async def test_invalidated_recording_cannot_leak_through_an_unrelated_one(
        self, client, telemetry
    ):
        """The missing join made one valid recording expose every stale GPS point."""
        from app.pipeline.revisions import CURRENT_REVISIONS, INVALIDATED_REVISION

        async with session_scope() as session:
            session.add(
                Recording(
                    rel_path="valid-without-points.ts",
                    filename="valid-without-points.ts",
                    size_bytes=1,
                    state=RecordingState.COMPLETED,
                    telemetry_revision=CURRENT_REVISIONS["telemetry"],
                )
            )
            await session.execute(
                update(Recording)
                .where(Recording.rel_path == "20260804174353_camera_0.ts")
                .values(telemetry_revision=INVALIDATED_REVISION)
            )

        body = (await client.get("/api/map/heatmap")).json()
        assert body["points"] == []
        assert body["total_points"] == 0

    async def test_precision_beyond_the_source_resolution_is_refused(self, client):
        # The overlay prints four decimals; offering more would invent precision.
        assert (await client.get("/api/map/heatmap?precision=7")).status_code == 422

    async def test_empty_library_is_an_empty_map_not_an_error(self, client):
        body = (await client.get("/api/map/heatmap")).json()
        assert body["points"] == []
        assert body["max_weight"] == 0
        assert body["total_points"] == 0


class TestHeatmapStatistics:
    """Two numbers on the heat map that were quietly wrong.

    Both were found by comparing the same data at different grid resolutions, which is
    something a user does casually with a dropdown.
    """

    @pytest.fixture
    async def both_cameras(self, db_session):
        from datetime import UTC, datetime, timedelta

        from app.db.models import Camera, CameraRole, TelemetryPoint

        async with session_scope() as session:
            cameras = {}
            for key, role in (("camera_0", CameraRole.FRONT), ("camera_1", CameraRole.REAR)):
                cam = (
                    await session.execute(select(Camera).where(Camera.key == key))
                ).scalar_one_or_none()
                if cam is None:
                    cam = Camera(key=key, name=key, role=role)
                    session.add(cam)
                    await session.flush()
                cameras[key] = cam

            base = datetime(2026, 8, 4, 17, 43, tzinfo=UTC)
            # The same 60 seconds of driving, filmed by both cameras at once.
            for key, cam in cameras.items():
                rec = Recording(
                    rel_path=f"{key}.ts",
                    filename=f"{key}.ts",
                    size_bytes=1,
                    state=RecordingState.COMPLETED,
                    camera_id=cam.id,
                    started_at=base,
                )
                session.add(rec)
                await session.flush()
                for step in range(60):
                    session.add(
                        TelemetryPoint(
                            recording_id=rec.id,
                            t_offset_s=float(step),
                            captured_at=base + timedelta(seconds=step),
                            # A wide spread of speeds, concentrated in a few cells, so a
                            # mean-of-means and a true mean differ visibly.
                            lat=-34.8088 + (step % 5) * 1e-3,
                            lon=138.6769 + (step % 5) * 1e-3,
                            has_fix=True,
                            speed_kmh=10.0 if step % 5 else 100.0,
                        )
                    )
            await session.flush()

    async def test_average_speed_does_not_move_with_the_grid(self, client, both_cameras):
        """It reported 25.3 km/h at one zoom and 49.9 at another, on identical data."""
        seen = {}
        for precision in (1, 2, 3, 4):
            body = (await client.get(f"/api/map/heatmap?precision={precision}")).json()
            seen[precision] = body["average_speed_kmh"]
        distinct_values = {v for v in seen.values() if v is not None}
        assert len(distinct_values) == 1, (
            f"average speed changes with the grid resolution: {seen}. It is a mean of "
            "per-cell means, so a cell holding one fix counts as much as a cell holding "
            "thousands."
        )

    async def test_the_true_mean_is_reported(self, client, both_cameras):
        body = (await client.get("/api/map/heatmap?precision=4")).json()
        # 60 s per camera: one fix in five at 100 km/h, the rest at 10.
        assert body["average_speed_kmh"] == pytest.approx(28.0, abs=0.5)

    async def test_time_represented_is_not_doubled_by_the_second_camera(self, client, both_cameras):
        """Both cameras film the same seconds; counting rows counted each one twice.

        On the live library that made the map claim more hours of driving than the sum of
        every journey's elapsed time, which is not a thing that can be true.
        """
        body = (await client.get("/api/map/heatmap?precision=4")).json()
        assert body["total_points"] == 60, (
            f"60 seconds of driving filmed by two cameras reported as "
            f"{body['total_points']} seconds"
        )


class TestVehicleSightings:
    """The Vehicles page must show the vehicles that were actually seen.

    It used to select from ``vehicles``, a table for identity across sightings that nothing
    in the pipeline has ever written a row to — re-identifying a car between drives is not
    implemented. So the page was structurally empty: it queried a table with no writer while
    1,071 real sightings sat one table over in ``tracked_objects``, and the empty state told
    the user that detection had not run.
    """

    @pytest.fixture
    async def sightings(self, db_session):
        from datetime import UTC, datetime

        from app.db.models import TrackedObject

        async with session_scope() as session:
            rec = Recording(
                rel_path="v.ts", filename="v.ts", size_bytes=1, state=RecordingState.COMPLETED
            )
            session.add(rec)
            await session.flush()
            base = datetime(2026, 7, 28, 9, 4, tzinfo=UTC)
            for index, label in enumerate(("car", "truck", "person")):
                session.add(
                    TrackedObject(
                        recording_id=rec.id,
                        track_key=index,
                        class_label=label,
                        confidence_max=0.9,
                        confidence_avg=0.8,
                        first_seen_offset_s=0.0,
                        last_seen_offset_s=5.0,
                        duration_s=5.0,
                        frame_count=12,
                        first_seen_at=base,
                        last_seen_at=base,
                        crop_path=f"tracks/{label}.jpg",
                    )
                )
            await session.flush()

    async def test_sightings_are_listed(self, client, sightings):
        body = (await client.get("/api/vehicles")).json()
        assert body["total"] > 0, (
            "the Vehicles page is empty while tracked objects exist; it is querying a "
            "table nothing writes to"
        )
        assert all(item["representative_crop_path"] for item in body["items"])

    async def test_people_are_not_listed_as_vehicles(self, client, sightings):
        labels = {i["class_label"] for i in (await client.get("/api/vehicles")).json()["items"]}
        assert "person" not in labels, "a page called Vehicles should not open on pedestrians"
        assert {"car", "truck"} == labels

    async def test_a_class_can_still_be_asked_for_explicitly(self, client, sightings):
        body = (await client.get("/api/vehicles?class_label=person")).json()
        assert body["total"] == 1

    async def test_a_listed_sighting_can_be_opened(self, client, sightings):
        listed = (await client.get("/api/vehicles")).json()["items"]
        for item in listed:
            response = await client.get(f"/api/vehicles/{item['id']}")
            assert response.status_code == 200, response.text

    async def test_reanalysis_hides_the_vehicle_list_and_direct_links(self, client, sightings):
        from app.pipeline.revisions import INVALIDATED_REVISION

        before = (await client.get("/api/vehicles")).json()
        vehicle_id = before["items"][0]["id"]
        async with session_scope() as session:
            await session.execute(
                update(Recording)
                .where(Recording.rel_path == "v.ts")
                .values(detection_revision=INVALIDATED_REVISION)
            )

        after = (await client.get("/api/vehicles")).json()
        status = (await client.get("/api/status")).json()
        assert after["total"] == 0
        assert status["totals"]["tracked_objects"] == 0
        assert (await client.get(f"/api/vehicles/{vehicle_id}")).status_code == 404


class TestPlateCrops:
    """The Plates grid is the page with the pictures on it.

    Crops are written to disk, recorded on the observation, and served correctly. The grid
    still showed "no vehicle image" under every card, because the list route builds its rows
    through the generic paginator — which validates the ORM row and stops — while the crops
    live on the observations. The detail route filled them in; the list route never did.

    A detail endpoint returning the right thing says nothing about the list that links to it.
    """

    @pytest.fixture
    async def plate_with_crops(self, db_session):
        from datetime import UTC, datetime

        from app.db.models import Plate, PlateObservation

        async with session_scope() as session:
            rec = Recording(
                rel_path="p.ts", filename="p.ts", size_bytes=1, state=RecordingState.COMPLETED
            )
            plate = Plate(normalised_text="S233AKF", display_text="S233AKF", best_confidence=0.99)
            session.add_all([rec, plate])
            await session.flush()
            base = datetime(2026, 7, 28, 9, 4, tzinfo=UTC)
            # Two observations; the better one owns the crops that should represent the plate.
            for index, (confidence, suffix) in enumerate(((0.72, "worse"), (0.99, "best"))):
                session.add(
                    PlateObservation(
                        plate_id=plate.id,
                        recording_id=rec.id,
                        t_offset_s=float(index),
                        captured_at=base,
                        raw_text="S233AKF",
                        normalised_text="S233AKF",
                        ocr_confidence=confidence,
                        detection_confidence=0.9,
                        vote_count=1,
                        plate_crop_path=f"plates/00000001/{suffix}.jpg",
                        vehicle_crop_path=f"plates/00000001/{suffix}_vehicle.jpg",
                    )
                )
            await session.flush()
            return plate.id

    async def test_the_list_carries_the_crops(self, client, plate_with_crops):
        body = (await client.get("/api/plates")).json()
        assert body["items"], "no plates returned"
        item = body["items"][0]
        assert item["representative_vehicle_path"], (
            "the Plates grid renders this field; empty means every card shows "
            '"no vehicle image" while the file sits on disk'
        )
        assert item["representative_crop_path"]

    async def test_the_list_and_the_detail_agree(self, client, plate_with_crops):
        listed = (await client.get("/api/plates")).json()["items"][0]
        detail = (await client.get(f"/api/plates/{plate_with_crops}")).json()
        assert listed["representative_crop_path"] == detail["representative_crop_path"]
        assert listed["representative_vehicle_path"] == detail["representative_vehicle_path"]

    async def test_the_best_observation_represents_the_plate(self, client, plate_with_crops):
        item = (await client.get("/api/plates")).json()["items"][0]
        assert "best" in item["representative_crop_path"], (
            f"expected the highest-confidence crop, got {item['representative_crop_path']}"
        )


class TestRouteOverlay:
    """The paths driven, drawn as lines under the heat.

    The heat blurs by design, so it says how often but not which road. These are the roads.
    """

    @pytest.fixture
    async def a_drive_with_a_tunnel(self, db_session):
        from datetime import UTC, datetime, timedelta

        from app.db.models import Journey, TelemetryPoint

        async with session_scope() as session:
            base = datetime(2026, 8, 4, 17, 43, tzinfo=UTC)
            j = Journey(started_at=base, ended_at=base + timedelta(minutes=5), duration_s=300.0)
            session.add(j)
            await session.flush()
            rec = Recording(
                rel_path="drive.ts",
                filename="drive.ts",
                size_bytes=1,
                state=RecordingState.COMPLETED,
                journey_id=j.id,
                started_at=base,
            )
            session.add(rec)
            await session.flush()
            for step in range(240):
                if 100 <= step < 160:
                    continue  # 60 seconds with no lock
                session.add(
                    TelemetryPoint(
                        recording_id=rec.id,
                        journey_id=j.id,
                        t_offset_s=float(step),
                        captured_at=base + timedelta(seconds=step),
                        lat=-34.8088 + step * 1.5e-4,
                        lon=138.6769 + step * 0.5e-4,
                        has_fix=True,
                        speed_kmh=55.0,
                    )
                )
            await session.flush()

    async def test_a_dropout_breaks_the_line_instead_of_crossing_it(
        self, client, a_drive_with_a_tunnel
    ):
        """The artefact this must never produce.

        Joining the fix before a gap to the fix after it draws a road straight through
        whatever the vehicle actually went around. One drive makes that a visible glitch;
        an overlay of every drive ever recorded makes it a spray of false chords that are
        indistinguishable from real roads.
        """
        from app.osd.engine import haversine_m

        body = (await client.get("/api/map/routes?simplify_m=0")).json()
        assert body["segments"] == 2, (
            f"the dropout should split the drive in two, got {body['segments']} segment(s)"
        )
        worst = 0.0
        for line in body["lines"]:
            for (a_lat, a_lon), (b_lat, b_lon) in pairwise(line):
                worst = max(worst, haversine_m(a_lat, a_lon, b_lat, b_lon))
        assert worst < 100, f"a {worst:.0f} m chord was drawn across the gap"

    async def test_simplification_does_not_reunite_the_gap(self, client, a_drive_with_a_tunnel):
        # Splitting has to happen before simplifying: run the other way round,
        # Douglas-Peucker treats the two ends of a dropout as collinear with everything
        # between them and collapses it back into one straight line.
        body = (await client.get("/api/map/routes?simplify_m=25")).json()
        assert body["segments"] == 2

    async def test_simplification_reduces_the_payload(self, client, a_drive_with_a_tunnel):
        raw = (await client.get("/api/map/routes?simplify_m=0")).json()
        simplified = (await client.get("/api/map/routes")).json()
        assert simplified["points"] < raw["points"]
        assert simplified["points"] > 0


class TestNotFound:
    async def test_missing_recording_is_404_not_500(self, client):
        assert (await client.get("/api/recordings/999999")).status_code == 404

    async def test_missing_plate_is_404(self, client):
        assert (await client.get("/api/plates/999999")).status_code == 404

    async def test_unknown_api_path_is_404(self, client):
        assert (await client.get("/api/definitely-not-a-route")).status_code == 404


class TestDateFiltering:
    """Picking one day must return that day.

    `<input type="date">` sends "2026-08-08", which parses to midnight, so an inclusive
    `<=` comparison excluded the entire day. On the live library filtering to a single
    date returned "No recordings match. Adjust the filters, or run a scan" while 24
    recordings sat under it — the user was told their footage was not indexed.

    The timezone half matters just as much: `started_at` is UTC and the picker speaks
    local time, which for Adelaide is nine and a half hours apart.
    """

    @pytest.fixture
    async def across_a_day(self, db_session):
        from datetime import UTC, datetime

        async with session_scope() as session:
            # 2026-08-08 in Adelaide (UTC+9:30) runs 2026-08-07 14:30Z .. 2026-08-08 14:30Z.
            for when in (
                datetime(2026, 8, 7, 15, 0, tzinfo=UTC),  # early on the 8th, local
                datetime(2026, 8, 8, 3, 0, tzinfo=UTC),  # midday on the 8th, local
                datetime(2026, 8, 8, 14, 0, tzinfo=UTC),  # late on the 8th, local
                datetime(2026, 8, 8, 15, 0, tzinfo=UTC),  # already the 9th, local
            ):
                session.add(
                    Recording(
                        rel_path=f"{when.isoformat()}.ts",
                        filename=f"{when.isoformat()}.ts",
                        size_bytes=1,
                        state=RecordingState.COMPLETED,
                        started_at=when,
                    )
                )
            await session.flush()

    async def test_a_single_day_is_not_empty(self, client, across_a_day):
        await client.put("/api/settings", json={"values": {"general.timezone": "UTC"}})
        body = (await client.get("/api/recordings?date_from=2026-08-08&date_to=2026-08-08")).json()
        assert body["total"] > 0, (
            "selecting a single date returned nothing; the end of the range excluded the whole day"
        )

    async def test_the_day_is_bounded_by_the_configured_timezone(self, client, across_a_day):
        await client.put(
            "/api/settings", json={"values": {"general.timezone": "Australia/Adelaide"}}
        )
        body = (await client.get("/api/recordings?date_from=2026-08-08&date_to=2026-08-08")).json()
        # Three of the four fixtures fall on the 8th in Adelaide; the last is the 9th.
        assert body["total"] == 3, (
            f"expected the three recordings that fall on 2026-08-08 locally, got {body['total']}"
        )

    async def test_an_explicit_instant_is_still_respected(self, client, across_a_day):
        # A full timestamp is not a bare date and must not be widened to a whole day.
        body = (await client.get("/api/recordings?date_to=2026-08-08T03:00:00%2B00:00")).json()
        assert body["total"] == 2


@pytest.mark.needs_ffmpeg
@pytest.mark.slow
class TestOsdDebugView:
    """The overlay debug view has to answer at whatever offset it is asked for.

    It is the one screen that shows what the pipeline actually reads, so an intermittent
    failure here is worse than most bugs: it is taken as evidence about the footage. The
    endpoint hand-rolled its frame grab, pairing `fps=1` with a one-second window; the
    frame-selection filter emits nothing whenever the instant it picks falls outside that
    window, which depends on where the seek lands. On the live deployment it answered at
    t=0 and t=10 and returned 422 at t=1, for a recording that decodes perfectly.

    **What these do and do not prove.** Reverting the fix was tried, and the offset cases
    below still passed: the synthetic fixtures' frame timing happens to yield a frame at
    every offset asked for, so they document the requirement without reproducing the
    original failure, which needed the real clip's timing. `test_past_the_end_explains
    _itself` does catch the other half — the hand-rolled loop let an FFmpegError escape as
    a 500, where a diagnostic must answer "could not decode" rather than crash.

    Reproducing the timing-dependent half would mean committing a fixture chosen for its
    awkward PTS layout, which is a lot of weight for one branch that the shared helper now
    covers anyway. Left as it is, honestly labelled, rather than implied to be a guard it
    is not.
    """

    @pytest.fixture
    async def clip_recording(self, db_session, app_config, fixture_dir):
        import shutil

        source = fixture_dir / "20260804174353_camera_0.ts"
        target = app_config.footage_dir / source.name
        shutil.copy(source, target)

        async with session_scope() as session:
            rec = Recording(
                rel_path=source.name,
                filename=source.name,
                size_bytes=target.stat().st_size,
                state=RecordingState.COMPLETED,
            )
            session.add(rec)
            await session.flush()
            return rec.id

    @pytest.mark.parametrize("offset", [0, 0.5, 1, 1.5, 2, 3])
    async def test_it_answers_at_any_offset(self, client, clip_recording, offset):
        response = await client.get(f"/api/recordings/{clip_recording}/osd-debug?t={offset}")
        assert response.status_code == 200, (
            f"t={offset} returned {response.status_code}: {response.text[:200]}"
        )
        body = response.json()
        assert body["region"]["width"] > 0 and body["region"]["height"] > 0
        assert "decoded_text" in body and "parsed" in body

    async def test_the_composite_image_renders(self, client, clip_recording):
        response = await client.get(f"/api/recordings/{clip_recording}/osd-debug.png?t=1")
        assert response.status_code == 200, response.text
        assert response.headers["content-type"] == "image/png"
        assert len(response.content) > 1000

    async def test_past_the_end_explains_itself(self, client, clip_recording):
        # A diagnostic that answers a decode failure with a stack trace is worse than
        # useless: the thing being diagnosed is often that the file will not decode.
        response = await client.get(f"/api/recordings/{clip_recording}/osd-debug?t=99999")
        assert response.status_code == 422
        assert "decoded" in response.text.lower()
