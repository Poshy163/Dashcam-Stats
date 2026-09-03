"""Public, credential-free observability for durable radio transitions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.core.settings_service import get_settings_service
from app.db.models import IngestRadioTransition


async def test_radio_status_without_history_reports_the_live_setting(client):
    response = await client.get("/api/ingest/radio-status")

    assert response.status_code == 200
    assert response.json() == {"quieting_enabled": False, "transition": None}


async def test_radio_status_exposes_only_bounded_restore_evidence(client, db_session):
    await get_settings_service().set_many({"ingest.enabled": True, "ingest.quiet_radios": True})
    now = datetime.now(UTC)
    db_session.add(
        IngestRadioTransition(
            transition_id="00000000-0000-0000-0000-000000000001",
            trigger="auto",
            phase="complete",
            active=False,
            created_at=now - timedelta(minutes=2),
            updated_at=now,
            completed_at=now,
            heartbeat_at=now,
            lease_owner="secret-lease-owner",
            lease_expires_at=now,
            device_address="secret-device-address:5555",
            device_boot_id="secret-boot-identity",
            transport_host="secret-transport-host",
            transport_interface="wlan0",
            capabilities_json=["secret-capability"],
            logger_status_path="/secret/logger/status.json",
            logger_request_id="secret-request-id",
            logger_quiesce_capable=True,
            logger_quiesce_requested_at=now - timedelta(minutes=2),
            logger_quiesce_acked_at=now - timedelta(minutes=2),
            logger_resume_attempted=True,
            logger_resume_verified=True,
            bluetooth_before="on",
            hotspot_before="off",
            hotspot_interface="ap0",
            hotspot_restore_ref="/secret/hotspot-capsule",
            bluetooth_disable_attempted=True,
            bluetooth_disable_verified=True,
            hotspot_disable_attempted=False,
            hotspot_disable_verified=True,
            bluetooth_restore_attempted=True,
            bluetooth_restore_verified=True,
            hotspot_restore_attempted=True,
            hotspot_restore_verified=True,
            obd_transfer_complete=True,
            recovery_required=False,
            last_error="Authorization: Bearer secret-token",
        )
    )
    await db_session.commit()

    response = await client.get("/api/ingest/radio-status")

    assert response.status_code == 200
    body = response.json()
    assert body["quieting_enabled"] is True
    completed_at = now.isoformat().replace("+00:00", "Z")
    assert body["transition"] == {
        "phase": "complete",
        "active": False,
        "recovery_required": False,
        "created_at": (now - timedelta(minutes=2)).isoformat().replace("+00:00", "Z"),
        "updated_at": completed_at,
        "completed_at": completed_at,
        "bluetooth": {
            "baseline": "on",
            "disable_attempted": True,
            "disable_verified": True,
            "restore_attempted": True,
            "restore_verified": True,
        },
        "hotspot": {
            "baseline": "off",
            "disable_attempted": False,
            "disable_verified": True,
            "restore_attempted": True,
            "restore_verified": True,
        },
        "obd_logger": {
            "quiesce_capable": True,
            "quiesce_attempted": True,
            "quiesce_verified": True,
            "resume_attempted": True,
            "resume_verified": True,
        },
        # Which side proved the baseline, and when the unit last spoke. Three timestamps
        # and one enum: no device address, no token, no capsule reference.
        "restore_evidence_source": None,
        "unit_reported_at": None,
        "unit_sleep_reported_at": None,
    }
    assert "secret" not in response.text


async def test_radio_status_prefers_an_active_transition_over_newer_history(client, db_session):
    now = datetime.now(UTC)
    common = {
        "trigger": "auto",
        "updated_at": now,
        "heartbeat_at": now,
        "lease_expires_at": now + timedelta(minutes=1),
        "device_address": "unit:5555",
        "transport_host": "unit",
        "lease_owner": "owner",
    }
    db_session.add_all(
        [
            IngestRadioTransition(
                **common,
                transition_id="00000000-0000-0000-0000-000000000002",
                phase="ingesting",
                active=True,
                created_at=now - timedelta(minutes=2),
            ),
            IngestRadioTransition(
                **{**common, "lease_owner": "finished-owner"},
                transition_id="00000000-0000-0000-0000-000000000003",
                phase="complete",
                active=False,
                created_at=now - timedelta(minutes=1),
                completed_at=now - timedelta(minutes=1),
            ),
        ]
    )
    await db_session.commit()

    response = await client.get("/api/ingest/radio-status")

    assert response.status_code == 200
    assert response.json()["transition"]["active"] is True
    assert response.json()["transition"]["phase"] == "ingesting"
