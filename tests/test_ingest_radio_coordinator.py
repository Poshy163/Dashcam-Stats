"""Durable logger quiescence and radio-transition safety contracts."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar

import pytest
from sqlalchemy import update

from app.core.process_lock import try_acquire
from app.db.models import IngestRadioTransition
from app.db.session import session_scope
from app.ingest import obd_control, obd_transfer, radio_coordinator


def _ack(**overrides) -> dict:
    value = {
        "schema_version": 1,
        "request_id": "request-1",
        "state": "ready",
        "ready_at_utc": "2026-08-30T01:02:03Z",
        "drive_id": None,
        "last_sample_at_utc": None,
        "bundle_filename": None,
        "bundle_sha256": None,
        "error": None,
    }
    value.update(overrides)
    return value


def test_ack_v1_is_exact_and_correlated():
    parsed = obd_control.parse_ack(json.dumps(_ack()), "request-1")
    assert parsed.ready
    assert parsed.request_id == "request-1"

    with pytest.raises(obd_control.LoggerControlError, match="schema v1"):
        obd_control.parse_ack(json.dumps({**_ack(), "unexpected": True}), "request-1")
    with pytest.raises(obd_control.LoggerControlError, match="active request"):
        obd_control.parse_ack(json.dumps(_ack()), "other-request")


def test_failed_ack_requires_a_bounded_redacted_error():
    parsed = obd_control.parse_ack(
        json.dumps(_ack(state="failed", error="Authorization: Bearer secret-value")),
        "request-1",
    )
    assert parsed.state == "failed"
    assert "secret-value" not in (parsed.error or "")

    with pytest.raises(obd_control.LoggerControlError, match="did not include an error"):
        obd_control.parse_ack(json.dumps(_ack(state="failed")), "request-1")


async def test_quiesce_timeout_is_bounded_and_never_accepts_a_missing_ack(monkeypatch):
    request_body = ""

    async def shell(_address, command, **_kwargs):
        nonlocal request_body
        if "printf resumed" in command:
            return "resumed"
        if "printf '%s'" in command and "ingestion-request.json.partial" in command:
            marker = "printf '%s' '"
            request_body = command.split(marker, 1)[1].split("' >", 1)[0]
            return ""
        if "cat '" in command and "ingestion-request.json'" in command:
            return request_body
        if "ingestion-ack.json" in command and "head -c" in command:
            return ""
        return ""

    ticks = iter((0.0, 2.0))
    monkeypatch.setattr(obd_control.adb, "shell", shell)
    monkeypatch.setattr(obd_control, "_monotonic", lambda: next(ticks))

    with pytest.raises(obd_control.LoggerControlError, match="in time"):
        await obd_control.request_quiesce(
            "unit:5555",
            "/storage/card/app/obd/status.json",
            timeout_s=1,
            hold_s=10,
            request_id="request-1",
        )
    request = json.loads(request_body)
    requested = datetime.fromisoformat(request["requested_at_utc"].replace("Z", "+00:00"))
    deadline = datetime.fromisoformat(request["deadline_at_utc"].replace("Z", "+00:00"))
    assert (deadline - requested).total_seconds() == 10


async def test_quiesce_revalidation_requires_a_live_correlated_lease(monkeypatch):
    requested = datetime.now(UTC) - timedelta(seconds=1)
    deadline = requested + timedelta(seconds=570)
    request = json.dumps(
        {
            "schema_version": 1,
            "request_id": "request-1",
            "action": obd_control.REQUEST_ACTION,
            "requested_at_utc": requested.isoformat().replace("+00:00", "Z"),
            "deadline_at_utc": deadline.isoformat().replace("+00:00", "Z"),
        },
        separators=(",", ":"),
    )
    acknowledgement = json.dumps(_ack(), separators=(",", ":"))

    async def shell(_address, command, **_kwargs):
        if "ingestion-request.json" in command:
            return request
        if "ingestion-ack.json" in command:
            return acknowledgement
        return ""

    monkeypatch.setattr(obd_control.adb, "shell", shell)
    ack = await obd_control.verify_quiesce(
        "unit:5555",
        "/storage/card/app/obd/status.json",
        "request-1",
        minimum_remaining_s=510,
    )
    assert ack.ready and ack.request_id == "request-1"


async def test_quiesce_revalidation_rejects_a_lease_near_expiry(monkeypatch):
    requested = datetime.now(UTC) - timedelta(seconds=500)
    deadline = requested + timedelta(seconds=510)
    request = json.dumps(
        {
            "schema_version": 1,
            "request_id": "request-1",
            "action": obd_control.REQUEST_ACTION,
            "requested_at_utc": requested.isoformat().replace("+00:00", "Z"),
            "deadline_at_utc": deadline.isoformat().replace("+00:00", "Z"),
        },
        separators=(",", ":"),
    )

    async def shell(_address, command, **_kwargs):
        if "ingestion-request.json" in command:
            return request
        if "ingestion-ack.json" in command:
            return json.dumps(_ack(), separators=(",", ":"))
        return ""

    monkeypatch.setattr(obd_control.adb, "shell", shell)
    with pytest.raises(obd_control.LoggerControlError, match="does not cover radio recovery"):
        await obd_control.verify_quiesce(
            "unit:5555",
            "/storage/card/app/obd/status.json",
            "request-1",
            minimum_remaining_s=30,
        )


async def test_quiesce_renewal_atomically_replaces_same_request_without_resume_edge(
    monkeypatch,
):
    requested = datetime.now(UTC) - timedelta(seconds=10)
    deadline = requested + timedelta(seconds=570)
    request_body = json.dumps(
        {
            "schema_version": 1,
            "request_id": "request-1",
            "action": obd_control.REQUEST_ACTION,
            "requested_at_utc": requested.isoformat().replace("+00:00", "Z"),
            "deadline_at_utc": deadline.isoformat().replace("+00:00", "Z"),
        },
        separators=(",", ":"),
    )
    acknowledgement = json.dumps(_ack(), separators=(",", ":"))
    commands: list[str] = []

    async def shell(_address, command, **_kwargs):
        nonlocal request_body
        commands.append(command)
        if "ingestion-request.json.renewal.partial" in command:
            marker = "printf '%s' '"
            renewed = command.split(marker, 1)[1].split("' >", 1)[0]
            assert request_body in command
            request_body = renewed
            return renewed
        if "ingestion-ack.json" in command:
            return acknowledgement
        if "ingestion-request.json" in command:
            return request_body
        return ""

    monkeypatch.setattr(obd_control.adb, "shell", shell)

    ack = await obd_control.renew_quiesce(
        "unit:5555",
        "/storage/card/app/obd/status.json",
        "request-1",
        hold_s=570,
        minimum_remaining_s=510,
    )

    assert ack.ready and ack.request_id == "request-1"
    renewed = json.loads(request_body)
    renewed_requested = datetime.fromisoformat(renewed["requested_at_utc"].replace("Z", "+00:00"))
    renewed_deadline = datetime.fromisoformat(renewed["deadline_at_utc"].replace("Z", "+00:00"))
    assert (renewed_deadline - renewed_requested).total_seconds() == 570
    assert renewed["request_id"] == "request-1"
    assert not any(
        "rm -f '/storage/card/app/obd/control/ingestion-request.json'" in command
        for command in commands
    )


async def test_quiesce_renewal_refuses_to_recreate_a_replaced_request(monkeypatch):
    requested = datetime.now(UTC) - timedelta(seconds=10)
    deadline = requested + timedelta(seconds=570)

    def request(request_id: str) -> str:
        return json.dumps(
            {
                "schema_version": 1,
                "request_id": request_id,
                "action": obd_control.REQUEST_ACTION,
                "requested_at_utc": requested.isoformat().replace("+00:00", "Z"),
                "deadline_at_utc": deadline.isoformat().replace("+00:00", "Z"),
            },
            separators=(",", ":"),
        )

    reads = iter((request("request-1"), request("replacement")))
    writes = 0

    async def shell(_address, command, **_kwargs):
        nonlocal writes
        if "ingestion-request.json.renewal.partial" in command:
            writes += 1
            return ""
        if "ingestion-ack.json" in command:
            return json.dumps(_ack(), separators=(",", ":"))
        if "ingestion-request.json" in command:
            return next(reads)
        return ""

    monkeypatch.setattr(obd_control.adb, "shell", shell)

    with pytest.raises(obd_control.LoggerControlError, match="changed while"):
        await obd_control.renew_quiesce(
            "unit:5555",
            "/storage/card/app/obd/status.json",
            "request-1",
            hold_s=570,
            minimum_remaining_s=510,
        )
    assert writes == 0


async def test_quiesce_renewal_refuses_a_request_too_close_to_expiry(monkeypatch):
    requested = datetime.now(UTC) - timedelta(seconds=560)
    deadline = requested + timedelta(seconds=570)
    request = json.dumps(
        {
            "schema_version": 1,
            "request_id": "request-1",
            "action": obd_control.REQUEST_ACTION,
            "requested_at_utc": requested.isoformat().replace("+00:00", "Z"),
            "deadline_at_utc": deadline.isoformat().replace("+00:00", "Z"),
        },
        separators=(",", ":"),
    )
    writes = 0

    async def shell(_address, command, **_kwargs):
        nonlocal writes
        if "ingestion-request.json.renewal.partial" in command:
            writes += 1
            return ""
        if "ingestion-ack.json" in command:
            return json.dumps(_ack(), separators=(",", ":"))
        if "ingestion-request.json" in command:
            return request
        return ""

    monkeypatch.setattr(obd_control.adb, "shell", shell)

    with pytest.raises(obd_control.LoggerControlError, match="too close to expiry"):
        await obd_control.renew_quiesce(
            "unit:5555",
            "/storage/card/app/obd/status.json",
            "request-1",
            hold_s=570,
            minimum_remaining_s=510,
        )
    assert writes == 0


async def test_logger_status_keeps_only_bounded_capabilities_and_metrics(monkeypatch):
    body = json.dumps(
        {
            "schema_version": 2,
            "state": "parked",
            "ownership_enabled": True,
            "ingestion_request_id": "request-1",
            "last_sample_at_utc": "2026-08-30T01:02:03Z",
            "capabilities": [obd_control.CAPABILITY],
            "metrics": {"commands_requested": 4, "queue_depth": 0},
            "raw_payload": "must not escape",
        }
    )

    async def shell(*_args, **_kwargs):
        return body

    monkeypatch.setattr(obd_transfer.adb, "shell", shell)
    status = await obd_transfer.read_logger_status("unit", "/safe/status.json")
    assert status == {
        "schema_version": 2,
        "state": "parked",
        "ownership_enabled": True,
        "ingestion_request_id": "request-1",
        "last_sample_at_utc": "2026-08-30T01:02:03Z",
        "capabilities": [obd_control.CAPABILITY],
        "metrics": {"commands_requested": 4, "queue_depth": 0},
    }
    assert obd_control.supports_quiesce(status)


@pytest.mark.parametrize(
    ("reply", "authoritative_absence"),
    [
        (obd_transfer._STATUS_NOT_INSTALLED, True),
        (obd_transfer._STATUS_INSTALLED_MISSING, False),
        (obd_transfer._STATUS_PACKAGE_CHECK_FAILED, False),
        ("", False),
    ],
)
async def test_missing_status_requires_positive_package_absence(
    monkeypatch, reply, authoritative_absence
):
    async def shell(*_args, **_kwargs):
        return reply

    monkeypatch.setattr(obd_transfer.adb, "shell", shell)
    status = await obd_transfer.read_logger_status("unit", "/safe/status.json")
    assert (status is None) is authoritative_absence
    if not authoritative_absence:
        assert status is not None and status["state"] == "status_unavailable"


class FakeController:
    instances: ClassVar[list[FakeController]] = []
    bluetooth_ok = True
    hotspot_ok = True
    watchdog_ok = True
    boot_id = "a" * 32 + "@01234567-89ab-cdef-0123-456789abcdef"
    remove_ok = True
    on_remove = None
    recovery_plan = radio_coordinator.radios.HotspotRecoveryPlan(
        radio_coordinator.radios.HOTSPOT_RESTORE_EXACT,
        ("CarSpot", "roadtrip99"),
    )

    def __init__(
        self,
        address: str,
        *,
        watchdog_deadline_s: int,
        report: object | None = None,
    ) -> None:
        self.address = address
        self.watchdog_deadline_s = watchdog_deadline_s
        self.report = report
        self.calls: list[str] = []
        self.released = False
        type(self).instances.append(self)

    def claim(self) -> None:
        self.calls.append("claim")

    async def restore_bluetooth(self, baseline: str) -> bool:
        self.calls.append(f"bluetooth:{baseline}")
        return type(self).bluetooth_ok

    async def restore_hotspot(
        self,
        baseline: str,
        config,
        restore_mode=None,
        *,
        expected_interface=None,
    ) -> bool:
        self.calls.append(f"hotspot:{baseline}")
        if baseline == "on":
            if restore_mode == radio_coordinator.radios.HOTSPOT_RESTORE_BLUETOOTH_REARM:
                assert config is None
                assert expected_interface == "ap0"
            else:
                assert config == ("CarSpot", "roadtrip99")
                assert restore_mode == radio_coordinator.radios.HOTSPOT_RESTORE_EXACT
        return type(self).hotspot_ok

    async def read_hotspot_recovery_plan(self, _path: str):
        self.calls.append("read-capsule")
        return type(self).recovery_plan

    async def read_hotspot_capsule(self, _path: str):
        self.calls.append("read-capsule")
        return "CarSpot", "roadtrip99"

    async def remove_hotspot_capsule(self, _path: str) -> bool:
        self.calls.append("remove-capsule")
        if type(self).on_remove is not None:
            await type(self).on_remove()
        return type(self).remove_ok

    async def stand_down_watchdog(self) -> bool:
        self.calls.append("stand-down")
        return True

    async def watchdog_healthy(self) -> bool:
        self.calls.append("watchdog-health")
        return type(self).watchdog_ok

    async def disable_bluetooth(self, *, before_change=None) -> bool:
        self.calls.append("watchdog-bluetooth")
        if before_change is not None:
            await before_change()
        self.calls.append("disable-bluetooth")
        return True

    async def disable_hotspot(self, *, before_change=None) -> bool:
        self.calls.append("watchdog-hotspot")
        if before_change is not None:
            await before_change()
        self.calls.append("disable-hotspot")
        return True

    async def release(self) -> None:
        self.released = True


@pytest.fixture
def fake_controller(monkeypatch):
    FakeController.instances = []
    FakeController.bluetooth_ok = True
    FakeController.hotspot_ok = True
    FakeController.watchdog_ok = True
    FakeController.boot_id = "a" * 32 + "@01234567-89ab-cdef-0123-456789abcdef"
    FakeController.remove_ok = True
    FakeController.on_remove = None
    FakeController.recovery_plan = radio_coordinator.radios.HotspotRecoveryPlan(
        radio_coordinator.radios.HOTSPOT_RESTORE_EXACT,
        ("CarSpot", "roadtrip99"),
    )
    monkeypatch.setattr(radio_coordinator.radios, "RadioController", FakeController)

    async def boot_id(_address):
        return FakeController.boot_id

    monkeypatch.setattr(radio_coordinator.radios, "read_device_boot_id", boot_id)
    return FakeController


async def test_logger_lease_keeps_bounded_radio_recovery_headroom(
    db_session, fake_controller, monkeypatch
):
    transition = await radio_coordinator.begin(
        trigger="manual",
        address="unit:5555",
        logger_status=None,
        logger_status_path="/safe/status.json",
        watchdog_deadline_s=9999,
    )
    assert transition.watchdog_deadline_s == radio_coordinator.MAX_WATCHDOG_DEADLINE_S == 480

    async def request(_address, _path, **kwargs):
        assert kwargs["timeout_s"] == obd_control.DEFAULT_TIMEOUT_S
        assert kwargs["hold_s"] == 570
        return obd_control.LoggerAck(
            request_id=kwargs["request_id"],
            state="ready",
            ready_at_utc="2026-08-30T01:02:03Z",
            drive_id=None,
            last_sample_at_utc=None,
            bundle_filename=None,
            bundle_sha256=None,
            error=None,
        )

    async def resume(_address, _path):
        return True

    monkeypatch.setattr(obd_control, "request_quiesce", request)
    monkeypatch.setattr(obd_control, "resume_logger", resume)
    assert (await transition.prepare_logger()).ready
    assert await transition.restore()


async def test_heartbeat_renews_the_same_logger_hold_until_restoration(
    db_session, fake_controller, monkeypatch
):
    renewed = asyncio.Event()
    request_ids: list[str] = []

    async def request(_address, _path, **kwargs):
        request_ids.append(kwargs["request_id"])
        return obd_control.LoggerAck(
            request_id=kwargs["request_id"],
            state="ready",
            ready_at_utc="2026-08-30T01:02:03Z",
            drive_id=None,
            last_sample_at_utc=None,
            bundle_filename=None,
            bundle_sha256=None,
            error=None,
        )

    async def renew(_address, _path, request_id, *, hold_s, minimum_remaining_s):
        assert request_id == request_ids[0]
        assert hold_s == 210
        assert minimum_remaining_s == 150
        renewed.set()
        return obd_control.LoggerAck(
            request_id=request_id,
            state="ready",
            ready_at_utc="2026-08-30T01:02:03Z",
            drive_id=None,
            last_sample_at_utc=None,
            bundle_filename=None,
            bundle_sha256=None,
            error=None,
        )

    async def resume(_address, _path):
        return True

    monkeypatch.setattr(radio_coordinator, "HEARTBEAT_INTERVAL_S", 0.001)
    monkeypatch.setattr(radio_coordinator, "LOGGER_QUIESCE_RENEW_INTERVAL_S", 0.0)
    monkeypatch.setattr(obd_control, "request_quiesce", request)
    monkeypatch.setattr(obd_control, "renew_quiesce", renew)
    monkeypatch.setattr(obd_control, "resume_logger", resume)
    transition = await radio_coordinator.begin(
        trigger="manual",
        address="unit:5555",
        logger_status=None,
        logger_status_path="/safe/status.json",
        watchdog_deadline_s=120,
    )

    assert (await transition.prepare_logger()).ready
    await asyncio.wait_for(renewed.wait(), timeout=1)
    assert await transition.restore()


async def test_transient_adb_logger_renewal_retries_without_cancelling_transfer(
    db_session, fake_controller, monkeypatch
):
    cancelled = asyncio.Event()
    recovered = asyncio.Event()
    attempts = 0

    async def request(_address, _path, **kwargs):
        return obd_control.LoggerAck(
            request_id=kwargs["request_id"],
            state="ready",
            ready_at_utc="2026-08-30T01:02:03Z",
            drive_id=None,
            last_sample_at_utc=None,
            bundle_filename=None,
            bundle_sha256=None,
            error=None,
        )

    async def renew(_address, _path, request_id, *, hold_s, minimum_remaining_s):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise radio_coordinator.adb.AdbError("temporary control-channel timeout")
        recovered.set()
        return obd_control.LoggerAck(
            request_id=request_id,
            state="ready",
            ready_at_utc="2026-08-30T01:02:03Z",
            drive_id=None,
            last_sample_at_utc=None,
            bundle_filename=None,
            bundle_sha256=None,
            error=None,
        )

    async def resume(_address, _path):
        return True

    monkeypatch.setattr(radio_coordinator, "HEARTBEAT_INTERVAL_S", 0.001)
    monkeypatch.setattr(radio_coordinator, "LOGGER_QUIESCE_RENEW_INTERVAL_S", 0.0)
    monkeypatch.setattr(obd_control, "request_quiesce", request)
    monkeypatch.setattr(obd_control, "renew_quiesce", renew)
    monkeypatch.setattr(obd_control, "resume_logger", resume)
    transition = await radio_coordinator.begin(
        trigger="manual",
        address="unit:5555",
        logger_status=None,
        logger_status_path="/safe/status.json",
        watchdog_deadline_s=120,
        lease_loss_callback=cancelled.set,
    )

    assert (await transition.prepare_logger()).ready
    await asyncio.wait_for(recovered.wait(), timeout=1)
    assert attempts >= 2
    assert not cancelled.is_set()
    assert not transition.lease_lost
    assert await transition.restore()


@pytest.mark.parametrize(
    ("bluetooth", "hotspot", "bt_attempted", "ap_attempted", "expected"),
    [
        (
            "on",
            "on",
            True,
            True,
            ["bluetooth:on", "read-capsule", "hotspot:on", "stand-down"],
        ),
        ("on", "off", True, False, ["bluetooth:on", "hotspot:off", "stand-down"]),
        ("off", "on", False, True, ["read-capsule", "hotspot:on", "stand-down"]),
        ("off", "off", False, False, []),
    ],
)
async def test_all_four_radio_baselines_restore_exactly(
    db_session,
    fake_controller,
    bluetooth,
    hotspot,
    bt_attempted,
    ap_attempted,
    expected,
):
    transition = await radio_coordinator.begin(
        trigger="manual",
        address="unit:5555",
        logger_status=None,
        logger_status_path="/safe/status.json",
        watchdog_deadline_s=120,
    )
    restore_ref = (
        f"/data/local/tmp/.dashcam_analyser_hotspot_{transition.transition_id}.json"
        if hotspot == "on"
        else None
    )
    await transition.checkpoint(
        bluetooth_before=bluetooth,
        hotspot_before=hotspot,
        bluetooth_disable_attempted=bt_attempted,
        hotspot_disable_attempted=ap_attempted,
        hotspot_restore_ref=restore_ref,
    )

    assert await transition.restore()
    calls = [
        call for call in transition.controller.calls if call not in {"claim", "remove-capsule"}
    ]
    assert calls == expected
    if restore_ref:
        assert "remove-capsule" in transition.controller.calls

    async with session_scope() as session:
        row = await session.get(IngestRadioTransition, transition.id)
        assert row is not None and not row.active
        assert row.phase == radio_coordinator.TransitionPhase.COMPLETE.value


async def test_database_constraint_prevents_two_radio_owners(db_session, fake_controller):
    first = await radio_coordinator.begin(
        trigger="manual",
        address="unit:5555",
        logger_status=None,
        logger_status_path="/safe/status.json",
        watchdog_deadline_s=120,
    )
    with pytest.raises(radio_coordinator.TransitionBusy):
        await radio_coordinator.begin(
            trigger="auto",
            address="unit:5555",
            logger_status=None,
            logger_status_path="/safe/status.json",
            watchdog_deadline_s=120,
        )
    assert await first.restore()

    second = await radio_coordinator.begin(
        trigger="auto",
        address="unit:5555",
        logger_status=None,
        logger_status_path="/safe/status.json",
        watchdog_deadline_s=120,
    )
    assert await second.restore()


def test_process_fence_blocks_a_separate_os_process_until_release(app_config):
    path = app_config.data_dir / "cross-process-radio-test.lock"
    lock = try_acquire(path)
    assert lock is not None
    backend = Path(__file__).resolve().parents[1] / "backend"
    script = (
        "import sys; from pathlib import Path; "
        "sys.path.insert(0, sys.argv[2]); "
        "from app.core.process_lock import try_acquire; "
        "lock = try_acquire(Path(sys.argv[1])); "
        "sys.exit(23 if lock is None else 0)"
    )
    blocked = subprocess.run(
        [sys.executable, "-c", script, str(path), str(backend)],
        check=False,
        capture_output=True,
    )
    assert blocked.returncode == 23

    lock.release()
    acquired = subprocess.run(
        [sys.executable, "-c", script, str(path), str(backend)],
        check=False,
        capture_output=True,
    )
    assert acquired.returncode == 0


async def test_heartbeat_failure_signals_cancellation_and_fences_expired_adoption(
    db_session, fake_controller, monkeypatch
):
    cancelled = asyncio.Event()
    original_checkpoint = radio_coordinator.RadioTransition.checkpoint

    async def fail_heartbeat(self, phase=None, **values):
        if asyncio.current_task() is self._heartbeat_task:
            raise RuntimeError("database renewal failed")
        return await original_checkpoint(self, phase, **values)

    monkeypatch.setattr(radio_coordinator, "HEARTBEAT_INTERVAL_S", 0.001)
    monkeypatch.setattr(radio_coordinator.RadioTransition, "checkpoint", fail_heartbeat)
    transition = await radio_coordinator.begin(
        trigger="manual",
        address="unit:5555",
        logger_status=None,
        logger_status_path="/safe/status.json",
        watchdog_deadline_s=120,
        lease_loss_callback=cancelled.set,
    )

    error = await asyncio.wait_for(transition.wait_for_lease_loss(), timeout=1)
    assert cancelled.is_set()
    assert transition.lease_lost
    assert "database renewal failed" in str(error)

    async with session_scope() as session:
        await session.execute(
            update(IngestRadioTransition)
            .where(IngestRadioTransition.id == transition.id)
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    assert not await radio_coordinator.reconcile_pending(address="unit:5555")
    assert transition.process_fence.held

    # Lease loss cancels data movement, not safety restoration. The original owner keeps
    # its OS fence and may still return the device to its exact baseline.
    assert await transition.restore()
    assert not transition.process_fence.held
    async with session_scope() as session:
        row = await session.get(IngestRadioTransition, transition.id)
        assert row is not None and not row.active
        assert row.phase == radio_coordinator.TransitionPhase.FAILED.value


async def test_watchdog_health_loss_signals_cancellation(db_session, fake_controller, monkeypatch):
    cancelled = asyncio.Event()
    fake_controller.watchdog_ok = False
    monkeypatch.setattr(radio_coordinator, "HEARTBEAT_INTERVAL_S", 0.001)
    transition = await radio_coordinator.begin(
        trigger="manual",
        address="unit:5555",
        logger_status=None,
        logger_status_path="/safe/status.json",
        watchdog_deadline_s=120,
        lease_loss_callback=cancelled.set,
    )

    error = await asyncio.wait_for(transition.wait_for_lease_loss(), timeout=1)
    assert cancelled.is_set()
    assert transition.lease_lost
    assert "detached on-unit radio recovery watchdog was lost" in str(error)
    assert "watchdog-health" in transition.controller.calls
    assert transition.process_fence.held

    # The failed independent recovery proof cancels data movement, but the owner
    # still holds its fence and must finish the normal safety-restoration path.
    assert await transition.restore()
    assert not transition.process_fence.held


async def test_expired_transition_is_reconciled_after_process_restart(db_session, fake_controller):
    transition = await radio_coordinator.begin(
        trigger="manual",
        address="unit:5555",
        logger_status=None,
        logger_status_path="/safe/status.json",
        watchdog_deadline_s=120,
    )
    await transition.checkpoint(
        bluetooth_before="on",
        hotspot_before="off",
        bluetooth_disable_attempted=True,
        bluetooth_disable_verified=True,
    )
    transition_id = transition.id
    await transition.close()  # process disappeared without terminalising its DB row
    async with session_scope() as session:
        await session.execute(
            update(IngestRadioTransition)
            .where(IngestRadioTransition.id == transition_id)
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )

    assert await radio_coordinator.reconcile_pending(address="unit:5555")
    recovered = FakeController.instances[-1]
    assert recovered.calls[1:3] == ["bluetooth:on", "hotspot:off"]
    async with session_scope() as session:
        row = await session.get(IngestRadioTransition, transition_id)
        assert row is not None and not row.active and not row.recovery_required
        assert row.phase == radio_coordinator.TransitionPhase.FAILED.value


async def test_expired_zlink_rearm_transition_recovers_from_credential_free_capsule(
    db_session, fake_controller, monkeypatch
):
    transition = await radio_coordinator.begin(
        trigger="manual",
        address="unit:5555",
        logger_status=None,
        logger_status_path="/safe/status.json",
        watchdog_deadline_s=120,
    )
    restore_ref = (
        f"{radio_coordinator.radios.HOTSPOT_CAPSULE_PREFIX}{transition.transition_id}.json"
    )
    await transition.checkpoint(
        bluetooth_before="on",
        hotspot_before="on",
        hotspot_interface="ap0",
        bluetooth_disable_attempted=True,
        bluetooth_disable_verified=True,
        hotspot_disable_attempted=True,
        hotspot_disable_verified=True,
        hotspot_restore_ref=restore_ref,
        logger_request_id="request-1",
    )
    transition_id = transition.id
    await transition.close()
    async with session_scope() as session:
        await session.execute(
            update(IngestRadioTransition)
            .where(IngestRadioTransition.id == transition_id)
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )

    FakeController.recovery_plan = radio_coordinator.radios.HotspotRecoveryPlan(
        radio_coordinator.radios.HOTSPOT_RESTORE_BLUETOOTH_REARM
    )

    async def resume_logger(_address, _path):
        FakeController.instances[-1].calls.append("resume-logger")
        return True

    monkeypatch.setattr(radio_coordinator.obd_control, "resume_logger", resume_logger)
    assert await radio_coordinator.reconcile_pending(address="unit:5555")
    recovered = FakeController.instances[-1]
    assert recovered.calls[1:7] == [
        "bluetooth:on",
        "read-capsule",
        "hotspot:on",
        "stand-down",
        "resume-logger",
        "remove-capsule",
    ]
    async with session_scope() as session:
        row = await session.get(IngestRadioTransition, transition_id)
        assert row is not None and not row.active and not row.recovery_required
        assert row.logger_resume_verified and row.hotspot_restore_ref is None


async def test_expired_transition_follows_dhcp_change_only_with_same_stable_device_identity(
    db_session, fake_controller, monkeypatch
):
    transition = await radio_coordinator.begin(
        trigger="manual",
        address="old-address:5555",
        logger_status=None,
        logger_status_path="/safe/status.json",
        watchdog_deadline_s=120,
    )
    transition_id = transition.id
    await transition.close()
    async with session_scope() as session:
        await session.execute(
            update(IngestRadioTransition)
            .where(IngestRadioTransition.id == transition_id)
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )

    async def same_boot(address):
        assert address == "new-address:5555"
        return "a" * 32 + "@11111111-2222-3333-4444-555555555555"

    monkeypatch.setattr(radio_coordinator.radios, "read_device_boot_id", same_boot)
    assert await radio_coordinator.reconcile_pending(address="new-address:5555")
    assert FakeController.instances[-1].address == "new-address:5555"
    async with session_scope() as session:
        row = await session.get(IngestRadioTransition, transition_id)
        assert row is not None and row.device_address == "new-address:5555"
        assert row.transport_host == "new-address"
        assert row.device_boot_id == "a" * 32 + "@11111111-2222-3333-4444-555555555555"


async def test_expired_transition_refuses_ip_reuse_with_different_boot_identity(
    db_session, fake_controller, monkeypatch
):
    transition = await radio_coordinator.begin(
        trigger="manual",
        address="unit:5555",
        logger_status=None,
        logger_status_path="/safe/status.json",
        watchdog_deadline_s=120,
    )
    transition_id = transition.id
    await transition.close()
    async with session_scope() as session:
        await session.execute(
            update(IngestRadioTransition)
            .where(IngestRadioTransition.id == transition_id)
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )

    async def different_boot(_address):
        return "11111111-2222-3333-4444-555555555555"

    monkeypatch.setattr(radio_coordinator.radios, "read_device_boot_id", different_boot)
    assert not await radio_coordinator.reconcile_pending(address="unit:5555")
    async with session_scope() as session:
        row = await session.get(IngestRadioTransition, transition_id)
        assert row is not None and row.active


async def test_failed_restore_stays_active_for_later_recovery(db_session, fake_controller):
    transition = await radio_coordinator.begin(
        trigger="manual",
        address="unit:5555",
        logger_status=None,
        logger_status_path="/safe/status.json",
        watchdog_deadline_s=120,
    )
    await transition.checkpoint(
        bluetooth_before="on",
        hotspot_before="off",
        bluetooth_disable_attempted=True,
    )
    FakeController.bluetooth_ok = False

    assert not await transition.restore(error="transfer cancelled")
    async with session_scope() as session:
        row = await session.get(IngestRadioTransition, transition.id)
        assert row is not None and row.active and row.recovery_required
        assert row.phase == radio_coordinator.TransitionPhase.RECOVERY_REQUIRED.value


async def test_exceptional_restore_is_terminal_before_its_fence_is_released(
    db_session, fake_controller, monkeypatch
):
    transition = await radio_coordinator.begin(
        trigger="manual",
        address="unit:5555",
        logger_status=None,
        logger_status_path="/safe/status.json",
        watchdog_deadline_s=120,
    )
    await transition.checkpoint(
        bluetooth_before="on",
        hotspot_before="off",
        bluetooth_disable_attempted=True,
    )
    original_checkpoint = radio_coordinator.RadioTransition.checkpoint
    failed = False

    async def fail_verified_checkpoint(self, phase=None, **values):
        nonlocal failed
        if self is transition and "bluetooth_restore_verified" in values and not failed:
            failed = True
            raise RuntimeError("restore checkpoint failed")
        return await original_checkpoint(self, phase, **values)

    monkeypatch.setattr(
        radio_coordinator.RadioTransition,
        "checkpoint",
        fail_verified_checkpoint,
    )

    with pytest.raises(RuntimeError, match="restore checkpoint failed"):
        await transition.restore()
    calls_at_release = list(transition.controller.calls)
    assert not transition.process_fence.held

    # The puller's outer cleanup may reach restore after the inner path raised. A closed
    # transition must return its terminal failure without touching the controller again.
    assert not await transition.restore()
    assert transition.controller.calls == calls_at_release
    async with session_scope() as session:
        row = await session.get(IngestRadioTransition, transition.id)
        assert row is not None and row.active and row.recovery_required
        assert row.phase == radio_coordinator.TransitionPhase.RECOVERY_REQUIRED.value
        assert row.lease_expires_at <= datetime.now(UTC)


async def test_cancelled_restore_releases_local_owner_and_expires_for_recovery(
    db_session, fake_controller
):
    transition = await radio_coordinator.begin(
        trigger="manual",
        address="unit:5555",
        logger_status=None,
        logger_status_path="/safe/status.json",
        watchdog_deadline_s=120,
    )
    await transition.checkpoint(
        bluetooth_before="on",
        hotspot_before="off",
        bluetooth_disable_attempted=True,
    )

    async def blocked(_baseline):
        await asyncio.sleep(60)
        return True

    transition.controller.restore_bluetooth = blocked
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(transition.restore(), timeout=0.01)
    await transition.require_recovery("radio restore timed out")

    assert transition.controller.released
    async with session_scope() as session:
        row = await session.get(IngestRadioTransition, transition.id)
        assert row is not None and row.active and row.recovery_required
        assert row.lease_expires_at <= datetime.now(UTC)


async def test_radio_quieting_refuses_to_run_before_obd_durability_checkpoint(
    db_session, fake_controller
):
    transition = await radio_coordinator.begin(
        trigger="manual",
        address="unit:5555",
        logger_status=None,
        logger_status_path="/safe/status.json",
        watchdog_deadline_s=120,
    )

    async def unexpected_capture():
        raise AssertionError("radio state must not be read before the OBD checkpoint")

    transition.controller.capture = unexpected_capture
    with pytest.raises(radio_coordinator.RadioTransitionError, match="OBD transfer durability"):
        await transition.capture_and_quiet()

    async with session_scope() as session:
        row = await session.get(IngestRadioTransition, transition.id)
        assert row is not None
        assert not row.obd_transfer_complete
        assert not row.bluetooth_disable_attempted and not row.hotspot_disable_attempted
    assert await transition.restore(error="quieting refused")


async def test_radio_quieting_revalidates_obd_lease_after_watchdog_and_checkpoints(
    db_session, fake_controller, monkeypatch
):
    transition = await radio_coordinator.begin(
        trigger="manual",
        address="unit:5555",
        logger_status=None,
        logger_status_path="/safe/status.json",
        watchdog_deadline_s=120,
    )
    await transition.checkpoint(
        logger_request_id="request-1",
        obd_transfer_complete=True,
    )

    async def capture():
        return radio_coordinator.radios.RadioSnapshot(
            bluetooth="on",
            hotspot="off",
        )

    async def expired(
        _address,
        _path,
        _request_id,
        *,
        hold_s,
        minimum_remaining_s,
    ):
        assert hold_s == 210
        assert minimum_remaining_s == 150
        async with session_scope() as session:
            row = await session.get(IngestRadioTransition, transition.id)
            assert row is not None and row.obd_transfer_complete
            assert row.bluetooth_before == "on" and row.hotspot_before == "off"
            assert row.phase == radio_coordinator.TransitionPhase.DISABLING_RADIOS.value
            assert row.bluetooth_disable_attempted and not row.hotspot_disable_attempted
        assert transition.controller.calls[-1] == "watchdog-bluetooth"
        raise obd_control.LoggerControlError("lease expired during the delayed OBD copy")

    async def resume(_address, _path):
        return True

    transition.controller.capture = capture
    monkeypatch.setattr(obd_control, "renew_quiesce", expired)
    monkeypatch.setattr(obd_control, "resume_logger", resume)

    with pytest.raises(radio_coordinator.RadioTransitionError, match="no longer covers"):
        await transition.capture_and_quiet()
    assert "disable-bluetooth" not in transition.controller.calls
    assert await transition.restore(error="quieting refused")

    async with session_scope() as session:
        row = await session.get(IngestRadioTransition, transition.id)
        assert row is not None and not row.active
        assert row.logger_resume_verified


async def test_radio_quieting_refuses_a_head_unit_reboot_during_obd_preparation(
    db_session, fake_controller
):
    transition = await radio_coordinator.begin(
        trigger="manual",
        address="unit:5555",
        logger_status=None,
        logger_status_path="/safe/status.json",
        watchdog_deadline_s=120,
    )
    await transition.mark_obd_transfer_complete()

    async def capture():
        return radio_coordinator.radios.RadioSnapshot(
            bluetooth="on",
            hotspot="off",
        )

    transition.controller.capture = capture
    FakeController.boot_id = "a" * 32 + "@fedcba98-7654-3210-fedc-ba9876543210"

    with pytest.raises(radio_coordinator.RadioTransitionError, match="restarted"):
        await transition.capture_and_quiet()
    assert transition.controller.calls[-1] == "watchdog-bluetooth"
    assert "disable-bluetooth" not in transition.controller.calls
    assert await transition.restore(error="quieting refused")


async def test_capsule_is_erased_when_its_durable_checkpoint_fails(
    db_session, fake_controller, monkeypatch
):
    transition = await radio_coordinator.begin(
        trigger="manual",
        address="unit:5555",
        logger_status=None,
        logger_status_path="/safe/status.json",
        watchdog_deadline_s=120,
    )
    capsule = f"{radio_coordinator.radios.HOTSPOT_CAPSULE_PREFIX}{transition.transition_id}.json"

    async def capture():
        return radio_coordinator.radios.RadioSnapshot(
            bluetooth="on",
            hotspot="on",
            hotspot_interface="ap0",
            transport_interface="wlan0",
            hotspot_config=("CarSpot", "roadtrip99"),
        )

    async def persist(_transition_id, _config):
        transition.controller.calls.append("persist-capsule")
        return capsule

    transition.controller.capture = capture
    transition.controller.persist_hotspot_capsule = persist
    await transition.mark_obd_transfer_complete()
    original_checkpoint = radio_coordinator.RadioTransition.checkpoint
    snapshot_failed = False

    async def fail_snapshot(self, phase=None, **values):
        nonlocal snapshot_failed
        if (
            self is transition
            and values.get("hotspot_restore_ref") == capsule
            and not snapshot_failed
        ):
            snapshot_failed = True
            raise RuntimeError("database checkpoint failed")
        return await original_checkpoint(self, phase, **values)

    monkeypatch.setattr(radio_coordinator.RadioTransition, "checkpoint", fail_snapshot)
    FakeController.remove_ok = False
    with pytest.raises(RuntimeError, match="checkpoint failed"):
        await transition.capture_and_quiet()
    assert transition.controller.calls[-1] == "remove-capsule"
    assert await transition.restore(error="capture failed")
    async with session_scope() as session:
        row = await session.get(IngestRadioTransition, transition.id)
        assert row is not None and not row.active
        assert row.hotspot_restore_ref == capsule


async def test_startup_tracks_and_cleans_a_precheckpoint_capsule(db_session, fake_controller):
    transition = await radio_coordinator.begin(
        trigger="manual",
        address="unit:5555",
        logger_status=None,
        logger_status_path="/safe/status.json",
        watchdog_deadline_s=120,
    )
    transition_id = transition.id
    expected = f"{radio_coordinator.radios.HOTSPOT_CAPSULE_PREFIX}{transition.transition_id}.json"
    await transition.close()
    async with session_scope() as session:
        await session.execute(
            update(IngestRadioTransition)
            .where(IngestRadioTransition.id == transition_id)
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )

    assert await radio_coordinator.reconcile_pending(address="unit:5555")
    recovered = FakeController.instances[-1]
    assert "remove-capsule" in recovered.calls
    async with session_scope() as session:
        row = await session.get(IngestRadioTransition, transition_id)
        assert row is not None and not row.active
        assert row.hotspot_restore_ref is None
    assert expected.endswith(f"{transition.transition_id}.json")


async def test_transition_deactivates_before_capsule_cleanup_and_retries_cleanup_on_arrival(
    db_session, fake_controller, monkeypatch
):
    transition = await radio_coordinator.begin(
        trigger="manual",
        address="unit:5555",
        logger_status=None,
        logger_status_path="/safe/status.json",
        watchdog_deadline_s=120,
    )
    restore_ref = (
        f"{radio_coordinator.radios.HOTSPOT_CAPSULE_PREFIX}{transition.transition_id}.json"
    )
    await transition.checkpoint(
        bluetooth_before="on",
        hotspot_before="on",
        bluetooth_disable_attempted=True,
        hotspot_disable_attempted=True,
        hotspot_restore_ref=restore_ref,
        logger_request_id="request-1",
    )

    async def resume(_address, _path):
        transition.controller.calls.append("logger-resume")
        return True

    async def observe_inactive_row():
        async with session_scope() as session:
            row = await session.get(IngestRadioTransition, transition.id)
            transition.controller.calls.append(f"row-active:{row.active if row else 'missing'}")

    monkeypatch.setattr(radio_coordinator.obd_control, "resume_logger", resume)
    FakeController.on_remove = observe_inactive_row
    assert await transition.restore()
    calls = transition.controller.calls
    assert calls.index("hotspot:on") < calls.index("stand-down")
    assert calls.index("stand-down") < calls.index("logger-resume")
    assert calls.index("logger-resume") < calls.index("remove-capsule")
    assert "row-active:False" in calls

    # A crash/failure after deactivation leaves an inactive cleanup reference, not an
    # unrecoverable active transition. The next observed arrival clears it idempotently.
    second = await radio_coordinator.begin(
        trigger="manual",
        address="unit:5555",
        logger_status=None,
        logger_status_path="/safe/status.json",
        watchdog_deadline_s=120,
    )
    second_ref = f"{radio_coordinator.radios.HOTSPOT_CAPSULE_PREFIX}{second.transition_id}.json"
    await second.checkpoint(
        bluetooth_before="on",
        hotspot_before="on",
        bluetooth_disable_attempted=True,
        hotspot_disable_attempted=True,
        hotspot_restore_ref=second_ref,
    )
    FakeController.on_remove = None
    FakeController.remove_ok = False
    assert await second.restore()
    async with session_scope() as session:
        row = await session.get(IngestRadioTransition, second.id)
        assert row is not None and not row.active
        assert row.hotspot_restore_ref == second_ref

    FakeController.remove_ok = True
    assert await radio_coordinator.reconcile_pending(address="unit:5555")
    async with session_scope() as session:
        row = await session.get(IngestRadioTransition, second.id)
        assert row is not None and row.hotspot_restore_ref is None


async def test_unreadable_hotspot_still_fails_closed_without_zlink_opt_in(
    db_session, fake_controller, monkeypatch
):
    transition = await radio_coordinator.begin(
        trigger="manual",
        address="unit:5555",
        logger_status=None,
        logger_status_path="/safe/status.json",
        watchdog_deadline_s=120,
    )

    async def capture():
        return radio_coordinator.radios.RadioSnapshot(
            bluetooth="on",
            hotspot="on",
            hotspot_interface="ap0",
            transport_interface="wlan0",
        )

    async def unexpected_support(_address):
        raise AssertionError("Zlink must not be probed without explicit opt-in")

    transition.controller.capture = capture
    monkeypatch.setattr(
        radio_coordinator.radios,
        "supports_zlink_bluetooth_rearm",
        unexpected_support,
    )

    await transition.mark_obd_transfer_complete()
    with pytest.raises(radio_coordinator.RadioTransitionError, match="Zlink re-arm"):
        await transition.capture_and_quiet()

    async with session_scope() as session:
        row = await session.get(IngestRadioTransition, transition.id)
        assert row is not None
        assert row.bluetooth_before == "on" and row.hotspot_before == "on"
        assert not row.bluetooth_disable_attempted and not row.hotspot_disable_attempted
    assert await transition.restore(error="quieting refused")


async def test_opted_in_zlink_rearm_is_durable_before_both_radios_are_forced_off(
    db_session, fake_controller, monkeypatch
):
    transition = await radio_coordinator.begin(
        trigger="manual",
        address="unit:5555",
        logger_status=None,
        logger_status_path="/safe/status.json",
        watchdog_deadline_s=120,
        allow_zlink_rearm=True,
    )
    restore_ref = (
        f"{radio_coordinator.radios.HOTSPOT_CAPSULE_PREFIX}{transition.transition_id}.json"
    )

    async def capture():
        return radio_coordinator.radios.RadioSnapshot(
            bluetooth="on",
            hotspot="on",
            hotspot_interface="ap0",
            transport_interface="wlan0",
        )

    async def supported(_address):
        return True

    async def persist(_transition_id):
        transition.controller.calls.append("persist-rearm")
        return restore_ref

    async def disable_bluetooth(*, before_change=None):
        async with session_scope() as session:
            row = await session.get(IngestRadioTransition, transition.id)
            assert row is not None
            assert row.obd_transfer_complete
            assert row.bluetooth_before == "on" and row.hotspot_before == "on"
            assert row.hotspot_restore_ref == restore_ref
            assert row.bluetooth_disable_attempted
            assert not row.hotspot_disable_attempted
        assert before_change is not None
        await before_change()
        transition.controller.calls.append("disable-bluetooth")
        return True

    async def disable_hotspot(*, before_change=None):
        assert before_change is None
        async with session_scope() as session:
            row = await session.get(IngestRadioTransition, transition.id)
            assert row is not None
            assert row.obd_transfer_complete
            assert row.hotspot_restore_ref == restore_ref
            assert row.bluetooth_disable_verified
            assert row.hotspot_disable_attempted
        transition.controller.calls.append("disable-hotspot")
        return True

    transition.controller.capture = capture
    transition.controller.persist_bluetooth_rearm_capsule = persist
    transition.controller.disable_bluetooth = disable_bluetooth
    transition.controller.disable_hotspot = disable_hotspot
    monkeypatch.setattr(radio_coordinator.radios, "supports_zlink_bluetooth_rearm", supported)

    await transition.mark_obd_transfer_complete()
    await transition.capture_and_quiet()

    calls = transition.controller.calls
    assert calls.index("persist-rearm") < calls.index("disable-bluetooth")
    assert calls.index("disable-bluetooth") < calls.index("disable-hotspot")
    async with session_scope() as session:
        row = await session.get(IngestRadioTransition, transition.id)
        assert row is not None
        assert row.hotspot_restore_ref == restore_ref
        assert row.bluetooth_disable_verified and row.hotspot_disable_verified
    assert await transition.restore()


def test_hotspot_secret_is_not_part_of_the_persisted_schema():
    columns = set(IngestRadioTransition.__table__.columns.keys())
    assert "hotspot_restore_ref" in columns
    assert all("passphrase" not in name and "password" not in name for name in columns)
