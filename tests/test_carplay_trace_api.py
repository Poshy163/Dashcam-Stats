"""Heavy diagnostics require an explicit, expiring request; GET stays passive."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.api.routes import ingest_carplay_trace as routes
from app.core.settings_service import get_settings_service
from app.ingest import carplay_timing


@pytest.fixture
def reconcile(monkeypatch):
    callback = Mock()
    monkeypatch.setattr(carplay_timing, "reconcile_on_unit_present", callback)
    monkeypatch.setattr(routes, "get_status", lambda: SimpleNamespace(unit_online=True))
    return callback


async def configure():
    settings = get_settings_service()
    await settings.set_many(
        {
            "ingest.unit_adb_address": "192.0.2.10:5555",
            "ingest.carplay_timing": True,
            "ingest.unit_logs": True,
        }
    )
    return settings


async def test_default_status_and_settings_do_not_enable_heavy_tracing(client, reconcile):
    response = await client.get("/api/ingest/carplay-trace")
    assert response.status_code == 200
    assert response.json()["requested"] is False
    assert response.json()["expires_at"] is None
    assert response.json()["remaining_s"] == 0
    assert not reconcile.called
    catalogue = (await client.get("/api/settings")).json()
    assert carplay_timing.CODEC_TRACE_UNTIL_KEY not in {
        row["key"] for category in catalogue for row in category["settings"]
    }


async def test_explicit_request_persists_deadline_and_queues_online_reconciliation(
    client, reconcile
):
    settings = await configure()
    before = int(datetime.now(UTC).timestamp())
    response = await client.post("/api/ingest/carplay-trace", json={"duration_s": 900})
    assert response.status_code == 200
    data = response.json()
    assert data["requested"] is True
    assert 898 <= data["remaining_s"] <= 900
    deadline = settings.get_nowait(carplay_timing.CODEC_TRACE_UNTIL_KEY)
    assert before + 900 <= deadline <= int(datetime.now(UTC).timestamp()) + 900
    reconcile.assert_called_once_with("192.0.2.10:5555", config_only=True)
    await settings.reload()
    assert settings.get_nowait(carplay_timing.CODEC_TRACE_UNTIL_KEY) == deadline
    assert (await client.get("/api/ingest/carplay-trace")).json()["requested"] is True
    assert reconcile.call_count == 1, "reading status must not renew the lease"


async def test_offline_request_does_not_claim_capture_or_extend_when_read(
    client, reconcile, monkeypatch
):
    settings = await configure()
    monkeypatch.setattr(routes, "get_status", lambda: SimpleNamespace(unit_online=False))
    response = await client.post("/api/ingest/carplay-trace", json={"duration_s": 60})
    assert response.json()["requested"] is True
    assert response.json()["unit_online"] is False
    deadline = settings.get_nowait(carplay_timing.CODEC_TRACE_UNTIL_KEY)
    assert not reconcile.called
    await client.get("/api/ingest/carplay-trace")
    assert settings.get_nowait(carplay_timing.CODEC_TRACE_UNTIL_KEY) == deadline


async def test_stop_persists_off_and_reconciles_without_changing_passive_or_backup(
    client, reconcile
):
    settings = await configure()
    await settings.set_many({"ingest.enabled": True})
    await client.post("/api/ingest/carplay-trace", json={"duration_s": 900})
    response = await client.delete("/api/ingest/carplay-trace")
    assert response.status_code == 200
    assert response.json()["requested"] is False
    assert settings.get_nowait(carplay_timing.CODEC_TRACE_UNTIL_KEY) == 0
    assert settings.get_nowait("ingest.carplay_timing") is True
    assert settings.get_nowait("ingest.enabled") is True
    assert reconcile.call_count == 2
    reconcile.assert_called_with("192.0.2.10:5555", config_only=True, force_off=True)


@pytest.mark.parametrize("disabled", ["ingest.carplay_timing", "ingest.unit_logs"])
async def test_existing_request_remains_stoppable_when_passive_is_disabled(
    client, reconcile, disabled
):
    settings = await configure()
    await client.post("/api/ingest/carplay-trace", json={"duration_s": 900})
    await settings.set(disabled, False)
    observed = (await client.get("/api/ingest/carplay-trace")).json()
    assert observed["requested"] is True
    assert observed["passive_enabled"] is False
    stopped = await client.delete("/api/ingest/carplay-trace")
    assert stopped.json()["requested"] is False
    reconcile.assert_called_with("192.0.2.10:5555", config_only=True, force_off=True)


@pytest.mark.parametrize(
    "payload",
    [
        {"duration_s": True},
        {"duration_s": "900"},
        {"duration_s": 900.5},
        {"duration_s": 59},
        {"duration_s": 1801},
        {"duration_s": 0},
        {"duration_s": 900, "enabled": True},
    ],
)
async def test_invalid_requests_cannot_enable_tracing(client, reconcile, payload):
    await configure()
    assert (await client.post("/api/ingest/carplay-trace", json=payload)).status_code == 422
    assert get_settings_service().get_nowait(carplay_timing.CODEC_TRACE_UNTIL_KEY) == 0
    assert not reconcile.called


@pytest.mark.parametrize("disabled", ["ingest.carplay_timing", "ingest.unit_logs"])
async def test_start_requires_passive_logging_and_sampling(client, reconcile, disabled):
    settings = await configure()
    await settings.set(disabled, False)
    assert (await client.post("/api/ingest/carplay-trace", json={})).status_code == 409
    assert settings.get_nowait(carplay_timing.CODEC_TRACE_UNTIL_KEY) == 0
    assert not reconcile.called


async def test_missing_unit_address_cannot_arm_trace(client, reconcile):
    settings = await configure()
    await settings.set("ingest.unit_adb_address", "")
    assert (await client.post("/api/ingest/carplay-trace", json={})).status_code == 409
    assert not reconcile.called


async def test_raw_settings_api_cannot_write_internal_deadline(client, reconcile):
    await configure()
    response = await client.put(
        "/api/settings", json={"values": {carplay_timing.CODEC_TRACE_UNTIL_KEY: 2000000000}}
    )
    assert response.status_code == 400
    assert get_settings_service().get_nowait(carplay_timing.CODEC_TRACE_UNTIL_KEY) == 0
    assert not reconcile.called


async def test_expired_or_invalid_future_deadline_is_not_a_live_request(client, reconcile):
    settings = await configure()
    for deadline in [
        int(datetime.now(UTC).timestamp()) - 1,
        int(datetime.now(UTC).timestamp()) + 1801,
    ]:
        await settings.set(carplay_timing.CODEC_TRACE_UNTIL_KEY, deadline, internal=True)
        assert (await client.get("/api/ingest/carplay-trace")).json()["requested"] is False
    assert not reconcile.called
