"""Wi-Fi observations stay current during transfers without enforcing radio policy."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from app.ingest import adb, band
from app.ingest import poller as poller_module
from app.ingest import status as status_module
from app.ingest.status import IngestStatus


@pytest.fixture
async def live_band(monkeypatch):
    status = IngestStatus()
    now = [100.0]
    monkeypatch.setattr(status_module, "get_status", lambda: status)
    monkeypatch.setattr(poller_module, "get_status", lambda: status)
    monkeypatch.setattr(band.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(band, "_last_link_refresh_at", None)
    monkeypatch.setattr(band, "_link_refresh_task", None)
    yield status, now
    await band.shutdown()


@pytest.mark.parametrize("failure", [adb.AdbError("timeout"), "unrecognised output"])
async def test_failed_band_read_clears_old_value_and_remains_throttled(
    monkeypatch, live_band, failure
):
    status, now = live_band
    status.set_wifi(5220, held=True, reason="existing policy hold")
    shell = AsyncMock()
    if isinstance(failure, Exception):
        shell.side_effect = failure
    else:
        shell.return_value = failure
    monkeypatch.setattr(adb, "shell", shell)
    assert await band.refresh_link_if_due("unit") is None
    assert status.wifi_frequency_mhz is None
    assert status.wifi_band_hold and status.wifi_band_hold_reason == "existing policy hold"
    now[0] += 29.9
    assert await band.refresh_link_if_due("unit") is None
    assert shell.await_count == 1
    now[0] += 0.1
    shell.side_effect = None
    shell.return_value = "Frequency: 2437MHz"
    assert await band.refresh_link_if_due("unit") == 2437
    assert shell.await_count == 2
    assert status.snapshot()["wifi_frequency_mhz"] == 2437


async def test_active_copy_observes_roaming_without_radio_controls(monkeypatch, live_band):
    status, now = live_band
    status.set_unit_online(True)
    status.set_wifi(2472, held=False, reason=None)
    status.try_begin()
    poller = poller_module.IngestPoller()
    poller._running = True
    shell = AsyncMock(side_effect=["Frequency: 2472MHz", "Frequency: 5220MHz"])
    monkeypatch.setattr(adb, "shell", shell)
    monkeypatch.setattr(poller, "_observe_unit_runtime", AsyncMock())
    monkeypatch.setattr(poller, "_address", lambda: "unit")
    monkeypatch.setattr(poller, "_interval", lambda: 1)
    monkeypatch.setattr(poller_module.carplay_timing, "recover_on_unit_present", Mock())
    forbidden = AsyncMock(side_effect=AssertionError("active observation must not change radios"))
    monkeypatch.setattr(band, "gate", forbidden)
    monkeypatch.setattr(band, "apply_selection_nudge", forbidden)
    monkeypatch.setattr(band.unifi, "kick_client", forbidden)
    monkeypatch.setattr(poller_module.puller, "probe_unit", forbidden)
    readings = []

    async def tick(_delay):
        assert band._link_refresh_task is not None
        await band._link_refresh_task
        readings.append(status.wifi_frequency_mhz)
        now[0] += 15
        if len(readings) == 3:
            poller._running = False

    monkeypatch.setattr(poller_module.asyncio, "sleep", tick)
    await poller._loop()
    assert readings == [2472, 2472, 5220]
    assert status.running and not status.cancel_event.is_set()
    assert shell.await_count == 2
    assert all(call.args == ("unit", "cmd wifi status") for call in shell.await_args_list)
    assert all(call.kwargs == {"timeout": band.BAND_TIMEOUT_S} for call in shell.await_args_list)
    forbidden.assert_not_called()


async def test_pending_refresh_is_single_flight(monkeypatch, live_band):
    status, _now = live_band
    entered = asyncio.Event()
    release = asyncio.Event()

    async def shell(*_args, **_kwargs):
        entered.set()
        await release.wait()
        return "Frequency: 5220MHz"

    reader = AsyncMock(side_effect=shell)
    monkeypatch.setattr(adb, "shell", reader)
    band.on_unit_present("unit")
    await entered.wait()
    task = band._link_refresh_task
    for _ in range(5):
        band.on_unit_present("unit")
    assert band._link_refresh_task is task
    assert reader.await_count == 1
    release.set()
    await task
    assert status.wifi_frequency_mhz == 5220
