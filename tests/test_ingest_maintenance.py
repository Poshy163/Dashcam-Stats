"""Maintenance must prove native cancellation; a boolean cannot extend a backup."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.ingest import adb, maintenance, puller, radios
from app.ingest.status import IngestStatus

BOOT = "01234567-1234-1234-1234-012345678901"
OTHER_BOOT = "01234567-1234-1234-1234-012345678902"
COUNTDOWN = (
    '<hierarchy><node package="com.android.systemui" '
    'resource-id="com.android.systemui:id/tv_countdown" '
    'class="android.widget.TextView" enabled="true" long-clickable="true" '
    'text="300 seconds until sleep" bounds="[600,300][1000,500]" /></hierarchy>'
)
NORMAL = '<hierarchy><node package="com.android.launcher" /></hierarchy>'


def power(**values):
    fields = {
        "boot": BOOT,
        "before": "1000.0 999",
        "acc": "0",
        "policy": "true",
        "sleeping": "0",
        "after": "1000.5 999",
        "acc2": "0",
        "policy2": "true",
        "sleeping2": "0",
        "boot2": BOOT,
    }
    fields.update(values)
    return "\n".join(fields.values())


@pytest.fixture
def controller(monkeypatch, tmp_path, app_config):
    result = maintenance.MaintenanceController(tmp_path / "cancelled.json")
    monkeypatch.setattr(maintenance, "_controller", result)
    return result


@pytest.mark.parametrize(
    "changes",
    [
        {"boot2": OTHER_BOOT},
        {"boot": "bad"},
        {"acc": "1"},
        {"acc2": "1"},
        {"policy": "false"},
        {"policy2": "false"},
        {"sleeping": "1"},
        {"sleeping2": "1"},
        {"before": "nan"},
        {"after": "inf"},
        {"after": "999 0"},
        {"after": "1004 0"},
    ],
)
def test_mixed_unknown_or_sleep_versus_shutdown_state_is_refused(changes):
    assert maintenance.parse_power(power(**changes)) is None


def test_exact_power_and_countdown_are_recognised():
    assert maintenance.parse_power(power()) == maintenance.PowerProof(BOOT, 1000.5)
    assert maintenance.countdown_target(COUNTDOWN) == (800, 400)
    assert maintenance.countdown_target(NORMAL) is None


@pytest.mark.parametrize(
    "xml",
    [
        "",
        "<hierarchy/>",
        "<!DOCTYPE hierarchy><hierarchy><node/></hierarchy>",
        COUNTDOWN.replace("300 seconds", "3 seconds"),
        COUNTDOWN.replace('long-clickable="true"', 'long-clickable="false"'),
        COUNTDOWN.replace('package="com.android.systemui"', 'package="other"'),
        COUNTDOWN.replace("[600,300][1000,500]", "[1000,300][600,500]"),
        COUNTDOWN.replace("</hierarchy>", COUNTDOWN[11:-12] + "</hierarchy>"),
        pytest.param("a" * (maintenance.MAX_UI_BYTES + 1), id="oversized-ui"),
    ],
)
def test_ambiguous_malformed_unsafe_or_imminent_screen_refuses(xml):
    with pytest.raises(maintenance.MaintenanceRefused):
        maintenance.countdown_target(xml)


async def test_already_dismissed_popup_never_gets_a_retroactive_hold(monkeypatch, controller):
    shell = AsyncMock()
    monkeypatch.setattr(adb, "shell", shell)
    monkeypatch.setattr(maintenance, "_attest", AsyncMock())
    monkeypatch.setattr(
        maintenance, "_power", AsyncMock(return_value=maintenance.PowerProof(BOOT, 1000))
    )
    monkeypatch.setattr(maintenance, "_screen", AsyncMock(return_value=NORMAL))
    with pytest.raises(maintenance.MaintenanceRefused, match="already dismissed"):
        await controller.start("unit", 900)
    shell.assert_not_awaited()
    assert not controller.snapshot()["active"]


async def test_wrong_systemui_hash_is_refused_before_a_gesture(monkeypatch, controller):
    shell = AsyncMock(return_value="0" * 64 + " SystemUI.apk")
    monkeypatch.setattr(adb, "shell", shell)
    with pytest.raises(maintenance.MaintenanceRefused, match="SystemUI"):
        await controller.start("unit", 900)
    assert shell.await_count == 1
    assert "input" not in shell.await_args.args[1]


async def test_verified_cancellation_publishes_owned_lease_then_stops_and_restores(
    monkeypatch, controller
):
    monkeypatch.setattr(maintenance, "_attest", AsyncMock())
    monkeypatch.setattr(
        maintenance, "_power", AsyncMock(return_value=maintenance.PowerProof(BOOT, 1000))
    )
    monkeypatch.setattr(maintenance, "_screen", AsyncMock(side_effect=[COUNTDOWN, NORMAL]))
    publish = AsyncMock()
    monkeypatch.setattr(controller, "_publish", publish)
    shell = AsyncMock(return_value="")
    monkeypatch.setattr(adb, "shell", shell)
    restore = AsyncMock(return_value=True)
    monkeypatch.setattr(puller, "stop_for_maintenance", restore)
    result = await controller.start("unit", 900)
    assert result["active"] and result["verified"]
    assert 839 <= result["remaining_s"] <= 840
    assert result["requires_ignition_cycle"]
    assert shell.await_args.args[1] == "input swipe 800 400 800 400 1500"
    publish.assert_awaited_once()
    stopped = await controller.stop()
    restore.assert_awaited_once()
    assert stopped["state"] == "inactive"
    assert not stopped["active"]
    assert stopped["requires_ignition_cycle"]
    assert all(
        "setprop" not in call.args[1] and "forcesuspend" not in call.args[1]
        for call in shell.await_args_list
    )


async def test_failed_after_gesture_closes_admission_and_restores(monkeypatch, controller):
    monkeypatch.setattr(maintenance, "_attest", AsyncMock())
    monkeypatch.setattr(
        maintenance, "_power", AsyncMock(return_value=maintenance.PowerProof(BOOT, 1000))
    )
    monkeypatch.setattr(maintenance, "_screen", AsyncMock(side_effect=[COUNTDOWN, COUNTDOWN]))
    monkeypatch.setattr(adb, "shell", AsyncMock(return_value=""))
    restore = AsyncMock(return_value=True)
    monkeypatch.setattr(puller, "stop_for_maintenance", restore)
    with pytest.raises(maintenance.MaintenanceRefused, match="disappear"):
        await controller.start("unit", 900)
    restore.assert_awaited_once()
    assert controller.remaining(BOOT) is None
    assert controller.native_cancelled(BOOT)
    assert not controller.snapshot()["countdown_cancelled"]


async def test_unanswered_gesture_is_uncertain_and_still_stops_backup(monkeypatch, controller):
    monkeypatch.setattr(maintenance, "_attest", AsyncMock())
    monkeypatch.setattr(
        maintenance, "_power", AsyncMock(return_value=maintenance.PowerProof(BOOT, 1000))
    )
    monkeypatch.setattr(maintenance, "_screen", AsyncMock(return_value=COUNTDOWN))
    monkeypatch.setattr(adb, "shell", AsyncMock(side_effect=adb.AdbError("transport timed out")))
    restore = AsyncMock(return_value=True)
    monkeypatch.setattr(puller, "stop_for_maintenance", restore)
    with pytest.raises(adb.AdbError):
        await controller.start("unit", 900)
    restore.assert_awaited_once()
    assert controller.remaining(BOOT) is None
    assert controller.native_cancelled(BOOT)
    assert not controller.snapshot()["countdown_cancelled"]


def arm_for_test(controller, now):
    controller._record_cancellation(BOOT)
    controller._state = "active"
    controller._boot = BOOT
    controller._verified_at = now
    controller._end = now + 900
    controller._cancelled = True


async def test_evidence_expiry_boot_and_ignition_invalidate_admission_without_native_deadline(
    controller, monkeypatch
):
    now = [5000.0]
    monkeypatch.setattr(maintenance.time, "monotonic", lambda: now[0])
    arm_for_test(controller, now[0])
    status = IngestStatus()
    status.observe_unit_runtime(adb.RuntimeObservation(BOOT, 1000, "off", 300))
    assert status.radio_quieting_allowed()
    assert status.sleep_countdown_remaining_s() == 840
    assert status.snapshot()["sleep_countdown_remaining_s"] is None
    assert status.snapshot()["sleep_countdown_source"] == "not_running"
    assert status.sleep_deadline_uptime_s() is None
    assert controller.remaining(OTHER_BOOT) is None
    now[0] += 30
    assert not status.radio_quieting_allowed()
    assert status.sleep_countdown_remaining_s() == 0
    status.observe_unit_runtime(adb.RuntimeObservation(BOOT, 1031, "on", 300))
    await controller._clear_task
    assert not controller.native_cancelled(BOOT)
    assert controller.remaining(BOOT) is None


async def test_heartbeat_loss_revokes_and_restores_without_force_sleep(monkeypatch, controller):
    arm_for_test(controller, maintenance.time.monotonic())
    controller._address = "unit"
    monkeypatch.setattr(maintenance, "HEARTBEAT_INTERVAL_S", 0)
    monkeypatch.setattr(
        maintenance, "_screen", AsyncMock(side_effect=adb.AdbError("private output"))
    )
    restore = AsyncMock(return_value=True)
    monkeypatch.setattr(puller, "stop_for_maintenance", restore)
    await controller._heartbeat()
    assert controller.snapshot()["state"] == "failed"
    assert "private" not in str(controller.snapshot())
    restore.assert_awaited_once()


@pytest.mark.parametrize(
    "record,acc,policy,sleeping,expected",
    [
        (f"01234567-1234-1234-1234-012345678901 {BOOT} 1040 1500", "0", "true", "0", "440"),
        (f"01234567-1234-1234-1234-012345678901 {BOOT} 999 1500", "0", "true", "0", "-59"),
        (f"01234567-1234-1234-1234-012345678901 {OTHER_BOOT} 1040 1500", "0", "true", "0", "-59"),
        (f"01234567-1234-1234-1234-012345678901 {BOOT} 99999 999999", "0", "true", "0", "-59"),
        (f"01234567-1234-1234-1234-012345678901 {BOOT} 1040 1500", "1", "true", "0", "-59"),
        (f"01234567-1234-1234-1234-012345678901 {BOOT} 1040 1500", "0", "false", "0", "-59"),
        (f"01234567-1234-1234-1234-012345678901 {BOOT} 1040 1500", "0", "true", "1", "-59"),
        ("forged true", "0", "true", "0", "-59"),
    ],
)
def test_real_shell_guard_uses_only_fresh_device_lease_and_fails_closed(
    record, acc, policy, sleeping, expected
):
    shell = shutil.which("sh")
    if shell is None and os.name == "nt":
        git_shell = Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "Git/bin/sh.exe"
        if git_shell.is_file():
            shell = str(git_shell)
    if shell is None:
        pytest.skip("POSIX shell needed to execute the Android watchdog guard")
    script = (
        'cat() { case "$1" in /proc/sys/kernel/random/boot_id) printf "%s" "$BOOT";; '
        '*) printf "%s" "$RECORD";; esac; }; '
        'settings() { case "$3" in acc_status) printf "%s" "$ACC";; '
        '*) printf "%s" "$SLEEPING";; esac; }; '
        'getprop() { printf "%s" "$POLICY"; }; '
        + radios._watchdog_sleep_guard_functions(None, sleep_deadline_uptime_s=1001)
        + 'now=1000; remaining=5000; reason=lease_expired; sleep_fold; printf "%s" "$remaining"'
    )
    result = subprocess.run(
        [shell, "-c", script],
        capture_output=True,
        text=True,
        check=True,
        timeout=3,
        env={
            **os.environ,
            "BOOT": BOOT,
            "RECORD": record,
            "ACC": acc,
            "POLICY": policy,
            "SLEEPING": sleeping,
        },
    )
    assert result.stdout == expected, result.stderr


def test_watchdog_restores_immediately_when_an_accepted_lease_later_expires():
    shell = shutil.which("sh")
    if shell is None and os.name == "nt":
        candidate = Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "Git/bin/sh.exe"
        shell = str(candidate) if candidate.is_file() else None
    if shell is None:
        pytest.skip("POSIX shell needed to execute the Android watchdog guard")
    record = f"01234567-1234-1234-1234-012345678901 {BOOT} 1040 1500"
    script = (
        f'cat() {{ case "$1" in /proc/sys/kernel/random/boot_id) printf "%s" "{BOOT}";; '
        f'*) printf "%s" "{record}";; esac; }}; '
        "settings() { printf 0; }; getprop() { printf true; }; "
        + radios._watchdog_sleep_guard_functions(None, sleep_deadline_uptime_s=99999)
        + 'now=1000; remaining=5000; reason=lease_expired; sleep_fold; printf "%s " "$remaining"; '
        + 'now=1041; remaining=5000; sleep_fold; printf "%s %s" "$remaining" "$reason"'
    )
    result = subprocess.run(
        [shell, "-c", script], capture_output=True, text=True, check=True, timeout=3
    )
    assert result.stdout == "440 0 lease_expired"


async def test_restoration_failure_keeps_visible_pending_state(monkeypatch, controller):
    arm_for_test(controller, maintenance.time.monotonic())
    monkeypatch.setattr(puller, "stop_for_maintenance", AsyncMock(return_value=False))
    stopped = await controller.stop()
    assert not stopped["active"]
    assert stopped["state"] == "restoring"
    assert "pending" in stopped["reason"]


async def test_changed_device_boot_revokes_without_claiming_a_fresh_sleep_edge(controller):
    arm_for_test(controller, maintenance.time.monotonic())
    controller.observe(adb.RuntimeObservation(OTHER_BOOT, 5, "off", 300))
    await controller._clear_task
    assert controller.remaining(BOOT) is None
    assert not controller.native_cancelled(OTHER_BOOT)
    assert controller.snapshot()["state"] == "restoring"


async def test_api_rejects_bool_fake_proof_and_excessive_duration(client, controller):
    for payload in ({"duration_s": True}, {"verified": True}, {"duration_s": 1801}):
        response = await client.post("/api/ingest/maintenance", json=payload)
        assert response.status_code == 422
    response = await client.get("/api/ingest/maintenance")
    assert response.status_code == 200
    assert response.json()["state"] == "inactive"
    assert not any(word in response.text for word in (BOOT, "token", "address"))


@pytest.mark.parametrize("runtime_boot", [BOOT, None])
async def test_restart_retains_cancellation_over_fresh_companion_old_off_edge(
    controller, monkeypatch, runtime_boot
):
    controller._record_cancellation(BOOT)
    restarted = maintenance.MaintenanceController(controller._record_path())
    monkeypatch.setattr(maintenance, "_controller", restarted)
    proof = adb.SleepDeadlineEvidence(1, runtime_boot, 12, 990_000, False, 800_000, 806_000, 1200)
    observation = adb.RuntimeObservation(runtime_boot, 1000, "off", 1200, 12, proof)
    status = IngestStatus()
    status.observe_unit_runtime(observation)
    assert restarted.native_cancelled(runtime_boot)
    assert not status.radio_quieting_allowed()
    assert status.sleep_deadline_uptime_s() is None
    assert status.sleep_countdown_remaining_s() == 0
    assert status.snapshot()["sleep_countdown_remaining_s"] is None
    # Merely observing OFF, loss of boot UUID, stopping the lease, and restarting
    # again do not remove the marker or fabricate a new native countdown.
    assert controller._record_path().exists()
    restarted.observe(observation)
    assert restarted._clear_task is None
    restarted.observe(adb.RuntimeObservation(runtime_boot, 1001, "on", 300, 12))
    await restarted._clear_task
    assert not controller._record_path().exists()
    another_restart = maintenance.MaintenanceController(controller._record_path())
    assert not another_restart.native_cancelled(runtime_boot)


@pytest.mark.parametrize(
    "raw",
    [
        '{"schema_version":1,"boot_id":"invalid","cancelled":true}',
        '{"schema_version":1,"cancelled":true}',
        '{"schema_version":true,"boot_id":"' + BOOT + '","cancelled":true}',
        '{"schema_version":1,"boot_id":"' + BOOT + '","cancelled":false}',
        '{"schema_version":1,"boot_id":"' + BOOT + '","cancelled":true,"private":"x"}',
        '{"schema_version":1,"boot_id":"' + BOOT + '","cancelled":true,"cancelled":true}',
        pytest.param("x" * 1025, id="oversized-record"),
    ],
)
def test_corrupt_cancellation_record_fails_closed_even_without_boot_identity(controller, raw):
    controller._record_path().write_text(raw)
    controller.load_once()
    assert controller.native_cancelled(BOOT)
    assert controller.native_cancelled(None)
    assert controller.snapshot()["requires_ignition_cycle"]


async def test_cancellation_record_is_durable_before_even_an_unanswered_gesture(
    controller, monkeypatch
):
    monkeypatch.setattr(maintenance, "_attest", AsyncMock())
    monkeypatch.setattr(
        maintenance, "_power", AsyncMock(return_value=maintenance.PowerProof(BOOT, 1000))
    )
    monkeypatch.setattr(maintenance, "_screen", AsyncMock(return_value=COUNTDOWN))

    async def gesture(*args, **kwargs):
        record = json.loads(controller._record_path().read_text())
        assert set(record) == {"schema_version", "boot_id", "cancelled", "generation"}
        assert (
            record["schema_version"] == 1
            and record["boot_id"] == BOOT
            and record["cancelled"] is True
        )
        assert maintenance._BOOT.fullmatch(record["generation"])
        raise adb.AdbError("unanswered gesture")

    monkeypatch.setattr(adb, "shell", gesture)
    monkeypatch.setattr(puller, "stop_for_maintenance", AsyncMock(return_value=True))
    with pytest.raises(adb.AdbError):
        await controller.start("unit", 900)
    restarted = maintenance.MaintenanceController(controller._record_path())
    assert restarted.native_cancelled(BOOT)
    assert not restarted.snapshot()["active"]


async def test_failed_durable_write_never_sends_the_gesture(controller, monkeypatch):
    monkeypatch.setattr(maintenance, "_attest", AsyncMock())
    monkeypatch.setattr(
        maintenance, "_power", AsyncMock(return_value=maintenance.PowerProof(BOOT, 1000))
    )
    monkeypatch.setattr(maintenance, "_screen", AsyncMock(return_value=COUNTDOWN))

    def cannot_save(*args):
        raise maintenance.MaintenanceRefused("The cancellation record could not be saved.")

    monkeypatch.setattr(controller, "_record_cancellation", cannot_save)
    shell = AsyncMock()
    monkeypatch.setattr(adb, "shell", shell)
    monkeypatch.setattr(puller, "stop_for_maintenance", AsyncMock(return_value=True))
    with pytest.raises(maintenance.MaintenanceRefused, match="saved"):
        await controller.start("unit", 900)
    shell.assert_not_awaited()


async def test_failed_clear_preserves_marker_until_another_fresh_edge(controller, monkeypatch):
    controller._record_cancellation(BOOT)
    monkeypatch.setattr(controller, "_clear_record", lambda *expected: False)
    controller.observe(adb.RuntimeObservation(BOOT, 1000, "on", 300))
    await controller._clear_task
    assert controller.native_cancelled(BOOT)
    restarted = maintenance.MaintenanceController(controller._record_path())
    assert restarted.native_cancelled(None)


async def test_queued_old_on_clear_cannot_erase_a_new_cancellation_on_same_boot(controller):
    controller._record_cancellation(BOOT)
    old_generation = controller._generation
    controller._record_cancellation(BOOT)
    new_generation = controller._generation
    assert new_generation != old_generation
    controller._cancelled, controller._boot = True, BOOT
    await controller._clear_after_observation(BOOT, old_generation)
    record = json.loads(controller._record_path().read_text())
    assert record["generation"] == new_generation
    assert controller._generation == new_generation
    assert controller.native_cancelled(BOOT)


async def test_corrupt_record_requires_positive_on_not_missing_boot(controller):
    controller._record_path().write_text("corrupt")
    controller.observe(adb.RuntimeObservation(None, 1000, "off", 300, 12))
    assert controller._clear_task is None and controller.native_cancelled(None)
    controller.observe(adb.RuntimeObservation(OTHER_BOOT, 10, "off", 300))
    assert controller._clear_task is None and controller.native_cancelled(OTHER_BOOT)
    controller.observe(adb.RuntimeObservation(OTHER_BOOT, 11, "on", 300))
    await controller._clear_task
    assert not controller.native_cancelled(OTHER_BOOT)
