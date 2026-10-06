"""Fresh sleep permission and positive OFF readback are different requirements."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import config
from app.ingest import adb, radios
from app.ingest.status import get_status, reset_status_for_tests

BOOT = "09255025-8fa6-4dd4-bede-607e8a2603bc"
ADDRESS = "192.0.2.10:5555"
STATION = 'WifiInfo: SSID: "example", IP: /192.0.2.10, Supplicant state: COMPLETED, RSSI: -60'
INTERFACES = "1: lo inet 127.0.0.1/8 scope host lo\n2: wlan0 inet 192.0.2.10/24 scope global wlan0"


def power(*, evidence=True, window=1200):
    return adb.RuntimeObservation(
        BOOT,
        120,
        "off",
        window,
        20,
        adb.SleepDeadlineEvidence(1, BOOT, 20, 119_000, False, 100_000, 105_000, window)
        if evidence
        else None,
    )


@pytest.fixture
def status(monkeypatch):
    reset_status_for_tests()
    status = get_status()
    status.try_begin()
    monkeypatch.setattr(
        config, "get_config", lambda: SimpleNamespace(obd_remote_status_file="/status")
    )
    yield status
    reset_status_for_tests()


@pytest.mark.parametrize("condition", ["sufficient", "short", "on", "cancelled"])
async def test_refresh_does_not_overrule_definite_permission_or_refusal(
    status, monkeypatch, condition
):
    observation = power(window=60 if condition == "short" else 1200)
    if condition == "on":
        observation = replace(observation, ignition_state="on", sleep_deadline_evidence=None)
    status.observe_unit_runtime(observation)
    if condition == "cancelled":
        status.cancel()
    read = AsyncMock(side_effect=AssertionError("no late read needed"))
    monkeypatch.setattr(adb, "runtime_observation", read)
    assert await radios.refresh_quieting_admission(ADDRESS) is (condition == "sufficient")
    read.assert_not_awaited()


@pytest.mark.parametrize(
    "reply", ["fresh", "missing", "unknown", "on", "short", "reboot", "cancelled"]
)
async def test_late_refresh_requires_same_boot_fresh_off_evidence(status, monkeypatch, reply):
    status.observe_unit_runtime(power(evidence=False))

    async def read(address, *, logger_status_path):
        assert address == ADDRESS and logger_status_path == "/status"
        if reply == "cancelled":
            status.cancel()
        if reply == "missing":
            return None
        if reply == "unknown":
            return power(evidence=False)
        if reply == "on":
            return replace(power(evidence=False), ignition_state="on")
        if reply == "short":
            return power(window=60)
        if reply == "reboot":
            observation = power()
            return replace(
                observation,
                boot_count=21,
                sleep_deadline_evidence=replace(observation.sleep_deadline_evidence, boot_count=21),
            )
        return power()

    read_mock = AsyncMock(side_effect=read)
    monkeypatch.setattr(adb, "runtime_observation", read_mock)
    assert await radios.refresh_quieting_admission(ADDRESS) is (reply == "fresh")
    read_mock.assert_awaited_once()


@pytest.mark.parametrize(
    "bluetooth,interfaces,station,expected",
    [
        ("0", INTERFACES, STATION, True),
        ("1", INTERFACES, STATION, False),
        ("null", INTERFACES, STATION, False),
        ("0", INTERFACES + "\n3: wlan2 inet 192.0.2.1/24 scope global wlan2", STATION, False),
        ("0", "", STATION, False),
        ("0", INTERFACES, "", False),
        ("0", INTERFACES, STATION.replace("192.0.2.10", "192.0.2.99"), False),
        ("0", INTERFACES, STATION.replace("COMPLETED", "DISCONNECTED"), False),
        ("0", "1: lo inet 127.0.0.1/8 scope host lo", "", True),
    ],
)
async def test_positive_radio_proof_rejects_live_ap_or_ambiguous_transport(
    monkeypatch, bluetooth, interfaces, station, expected
):
    commands = []

    async def shell(_address, command, **_kwargs):
        commands.append(command)
        if command == "settings get global bluetooth_on":
            return bluetooth
        if command == "ip -o addr show up; exit 0":
            return interfaces
        if command == "cmd wifi status":
            return station
        raise AssertionError(f"unexpected radio control: {command}")

    monkeypatch.setattr(adb, "shell", shell)
    controller = radios.RadioController(ADDRESS, watchdog_deadline_s=120)
    assert await controller.quiet_state_verified() is expected
    assert len(commands) == 3, "verification is read-only and bounded"


@pytest.mark.parametrize(
    "reply",
    [
        STATION.replace("192.0.2.10", "192.0.2.100"),
        'WifiInfo: SSID: "spoof, IP: /192.0.2.10, Supplicant state: COMPLETED", IP: /192.0.2.99, Supplicant state: DISCONNECTED',
        STATION.replace("WifiInfo:", "SSID:"),
        STATION + "x" * 16_384,
        adb.AdbError("unreachable"),
    ],
)
async def test_station_proof_rejects_spoofed_truncated_or_unreadable_status(monkeypatch, reply):
    shell = AsyncMock(
        side_effect=reply if isinstance(reply, Exception) else None, return_value=reply
    )
    monkeypatch.setattr(adb, "shell", shell)
    assert not await radios._transport_is_wifi_station(ADDRESS)
    shell.assert_awaited_once_with(ADDRESS, "cmd wifi status", timeout=radios.RADIO_TIMEOUT_S)
