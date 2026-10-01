"""New quieting requires sleep headroom; restoration never does."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.ingest import adb, radios
from app.ingest import status as status_module
from app.ingest.models import RunResult, RunState

BOOT = "01234567-1234-1234-1234-012345678901"


@pytest.fixture
def power(monkeypatch):
    now = [100.0]
    status = status_module.IngestStatus()
    monkeypatch.setattr(status_module.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(status_module, "get_status", lambda: status)
    return status, now


def edge(status, window=1200, previous_window=None):
    if previous_window is None:
        previous_window = window
    status.observe_unit_runtime(adb.RuntimeObservation(BOOT, 100, "on", previous_window))
    status.observe_unit_runtime(adb.RuntimeObservation(BOOT, 101, "off", window))


@pytest.mark.parametrize(
    "window,allowed", [(59, False), (60, False), (93, False), (94, True), (1200, True)]
)
def test_strict_one_minute_budget_includes_observation_uncertainty(power, window, allowed):
    status, _now = power
    edge(status, window)
    assert status.radio_quieting_allowed() is allowed
    assert status.radio_quieting_hold is not allowed


@pytest.mark.parametrize("condition", ["startup_off", "failed", "stale", "offline", "on", "reboot"])
def test_unknown_or_untrustworthy_sleep_deadline_cannot_quiet(power, condition):
    status, now = power
    if condition == "startup_off":
        status.observe_unit_runtime(adb.RuntimeObservation(BOOT, 100, "off", 1200))
    else:
        edge(status)
        if condition == "failed":
            status.unit_observation_failed()
        elif condition == "stale":
            now[0] += 31
        elif condition == "offline":
            status.set_unit_online(False)
        elif condition == "on":
            status.observe_unit_runtime(adb.RuntimeObservation(BOOT, 102, "on", 1200))
        else:
            status.observe_unit_runtime(adb.RuntimeObservation(BOOT[:-1] + "2", 2, "off", 1200))
    assert not radios.new_quieting_allowed()
    assert "unknown" in status.radio_quieting_hold_reason


def test_property_rewrite_cannot_invent_or_extend_quieting_budget(power):
    status, _now = power
    edge(status, 1200, previous_window=60)
    # The property could have changed after native countdown latched the old60s.
    assert not radios.new_quieting_allowed()
    status.set_sleep_window(1200, restarted=True)
    status.observe_unit_runtime(adb.RuntimeObservation(BOOT, 102, "off", 1200))
    assert not radios.new_quieting_allowed()


def test_quieting_note_is_only_shown_for_active_online_run_and_clears_next_run(power):
    status, _now = power
    status.observe_unit_runtime(adb.RuntimeObservation(BOOT, 100, "off", 1200))
    status.try_begin()
    assert not radios.new_quieting_allowed()
    assert status.snapshot()["radio_quieting_hold"]
    assert "unknown" in status.snapshot()["radio_quieting_hold_reason"]
    status.set_unit_online(False)
    assert not status.snapshot()["radio_quieting_hold"]
    assert status.snapshot()["radio_quieting_hold_reason"] is None
    status.finish(RunResult(state=RunState.OK))
    status.set_unit_online(True)
    assert not status.snapshot()["radio_quieting_hold"]
    status.try_begin()
    assert not status.radio_quieting_hold
    assert status.radio_quieting_hold_reason is None


@pytest.mark.parametrize("operation", ["disable_bluetooth", "disable_hotspot"])
async def test_direct_controller_unknown_deadline_stops_before_watchdog_and_changes(
    monkeypatch, power, operation
):
    controller = radios.RadioController("unit", watchdog_deadline_s=300)
    controller._hotspot_capsule_path = "/ignored/test-capsule"
    watchdog = AsyncMock(return_value=True)
    changed = AsyncMock(return_value=True)
    monkeypatch.setattr(controller, "_ensure_watchdog_locked", watchdog)
    monkeypatch.setattr(radios, "_set_bluetooth", changed)
    monkeypatch.setattr(radios, "_stop_hotspot", changed)
    assert not await getattr(controller, operation)()
    watchdog.assert_not_called()
    changed.assert_not_called()


@pytest.mark.parametrize("operation", ["disable_bluetooth", "disable_hotspot"])
@pytest.mark.parametrize("delay_stage", ["watchdog", "final_guard"])
async def test_controller_rechecks_after_preparation_before_each_side_effect(
    monkeypatch, power, operation, delay_stage
):
    status, now = power
    edge(status, 100)  # 67s usable; another8s crosses the admission boundary.
    controller = radios.RadioController("unit", watchdog_deadline_s=300)
    controller._hotspot_capsule_path = "/ignored/test-capsule"

    async def watchdog(**_kwargs):
        if delay_stage == "watchdog":
            now[0] += 8
        return True

    async def final_guard():
        if delay_stage == "final_guard":
            now[0] += 8

    changed = AsyncMock(return_value=True)
    monkeypatch.setattr(controller, "_ensure_watchdog_locked", watchdog)
    monkeypatch.setattr(radios, "_set_bluetooth", changed)
    monkeypatch.setattr(radios, "_stop_hotspot", changed)
    assert not await getattr(controller, operation)(before_change=final_guard)
    changed.assert_not_called()
    assert "one minute" in status.radio_quieting_hold_reason


@pytest.mark.parametrize("operation", ["disable_bluetooth", "disable_hotspot"])
async def test_controller_allows_quieting_with_fresh_sufficient_budget(
    monkeypatch, power, operation
):
    status, _now = power
    edge(status)
    controller = radios.RadioController("unit", watchdog_deadline_s=300)
    controller._hotspot_capsule_path = "/ignored/test-capsule"
    monkeypatch.setattr(controller, "_ensure_watchdog_locked", AsyncMock(return_value=True))
    bluetooth = AsyncMock(return_value=True)
    hotspot = AsyncMock(return_value=(True, ""))
    monkeypatch.setattr(radios, "_set_bluetooth", bluetooth)
    monkeypatch.setattr(radios, "_confirm_bluetooth_off", AsyncMock(return_value=True))
    monkeypatch.setattr(radios, "_stop_hotspot", hotspot)
    monkeypatch.setattr(radios, "_persist_refusal", AsyncMock())
    assert await getattr(controller, operation)()
    assert bluetooth.await_count + hotspot.await_count == 1


@pytest.mark.parametrize("baseline", ["on", "off"])
async def test_bluetooth_restoration_remains_allowed_with_unknown_deadline(
    monkeypatch, power, baseline
):
    controller = radios.RadioController("unit", watchdog_deadline_s=300)
    changed = AsyncMock(return_value=True)
    monkeypatch.setattr(radios, "_set_bluetooth", changed)
    monkeypatch.setattr(radios, "_bluetooth_is_on", AsyncMock(return_value=True))
    monkeypatch.setattr(radios, "_confirm_bluetooth_on", AsyncMock(return_value=True))
    monkeypatch.setattr(radios, "_confirm_bluetooth_off", AsyncMock(return_value=True))
    assert await controller.restore_bluetooth(baseline)
    changed.assert_awaited_once_with("unit", enable=baseline == "on")


async def test_hotspot_off_baseline_restoration_is_not_a_new_quieting_attempt(monkeypatch, power):
    controller = radios.RadioController("unit", watchdog_deadline_s=300)
    monkeypatch.setattr(radios, "_serving_ap", AsyncMock(return_value="wlan2"))
    stop = AsyncMock(return_value=(True, ""))
    monkeypatch.setattr(radios, "_stop_hotspot", stop)
    assert await controller.restore_hotspot("off", None)
    stop.assert_awaited_once_with("unit")


async def test_legacy_quiet_unknown_deadline_never_disables(monkeypatch, power):
    quiet = radios.RadioQuiet("unit", online_for=60, watchdog_deadline_s=300)
    changed = AsyncMock()
    monkeypatch.setattr(radios, "_set_bluetooth", changed)
    monkeypatch.setattr(radios, "_stop_hotspot", changed)
    await quiet._quiet()
    changed.assert_not_called()


async def test_legacy_partial_quiet_is_restored_when_budget_expires(monkeypatch, power):
    status, now = power
    edge(status, 100)
    quiet = radios.RadioQuiet("unit", online_for=60, watchdog_deadline_s=300)

    async def bluetooth():
        quiet.bluetooth_off = True
        now[0] += 8

    monkeypatch.setattr(quiet, "_quiet_bluetooth", bluetooth)
    monkeypatch.setattr(radios, "_serving_ap", AsyncMock(return_value="wlan2"))
    monkeypatch.setattr(adb, "shell", AsyncMock(return_value=""))
    stop = AsyncMock()
    restore = AsyncMock()
    monkeypatch.setattr(radios, "_stop_hotspot", stop)
    monkeypatch.setattr(quiet, "_restore_bluetooth", restore)
    await quiet._quiet()
    stop.assert_not_called()
    restore.assert_awaited_once()


@pytest.mark.parametrize("restoring", [False, True])
async def test_bluetooth_retry_rechecks_budget_but_original_off_restore_is_exempt(
    monkeypatch, power, restoring
):
    status, now = power
    edge(status, 100)
    commands = []

    async def shell(_address, command, **_kwargs):
        commands.append(command)
        if len(commands) == 1:
            now[0] += 8
            raise adb.AdbError("lost first toggle reply")
        return ""

    monkeypatch.setattr(adb, "shell", shell)
    result = await radios._set_bluetooth(
        "unit",
        enable=False,
        before_attempt=None if restoring else radios.new_quieting_allowed,
    )
    assert result is restoring
    assert commands == (
        ["cmd bluetooth_manager disable", "svc bluetooth disable"]
        if restoring
        else ["cmd bluetooth_manager disable"]
    )


@pytest.mark.parametrize("restoring", [False, True])
async def test_hotspot_fallback_rechecks_budget_but_original_off_restore_is_exempt(
    monkeypatch, power, restoring
):
    status, now = power
    edge(status, 100)
    commands = []

    async def shell(_address, command, **_kwargs):
        commands.append(command)
        if len(commands) == 1:
            now[0] += 8
            raise adb.AdbError("lost first stop reply")
        return ""

    monkeypatch.setattr(adb, "shell", shell)
    monkeypatch.setattr(radios, "_stop_took_effect", AsyncMock(side_effect=[False, True]))
    stopped, _reason = await radios._stop_hotspot(
        "unit",
        before_attempt=None if restoring else radios.new_quieting_allowed,
    )
    assert stopped is restoring
    assert len(commands) == (2 if restoring else 1)
    assert ("cmd wifi stop-softap" in commands) is restoring
