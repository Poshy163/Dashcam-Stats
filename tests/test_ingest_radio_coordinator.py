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
    boot_id = "a" * 32 + "@01234567-89ab-cdef-0123-456789abcdef"
    remove_ok = True
    on_remove = None

    def __init__(self, address: str, *, watchdog_deadline_s: int) -> None:
        self.address = address
        self.watchdog_deadline_s = watchdog_deadline_s
        self.calls: list[str] = []
        self.released = False
        type(self).instances.append(self)

    def claim(self) -> None:
        self.calls.append("claim")

    async def restore_bluetooth(self, baseline: str) -> bool:
        self.calls.append(f"bluetooth:{baseline}")
        return type(self).bluetooth_ok

    async def restore_hotspot(self, baseline: str, config) -> bool:
        self.calls.append(f"hotspot:{baseline}")
        if baseline == "on":
            assert config == ("CarSpot", "roadtrip99")
        return type(self).hotspot_ok

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

    async def release(self) -> None:
        self.released = True


@pytest.fixture
def fake_controller(monkeypatch):
    FakeController.instances = []
    FakeController.bluetooth_ok = True
    FakeController.hotspot_ok = True
    FakeController.boot_id = "a" * 32 + "@01234567-89ab-cdef-0123-456789abcdef"
    FakeController.remove_ok = True
    FakeController.on_remove = None
    monkeypatch.setattr(radio_coordinator.radios, "RadioController", FakeController)

    async def boot_id(_address):
        return FakeController.boot_id

    monkeypatch.setattr(radio_coordinator.radios, "read_device_boot_id", boot_id)
    return FakeController


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


async def test_safety_deadline_restores_baseline_without_releasing_ingest_fence(
    db_session, fake_controller
):
    transition = await radio_coordinator.begin(
        trigger="manual",
        address="unit:5555",
        logger_status=None,
        logger_status_path="/safe/status.json",
        watchdog_deadline_s=120,
    )

    assert await transition.restore_radio_baseline(error=RuntimeError("quiet deadline"))
    assert "remove-capsule" not in transition.controller.calls
    async with session_scope() as session:
        row = await session.get(IngestRadioTransition, transition.id)
        assert row is not None and row.active
        assert row.phase == radio_coordinator.TransitionPhase.INGESTING.value
    with pytest.raises(radio_coordinator.TransitionBusy):
        await radio_coordinator.begin(
            trigger="poller",
            address="unit:5555",
            logger_status=None,
            logger_status_path="/safe/status.json",
            watchdog_deadline_s=120,
        )

    assert await transition.restore()
    async with session_scope() as session:
        row = await session.get(IngestRadioTransition, transition.id)
        assert row is not None and not row.active
        assert row.phase == radio_coordinator.TransitionPhase.FAILED.value


async def test_post_deadline_crash_retains_capsule_for_startup_recovery(
    db_session, fake_controller
):
    transition = await radio_coordinator.begin(
        trigger="manual",
        address="unit:5555",
        logger_status=None,
        logger_status_path="/safe/status.json",
        watchdog_deadline_s=120,
    )
    restore_ref = f"/data/local/tmp/.dashcam_analyser_hotspot_{transition.transition_id}.json"
    await transition.checkpoint(
        bluetooth_before="on",
        hotspot_before="on",
        bluetooth_disable_attempted=True,
        hotspot_disable_attempted=True,
        hotspot_restore_ref=restore_ref,
    )
    assert await transition.restore_radio_baseline(error=RuntimeError("quiet deadline"))
    assert "remove-capsule" not in transition.controller.calls

    transition_id = transition.id
    await transition.close()  # process dies while the protected capsule still exists
    async with session_scope() as session:
        await session.execute(
            update(IngestRadioTransition)
            .where(IngestRadioTransition.id == transition_id)
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )

    assert await radio_coordinator.reconcile_pending(address="unit:5555")
    recovered = FakeController.instances[-1]
    assert "read-capsule" in recovered.calls
    assert "remove-capsule" in recovered.calls
    async with session_scope() as session:
        row = await session.get(IngestRadioTransition, transition_id)
        assert row is not None and not row.active and not row.recovery_required
        assert row.phase == radio_coordinator.TransitionPhase.FAILED.value


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


def test_hotspot_secret_is_not_part_of_the_persisted_schema():
    columns = set(IngestRadioTransition.__table__.columns.keys())
    assert "hotspot_restore_ref" in columns
    assert all("passphrase" not in name and "password" not in name for name in columns)
