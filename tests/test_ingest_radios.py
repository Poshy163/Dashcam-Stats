"""Quieting the head unit's Bluetooth and hotspot while a transfer runs.

No device anywhere in here: the control channel is faked and every assertion is about
what would have been typed at the unit's shell, in what order. The invariants under test
are the ones that matter in a car rather than a test rig: nothing is touched during a
fleeting connection, nothing is turned off that cannot be turned back on, and every exit
path — including the ones where the car has already driven away — leaves a way for
Bluetooth to come back.
"""

from __future__ import annotations

import asyncio

import pytest

from app.ingest import adb, radios

#: A dumpsys extract shaped like the modern (Android 13+) rendering, WifiSsid wrapper
#: and all, surrounded by client-side noise that must not be mistaken for the hotspot.
_DUMP_MODERN = """
WifiClientModeImpl:
  current SSID: "HomeNetwork"
WifiApConfigStore:
  mPersistentWifiApConfig SoftApConfiguration{ssid = WifiSsid{"CarSpot"}, bssid = null,
  passphrase = roadtrip99, securityType = 1, band = 2}
"""

_DUMP_BARE = """
WifiApConfigStore
  ssid = CarSpot
  passphrase = roadtrip99
"""


class FakeProcess:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.killed = False

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        return self.returncode or 0


class StubSettings:
    """Just enough of the settings service for the marker reads and writes."""

    def __init__(self, values: dict | None = None) -> None:
        self.values = dict(values or {})

    def get_nowait(self, key, default=None):
        return self.values.get(key, default)

    async def set(self, key, value, *, internal=False):
        self.values[key] = value
        return value


@pytest.fixture
def unit_shell(monkeypatch):
    """A fake control channel: records every command, answers by substring.

    A reply that is an exception is raised, which is how "the car has left" is spelt.
    """
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

    async def no_watchdog(address, deadline_s):
        return None

    monkeypatch.setattr(radios, "_arm_watchdog", no_watchdog)
    stub = StubSettings()
    monkeypatch.setattr(radios, "get_settings_service", lambda: stub)

    class Harness:
        pass

    harness = Harness()
    harness.commands = commands
    harness.replies = replies
    harness.settings = stub
    return harness


def _issued(commands: list[str], fragment: str) -> list[int]:
    return [index for index, command in enumerate(commands) if fragment in command]


class TestTheGuard:
    """Nothing is touched until the unit has been connected for more than ten seconds."""

    def test_the_delay_is_what_remains_of_the_ten_seconds(self):
        assert radios.RadioQuiet("u", online_for=3.0, watchdog_deadline_s=1).delay == 7.0
        assert radios.RadioQuiet("u", online_for=60.0, watchdog_deadline_s=1).delay == 0.0

    async def test_a_run_that_ends_inside_the_guard_touches_nothing(self, unit_shell):
        """The car that was only turning around keeps its phone connection."""
        quiet = radios.begin_quiet("u:5555", online_for=0.0, watchdog_deadline_s=60)
        await quiet.finish()

        assert unit_shell.commands == []
        assert unit_shell.settings.values.get(radios.MARKER_KEY) is None


class TestBluetooth:
    async def test_on_goes_off_for_the_transfer_and_back_on_after(self, unit_shell):
        unit_shell.replies["bluetooth_on"] = "1"
        quiet = radios.begin_quiet("u:5555", online_for=60.0, watchdog_deadline_s=300)
        await quiet._task

        disables = _issued(unit_shell.commands, "cmd bluetooth_manager disable")
        assert disables, "Bluetooth was on and should have been turned off"
        assert unit_shell.settings.values[radios.MARKER_KEY] == "bluetooth"

        await quiet.finish()

        enables = _issued(unit_shell.commands, "cmd bluetooth_manager enable")
        assert enables and disables[0] < enables[0]
        assert _issued(unit_shell.commands, f"rm -f '{radios.FLAG_PATH}'")
        assert unit_shell.settings.values[radios.MARKER_KEY] == ""

    async def test_already_off_is_left_exactly_alone(self, unit_shell):
        """Restore must never mean turning on a radio the operator keeps off."""
        unit_shell.replies["bluetooth_on"] = "0"
        quiet = radios.begin_quiet("u:5555", online_for=60.0, watchdog_deadline_s=300)
        await quiet._task
        await quiet.finish()

        assert not _issued(unit_shell.commands, "bluetooth_manager disable")
        assert not _issued(unit_shell.commands, "bluetooth_manager enable")

    async def test_an_unreadable_state_is_left_alone(self, unit_shell):
        unit_shell.replies["bluetooth_on"] = "null"
        quiet = radios.begin_quiet("u:5555", online_for=60.0, watchdog_deadline_s=300)
        await quiet._task
        await quiet.finish()

        assert not _issued(unit_shell.commands, "bluetooth_manager disable")

    async def test_a_refused_disable_needs_no_restore(self, unit_shell):
        unit_shell.replies["bluetooth_on"] = "1"
        unit_shell.replies["cmd bluetooth_manager disable"] = "Error: unknown"
        unit_shell.replies["svc bluetooth disable"] = adb.AdbError("no svc here")
        quiet = radios.begin_quiet("u:5555", online_for=60.0, watchdog_deadline_s=300)
        await quiet._task

        assert not quiet.bluetooth_off
        assert unit_shell.settings.values[radios.MARKER_KEY] == ""

        await quiet.finish()
        assert not _issued(unit_shell.commands, "bluetooth_manager enable")

    async def test_a_failed_restore_leaves_the_marker_and_the_flag(self, unit_shell):
        """The car left mid-transfer. The watchdog and the next arrival take over."""
        unit_shell.replies["bluetooth_on"] = "1"
        quiet = radios.begin_quiet("u:5555", online_for=60.0, watchdog_deadline_s=300)
        await quiet._task
        assert quiet.bluetooth_off

        unit_shell.replies["cmd bluetooth_manager enable"] = adb.AdbError("car has left")
        unit_shell.replies["svc bluetooth enable"] = adb.AdbError("car has left")
        await quiet.finish()

        assert unit_shell.settings.values[radios.MARKER_KEY] == "bluetooth"
        assert not _issued(unit_shell.commands, f"rm -f '{radios.FLAG_PATH}'")

    async def test_a_successful_restore_stands_down_the_watchdog(self, unit_shell, monkeypatch):
        watchdog = FakeProcess()

        async def armed(address, deadline_s):
            return watchdog

        monkeypatch.setattr(radios, "_arm_watchdog", armed)
        unit_shell.replies["bluetooth_on"] = "1"
        quiet = radios.begin_quiet("u:5555", online_for=60.0, watchdog_deadline_s=300)
        await quiet._task
        await quiet.finish()

        assert watchdog.killed


class TestTheWatchdog:
    async def test_it_is_left_on_the_unit_gated_on_the_flag(self, monkeypatch):
        """The remote command must be able to act with this app entirely gone."""
        captured: dict[str, tuple] = {}

        async def fake_spawn(*args, **kwargs):
            captured["args"] = args
            return FakeProcess()

        monkeypatch.setattr(radios.asyncio, "create_subprocess_exec", fake_spawn)
        monkeypatch.setattr(adb, "adb_path", lambda: "adb")

        await radios._arm_watchdog("u:5555", 300)

        command = captured["args"][-1]
        assert "sleep 300" in command
        assert radios.FLAG_PATH in command
        assert "bluetooth_manager enable" in command
        # The gate: a watchdog whose run already restored must do nothing at all.
        assert f"[ -f '{radios.FLAG_PATH}' ] || exit 0" in command


class TestTheHotspot:
    async def test_seen_running_it_is_stopped_and_started_again(self, unit_shell):
        unit_shell.replies["/sys/class/net"] = "ap0"
        unit_shell.replies["dumpsys wifi"] = _DUMP_MODERN
        unit_shell.replies["bluetooth_on"] = "0"
        quiet = radios.begin_quiet("u:5555", online_for=60.0, watchdog_deadline_s=300)
        await quiet._task

        stops = _issued(unit_shell.commands, "cmd wifi stop-softap")
        assert stops
        assert quiet.hotspot_restore == ("CarSpot", "roadtrip99")

        await quiet.finish()
        starts = _issued(unit_shell.commands, "cmd wifi start-softap 'CarSpot' wpa2 'roadtrip99'")
        assert starts and stops[0] < starts[0]

    async def test_with_no_recoverable_config_it_is_stopped_but_never_guessed_at(self, unit_shell):
        unit_shell.replies["/sys/class/net"] = "ap0"
        unit_shell.replies["dumpsys wifi"] = "nothing useful in here"
        unit_shell.replies["bluetooth_on"] = "0"
        quiet = radios.begin_quiet("u:5555", online_for=60.0, watchdog_deadline_s=300)
        await quiet._task
        await quiet.finish()

        assert _issued(unit_shell.commands, "cmd wifi stop-softap")
        assert not _issued(unit_shell.commands, "start-softap")

    async def test_not_running_it_is_still_asked_to_stop_but_never_started(self, unit_shell):
        """ "Make sure it is off" is the point; a stop on a stopped AP is a no-op."""
        unit_shell.replies["bluetooth_on"] = "0"
        quiet = radios.begin_quiet("u:5555", online_for=60.0, watchdog_deadline_s=300)
        await quiet._task
        await quiet.finish()

        assert _issued(unit_shell.commands, "cmd wifi stop-softap")
        assert not _issued(unit_shell.commands, "start-softap")


class TestTheConfigParse:
    """Scraped text headed for a shell: parsed conservatively, refused on any doubt."""

    def test_the_modern_wifissid_wrapper_is_peeled(self):
        assert radios._parse_softap_config(_DUMP_MODERN) == ("CarSpot", "roadtrip99")

    def test_a_bare_older_rendering_is_accepted(self):
        assert radios._parse_softap_config(_DUMP_BARE) == ("CarSpot", "roadtrip99")

    def test_the_client_sides_network_is_never_mistaken_for_the_hotspot(self):
        dump = 'WifiClientModeImpl:\n  current SSID: "HomeNetwork"\n'
        assert radios._parse_softap_config(dump) is None

    @pytest.mark.parametrize(
        "passphrase",
        ["<redacted>", "short"],
    )
    def test_a_redacted_or_truncated_passphrase_is_refused(self, passphrase):
        dump = f"WifiApConfigStore\n  ssid = CarSpot\n  passphrase = {passphrase}\n"
        assert radios._parse_softap_config(dump) is None

    def test_a_hostile_ssid_never_reaches_a_shell(self):
        dump = 'WifiApConfigStore\n  ssid = "a\'; rm -rf /"\n  passphrase = roadtrip99\n'
        assert radios._parse_softap_config(dump) is None


class TestTheArrivalRestore:
    """The engine stopped mid-transfer yesterday; the car is back on the driveway."""

    async def test_a_pending_marker_turns_bluetooth_back_on(self, unit_shell):
        unit_shell.settings.values[radios.MARKER_KEY] = "bluetooth"

        radios.restore_if_pending("u:5555")
        await asyncio.gather(*list(radios._tasks))

        assert _issued(unit_shell.commands, "cmd bluetooth_manager enable")
        assert _issued(unit_shell.commands, f"rm -f '{radios.FLAG_PATH}'")
        assert unit_shell.settings.values[radios.MARKER_KEY] == ""

    async def test_a_failed_attempt_keeps_the_marker_for_the_next_arrival(self, unit_shell):
        unit_shell.settings.values[radios.MARKER_KEY] = "bluetooth"
        unit_shell.replies["cmd bluetooth_manager enable"] = adb.AdbError("gone again")
        unit_shell.replies["svc bluetooth enable"] = adb.AdbError("gone again")

        radios.restore_if_pending("u:5555")
        await asyncio.gather(*list(radios._tasks))

        assert unit_shell.settings.values[radios.MARKER_KEY] == "bluetooth"

    async def test_nothing_pending_spawns_nothing(self, unit_shell):
        radios.restore_if_pending("u:5555")

        assert not radios._tasks
        assert unit_shell.commands == []


class TestOnlineFor:
    def test_the_clock_starts_at_first_sight_and_stops_at_departure(self):
        from app.ingest.status import IngestStatus

        status = IngestStatus()
        assert status.online_for() == 0.0

        status.set_unit_online(True)
        started = status._online_since
        assert status.online_for() >= 0.0

        # Staying online must not restart the clock: the guard would never elapse.
        status.set_unit_online(True)
        assert status._online_since == started

        status.set_unit_online(False)
        assert status.online_for() == 0.0
