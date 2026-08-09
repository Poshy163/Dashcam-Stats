"""Every read endpoint must actually return 200.

Generating the OpenAPI schema proves the routes *register*; it does not prove they
respond. `/api/settings` registered perfectly and then returned 500 to every single
request, because the serialiser emitted ``choices: None`` for settings that are not
select fields while the response model types that field as a list. Pydantic rejected it
on the way out -- 60 validation errors per call -- and the Settings page was unreachable.

Nothing caught it because no test had ever called the endpoint. These do.
"""

from __future__ import annotations

import httpx
import pytest
from httpx import ASGITransport

from app.db.models import Plate, Recording, RecordingState
from app.db.session import session_scope


@pytest.fixture
async def client(db_session):
    """Talks to the real app over ASGI.

    The lifespan is deliberately not run: it would start the worker pool and scheduler,
    which these tests neither need nor want. `db_session` has already migrated the
    database and initialised the settings service, which is what the routes depend on.
    """
    from app.main import app

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


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
        assert len(body["route"]) >= 2, "the map needs the route geometry"

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

    async def test_precision_beyond_the_source_resolution_is_refused(self, client):
        # The overlay prints four decimals; offering more would invent precision.
        assert (await client.get("/api/map/heatmap?precision=7")).status_code == 422

    async def test_empty_library_is_an_empty_map_not_an_error(self, client):
        body = (await client.get("/api/map/heatmap")).json()
        assert body["points"] == []
        assert body["max_weight"] == 0
        assert body["total_points"] == 0


class TestNotFound:
    async def test_missing_recording_is_404_not_500(self, client):
        assert (await client.get("/api/recordings/999999")).status_code == 404

    async def test_missing_plate_is_404(self, client):
        assert (await client.get("/api/plates/999999")).status_code == 404

    async def test_unknown_api_path_is_404(self, client):
        assert (await client.get("/api/definitely-not-a-route")).status_code == 404
