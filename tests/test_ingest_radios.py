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
                if isinstance(value, list):
                    # Sequenced answers, for the probes that get asked twice -- "is the
                    # hotspot serving" before a stop, and again to prove the stop worked.
                    # The final entry sticks once the sequence runs out.
                    value = value.pop(0) if len(value) > 1 else value[0]
                if isinstance(value, Exception):
                    raise value
                return value
        return ""

    monkeypatch.setattr(adb, "shell", shell)

    async def shell_script(address, script, **kwargs):
        return await shell(address, script, **kwargs)

    monkeypatch.setattr(radios, "_shell_script", shell_script)

    async def no_watchdog(address, deadline_s, **kwargs):
        return None

    monkeypatch.setattr(radios, "_arm_watchdog", no_watchdog)
    # The stop's settle budget is a real few seconds against a car; here it only needs to
    # be long enough to poll the fake `ip` output a couple of times, so it is squeezed to
    # keep the "the stop never took" tests off the wall clock.
    monkeypatch.setattr(radios, "STOP_SETTLE_BUDGET_S", 0.05)
    monkeypatch.setattr(radios, "STOP_SETTLE_POLL_S", 0.01)
    monkeypatch.setattr(radios, "HOTSPOT_REARM_STABILITY_S", 0.005)
    stub = StubSettings()
    monkeypatch.setattr(radios, "get_settings_service", lambda: stub)
    # The arrival restore is debounced per address across the process, so one test's
    # attempt would otherwise decide whether the next one is allowed to make its own.
    monkeypatch.setattr(radios, "_last_restore", {})

    class Harness:
        pass

    harness = Harness()
    harness.commands = commands
    harness.replies = replies
    harness.settings = stub
    return harness


def _issued(commands: list[str], fragment: str) -> list[int]:
    return [index for index, command in enumerate(commands) if fragment in command]


def test_hotspot_passphrase_is_excluded_from_radio_object_representations():
    snapshot = radios.RadioSnapshot(
        bluetooth="on",
        hotspot="on",
        hotspot_config=("CarSpot", "roadtrip99"),
    )
    quiet = radios.RadioQuiet(
        "unit",
        online_for=60,
        watchdog_deadline_s=120,
        hotspot_restore=("CarSpot", "roadtrip99"),
    )
    assert "roadtrip99" not in repr(snapshot)
    assert "roadtrip99" not in repr(quiet)


async def test_hotspot_credentials_travel_over_stdin_not_process_argv(monkeypatch):
    captured = {}

    class ScriptProcess:
        returncode = 0

        async def communicate(self, body):
            captured["body"] = body
            return b"ok\n", b""

    async def spawn(*args, **kwargs):
        captured["args"] = args
        return ScriptProcess()

    monkeypatch.setattr(radios.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(adb, "adb_path", lambda: "adb")
    reply = await radios._shell_script(
        "unit:5555",
        "cmd wifi start-softap 'CarSpot' wpa2 'roadtrip99'",
        timeout=1,
    )

    assert reply == "ok"
    assert "roadtrip99" not in " ".join(captured["args"])
    assert b"roadtrip99" in captured["body"]


async def test_device_identity_is_stable_across_reboots_without_storing_serial(monkeypatch):
    boot_one = "01234567-89ab-cdef-0123-456789abcdef"
    boot_two = "11111111-2222-3333-4444-555555555555"

    async def shell(_address, _command, **_kwargs):
        return f"dashcam-serial-42\n{boot_one}"

    monkeypatch.setattr(adb, "shell", shell)
    first = await radios.read_device_boot_id("unit:5555")
    assert first is not None and first.endswith(f"@{boot_one}")
    assert "dashcam-serial-42" not in first
    fingerprint = first.partition("@")[0]
    assert radios.same_device_identity(first, f"{fingerprint}@{boot_two}")
    assert not radios.same_device_identity(first, f"{'f' * 32}@{boot_two}")


async def test_zlink_package_gate_is_positive_and_bounded(unit_shell):
    unit_shell.replies["pm path com.zjinnova.zlink"] = (
        "package:/system/app/CarZhiJian/CarZhiJian.apk\n"
        "6.1.02\n"
        "600102\n"
        "c8f43e1a2dbd957220194f59ded0eb64581a571fde59d55386e6c5b4d49967d3"
    )
    assert await radios.supports_zlink_bluetooth_rearm("unit:5555")
    assert len(_issued(unit_shell.commands, "pm path com.zjinnova.zlink")) == 1


async def test_zlink_package_gate_retries_a_transient_attestation_failure(monkeypatch):
    replies = iter(
        [
            adb.AdbError("adb transport temporarily unavailable"),
            (
                "package:/system/app/CarZhiJian/CarZhiJian.apk\n"
                "6.1.02\n"
                "600102\n"
                "c8f43e1a2dbd957220194f59ded0eb64581a571fde59d55386e6c5b4d49967d3"
            ),
        ]
    )

    async def shell(_address, _command, **_kwargs):
        reply = next(replies)
        if isinstance(reply, Exception):
            raise reply
        return reply

    async def no_wait(_delay):
        return None

    monkeypatch.setattr(adb, "shell", shell)
    monkeypatch.setattr(radios.asyncio, "sleep", no_wait)
    assert await radios.supports_zlink_bluetooth_rearm("unit:5555")


@pytest.mark.parametrize(
    "reply",
    [
        "package:/data/app/~~random/com.zjinnova.zlink/base.apk\n6.1.02\n600102\n",
        "package:/system/app/CarZhiJian/renamed.apk\n6.1.02\n600102\n",
        "package:/system/app/CarZhiJian/CarZhiJian.apk\n6.1.03\n600102\n"
        "c8f43e1a2dbd957220194f59ded0eb64581a571fde59d55386e6c5b4d49967d3",
        "package:/system/app/CarZhiJian/CarZhiJian.apk\n6.1.02\n600103\n"
        "c8f43e1a2dbd957220194f59ded0eb64581a571fde59d55386e6c5b4d49967d3",
        "package:/system/app/CarZhiJian/CarZhiJian.apk\n6.1.02\n600102\n"
        "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
        "package:/system/app/CarZhiJian/CarZhiJian.apk\n6.1.02\n600102\n",
        "",
    ],
)
async def test_zlink_package_gate_rejects_unapproved_install_or_build(unit_shell, reply):
    unit_shell.replies["pm path com.zjinnova.zlink"] = reply
    assert not await radios.supports_zlink_bluetooth_rearm("unit:5555")


@pytest.mark.parametrize(
    "body",
    [
        '{"schema_version":2,"restore_mode":"other"}',
        '{"schema_version":2,"restore_mode":"bluetooth_rearm","extra":true}',
        '{"schema_version":3,"restore_mode":"bluetooth_rearm"}',
    ],
)
async def test_bluetooth_rearm_capsule_parser_rejects_schema_drift(body):
    controller = radios.RadioController("unit:5555", watchdog_deadline_s=300)

    async def read(_path):
        return body

    controller._read_hotspot_capsule = read
    path = f"{radios.HOTSPOT_CAPSULE_PREFIX}01234567-89ab-cdef-0123-456789abcdef.json"
    assert await controller.read_hotspot_recovery_plan(path) is None


async def test_bluetooth_rearm_capsule_is_credential_free_and_versioned():
    controller = radios.RadioController("unit:5555", watchdog_deadline_s=300)
    body = '{"schema_version":2,"restore_mode":"bluetooth_rearm"}'

    async def read(_path):
        return body

    controller._read_hotspot_capsule = read
    path = f"{radios.HOTSPOT_CAPSULE_PREFIX}01234567-89ab-cdef-0123-456789abcdef.json"
    plan = await controller.read_hotspot_recovery_plan(path)
    assert plan == radios.HotspotRecoveryPlan(radios.HOTSPOT_RESTORE_BLUETOOTH_REARM)
    assert "ssid" not in body and "passphrase" not in body


@pytest.fixture(autouse=True)
def clean_module_state():
    """The claim counter and the restore tasks are module globals; no test may inherit
    another's. A leaked claim would silently disable the arrival restore for the rest of
    the session, which is exactly the failure these tests exist to catch."""
    radios._active = 0
    radios._tasks.clear()
    yield
    radios._active = 0
    radios._tasks.clear()


#: An address on the AP's own subnet, as a soft AP always holds while it is serving.
_IP_WITH_AP = (
    "1: lo    inet 127.0.0.1/8 scope host lo\n"
    "23: wlan0    inet 192.168.1.122/24 brd 192.168.1.255 scope global wlan0\n"
    "25: ap0    inet 192.168.43.1/24 brd 192.168.43.255 scope global ap0\n"
)

_IP_NO_AP = (
    "1: lo    inet 127.0.0.1/8 scope host lo\n"
    "23: wlan0    inet 192.168.1.122/24 brd 192.168.1.255 scope global wlan0\n"
)

#: What an unrooted unit actually answers to `cmd wifi stop-softap`. WifiManager.stopSoftAp
#: gates on NETWORK_STACK/MAINLINE_NETWORK_STACK, which the shell (uid 2000) does not hold,
#: so the wifi command throws — which is why the real stop rides the tethering binder, and
#: this command is only the fallback for a rooted/debuggable unit.
_SECURITY_EXCEPTION = (
    "java.lang.SecurityException: Neither user 2000 nor current process has "
    "android.permission.MAINLINE_NETWORK_STACK."
)

#: The tests that care about the hotspot must use an address the fake `ip` output agrees
#: with, because the interface carrying it is excluded by address rather than by name.
UNIT = "192.168.1.122:5555"


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

        async def armed(address, deadline_s, **kwargs):
            return watchdog

        monkeypatch.setattr(radios, "_arm_watchdog", armed)
        unit_shell.replies["bluetooth_on"] = "1"
        quiet = radios.begin_quiet("u:5555", online_for=60.0, watchdog_deadline_s=300)
        await quiet._task
        await quiet.finish()

        assert watchdog.killed

    async def test_an_enable_that_does_not_stick_keeps_every_layer_in_place(
        self, unit_shell, monkeypatch
    ):
        """A clean `enable` is not proof the radio came back.

        If it is still off, the marker, the flag and the watchdog are the only things
        left holding the promise -- so all three have to survive. Clearing them on an
        unverified success is what would strand the driver's hands-free with nothing at
        all left to repair it.
        """
        watchdog = FakeProcess()

        async def armed(address, deadline_s, **kwargs):
            return watchdog

        monkeypatch.setattr(radios, "_arm_watchdog", armed)
        # On when asked at the start of the window, still off when asked after the enable.
        unit_shell.replies["bluetooth_on"] = ["1", "0"]

        quiet = radios.begin_quiet("u:5555", online_for=60.0, watchdog_deadline_s=300)
        await quiet._task
        await quiet.finish()

        assert unit_shell.settings.values[radios.MARKER_KEY] == "bluetooth"
        assert not _issued(unit_shell.commands, f"rm -f '{radios.FLAG_PATH}'")
        assert not watchdog.killed, "the watchdog stood down over an unconfirmed restore"


class TestTheWatchdog:
    async def test_it_is_left_on_the_unit_gated_on_the_flag(self, monkeypatch):
        """The remote command must be able to act with this app entirely gone."""
        captured: dict[str, tuple] = {}

        async def fake_spawn(*args, **kwargs):
            captured["args"] = args
            return FakeProcess()

        async def fake_shell(address, command, **kwargs):
            assert address == "u:5555"
            return "armed" if radios.WATCHDOG_READY_PATH in command else ""

        monkeypatch.setattr(radios.asyncio, "create_subprocess_exec", fake_spawn)
        monkeypatch.setattr(adb, "adb_path", lambda: "adb")
        monkeypatch.setattr(adb, "shell", fake_shell)

        await radios._arm_watchdog("u:5555", 300)

        command = captured["args"][-1]
        assert "sleep 300" in command
        assert radios.FLAG_PATH in command
        assert "bluetooth_manager enable" in command
        # The gate: a watchdog whose run already restored must do nothing at all.
        assert f"[ -f '{radios.FLAG_PATH}' ] ||" in command
        assert radios.WATCHDOG_READY_PATH in command

    @pytest.mark.parametrize(
        ("hotspot_baseline", "with_capsule", "expected", "forbidden"),
        [
            (
                "off",
                False,
                ("bluetooth_manager enable", "sleep 6", "service call tethering"),
                ("start-softap",),
            ),
            (
                "on",
                True,
                ("bluetooth_manager enable", "sleep 6", "start-softap"),
                ("service call tethering",),
            ),
            (
                "transport",
                False,
                ("bluetooth_manager enable",),
                ("sleep 6", "service call tethering", "start-softap"),
            ),
        ],
    )
    async def test_crash_watchdog_restores_exact_hotspot_baseline_after_bluetooth(
        self,
        monkeypatch,
        hotspot_baseline,
        with_capsule,
        expected,
        forbidden,
    ):
        captured: dict[str, tuple] = {}
        capsule = (
            f"{radios.HOTSPOT_CAPSULE_PREFIX}01234567-89ab-cdef-0123-456789abcdef.json"
            if with_capsule
            else None
        )

        async def fake_spawn(*args, **kwargs):
            captured["args"] = args
            return FakeProcess()

        async def fake_shell(_address, command, **_kwargs):
            return "armed" if radios.WATCHDOG_READY_PATH in command else ""

        monkeypatch.setattr(radios.asyncio, "create_subprocess_exec", fake_spawn)
        monkeypatch.setattr(adb, "adb_path", lambda: "adb")
        monkeypatch.setattr(adb, "shell", fake_shell)

        assert await radios._arm_watchdog(
            "u:5555",
            300,
            restore_bluetooth=True,
            hotspot_baseline=hotspot_baseline,
            hotspot_capsule_path=capsule,
        )
        command = captured["args"][-1]
        positions = [command.index(fragment) for fragment in expected]
        assert positions == sorted(positions)
        assert all(fragment not in command for fragment in forbidden)

    async def test_hotspot_watchdog_retains_recovery_capsule_until_verified(self, monkeypatch):
        captured: dict[str, tuple] = {}
        capsule = f"{radios.HOTSPOT_CAPSULE_PREFIX}01234567-89ab-cdef-0123-456789abcdef.json"

        async def fake_spawn(*args, **kwargs):
            captured["args"] = args
            return FakeProcess()

        async def fake_shell(_address, command, **_kwargs):
            return "armed" if radios.WATCHDOG_READY_PATH in command else ""

        monkeypatch.setattr(radios.asyncio, "create_subprocess_exec", fake_spawn)
        monkeypatch.setattr(adb, "adb_path", lambda: "adb")
        monkeypatch.setattr(adb, "shell", fake_shell)

        assert await radios._arm_watchdog(
            "u:5555",
            300,
            restore_bluetooth=False,
            hotspot_capsule_path=capsule,
        )
        command = captured["args"][-1]
        assert "start-softap" in command and capsule in command
        assert 'rm -f "$capsule"' not in command
        assert f"rm -f '{radios.FLAG_PATH}'" not in command

    async def test_bluetooth_rearm_watchdog_never_invents_credentials_or_vendor_actions(
        self, monkeypatch
    ):
        captured: dict[str, tuple] = {}
        capsule = f"{radios.HOTSPOT_CAPSULE_PREFIX}01234567-89ab-cdef-0123-456789abcdef.json"

        async def fake_spawn(*args, **kwargs):
            captured["args"] = args
            return FakeProcess()

        async def fake_shell(_address, command, **_kwargs):
            return "armed" if radios.WATCHDOG_READY_PATH in command else ""

        monkeypatch.setattr(radios.asyncio, "create_subprocess_exec", fake_spawn)
        monkeypatch.setattr(adb, "adb_path", lambda: "adb")
        monkeypatch.setattr(adb, "shell", fake_shell)

        assert await radios._arm_watchdog(
            "u:5555",
            300,
            restore_bluetooth=True,
            hotspot_baseline="on",
            hotspot_capsule_path=capsule,
            hotspot_restore_mode=radios.HOTSPOT_RESTORE_BLUETOOTH_REARM,
        )
        command = captured["args"][-1]
        # The strategy was validated before arming. Capsule loss after the radios go down
        # must not prevent recovery of the independently captured Bluetooth baseline.
        assert capsule not in command
        assert command.index("bluetooth_manager enable") < command.index("sleep 6")
        assert "start-softap" not in command
        assert "com.zjinnova.zlink.action" not in command

        assert not await radios._arm_watchdog(
            "u:5555",
            300,
            restore_bluetooth=False,
            hotspot_baseline="on",
            hotspot_capsule_path=capsule,
            hotspot_restore_mode=radios.HOTSPOT_RESTORE_BLUETOOTH_REARM,
        )

    async def test_bluetooth_is_not_touched_without_a_proven_remote_watchdog(self, unit_shell):
        unit_shell.replies["bluetooth_on"] = "1"
        controller = radios.RadioController("u:5555", watchdog_deadline_s=300)
        controller.claim()

        assert not await controller.disable_bluetooth()

        assert not _issued(unit_shell.commands, "bluetooth_manager disable")
        assert unit_shell.settings.values[radios.MARKER_KEY] == ""
        await controller.release()

    @pytest.mark.parametrize(
        ("address", "interfaces", "expected_baseline"),
        [
            (
                UNIT,
                "23: eth0    inet 192.168.1.122/24 brd 192.168.1.255 scope global eth0\n",
                "off",
            ),
            (
                "192.168.43.1:5555",
                "25: ap0    inet 192.168.43.1/24 brd 192.168.43.255 scope global ap0\n",
                "transport",
            ),
        ],
    )
    async def test_controller_arms_watchdog_with_persisted_hotspot_classification(
        self, unit_shell, monkeypatch, address, interfaces, expected_baseline
    ):
        observed = {}

        async def armed(_address, _deadline_s, **kwargs):
            observed.update(kwargs)
            return FakeProcess()

        monkeypatch.setattr(radios, "_arm_watchdog", armed)
        unit_shell.replies["bluetooth_on"] = ["1", "0"]
        unit_shell.replies["ip -o addr"] = interfaces
        controller = radios.RadioController(address, watchdog_deadline_s=300)
        controller.claim()
        try:
            snapshot = await controller.capture()
            assert snapshot.hotspot == expected_baseline
            assert await controller.disable_bluetooth()
        finally:
            await controller.release()

        assert observed["hotspot_baseline"] == expected_baseline

    async def test_final_safety_guard_runs_after_watchdog_before_bluetooth(
        self, unit_shell, monkeypatch
    ):
        order = []

        async def armed(_address, _deadline_s, **_kwargs):
            order.append("watchdog")
            return FakeProcess()

        async def guard():
            order.append("guard")
            assert not _issued(unit_shell.commands, "bluetooth_manager disable")

        monkeypatch.setattr(radios, "_arm_watchdog", armed)
        unit_shell.replies["bluetooth_on"] = ["1", "0"]
        unit_shell.replies["ip -o addr"] = _IP_NO_AP
        controller = radios.RadioController(UNIT, watchdog_deadline_s=300)
        controller.claim()
        try:
            await controller.capture()
            assert await controller.disable_bluetooth(before_change=guard)
        finally:
            await controller.release()

        assert order == ["watchdog", "guard"]
        assert _issued(unit_shell.commands, "bluetooth_manager disable")


class TestTheHotspot:
    """Stopping a soft AP from an unrooted shell rides the tethering binder, not the wifi
    service -- ``stopTethering`` gates on TETHER_PRIVILEGED, which the shell holds, while
    ``stopSoftAp`` gates on a network-stack permission it does not."""

    async def test_a_serving_hotspot_is_stopped_and_started_again(self, unit_shell):
        unit_shell.replies["bluetooth_on"] = "0"
        # Serving before the stop, gone after it -- the only evidence that counts.
        unit_shell.replies["ip -o addr"] = [_IP_WITH_AP, _IP_NO_AP]
        unit_shell.replies["dumpsys wifi"] = _DUMP_MODERN

        quiet = radios.begin_quiet(UNIT, online_for=60.0, watchdog_deadline_s=300)
        await quiet._task

        stops = _issued(unit_shell.commands, "service call tethering")
        assert stops, "the hotspot stop must go through the tethering binder"
        assert not _issued(unit_shell.commands, "cmd wifi stop-softap"), (
            "the wifi fallback must not be reached once the binder has taken the AP down"
        )
        assert quiet.hotspot_restore == ("CarSpot", "roadtrip99")
        assert unit_shell.settings.values[radios.REFUSAL_KEY] == "", (
            "a stop that worked must clear any standing refusal"
        )

        await quiet.finish()
        starts = _issued(unit_shell.commands, "cmd wifi start-softap 'CarSpot' wpa2 'roadtrip99'")
        assert starts and stops[0] < starts[0]

    async def test_bluetooth_is_taken_down_before_the_hotspot_is_stopped(self, unit_shell):
        """Load-bearing order: while Bluetooth is on, this unit re-arms the soft AP within
        seconds of any stop, so the AP must not be stopped until Bluetooth is already down.
        Getting this backwards is a stop that never sticks."""
        unit_shell.replies["bluetooth_on"] = "1"
        unit_shell.replies["ip -o addr"] = [_IP_WITH_AP, _IP_NO_AP]
        unit_shell.replies["dumpsys wifi"] = _DUMP_MODERN

        quiet = radios.begin_quiet(UNIT, online_for=60.0, watchdog_deadline_s=300)
        await quiet._task

        disables = _issued(unit_shell.commands, "cmd bluetooth_manager disable")
        stops = _issued(unit_shell.commands, "service call tethering")
        assert disables and stops
        assert disables[0] < stops[0], "the hotspot was stopped before Bluetooth came down"

    async def test_a_unit_that_refuses_the_stop_is_reported_rather_than_believed(self, unit_shell):
        """A unit where neither lever takes the AP down -- the binder does nothing (no
        TETHER_PRIVILEGED, or a code the vendor moved) and the wifi fallback throws. The
        AP keeps beaconing through every transfer, and the first version of this said
        nothing at all about it; now the unit's own words are carried to the Settings
        page."""
        unit_shell.replies["bluetooth_on"] = "0"
        unit_shell.replies["ip -o addr"] = _IP_WITH_AP  # it never goes away
        unit_shell.replies["dumpsys wifi"] = _DUMP_MODERN
        unit_shell.replies["stop-softap"] = _SECURITY_EXCEPTION

        quiet = radios.begin_quiet(UNIT, online_for=60.0, watchdog_deadline_s=300)
        await quiet._task
        await quiet.finish()

        assert quiet.hotspot_restore is None, "a hotspot that never stopped was restarted"
        assert not _issued(unit_shell.commands, "start-softap")
        assert "MAINLINE_NETWORK_STACK" in unit_shell.settings.values[radios.REFUSAL_KEY], (
            "the unit's own words must outlive the log line, on the Settings page"
        )

    async def test_a_stop_that_is_accepted_but_changes_nothing_is_a_failure(self, unit_shell):
        """A zero exit status is not the question. Whether it is still serving is."""
        unit_shell.replies["bluetooth_on"] = "0"
        unit_shell.replies["ip -o addr"] = _IP_WITH_AP
        unit_shell.replies["dumpsys wifi"] = _DUMP_MODERN

        quiet = radios.begin_quiet(UNIT, online_for=60.0, watchdog_deadline_s=300)
        await quiet._task
        await quiet.finish()

        assert quiet.hotspot_restore is None
        assert not _issued(unit_shell.commands, "start-softap")

    async def test_a_stop_is_not_verified_when_adb_disappears_during_observation(self, unit_shell):
        """Losing the inventory reply is not evidence that the AP went away."""
        unit_shell.replies["bluetooth_on"] = "0"
        unit_shell.replies["ip -o addr"] = [
            _IP_WITH_AP,
            adb.AdbError("transport disappeared after stop"),
        ]
        unit_shell.replies["dumpsys wifi"] = _DUMP_MODERN

        quiet = radios.begin_quiet(UNIT, online_for=60.0, watchdog_deadline_s=300)
        await quiet._task
        await quiet.finish()

        assert _issued(unit_shell.commands, "service call tethering")
        assert quiet.hotspot_restore is None
        assert not _issued(unit_shell.commands, "start-softap")

    async def test_durable_off_restore_rejects_an_unreadable_inventory(self, unit_shell):
        unit_shell.replies["ip -o addr"] = adb.AdbError("unit went away")
        controller = radios.RadioController(UNIT, watchdog_deadline_s=300)

        assert not await controller.restore_hotspot("off", None)

    async def test_durable_off_restore_catches_a_delayed_bluetooth_hotspot_rearm(
        self, unit_shell, monkeypatch
    ):
        """OFF is not final while the vendor's accepted Bluetooth enable can still add AP."""
        monkeypatch.setattr(radios, "HOTSPOT_REARM_SETTLE_S", 0.04)
        monkeypatch.setattr(radios, "HOTSPOT_REARM_POLL_S", 0.005)
        unit_shell.replies["bluetooth_on"] = "1"
        # Initially OFF, then the delayed vendor re-arm appears, then the binder stop is
        # observed. The final OFF reply sticks through the rest of the settle window.
        unit_shell.replies["ip -o addr"] = [_IP_NO_AP, _IP_WITH_AP, _IP_NO_AP]
        controller = radios.RadioController(UNIT, watchdog_deadline_s=300)

        assert await controller.restore_bluetooth("on")
        assert await controller.restore_hotspot("off", None)
        assert _issued(unit_shell.commands, "service call tethering")

    async def test_fresh_adopter_waits_when_first_controller_dies_during_rearm_settle(
        self, unit_shell, monkeypatch
    ):
        """A's process-local deadline dies with it; B must create a new conservative one."""
        monkeypatch.setattr(radios, "HOTSPOT_REARM_SETTLE_S", 0.04)
        monkeypatch.setattr(radios, "HOTSPOT_REARM_POLL_S", 0.005)
        unit_shell.replies["bluetooth_on"] = "1"

        first = radios.RadioController(UNIT, watchdog_deadline_s=300)
        assert await first.restore_bluetooth("on")
        # Process A disappears here before observing the hotspot. B finds Bluetooth ON,
        # sees no AP at first, then catches and stops the re-arm that A triggered late.
        second = radios.RadioController(UNIT, watchdog_deadline_s=300)
        assert await second.restore_bluetooth("on")
        unit_shell.replies["ip -o addr"] = [_IP_NO_AP, _IP_WITH_AP, _IP_NO_AP]

        assert await second.restore_hotspot("off", None)
        assert len(_issued(unit_shell.commands, "cmd bluetooth_manager enable")) == 2
        assert _issued(unit_shell.commands, "service call tethering")

    async def test_durable_on_restore_waits_for_vendor_rearm_before_starting_duplicate(
        self, unit_shell, monkeypatch
    ):
        monkeypatch.setattr(radios, "HOTSPOT_REARM_SETTLE_S", 0.04)
        monkeypatch.setattr(radios, "HOTSPOT_REARM_POLL_S", 0.005)
        unit_shell.replies["bluetooth_on"] = "1"
        unit_shell.replies["ip -o addr"] = [_IP_NO_AP, _IP_WITH_AP]
        unit_shell.replies["dumpsys wifi"] = _DUMP_MODERN
        controller = radios.RadioController(UNIT, watchdog_deadline_s=300)

        assert await controller.restore_bluetooth("on")
        assert await controller.restore_hotspot("on", ("CarSpot", "roadtrip99"))
        assert not _issued(unit_shell.commands, "cmd wifi start-softap")

    async def test_opted_in_zlink_restore_requires_the_vendor_ap_to_reappear(
        self, unit_shell, monkeypatch
    ):
        monkeypatch.setattr(radios, "HOTSPOT_REARM_SETTLE_S", 0.04)
        monkeypatch.setattr(radios, "HOTSPOT_REARM_POLL_S", 0.005)
        unit_shell.replies["bluetooth_on"] = "1"
        unit_shell.replies["ip -o addr"] = [_IP_NO_AP, _IP_WITH_AP]
        controller = radios.RadioController(UNIT, watchdog_deadline_s=300)

        assert await controller.restore_bluetooth("on")
        assert await controller.restore_hotspot(
            "on",
            None,
            radios.HOTSPOT_RESTORE_BLUETOOTH_REARM,
            expected_interface="ap0",
        )
        assert not _issued(unit_shell.commands, "start-softap")

    async def test_opted_in_zlink_restore_stays_pending_when_the_ap_does_not_return(
        self, unit_shell, monkeypatch
    ):
        monkeypatch.setattr(radios, "HOTSPOT_REARM_SETTLE_S", 0.02)
        monkeypatch.setattr(radios, "HOTSPOT_REARM_POLL_S", 0.005)
        unit_shell.replies["bluetooth_on"] = "1"
        unit_shell.replies["ip -o addr"] = _IP_NO_AP
        controller = radios.RadioController(UNIT, watchdog_deadline_s=300)

        assert await controller.restore_bluetooth("on")
        assert not await controller.restore_hotspot(
            "on",
            None,
            radios.HOTSPOT_RESTORE_BLUETOOTH_REARM,
            expected_interface="ap0",
        )

    async def test_opted_in_zlink_restore_rejects_a_different_serving_ap(
        self, unit_shell, monkeypatch
    ):
        monkeypatch.setattr(radios, "HOTSPOT_REARM_SETTLE_S", 0.02)
        monkeypatch.setattr(radios, "HOTSPOT_REARM_POLL_S", 0.005)
        unit_shell.replies["bluetooth_on"] = "1"
        unit_shell.replies["ip -o addr"] = _IP_WITH_AP.replace("ap0", "wlan1")
        controller = radios.RadioController(UNIT, watchdog_deadline_s=300)

        assert await controller.restore_bluetooth("on")
        assert not await controller.restore_hotspot(
            "on",
            None,
            radios.HOTSPOT_RESTORE_BLUETOOTH_REARM,
            expected_interface="ap0",
        )

    async def test_opted_in_zlink_restore_rejects_an_ap_that_disappears_during_settle(
        self, unit_shell, monkeypatch
    ):
        monkeypatch.setattr(radios, "HOTSPOT_REARM_SETTLE_S", 0.02)
        monkeypatch.setattr(radios, "HOTSPOT_REARM_POLL_S", 0.005)
        unit_shell.replies["bluetooth_on"] = "1"
        unit_shell.replies["ip -o addr"] = [_IP_WITH_AP, _IP_NO_AP]
        controller = radios.RadioController(UNIT, watchdog_deadline_s=300)

        assert await controller.restore_bluetooth("on")
        assert not await controller.restore_hotspot(
            "on",
            None,
            radios.HOTSPOT_RESTORE_BLUETOOTH_REARM,
            expected_interface="ap0",
        )

    async def test_opted_in_zlink_restore_restarts_stability_after_an_ap_flap(
        self, unit_shell, monkeypatch
    ):
        monkeypatch.setattr(radios, "HOTSPOT_REARM_SETTLE_S", 0.02)
        monkeypatch.setattr(radios, "HOTSPOT_REARM_POLL_S", 0.002)
        unit_shell.replies["bluetooth_on"] = "1"
        unit_shell.replies["ip -o addr"] = [_IP_WITH_AP, _IP_NO_AP, _IP_WITH_AP]
        controller = radios.RadioController(UNIT, watchdog_deadline_s=300)

        assert await controller.restore_bluetooth("on")
        assert await controller.restore_hotspot(
            "on",
            None,
            radios.HOTSPOT_RESTORE_BLUETOOTH_REARM,
            expected_interface="ap0",
        )
        assert len(_issued(unit_shell.commands, "ip -o addr")) >= 3

    async def test_empty_or_unparseable_inventory_is_unknown_not_proven_off(self, unit_shell):
        unit_shell.replies["ip -o addr"] = "diagnostic noise without an address"

        assert await radios._serving_ap(UNIT) is None

    async def test_nothing_serving_means_nothing_is_issued(self, unit_shell):
        """A dormant `wlan1` is not a hotspot, and a stop nobody needs is a round trip
        that can only produce a misleading log line."""
        unit_shell.replies["bluetooth_on"] = "0"
        unit_shell.replies["ip -o addr"] = _IP_NO_AP

        quiet = radios.begin_quiet(UNIT, online_for=60.0, watchdog_deadline_s=300)
        await quiet._task
        await quiet.finish()

        assert not _issued(unit_shell.commands, "softap")
        assert not _issued(unit_shell.commands, "dumpsys wifi")

    async def test_the_link_the_transfer_runs_over_is_never_stopped(self, unit_shell):
        """If somebody's server is itself a client of the dashcam's hotspot, that hotspot
        *is* the transfer's link -- stopping it would cut the one thing this feature
        exists to speed up."""
        unit_shell.replies["bluetooth_on"] = "0"
        unit_shell.replies["ip -o addr"] = (
            "1: lo    inet 127.0.0.1/8 scope host lo\n"
            "25: ap0    inet 192.168.43.1/24 brd 192.168.43.255 scope global ap0\n"
        )

        quiet = radios.begin_quiet("192.168.43.1:5555", online_for=60.0, watchdog_deadline_s=300)
        await quiet._task
        await quiet.finish()

        assert not _issued(unit_shell.commands, "softap")

    async def test_durable_controller_records_the_active_hotspot_as_transport(self, unit_shell):
        """The persisted baseline names this separately from OFF, while still issuing no
        stop command that would tear down ADB and the bulk TCP socket together."""
        unit_shell.replies["bluetooth_on"] = "0"
        unit_shell.replies["ip -o addr"] = (
            "1: lo    inet 127.0.0.1/8 scope host lo\n"
            "25: ap0    inet 192.168.43.1/24 brd 192.168.43.255 scope global ap0\n"
        )
        control = radios.RadioController("192.168.43.1:5555", watchdog_deadline_s=300)
        control.claim()
        try:
            snapshot = await control.capture()
        finally:
            await control.release()

        assert snapshot.hotspot == "transport"
        assert snapshot.hotspot_interface == "ap0"
        assert snapshot.transport_interface == "ap0"
        assert not _issued(unit_shell.commands, "service call tethering")
        assert not _issued(unit_shell.commands, "stop-softap")

    async def test_durable_controller_still_finds_a_separate_ap_beside_sta_transport(
        self, unit_shell
    ):
        unit_shell.replies["bluetooth_on"] = "0"
        unit_shell.replies["ip -o addr"] = _IP_WITH_AP
        unit_shell.replies["dumpsys wifi"] = _DUMP_MODERN
        control = radios.RadioController(UNIT, watchdog_deadline_s=300)
        control.claim()
        try:
            snapshot = await control.capture()
        finally:
            await control.release()

        assert snapshot.hotspot == "on"
        assert snapshot.hotspot_interface == "ap0"
        assert snapshot.transport_interface == "wlan0"

    async def test_with_no_recoverable_config_it_is_stopped_but_never_guessed_at(self, unit_shell):
        """Stopping it is the operator's call, and they have made it: the transfer gets the
        radio to itself. What must not happen is inventing a passphrase to start it again,
        so the stop is taken and the restore declines, loudly."""
        unit_shell.replies["bluetooth_on"] = "0"
        unit_shell.replies["ip -o addr"] = [_IP_WITH_AP, _IP_NO_AP]
        unit_shell.replies["dumpsys wifi"] = "nothing useful in here"

        quiet = radios.begin_quiet(UNIT, online_for=60.0, watchdog_deadline_s=300)
        await quiet._task
        await quiet.finish()

        assert _issued(unit_shell.commands, "service call tethering")
        assert not _issued(unit_shell.commands, "start-softap")


class TestConfirmingBluetoothCameBack:
    """`enable` returns Success immediately; `bluetooth_on` needs a moment to agree.

    Checking once straight after the enable raced the radio and lost, which is what turned
    a working restore into 7 "could not confirm" warnings against 23 disables — each one
    leaving the marker set and the driver without Bluetooth until the next arrival.
    """

    async def test_it_waits_for_the_radio_rather_than_asking_once(self, monkeypatch):
        calls = {"n": 0}

        async def slow(address):
            calls["n"] += 1
            return calls["n"] >= 3

        monkeypatch.setattr(radios, "_bluetooth_is_on", slow)
        monkeypatch.setattr(radios, "CONFIRM_INTERVAL_S", 0.01)

        assert await radios._confirm_bluetooth_on(UNIT) is True
        assert calls["n"] >= 3, "it gave up before the radio had come up"

    async def test_it_still_gives_up_rather_than_assuming(self, monkeypatch):
        """A timeout must stay False: the marker, the flag and the next-arrival repair are
        all held in place by a negative answer."""

        async def never(address):
            return False

        monkeypatch.setattr(radios, "_bluetooth_is_on", never)
        monkeypatch.setattr(radios, "CONFIRM_TIMEOUT_S", 0.05)
        monkeypatch.setattr(radios, "CONFIRM_INTERVAL_S", 0.01)

        assert await radios._confirm_bluetooth_on(UNIT) is False


class TestTheRefusalCheck:
    """Neither the exit status nor the text alone is enough, so both are consulted."""

    @pytest.mark.parametrize(
        "reply",
        [
            _SECURITY_EXCEPTION,
            "Unknown command: stop-all-tethering",
            "Error: no such service",
            "cmd: Can't find service: tethering",
            "Usage: cmd wifi ...",
        ],
    )
    def test_a_refusal_is_not_success(self, reply):
        assert not radios._accepted(reply)

    @pytest.mark.parametrize("reply", ["", "\n", "Soft AP stopped"])
    def test_a_quiet_or_positive_reply_is(self, reply):
        assert radios._accepted(reply)


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
        # The radio reports itself on afterwards, which is what standing the watchdog down
        # is allowed to depend on.
        unit_shell.replies["bluetooth_on"] = "1"
        unit_shell.settings.values[radios.MARKER_KEY] = "bluetooth"

        radios.restore_if_pending("u:5555")
        await asyncio.gather(*list(radios._tasks))

        assert _issued(unit_shell.commands, "cmd bluetooth_manager enable")
        assert _issued(unit_shell.commands, f"rm -f '{radios.FLAG_PATH}'")
        assert unit_shell.settings.values[radios.MARKER_KEY] == ""

    async def test_an_unverified_enable_keeps_the_marker_and_the_flag(self, unit_shell):
        """The command was accepted and the radio is still off.

        This is the failure that strands the driver's hands-free: clearing the marker and
        the on-unit watchdog flag on an unverified `enable` leaves nothing anywhere holding
        the promise, so the next arrival has nothing to repair and the read-only "Radios
        awaiting restore" setting reads as clean. The in-run restore has always confirmed
        before standing down; this path did not.
        """
        unit_shell.replies["bluetooth_on"] = "0"
        unit_shell.settings.values[radios.MARKER_KEY] = "bluetooth"

        radios.restore_if_pending("u:5555")
        await asyncio.gather(*list(radios._tasks))

        assert _issued(unit_shell.commands, "cmd bluetooth_manager enable")
        assert not _issued(unit_shell.commands, f"rm -f '{radios.FLAG_PATH}'")
        assert unit_shell.settings.values[radios.MARKER_KEY] == "bluetooth"

    async def test_a_failed_attempt_keeps_the_marker_for_the_next_arrival(self, unit_shell):
        unit_shell.settings.values[radios.MARKER_KEY] = "bluetooth"
        unit_shell.replies["cmd bluetooth_manager enable"] = adb.AdbError("gone again")
        unit_shell.replies["svc bluetooth enable"] = adb.AdbError("gone again")

        radios.restore_if_pending("u:5555")
        await asyncio.gather(*list(radios._tasks))

        assert unit_shell.settings.values[radios.MARKER_KEY] == "bluetooth"

    async def test_it_stands_down_while_a_transfer_owns_the_radios(self, unit_shell, monkeypatch):
        """Otherwise a lagging restore turns Bluetooth back on mid-transfer and then
        clears the marker the running window wrote, taking the on-unit watchdog and the
        next-arrival repair with it."""
        unit_shell.settings.values[radios.MARKER_KEY] = "bluetooth"
        monkeypatch.setattr(radios, "_active", 1)

        radios.restore_if_pending("u:5555")
        await asyncio.gather(*list(radios._tasks))

        assert not _issued(unit_shell.commands, "bluetooth_manager enable")
        assert unit_shell.settings.values[radios.MARKER_KEY] == "bluetooth"

    async def test_a_marker_cleared_while_it_queued_is_not_acted_on(self, unit_shell):
        """It re-reads under the lock rather than trusting what it was started with."""
        unit_shell.settings.values[radios.MARKER_KEY] = "bluetooth"

        radios.restore_if_pending("u:5555")
        unit_shell.settings.values[radios.MARKER_KEY] = ""
        await asyncio.gather(*list(radios._tasks))

        assert not _issued(unit_shell.commands, "bluetooth_manager enable")

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
