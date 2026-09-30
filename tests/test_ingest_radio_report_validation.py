"""Unit reports must discharge actual obligations, not merely omit failures."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.db.models import IngestRadioTransition
from app.ingest.radio_coordinator import UnitRadioReport, apply_unit_report

TRANSITION_ID = "00000000-0000-0000-0000-000000000001"
TOKEN = "a" * 48


async def _seed(session, **overrides):
    now = datetime.now(UTC)
    values = {
        "transition_id": TRANSITION_ID,
        "trigger": "auto",
        "phase": "recovery_required",
        "active": True,
        "lease_owner": "test-owner",
        "lease_expires_at": now - timedelta(seconds=60),
        "device_address": "192.0.2.1:5555",
        "transport_host": "192.0.2.1",
        "bluetooth_before": "on",
        "hotspot_before": "on",
        "hotspot_interface": "wlan1",
        "bluetooth_disable_attempted": True,
        "hotspot_disable_attempted": True,
        "bluetooth_restore_verified": False,
        "hotspot_restore_verified": False,
        "recovery_required": True,
        "unit_report_token": TOKEN,
    }
    values.update(overrides)
    session.add(IngestRadioTransition(**values))
    await session.commit()


def _report(**overrides):
    values = {
        "transition_id": TRANSITION_ID,
        "token": TOKEN,
        "reason": "pre_sleep",
        "bluetooth": "1",
        "hotspot": "1",
        "interface": "wlan1",
    }
    values.update(overrides)
    return UnitRadioReport(**values)


async def _row(session):
    session.expire_all()
    return await session.scalar(
        select(IngestRadioTransition).where(IngestRadioTransition.transition_id == TRANSITION_ID)
    )


@pytest.mark.parametrize("radio", ["bluetooth", "hotspot"])
async def test_skipping_a_required_radio_keeps_recovery_debt(db_session, radio):
    await _seed(db_session)
    assert await apply_unit_report(_report(**{radio: "skip"}))
    row = await _row(db_session)
    assert row.active
    assert row.recovery_required
    assert row.phase == "recovery_required"
    assert row.completed_at is None
    assert row.unit_report_token == TOKEN
    assert getattr(row, f"{radio}_restore_verified") is False


@pytest.mark.parametrize("radio", ["bluetooth", "hotspot"])
async def test_unknown_report_is_not_restoration_evidence(db_session, radio):
    await _seed(db_session)
    assert not await apply_unit_report(_report(**{radio: "unknown"}))
    row = await _row(db_session)
    assert row.active and row.recovery_required
    assert row.unit_reported_at is None
    assert row.unit_report_token == TOKEN


@pytest.mark.parametrize("interface", ["", "wlan0", "ap9"])
async def test_hotspot_success_requires_the_captured_interface(db_session, interface):
    await _seed(db_session)
    assert await apply_unit_report(_report(interface=interface))
    row = await _row(db_session)
    assert row.bluetooth_restore_verified
    assert not row.hotspot_restore_verified
    assert row.active and row.recovery_required
    assert row.unit_report_token == TOKEN
    assert "hotspot" in row.last_error


async def test_verified_radios_do_not_release_an_unresumed_logger(db_session):
    await _seed(db_session, logger_request_id="paused-logger", logger_resume_verified=False)
    assert await apply_unit_report(_report())
    row = await _row(db_session)
    assert row.bluetooth_restore_verified and row.hotspot_restore_verified
    assert row.active and row.recovery_required
    assert not row.logger_resume_verified
    assert row.logger_request_id == "paused-logger"
    assert row.unit_report_token == TOKEN
    assert "OBD logger" in row.last_error


async def test_off_hotspot_still_needs_verification_after_bluetooth_restore(db_session):
    await _seed(db_session, hotspot_before="off", hotspot_disable_attempted=False)
    assert await apply_unit_report(_report(hotspot="skip", interface=""))
    row = await _row(db_session)
    assert row.active and row.recovery_required
    assert not row.hotspot_restore_verified


@pytest.mark.parametrize("baseline", ["off", "transport"])
async def test_genuinely_untouched_radios_can_be_skipped(db_session, baseline):
    await _seed(
        db_session,
        bluetooth_before="off",
        bluetooth_disable_attempted=False,
        hotspot_before=baseline,
        hotspot_disable_attempted=False,
    )
    assert await apply_unit_report(_report(bluetooth="skip", hotspot="skip", interface=""))
    row = await _row(db_session)
    assert not row.active and not row.recovery_required
    assert not row.bluetooth_restore_verified and not row.hotspot_restore_verified
    assert row.phase == "complete"


async def test_transport_ap_is_not_a_debt_after_bluetooth_restore(db_session):
    await _seed(db_session, hotspot_before="transport", hotspot_disable_attempted=False)
    assert await apply_unit_report(_report(hotspot="skip", interface=""))
    row = await _row(db_session)
    assert row.bluetooth_restore_verified
    assert not row.active and not row.recovery_required


async def test_matching_report_and_verified_logger_can_complete(db_session):
    await _seed(db_session, logger_request_id="paused-logger", logger_resume_verified=True)
    assert await apply_unit_report(_report())
    row = await _row(db_session)
    assert row.bluetooth_restore_verified and row.hotspot_restore_verified
    assert not row.active and not row.recovery_required
    assert row.completed_at is not None
    assert row.unit_report_token is None


async def test_live_owner_retains_its_lease_after_mismatched_report(db_session):
    expiry = datetime.now(UTC) + timedelta(seconds=60)
    await _seed(db_session, lease_expires_at=expiry, recovery_required=False)
    assert await apply_unit_report(_report(interface="wlan0"))
    row = await _row(db_session)
    assert row.active
    assert row.lease_expires_at == expiry
    assert not row.hotspot_restore_verified
    assert row.completed_at is None
