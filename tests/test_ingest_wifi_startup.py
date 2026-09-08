"""The startup hold is opt-in and only armed while positively parked."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.ingest import adb, wifi_startup


@pytest.fixture(autouse=True)
async def clean():
    await wifi_startup.shutdown()
    yield
    await wifi_startup.shutdown()


@pytest.mark.parametrize("ignition", ["on", "unknown", None])
async def test_no_install_or_radio_request_while_driving_or_unknown(monkeypatch, ignition):
    call = AsyncMock()
    monkeypatch.setattr(wifi_startup, "get_status", lambda: SimpleNamespace(ignition_state=ignition))
    monkeypatch.setattr(wifi_startup, "arm", call)
    wifi_startup.on_unit_present("unit:5555")
    await asyncio.sleep(0)
    call.assert_not_called()


async def test_parked_presence_debounces_per_unit(monkeypatch):
    call = AsyncMock(return_value=True)
    monkeypatch.setattr(wifi_startup, "get_status", lambda: SimpleNamespace(ignition_state="off"))
    monkeypatch.setattr(wifi_startup, "arm", call)
    monkeypatch.setattr(wifi_startup.time, "monotonic", lambda: 1000)
    wifi_startup.on_unit_present("unit:5555")
    wifi_startup.on_unit_present("unit:5555")
    await asyncio.gather(*list(wifi_startup._tasks))
    call.assert_awaited_once_with("unit:5555")


@pytest.mark.parametrize("result,requested", [
    ("not_enabled", False), ("ignition_hold", False), ("recovery_pending", False),
    ("companion_unavailable", False), ("launched", True), ("already_running", True),
    ("unexpected", False),
])
async def test_remote_verdict_is_not_inferred_from_success_exit(monkeypatch, result, requested):
    monkeypatch.setattr(adb, "shell", AsyncMock(return_value=result + "\n"))
    assert await wifi_startup.arm("unit:5555") is requested


async def test_connection_failure_is_not_reported_as_armed(monkeypatch):
    monkeypatch.setattr(adb, "shell", AsyncMock(side_effect=adb.AdbError("unavailable")))
    assert not await wifi_startup.arm("unit:5555")
