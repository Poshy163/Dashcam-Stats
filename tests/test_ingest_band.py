"""Holding a transfer for the 5 GHz band.

Same discipline as the radio-quieting tests: no device, a faked control channel, and
every assertion about what would have been typed at the unit's shell. The invariants
that matter are the protective ones — an unreadable answer never holds a backup, a hold
never masquerades as a fault, and nothing in here can ever disable the unit's WiFi.
"""

from __future__ import annotations

import pytest

from app.ingest import adb, band
from app.ingest.status import get_status, reset_status_for_tests

#: `cmd wifi status` as Android 15 actually prints it (WifiShellCommand `status`, with
#: WifiInfo#toString inlined) — connected on 5 GHz.
_STATUS_5G = (
    "Wifi is enabled\n"
    "==== Primary ClientModeManager instance ====\n"
    'Wifi is connected to "HomeNet"\n'
    'WifiInfo: SSID: "HomeNet", BSSID: a4:2b:8c:11:22:33, MAC: 02:00:00:00:00:00, '
    "IP: /192.168.1.122, Security type: 2, Supplicant state: COMPLETED, "
    "Wi-Fi standard: 11ac, RSSI: -48, Link speed: 433Mbps, Frequency: 5220MHz, "
    "Net ID: 3, Metered hint: false, score: 60"
)

_STATUS_24G = _STATUS_5G.replace("Frequency: 5220MHz", "Frequency: 2437MHz").replace(
    "Link speed: 433Mbps", "Link speed: 72Mbps"
)

#: `cmd wifi list-scan-results` rows, in ScanResultUtil.dumpScanResults's printf shape —
#: the RSSI column carries the per-chain suffix on real output.
_SCAN_WITH_5G = (
    "    BSSID              Frequency      RSSI           Age(sec)     SSID          Flags\n"
    "  a4:2b:8c:11:22:33       5220        -52(0:-52)       2.500    HomeNet        [WPA2-PSK-CCMP][ESS]\n"
    "  a4:2b:8c:11:22:34       2437        -40(0:-40)       2.500    HomeNet        [WPA2-PSK-CCMP][ESS]\n"
)

_SCAN_24_ONLY = (
    "    BSSID              Frequency      RSSI           Age(sec)     SSID          Flags\n"
    "  a4:2b:8c:11:22:34       2437        -40(0:-40)       2.500    HomeNet        [WPA2-PSK-CCMP][ESS]\n"
)

_SCAN_5G_TOO_WEAK = (
    "    BSSID              Frequency      RSSI           Age(sec)     SSID          Flags\n"
    "  a4:2b:8c:11:22:33       5220        -86(0:-86)       2.500    HomeNet        [WPA2-PSK-CCMP][ESS]\n"
)


class StubSettings:
    def __init__(self, values: dict | None = None) -> None:
        self.values = dict(values or {})

    def get_nowait(self, key, default=None):
        return self.values.get(key, default)


@pytest.fixture
def unit_shell(monkeypatch):
    """A fake control channel, same shape as the radio tests'."""
    commands: list[str] = []
    replies: dict[str, object] = {}

    async def shell(address, command, **kwargs):
        commands.append(command)
        for key, value in replies.items():
            if key in command:
                if isinstance(value, Exception):
                    raise value
                return value
        return ""

    monkeypatch.setattr(adb, "shell", shell)
    monkeypatch.setattr(band, "SCAN_SETTLE_S", 0.0)

    stub = StubSettings()
    monkeypatch.setattr(band, "get_settings_service", lambda: stub)

    class Harness:
        pass

    harness = Harness()
    harness.commands = commands
    harness.replies = replies
    harness.settings = stub
    return harness


@pytest.fixture(autouse=True)
def clean_module_state():
    reset_status_for_tests()
    yield
    reset_status_for_tests()


class TestParsing:
    def test_the_real_status_output_yields_frequency_and_ssid(self):
        assert band.parse_link(_STATUS_5G) == (5220, "HomeNet")
        assert band.parse_link(_STATUS_24G) == (2437, "HomeNet")

    def test_an_unrecognisable_reply_is_unknown_not_a_guess(self):
        assert band.parse_link("cmd: Can't find service: wifi") == (None, "")
        assert band.parse_link("") == (None, "")

    def test_band_boundaries(self):
        assert not band.is_fast(2412)
        assert not band.is_fast(2484)
        assert band.is_fast(5180)
        assert band.is_fast(5955), "a 6GHz unit deserves a pass, not a hold"

    def test_scan_finds_the_home_network_on_5ghz(self):
        assert band.parse_scan_for_5g(_SCAN_WITH_5G, "HomeNet") is True

    def test_scan_with_only_24_says_no(self):
        assert band.parse_scan_for_5g(_SCAN_24_ONLY, "HomeNet") is False

    def test_a_neighbours_5ghz_does_not_count(self):
        scan = (
            "  de:ad:be:ef:00:01       5745        -50(0:-50)       3.000    NextDoor5    [ESS]\n"
        )
        assert band.parse_scan_for_5g(scan, "HomeNet") is False

    def test_a_5ghz_android_would_not_associate_with_does_not_count(self):
        assert band.parse_scan_for_5g(_SCAN_5G_TOO_WEAK, "HomeNet") is False

    def test_nothing_parseable_is_unknown(self):
        assert band.parse_scan_for_5g("No scan results", "HomeNet") is None
        assert band.parse_scan_for_5g("", "HomeNet") is None


class TestTheGate:
    async def test_policy_any_asks_the_unit_nothing(self, unit_shell):
        assert await band.gate("u:5555")
        assert unit_shell.commands == []

    async def test_on_5ghz_the_transfer_proceeds_without_a_scan(self, unit_shell):
        unit_shell.settings.values["ingest.wifi_band"] = "require_5ghz"
        unit_shell.replies["cmd wifi status"] = _STATUS_5G
        assert await band.gate("u:5555")
        assert not [c for c in unit_shell.commands if "start-scan" in c]
        assert get_status().wifi_frequency_mhz == 5220
        assert not get_status().wifi_band_hold

    async def test_require_holds_on_24_and_says_why(self, unit_shell):
        unit_shell.settings.values["ingest.wifi_band"] = "require_5ghz"
        unit_shell.replies["cmd wifi status"] = _STATUS_24G
        unit_shell.replies["list-scan-results"] = _SCAN_WITH_5G

        assert not await band.gate("u:5555")
        status = get_status()
        assert status.wifi_band_hold
        assert status.wifi_band_hold_reason and "2.4" in status.wifi_band_hold_reason

    async def test_the_hold_reason_distinguishes_the_two_causes(self, unit_shell):
        """ "5GHz is there and it chose 2.4" and "5GHz is not reachable from here" want
        completely different things from the operator, so they must not read alike."""
        unit_shell.settings.values["ingest.wifi_band"] = "require_5ghz"
        unit_shell.replies["cmd wifi status"] = _STATUS_24G

        unit_shell.replies["list-scan-results"] = _SCAN_WITH_5G
        await band.gate("u:5555")
        in_range = get_status().wifi_band_hold_reason

        unit_shell.replies["list-scan-results"] = _SCAN_24_ONLY
        await band.gate("u:5555")
        out_of_range = get_status().wifi_band_hold_reason

        assert in_range != out_of_range
        assert "5GHz-only SSID" in in_range
        assert "not reachable on 5GHz" in out_of_range

    async def test_prefer_warns_but_copies_on_24(self, unit_shell):
        unit_shell.settings.values["ingest.wifi_band"] = "prefer_5ghz"
        unit_shell.replies["cmd wifi status"] = _STATUS_24G
        assert await band.gate("u:5555")
        assert not get_status().wifi_band_hold

    async def test_an_unreadable_band_never_holds_a_backup(self, unit_shell):
        """Degrade to the behaviour the feature shipped without, not a silent stop."""
        unit_shell.settings.values["ingest.wifi_band"] = "require_5ghz"
        unit_shell.replies["cmd wifi status"] = adb.AdbError("car has left")
        assert await band.gate("u:5555")

    async def test_an_unparseable_band_never_holds_a_backup(self, unit_shell):
        unit_shell.settings.values["ingest.wifi_band"] = "require_5ghz"
        unit_shell.replies["cmd wifi status"] = "cmd: Can't find service: wifi"
        assert await band.gate("u:5555")


class TestTheHoldIsCleared:
    async def test_switching_back_to_any_clears_a_standing_hold(self, unit_shell):
        """The Backup page hides its real error banner while a hold is showing, so a
        stale hold does not merely look wrong -- it conceals genuine failures behind a
        reassuring explanation."""
        unit_shell.settings.values["ingest.wifi_band"] = "require_5ghz"
        unit_shell.replies["cmd wifi status"] = _STATUS_24G
        unit_shell.replies["list-scan-results"] = _SCAN_WITH_5G
        await band.gate("u:5555")
        assert get_status().wifi_band_hold

        unit_shell.settings.values["ingest.wifi_band"] = "any"
        assert await band.gate("u:5555")
        assert not get_status().wifi_band_hold
        assert get_status().wifi_band_hold_reason is None

    async def test_a_settings_failure_reading_as_any_also_clears(self, unit_shell, monkeypatch):
        unit_shell.settings.values["ingest.wifi_band"] = "require_5ghz"
        unit_shell.replies["cmd wifi status"] = _STATUS_24G
        unit_shell.replies["list-scan-results"] = _SCAN_WITH_5G
        await band.gate("u:5555")
        assert get_status().wifi_band_hold

        def exploding():
            raise RuntimeError("settings are gone")

        monkeypatch.setattr(band, "get_settings_service", exploding)
        assert await band.gate("u:5555")
        assert not get_status().wifi_band_hold

    async def test_reaching_5ghz_clears_a_previous_hold(self, unit_shell):
        unit_shell.settings.values["ingest.wifi_band"] = "require_5ghz"
        unit_shell.replies["cmd wifi status"] = _STATUS_24G
        unit_shell.replies["list-scan-results"] = _SCAN_WITH_5G
        await band.gate("u:5555")
        assert get_status().wifi_band_hold

        unit_shell.replies["cmd wifi status"] = _STATUS_5G
        assert await band.gate("u:5555")
        assert not get_status().wifi_band_hold
        assert get_status().wifi_band_hold_reason is None

    async def test_the_car_leaving_clears_the_hold(self, unit_shell):
        unit_shell.settings.values["ingest.wifi_band"] = "require_5ghz"
        unit_shell.replies["cmd wifi status"] = _STATUS_24G
        unit_shell.replies["list-scan-results"] = _SCAN_WITH_5G
        await band.gate("u:5555")

        get_status().set_unit_online(False)
        assert not get_status().wifi_band_hold
        assert get_status().wifi_band_hold_reason is None
        assert get_status().wifi_frequency_mhz is None


class TestNothingHereCanDisableTheUnitsWifi:
    """The bounce that was written and taken out again.

    `set-wifi-enabled disabled` persists WIFI_ON=0 and cuts the link its own shell rides
    on, and this unit has no battery -- so an engine stopping in that window leaves it
    deaf at every future start, recoverable only by hand in the car. No path through this
    module may ever issue it.
    """

    def test_the_module_contains_no_wifi_disable_anywhere(self):
        from pathlib import Path

        source = Path(band.__file__).read_text(encoding="utf-8")
        code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
        # The docstring explains at length why this is absent; strip it before looking.
        body = code.split('"""', 2)[-1]
        assert "set-wifi-enabled disabled" not in body
        assert "svc wifi disable" not in body

    async def test_no_gate_path_issues_a_state_changing_wifi_command(self, unit_shell):
        """Every reachable branch, checked for what it actually typed at the unit."""
        for policy in ("any", "prefer_5ghz", "require_5ghz"):
            for status_reply in (_STATUS_5G, _STATUS_24G, "nonsense", ""):
                unit_shell.commands.clear()
                unit_shell.settings.values["ingest.wifi_band"] = policy
                unit_shell.replies["cmd wifi status"] = status_reply
                unit_shell.replies["list-scan-results"] = _SCAN_WITH_5G
                await band.gate("u:5555")
                for command in unit_shell.commands:
                    assert "disable" not in command, command
                    assert "set-wifi-enabled" not in command, command
