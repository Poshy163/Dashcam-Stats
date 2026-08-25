"""The arrival gate: hold the first automatic pull until the unit has been running long
enough to be arriving home rather than leaving.

No device here. The unit has no battery, so its uptime is the length of the current drive;
``adb.uptime`` is faked to stand in for that, and the status singleton is inspected directly
to prove a hold is published where the Backup page can see it.
"""

from __future__ import annotations

import pytest

from app.ingest import adb, poller
from app.ingest.status import get_status, reset_status_for_tests


class StubSettings:
    def __init__(self, values: dict | None = None) -> None:
        self.values = dict(values or {})

    def get_nowait(self, key, default=None):
        return self.values.get(key, default)


@pytest.fixture(autouse=True)
def clean_status():
    reset_status_for_tests()
    yield
    reset_status_for_tests()


@pytest.fixture
def gate(monkeypatch):
    """A poller whose settings and uptime reads are faked. `set_uptime(v)` decides what the
    unit will answer for its running time on the next `_arrival_ready`."""
    settings = StubSettings()
    monkeypatch.setattr(poller, "get_settings_service", lambda: settings)

    def set_uptime(value):
        async def fake_uptime(address):
            return value

        monkeypatch.setattr(adb, "uptime", fake_uptime)

    return poller.IngestPoller(), settings, set_uptime


class TestUptimeParsing:
    async def test_uptime_reads_the_first_field_of_proc_uptime(self, monkeypatch):
        async def fake_shell(address, command, **kwargs):
            assert "/proc/uptime" in command
            return "3456.78 91011.12\n"

        monkeypatch.setattr(adb, "shell", fake_shell)
        assert await adb.uptime("u:5555") == 3456.78

    async def test_an_unreadable_uptime_is_none(self, monkeypatch):
        async def fake_shell(address, command, **kwargs):
            raise adb.AdbError("car has left")

        monkeypatch.setattr(adb, "shell", fake_shell)
        assert await adb.uptime("u:5555") is None


class TestTheArrivalGate:
    async def test_a_threshold_of_zero_never_holds(self, gate):
        """The gate off is the old behaviour exactly: pull the moment the unit appears."""
        p, settings, set_uptime = gate
        settings.values["ingest.min_uptime_s"] = 0
        set_uptime(3.0)  # only just booted, but the gate is switched off
        assert await p._arrival_ready("u:5555") is True
        assert get_status().arrival_hold is False

    async def test_a_high_uptime_is_treated_as_an_arrival(self, gate):
        p, settings, set_uptime = gate
        settings.values["ingest.min_uptime_s"] = 120
        set_uptime(600.0)  # ten minutes up -- has been driving, so this is coming home
        assert await p._arrival_ready("u:5555") is True
        assert get_status().arrival_hold is False
        assert get_status().unit_uptime_s == 600.0

    async def test_a_low_uptime_is_held_with_a_reason(self, gate):
        p, settings, set_uptime = gate
        settings.values["ingest.min_uptime_s"] = 120
        set_uptime(15.0)  # just booted -- the signature of pulling off the driveway
        assert await p._arrival_ready("u:5555") is False
        assert get_status().arrival_hold is True
        assert "arrive" in (get_status().arrival_hold_reason or "")
        assert get_status().unit_uptime_s == 15.0

    async def test_an_unreadable_uptime_proceeds_rather_than_holding(self, gate):
        """A backup that quietly stops happening is worse than one that starts early."""
        p, settings, set_uptime = gate
        settings.values["ingest.min_uptime_s"] = 120
        set_uptime(None)  # the unit would not say
        assert await p._arrival_ready("u:5555") is True
        assert get_status().arrival_hold is False

    async def test_a_run_that_starts_clears_the_hold(self, gate):
        """The field bug: a manual pull bypasses this gate entirely, so a hold published
        earlier stood while the transfer visibly ran -- state=running, phase=transferring,
        and the Backup page still saying "Waiting until you're home"."""
        p, settings, set_uptime = gate
        settings.values["ingest.min_uptime_s"] = 120
        set_uptime(15.0)
        assert await p._arrival_ready("u:5555") is False
        assert get_status().arrival_hold is True

        assert get_status().try_begin() is True

        assert get_status().arrival_hold is False, "a run under way is not a run being held"
        assert get_status().snapshot()["arrival_hold_reason"] is None

    async def test_crossing_the_threshold_clears_a_standing_hold(self, gate):
        """A genuine arrival with a short trip: held at first, released the tick its uptime
        crosses the line, and the hold reason cleared so the page stops explaining it."""
        p, settings, set_uptime = gate
        settings.values["ingest.min_uptime_s"] = 120

        set_uptime(30.0)
        assert await p._arrival_ready("u:5555") is False
        assert get_status().arrival_hold is True

        set_uptime(130.0)
        assert await p._arrival_ready("u:5555") is True
        assert get_status().arrival_hold is False
        assert get_status().arrival_hold_reason is None
