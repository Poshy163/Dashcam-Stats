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


class TestNotFound:
    async def test_missing_recording_is_404_not_500(self, client):
        assert (await client.get("/api/recordings/999999")).status_code == 404

    async def test_missing_plate_is_404(self, client):
        assert (await client.get("/api/plates/999999")).status_code == 404

    async def test_unknown_api_path_is_404(self, client):
        assert (await client.get("/api/definitely-not-a-route")).status_code == 404
