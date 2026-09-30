"""Regression cases from the September 2026 application audit."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from app.db.models import (
    Camera,
    Journey,
    Plate,
    PlateObservation,
    Recording,
    RecordingState,
    TelemetryPoint,
)
from app.db.session import session_scope

TEST_KEY = "audit-fixture-key-only-0123456789abcdef"


@pytest.fixture
async def shell_client(db_session, tmp_path, monkeypatch):
    from app import main
    from app.ingest import origin

    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "index.html").write_text("<!doctype html><title>Test</title>", encoding="utf-8")
    monkeypatch.setattr(main, "FRONTEND_DIST", frontend)
    app = main.create_app()
    origin.reset_for_tests()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://trusted.test"
    ) as client:
        yield client
    origin.reset_for_tests()


async def _secure(client):
    assert (
        await client.put(
            "/api/auth/credential", json={"username": "audit", "password": "fixture-password"}
        )
    ).status_code == 204
    assert (
        await client.put(
            "/api/settings",
            json={"values": {"security.require_login": True, "security.api_key": TEST_KEY}},
        )
    ).status_code == 200


@pytest.mark.parametrize("require_login", [False, True])
async def test_signed_out_shell_cannot_poison_key_destination(shell_client, require_login):
    from app.core.settings_service import get_settings_service
    from app.ingest import origin

    if require_login:
        await _secure(shell_client)
    else:
        assert (
            await shell_client.put("/api/settings", json={"values": {"security.api_key": TEST_KEY}})
        ).status_code == 200
    await origin.remember("http", "trusted.test")
    shell_client.cookies.clear()
    response = await shell_client.get("/backup", headers={"Host": "untrusted.example"})
    assert response.status_code == 200
    assert origin.backup_url().startswith("http://trusted.test/")
    assert get_settings_service().get_nowait(origin.LEARNED_KEY) == "http://trusted.test"


async def test_auth_state_learns_origin_after_spa_login(shell_client):
    from app.ingest import origin

    await _secure(shell_client)
    shell_client.cookies.clear()
    origin.reset_for_tests()
    assert (
        await shell_client.post(
            "/api/auth/login", json={"username": "audit", "password": "fixture-password"}
        )
    ).status_code == 200
    assert (await shell_client.get("/api/auth/state")).json()["authenticated"]
    assert origin.backup_url().startswith("http://trusted.test/")


async def test_keyless_public_dashboard_still_learns_origin(shell_client):
    from app.ingest import origin

    assert (await shell_client.get("/backup")).status_code == 200
    assert origin.backup_url() == "http://trusted.test/backup?kiosk=1"


@pytest.mark.parametrize(
    ("day", "next_midnight"),
    [
        ("2026-10-04", datetime(2026, 10, 4, 13, 30, tzinfo=UTC)),
        ("2026-04-05", datetime(2026, 4, 5, 14, 30, tzinfo=UTC)),
    ],
)
async def test_date_filter_uses_next_local_midnight_across_dst(client, day, next_midnight):
    await client.put("/api/settings", json={"values": {"general.timezone": "Australia/Adelaide"}})
    async with session_scope() as session:
        for name, offset in (("inside", -30), ("outside", 30)):
            session.add(
                Recording(
                    rel_path=f"{name}.ts",
                    filename=f"{name}.ts",
                    state=RecordingState.COMPLETED,
                    started_at=next_midnight + timedelta(minutes=offset),
                )
            )
    response = await client.get("/api/recordings", params={"date_from": day, "date_to": day})
    assert response.status_code == 200, response.text
    assert [r["filename"] for r in response.json()["items"]] == ["inside.ts"]


@pytest.mark.parametrize("instant", ["2026-08-08T00:00:00Z", "2026-08-08T00:00:00"])
async def test_explicit_midnight_is_an_inclusive_instant(client, instant):
    await client.put("/api/settings", json={"values": {"general.timezone": "Australia/Adelaide"}})
    async with session_scope() as session:
        for name, hour in (("boundary", 0), ("later", 1)):
            session.add(
                Recording(
                    rel_path=f"{name}.ts",
                    filename=f"{name}.ts",
                    state=RecordingState.COMPLETED,
                    started_at=datetime(2026, 8, 8, hour, tzinfo=UTC),
                )
            )
    response = await client.get("/api/recordings", params={"date_to": instant})
    assert response.status_code == 200, response.text
    assert [r["filename"] for r in response.json()["items"]] == ["boundary.ts"]


async def test_date_search_uses_the_display_timezone(client):
    await client.put("/api/settings", json={"values": {"general.timezone": "Australia/Adelaide"}})
    started = datetime(2026, 8, 7, 15, tzinfo=UTC)
    async with session_scope() as session:
        journey = Journey(started_at=started, ended_at=started + timedelta(minutes=10))
        session.add(journey)
        await session.flush()
        session.add(
            Recording(
                rel_path="early.ts",
                filename="early.ts",
                state=RecordingState.COMPLETED,
                started_at=started,
                journey_id=journey.id,
            )
        )
        journey_id = journey.id
    response = await client.get("/api/search", params={"q": "2026-08-08"})
    assert response.status_code == 200, response.text
    assert [j["id"] for j in response.json()["journeys"]] == [journey_id]


@pytest.mark.parametrize("hidden_state", ["ignored", "processing"])
async def test_plate_thumbnail_matches_visible_observations(client, hidden_state):
    async with session_scope() as session:
        plate = Plate(normalised_text="TEST123", display_text="TEST123", best_confidence=0.99)
        session.add(plate)
        await session.flush()
        plate_id = plate.id
        for name, confidence in (("visible", 0.8), ("hidden", 0.99)):
            rec = Recording(
                rel_path=f"{name}.ts",
                filename=f"{name}.ts",
                ignored=name == "hidden" and hidden_state == "ignored",
                state=(
                    RecordingState.PROCESSING
                    if name == "hidden" and hidden_state == "processing"
                    else RecordingState.COMPLETED
                ),
            )
            session.add(rec)
            await session.flush()
            session.add(
                PlateObservation(
                    plate_id=plate_id,
                    recording_id=rec.id,
                    t_offset_s=0,
                    raw_text="TEST123",
                    normalised_text="TEST123",
                    ocr_confidence=confidence,
                    detection_confidence=confidence,
                    plate_crop_path=f"plates/{name}.jpg",
                )
            )
    for path in ("/api/plates", f"/api/plates/{plate_id}"):
        response = await client.get(path)
        assert response.status_code == 200, response.text
        data = response.json()
        item = data["items"][0] if "items" in data else data
        assert item["observation_count"] == 1
        assert item["representative_crop_path"] == "plates/visible.jpg"
    filtered = await client.get("/api/plates", params={"min_confidence": 0.9})
    assert filtered.status_code == 200, filtered.text
    assert filtered.json()["total"] == 0


async def test_metadata_export_loads_camera_and_preserves_final_gps_verdict(client):
    async with session_scope() as session:
        camera = await session.scalar(select(Camera).limit(1))
        recording = Recording(
            rel_path="export.ts",
            filename="export.ts",
            state=RecordingState.COMPLETED,
            camera_id=camera.id,
        )
        session.add(recording)
        await session.flush()
        recording_id = recording.id
        camera_id = camera.id
        session.add(
            TelemetryPoint(
                recording_id=recording_id,
                t_offset_s=0,
                has_fix=False,
                gps_quality="rejected",
                gps_reason="jump",
                quality_json={"gps_status": "valid"},
            )
        )
    response = await client.get(f"/api/recordings/{recording_id}/export.json")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["recording"]["camera"]["id"] == camera_id
    quality = body["telemetry"][0]["quality"]
    assert quality["gps_status"] == "rejected"
    assert quality["observed_gps_status"] == "valid"
    assert quality["gps_reason"] == "jump"
