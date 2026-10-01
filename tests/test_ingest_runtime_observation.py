"""Same-IP reboot and stale power observations must not become a current sleep timer."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest

from app.ingest import adb
from app.ingest import poller as poller_module
from app.ingest import status as status_module
from app.ingest.models import RunState
from app.ingest.status import IngestStatus

BOOT_A = "01234567-1234-1234-1234-012345678901"
BOOT_B = "01234567-1234-1234-1234-012345678902"


def observation(boot=BOOT_A, uptime=100.0, acc="off", window=1200):
    return adb.RuntimeObservation(boot, uptime, acc, window)


@pytest.fixture
def clock(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(status_module.time, "monotonic", lambda: now[0])
    return now


@pytest.mark.parametrize("acc,expected", [("0", "off"), ("1", "on"), ("null", "unknown")])
async def test_read_only_snapshot_is_single_bounded_shell(monkeypatch, acc, expected):
    shell = AsyncMock(return_value=f"{BOOT_A}\n123.45 100\n{acc}\n1200\n{BOOT_A}\n")
    monkeypatch.setattr(adb, "shell", shell)
    result = await adb.runtime_observation("unit:5555")
    assert result == observation(uptime=123.45, acc=expected)
    shell.assert_awaited_once()
    args, kwargs = shell.await_args
    assert args[0] == "unit:5555"
    assert kwargs == {"timeout": 3.0}
    assert args[1].count("cat /proc/sys/kernel/random/boot_id") == 2
    assert "settings get global acc_status" in args[1]
    assert "getprop persist.sys.sleep.countdown.time" in args[1]
    assert not any(word in args[1] for word in ("setprop", "disconnect", "connect", "su "))


@pytest.mark.parametrize(
    "reply",
    [
        "",
        "garbage",
        f"{BOOT_A}\nnan 0\n0\n1200\n{BOOT_A}",
        f"{BOOT_A}\ninf 0\n0\n1200\n{BOOT_A}",
        f"{BOOT_A}\n-1 0\n0\n1200\n{BOOT_A}",
        f"{BOOT_A}\n123 0\n0\n1200\n{BOOT_B}",
        "wrong\n123 0\n0\n1200\nwrong",
    ],
)
async def test_invalid_or_mixed_boot_snapshot_is_not_power_evidence(monkeypatch, reply):
    monkeypatch.setattr(adb, "shell", AsyncMock(return_value=reply))
    assert await adb.runtime_observation("unit") is None


async def test_failed_snapshot_and_missing_property_stay_unknown(monkeypatch):
    shell = AsyncMock(side_effect=adb.AdbError("timeout"))
    monkeypatch.setattr(adb, "shell", shell)
    assert await adb.runtime_observation("unit") is None
    shell.side_effect = None
    shell.return_value = f"{BOOT_A}\n123 0\n0\n\n{BOOT_A}\n"
    assert await adb.runtime_observation("unit") == observation(uptime=123, window=None)


def test_first_seen_off_cannot_borrow_run_start_or_property_write_as_deadline(clock):
    status = IngestStatus()
    status.set_unit_online(True)
    status.try_begin()
    status.set_ignition_state("off")
    status.set_sleep_window(1200, restarted=True)
    status.observe_unit_runtime(observation())
    snapshot = status.snapshot()
    assert snapshot["unit_observation_fresh"]
    assert snapshot["unit_observation_age_s"] == 0
    assert snapshot["unit_observation_ttl_s"] == 30
    assert snapshot["ignition_state"] == "off"
    assert snapshot["ignition_off_at"] is None
    assert snapshot["sleep_countdown_remaining_s"] is None
    assert snapshot["sleep_countdown_source"] == "unknown"
    assert snapshot["sleep_window_prediction"] is None


def test_observed_edge_estimate_does_not_extend_on_property_rewrite(clock):
    status = IngestStatus()
    status.observe_unit_runtime(observation(acc="on"))
    assert status.snapshot()["sleep_countdown_source"] == "not_running"
    assert status.snapshot()["sleep_countdown_remaining_s"] is None
    clock[0] += 15
    status.observe_unit_runtime(observation(uptime=115))
    assert status.snapshot()["sleep_countdown_remaining_s"] == 1200
    clock[0] += 10
    status.set_sleep_window(300, restarted=True)
    status.observe_unit_runtime(observation(uptime=125, window=300))
    snapshot = status.snapshot()
    assert snapshot["sleep_countdown_source"] == "estimated"
    assert snapshot["sleep_countdown_remaining_s"] == 1190
    assert snapshot["sleep_window_seconds"] == 300
    assert snapshot["ignition_off_at"] is not None


@pytest.mark.parametrize("failure", ["failed", "expired", "offline", "unknown_acc"])
def test_untrustworthy_power_cannot_keep_or_recreate_countdown(clock, failure):
    status = IngestStatus()
    status.observe_unit_runtime(observation(acc="on"))
    clock[0] += 15
    status.observe_unit_runtime(observation(uptime=115))
    last_success = status.snapshot()["unit_observed_at"]
    if failure == "failed":
        status.unit_observation_failed()
    elif failure == "offline":
        status.set_unit_online(False)
    elif failure == "unknown_acc":
        status.observe_unit_runtime(observation(uptime=115, acc="unknown"))
    else:
        clock[0] += 31
    snapshot = status.snapshot()
    assert snapshot["sleep_countdown_remaining_s"] is None
    assert snapshot["sleep_countdown_source"] == "unknown"
    assert snapshot["ignition_state"] == "unknown"
    if failure != "unknown_acc":
        assert not snapshot["unit_observation_fresh"]
        assert snapshot["unit_uptime_s"] is None
        assert snapshot["unit_observed_at"] == last_success
    # The missed interval could contain another ACC cycle: already-off is unknown again.
    status.observe_unit_runtime(observation(uptime=200))
    assert status.snapshot()["sleep_countdown_remaining_s"] is None


def test_reboot_invalidates_old_run_countdown_without_releasing_single_flight(clock):
    status = IngestStatus()
    status.observe_unit_runtime(observation(uptime=1_113_630, acc="on"))
    clock[0] += 15
    status.observe_unit_runtime(observation(uptime=1_113_645))
    status.try_begin()
    status.backlog_known = True
    status.set_sleep_window(1200, restarted=True)
    clock[0] += 15
    assert status.observe_unit_runtime(observation(boot=BOOT_B, uptime=127))
    assert status.running and status.cancel_event.is_set()
    assert not status.try_begin(), "cleanup still owns the staging directory and radios"
    assert not status.backlog_known
    assert status.sleep_window_started_monotonic is None
    assert status.ignition_off_monotonic is None
    assert status.online_for() == 0
    snapshot = status.snapshot()
    assert snapshot["unit_uptime_s"] == 127
    assert snapshot["sleep_countdown_remaining_s"] is None
    assert snapshot["sleep_countdown_source"] == "unknown"
    assert snapshot["state"] == "running"


def test_stale_cached_logger_cannot_overwrite_new_power_snapshot(clock):
    status = IngestStatus()
    status.observe_unit_runtime(observation(boot=BOOT_B, uptime=127, acc="on"))
    status.set_ignition_state("off")
    status.set_sleep_window(1200, restarted=True)
    snapshot = status.snapshot()
    assert snapshot["ignition_state"] == "on"
    assert snapshot["sleep_countdown_source"] == "not_running"
    assert snapshot["ignition_off_at"] is None


async def test_poller_observes_running_reboot_and_resets_visit_not_radio_ownership(
    monkeypatch, clock
):
    status = IngestStatus()
    status.observe_unit_runtime(observation(acc="on"))
    status.try_begin()
    poller = poller_module.IngestPoller()
    poller._running = poller._was_online = True
    poller._backups_this_visit = 3
    poller._error_retries_started = 3
    poller._visit_info = object()
    poller._idle_since = 20
    reader = AsyncMock(return_value=observation(boot=BOOT_B, uptime=127))
    forbidden = AsyncMock(side_effect=AssertionError("active run owns transport and radio lease"))
    monkeypatch.setattr(poller_module, "get_status", lambda: status)
    monkeypatch.setattr(poller_module.adb, "runtime_observation", reader)
    monkeypatch.setattr(poller, "_address", lambda: "unit")
    monkeypatch.setattr(poller, "_interval", lambda: 1)
    monkeypatch.setattr(poller_module.carplay_timing, "recover_on_unit_present", Mock())
    monkeypatch.setattr(poller_module.band, "on_unit_present", Mock())
    monkeypatch.setattr(poller_module.puller, "probe_unit", forbidden)
    monkeypatch.setattr(poller, "_recover_pending_while_online", forbidden)

    async def stop(_delay):
        poller._running = False

    monkeypatch.setattr(poller_module.asyncio, "sleep", stop)
    await poller._loop()
    reader.assert_awaited_once_with("unit")
    forbidden.assert_not_called()
    assert status.running and status.cancel_event.is_set()
    assert not poller._was_online
    assert poller._backups_this_visit == poller._error_retries_started == 0
    assert poller._visit_info is None and poller._idle_since == 0


async def test_poller_reads_before_capped_visit_skip(monkeypatch, clock):
    status = IngestStatus()
    status.observe_unit_runtime(observation())
    status.set_state(RunState.OK)
    poller = poller_module.IngestPoller()
    poller._running = poller._was_online = True
    poller._backups_this_visit = 3
    reader = AsyncMock(return_value=observation(uptime=115))
    monkeypatch.setattr(poller_module, "get_status", lambda: status)
    monkeypatch.setattr(poller_module.adb, "runtime_observation", reader)
    monkeypatch.setattr(poller_module.adb, "is_listening", AsyncMock(return_value=True))
    monkeypatch.setattr(poller, "_enabled", lambda: True)
    monkeypatch.setattr(poller, "_address", lambda: "unit")
    monkeypatch.setattr(poller, "_interval", lambda: 1)
    monkeypatch.setattr(poller, "_recover_pending_while_online", AsyncMock(return_value=False))

    def capped(_status):
        assert reader.await_count == 1
        return False

    monkeypatch.setattr(poller, "_should_drain_again", capped)
    for module in (
        poller_module.health,
        poller_module.unit_logs,
        poller_module.carplay_timing,
        poller_module.wifi_startup,
        poller_module.band,
    ):
        monkeypatch.setattr(module, "on_unit_present", Mock())

    async def stop(_delay):
        poller._running = False

    monkeypatch.setattr(poller_module.asyncio, "sleep", stop)
    await poller._loop()
    assert status.snapshot()["unit_uptime_s"] == 115
    assert poller._backups_this_visit == 3


async def test_runtime_reads_throttle_failures_and_expire_independently(monkeypatch, clock):
    status = IngestStatus()
    poller = poller_module.IngestPoller()
    reader = AsyncMock(side_effect=[observation(acc="on"), None, observation()])
    monkeypatch.setattr(poller_module, "get_status", lambda: status)
    monkeypatch.setattr(poller_module.adb, "runtime_observation", reader)
    await poller._observe_unit_runtime("unit")
    clock[0] += 14
    await poller._observe_unit_runtime("unit")
    assert reader.await_count == 1
    clock[0] += 1
    await poller._observe_unit_runtime("unit")
    assert reader.await_count == 2
    assert not status.snapshot()["unit_observation_fresh"]
    clock[0] += 15
    await poller._observe_unit_runtime("unit")
    assert status.snapshot()["sleep_countdown_remaining_s"] is None
    clock[0] += 31
    assert not status.snapshot()["unit_observation_fresh"]
