"""Giving the car's screen back to CarPlay when the copying is done.

Restoring both radios turns out not to be enough on this unit. Observed in the car: while
the browser owns the foreground the driver's phone does not pair, so a unit that goes to
sleep still showing the backup page wakes up without CarPlay however correctly Bluetooth
and the hotspot were put back -- and parking somewhere the page never opens, where Zlink
keeps the foreground throughout, connects promptly.

Cancelling the task that held the page there was never the same as taking it off the
screen. These cover the two places that now do: the run's own cleanup, and the detached
watchdog for the windows the server is not around to see the end of.
"""

from __future__ import annotations

import pytest

from app.ingest import adb, radios

RESOLVED = "com.zjinnova.zlink/com.zjinnova.android.zlink.features.main.MainActivity"


class _Unit:
    """Records what was asked of the unit, and answers as the live one was measured to."""

    def __init__(self, *, resolve: str | None = RESOLVED, foreground: list[str] | None = None):
        self.commands: list[str] = []
        self._resolve = resolve
        # Consumed in order, so a test can say "browser, then CarPlay".
        self._foreground = foreground or [adb.CARPLAY_PACKAGE]

    async def shell(self, address: str, command: str, *, timeout: float = 0) -> str:
        self.commands.append(command)
        if "resolve-activity" in command:
            if self._resolve is None:
                raise adb.AdbError("resolve failed")
            return f"priority=0 preferredOrder=0 match=0x108000\n{self._resolve}\n"
        if "dumpsys activity activities" in command:
            package = self._foreground.pop(0) if len(self._foreground) > 1 else self._foreground[0]
            return f"  topResumedActivity=ActivityRecord{{a u0 {package}/.Main t1 d0}}"
        return ""


@pytest.fixture
def unit(monkeypatch):
    def _make(**kwargs):
        fake = _Unit(**kwargs)
        monkeypatch.setattr(adb, "shell", fake.shell)
        return fake

    return _make


# ---------------------------------------------------------------------------------------
# The run's own cleanup
# ---------------------------------------------------------------------------------------


async def test_the_resolved_launcher_is_started_and_confirmed(unit):
    fake = unit(foreground=[adb.CARPLAY_PACKAGE])

    assert await adb.hand_screen_back("1.2.3.4:5555") is True

    assert any(f"am start -n {RESOLVED}" in c for c in fake.commands)
    # Confirmed by looking, never by the intent being accepted.
    assert any("dumpsys activity activities" in c for c in fake.commands)


async def test_the_activity_is_resolved_rather_than_hard_coded(unit):
    """A launcher name learned from one vendor build is right until it is not."""
    fake = unit(resolve="com.zjinnova.zlink/.SomeOtherActivity")

    assert await adb.hand_screen_back("1.2.3.4:5555") is True

    assert any("am start -n com.zjinnova.zlink/.SomeOtherActivity" in c for c in fake.commands)


async def test_monkey_is_the_fallback_when_resolution_fails(unit):
    """It needs only the package, which is the part that cannot move."""
    fake = unit(resolve=None)

    assert await adb.hand_screen_back("1.2.3.4:5555") is True

    assert any("monkey -p com.zjinnova.zlink" in c for c in fake.commands)


async def test_a_screen_that_will_not_come_back_is_reported_not_raised(unit):
    """The footage is already home; this is a courtesy and may not fail a run."""
    unit(foreground=["com.android.chrome"])

    assert await adb.hand_screen_back("1.2.3.4:5555") is False


async def test_an_unreachable_unit_is_not_an_exception(unit, monkeypatch):
    async def dead(address, command, *, timeout=0):
        raise adb.AdbError("device offline")

    monkeypatch.setattr(adb, "shell", dead)

    assert await adb.hand_screen_back("1.2.3.4:5555") is False


async def test_the_foreground_package_is_read_from_the_resumed_activity(unit):
    unit(foreground=["com.android.chrome"])

    assert await adb.foreground_package("1.2.3.4:5555") == "com.android.chrome"


async def test_an_unreadable_foreground_is_empty_rather_than_a_guess(monkeypatch):
    async def dead(address, command, *, timeout=0):
        raise adb.AdbError("device offline")

    monkeypatch.setattr(adb, "shell", dead)

    assert await adb.foreground_package("1.2.3.4:5555") == ""


# ---------------------------------------------------------------------------------------
# The watchdog, for the windows the server does not see the end of
# ---------------------------------------------------------------------------------------


async def _armed(monkeypatch, **kwargs) -> str:
    """The restore branch of a watchdog script, as it would be written to the unit."""
    captured: dict[str, str] = {}

    async def fake_shell_script(address, script, *, timeout):
        captured["script"] = script
        raise RuntimeError("stop before launching")

    async def fake_discard(address, handle):
        return None

    monkeypatch.setattr(radios, "_shell_script", fake_shell_script)
    monkeypatch.setattr(radios, "_discard_watchdog_candidate", fake_discard)
    await radios._arm_watchdog("1.2.3.4:5555", 300, **kwargs)
    return captured.get("script", "")


async def test_the_watchdog_hands_the_screen_back_after_the_radios(monkeypatch):
    """Order is the point: Zlink comes forward once there is a radio for it to use."""
    script = await _armed(monkeypatch, restore_bluetooth=True, hotspot_baseline="off")

    fired = script[script.rfind("bluetooth_manager enable") :]
    bluetooth = fired.find("bluetooth_manager enable")
    hotspot = fired.find("stop-softap")
    screen = fired.find("monkey -p com.zjinnova.zlink")

    assert -1 not in (bluetooth, hotspot, screen)
    assert bluetooth < hotspot < screen


async def test_the_watchdog_leaves_the_screen_alone_when_asked(monkeypatch):
    script = await _armed(
        monkeypatch, restore_bluetooth=True, hotspot_baseline="off", hand_screen_back=False
    )

    assert "monkey -p com.zjinnova.zlink" not in script


async def test_a_watchdog_with_nothing_to_restore_is_not_armed_just_to_move_a_window(monkeypatch):
    """The screen is a rider on a real restore, never a reason to leave a process behind."""
    script = await _armed(monkeypatch, restore_bluetooth=False, hotspot_baseline="unknown")

    assert script == ""
