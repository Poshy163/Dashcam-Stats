"""Restarting the head unit's ADB as root, and the three ways that can go wrong.

No device: the adb client is faked and every assertion is about what would have been
asked of the unit and what the app concluded from the answer. The invariants are the
protective ones — a production build is asked exactly once, a daemon that never comes
back does not fail the run, and nothing here ever elevates unless both settings that
justify it are on.
"""

from __future__ import annotations

import pytest

from app.ingest import adb, elevate

#: The three answers adbd actually gives, verbatim.
_PRODUCTION = "adbd cannot run as root in production builds"
_ALREADY = "adbd is already running as root"
_RESTARTING = "restarting adbd as root"


class StubSettings:
    def __init__(self, values: dict | None = None) -> None:
        self.values = dict(values or {})

    def get_nowait(self, key, default=None):
        return self.values.get(key, default)

    async def set(self, key, value, *, internal=False):
        self.values[key] = value
        return value


@pytest.fixture
def unit(monkeypatch):
    """A fake adb client: records the root requests, the reconnects and the shells."""
    roots: list[str] = []
    reconnects: list[str] = []
    state = {"root_reply": _PRODUCTION, "uid": "2000", "reconnect_error": None}

    async def fake_root(address):
        roots.append(address)
        reply = state["root_reply"]
        if isinstance(reply, Exception):
            raise reply
        return reply

    async def fake_reconnect(address, *, timeout=adb.CONTROL_TIMEOUT_S):
        reconnects.append(address)
        if state["reconnect_error"]:
            raise state["reconnect_error"]

    async def fake_shell(address, command, **kwargs):
        if "id -u" in command:
            uid = state["uid"]
            if isinstance(uid, Exception):
                raise uid
            return uid
        return ""

    monkeypatch.setattr(adb, "root", fake_root)
    monkeypatch.setattr(adb, "reconnect", fake_reconnect)
    monkeypatch.setattr(adb, "shell", fake_shell)
    monkeypatch.setattr(elevate, "RECONNECT_POLL_S", 0.0)
    monkeypatch.setattr(elevate, "RECONNECT_BUDGET_S", 0.05)

    settings = StubSettings({"ingest.adb_root": True, "ingest.quiet_radios": True})
    monkeypatch.setattr(elevate, "get_settings_service", lambda: settings)

    class Harness:
        pass

    harness = Harness()
    harness.roots = roots
    harness.reconnects = reconnects
    harness.state = state
    harness.settings = settings
    return harness


@pytest.fixture(autouse=True)
def clean_module_state():
    elevate.reset_for_tests()
    yield
    elevate.reset_for_tests()


class TestWhenItIsAskedAtAll:
    async def test_off_by_default_asks_the_unit_nothing(self, unit):
        unit.settings.values["ingest.adb_root"] = False
        assert not await elevate.ensure_root("u:5555")
        assert unit.roots == []

    async def test_it_does_not_fire_for_a_hotspot_nobody_is_quieting(self, unit):
        """`requires` disables the input; it does not retract a value already stored,
        so the pairing is enforced here too rather than trusted to the UI."""
        unit.settings.values["ingest.quiet_radios"] = False
        assert not await elevate.ensure_root("u:5555")
        assert unit.roots == []


class TestAProductionBuild:
    async def test_it_is_asked_once_and_never_again(self, unit):
        """The ordinary unit. Asking every window would spend the window on an answer
        that cannot change without a reflash."""
        unit.state["root_reply"] = _PRODUCTION

        assert not await elevate.ensure_root("u:5555")
        assert not await elevate.ensure_root("u:5555")
        assert not await elevate.ensure_root("u:5555")

        assert len(unit.roots) == 1, "a build's refusal was re-asked"
        assert "production build" in unit.settings.values[elevate.STATE_KEY]

    async def test_the_refusal_is_visible_rather_than_silent(self, unit):
        unit.state["root_reply"] = "adbd cannot run as root in production builds"
        await elevate.ensure_root("u:5555")
        assert unit.settings.values[elevate.STATE_KEY].startswith("not available")

    async def test_a_restart_is_never_awaited_for_a_refusal(self, unit):
        unit.state["root_reply"] = _PRODUCTION
        await elevate.ensure_root("u:5555")
        assert unit.reconnects == [], "a refused unit was waited on as though it restarted"


class TestADebuggableUnit:
    async def test_a_restart_is_waited_for_and_confirmed(self, unit):
        unit.state["root_reply"] = _RESTARTING
        unit.state["uid"] = "0"

        assert await elevate.ensure_root("u:5555")
        assert unit.reconnects, "the control channel was not re-established"
        assert unit.settings.values[elevate.STATE_KEY].startswith("available")

    async def test_already_root_costs_one_round_trip_and_no_wait(self, unit):
        """The second and later runs of a window. Nothing restarted, nothing to wait for."""
        unit.state["root_reply"] = _ALREADY
        unit.state["uid"] = "0"

        assert await elevate.ensure_root("u:5555")
        assert unit.reconnects == []

    async def test_an_unchanged_answer_is_not_rewritten_every_run(self, unit, monkeypatch):
        """The poller starts another run the moment each one ends, so "available" is
        reached over and over while the car sits there. Storing it afresh each time is a
        database round trip per run to write a string that already says that."""
        writes: list[str] = []
        original = unit.settings.set

        async def counting_set(key, value, *, internal=False):
            writes.append(value)
            return await original(key, value, internal=internal)

        monkeypatch.setattr(unit.settings, "set", counting_set)
        unit.state["root_reply"] = _ALREADY
        unit.state["uid"] = "0"

        for _ in range(4):
            await elevate.ensure_root("u:5555")
        assert len(writes) == 1, f"the same state was written {len(writes)} times"

    async def test_success_is_never_cached_because_the_unit_reboots(self, unit):
        """Root is a property of this boot, not of the build -- the car switches off and
        adbd comes back as uid 2000, so every run has to ask again."""
        unit.state["root_reply"] = _ALREADY
        unit.state["uid"] = "0"

        await elevate.ensure_root("u:5555")
        await elevate.ensure_root("u:5555")
        assert len(unit.roots) == 2

    async def test_an_unconfirmed_restart_is_not_believed(self, unit):
        """A clean 'restarting' is not proof. Only uid 0 is."""
        unit.state["root_reply"] = _RESTARTING
        unit.state["uid"] = "2000"

        assert not await elevate.ensure_root("u:5555")


class TestWhenTheDaemonDoesNotComeBack:
    async def test_it_is_reported_but_never_cached(self, unit):
        """The dangerous case. The unit reboots at the next engine start and comes back
        exactly as configured, so this must not become a permanent verdict."""
        unit.state["root_reply"] = _RESTARTING
        unit.state["reconnect_error"] = adb.AdbError("no route to host")

        assert not await elevate.ensure_root("u:5555")
        assert "u:5555" not in elevate._impossible
        assert elevate.channel_lost()

        elevate._cooldown.clear()
        unit.state["reconnect_error"] = None
        unit.state["uid"] = "0"
        assert await elevate.ensure_root("u:5555"), "a transient loss became permanent"

    async def test_a_daemon_that_answers_as_2000_is_not_a_lost_channel(self, unit):
        """The distinction the caller acts on. A unit that came back and simply did not
        elevate is the ordinary "no root here" outcome and the transfer must proceed;
        only a channel that never answered is a reason to stop."""
        unit.state["root_reply"] = _RESTARTING
        unit.state["uid"] = "2000"

        assert not await elevate.ensure_root("u:5555")
        assert not elevate.channel_lost(), "a live channel was reported as lost"

    async def test_a_channel_that_never_answers_is_a_lost_channel(self, unit):
        """`reconnect` cannot be the discriminator: `_adb` raises only on a timeout, so a
        plain "cannot connect" returns normally and a reconnect against a dead unit looks
        exactly like a good one. Only `id -u` failing to answer proves it."""
        unit.state["root_reply"] = _RESTARTING
        unit.state["reconnect_error"] = None  # reconnect "succeeds"
        unit.state["uid"] = adb.AdbError("device not found")

        assert not await elevate.ensure_root("u:5555")
        assert elevate.channel_lost()

    async def test_a_lost_channel_is_not_retried_for_the_rest_of_the_visit(self, unit):
        """Otherwise the poller's re-drain restarts the daemon again, waits, gives up
        again, and the whole window goes on restarting adbd instead of copying."""
        unit.state["root_reply"] = _RESTARTING
        unit.state["uid"] = adb.AdbError("device not found")
        assert not await elevate.ensure_root("u:5555")
        assert elevate.channel_lost()

        before = len(unit.roots)
        # The unit is back and would happily elevate now -- but the retry must still
        # stand down, because it cannot tell that apart from the loop.
        unit.state["uid"] = "0"
        assert not await elevate.ensure_root("u:5555")
        assert len(unit.roots) == before, "the daemon was restarted again inside the cooldown"
        assert not elevate.channel_lost(), "a skipped attempt must not report a dead channel"


class TestAVanishedCar:
    """`adb root` does not raise for these: `_adb` ignores the exit status, so a departed
    car comes back as a *string*. Misreading it as a restart spends the whole reconnect
    budget waiting for a daemon that was never restarted."""

    @pytest.mark.parametrize(
        "reply",
        [
            "error: device '192.168.1.122:5555' not found",
            "error: device offline",
            "error: closed",
        ],
    )
    async def test_it_is_recognised_without_waiting_for_a_restart(self, unit, reply):
        unit.state["root_reply"] = reply

        assert not await elevate.ensure_root("u:5555")
        assert unit.reconnects == [], "waited for a restart that never happened"
        assert "u:5555" not in elevate._impossible, "a departed car is not a production build"
        assert not elevate.channel_lost()


class TestTheRefusalIsNotOverclaimed:
    """A vendor policy or an SELinux denial on a debuggable unit can also refuse. Telling
    that operator their firmware is a production build sends them to reflash a unit whose
    build is fine."""

    @pytest.mark.parametrize("reply", ["Permission denied", "root access is not allowed"])
    async def test_an_unrecognised_refusal_is_not_called_a_production_build(self, unit, reply):
        unit.state["root_reply"] = reply
        unit.state["uid"] = "2000"

        assert not await elevate.ensure_root("u:5555")
        assert "production build" not in unit.settings.values.get(elevate.STATE_KEY, "")

    async def test_a_root_call_that_throws_still_waits_for_the_daemon(self, unit):
        """A timeout is the ambiguous answer: the daemon may be mid-restart, and
        concluding 'no root' while it is coming back leaves the run talking to a channel
        it believes is dead."""
        unit.state["root_reply"] = adb.AdbError("adb root timed out after 10s")
        unit.state["uid"] = "0"

        assert await elevate.ensure_root("u:5555")
        assert unit.reconnects

    async def test_nothing_here_ever_raises_into_the_run(self, unit):
        unit.state["root_reply"] = adb.AdbError("car has left")
        unit.state["reconnect_error"] = adb.AdbError("car has left")
        assert await elevate.ensure_root("u:5555") is False


class TestTheRetryBudgetIsRealTime:
    """A budget only bounds anything if each attempt fails faster than the budget.

    Both `reconnect` and `id -u` default to the twenty-second control timeout. Left at
    that, one attempt against a dead daemon outlasts the whole retry window and spends
    half a driveway window doing it.
    """

    async def test_every_call_in_the_loop_is_bounded_well_inside_the_budget(self, monkeypatch):
        # Read the real budget before shortening it, so the assertion is about what
        # ships rather than about what this test set up.
        real_budget = elevate.RECONNECT_BUDGET_S
        timeouts: list[float] = []

        async def fake_root(address):
            return _RESTARTING

        async def fake_reconnect(address, *, timeout=adb.CONTROL_TIMEOUT_S):
            timeouts.append(timeout)
            raise adb.AdbError("still down")

        monkeypatch.setattr(adb, "root", fake_root)
        monkeypatch.setattr(adb, "reconnect", fake_reconnect)
        monkeypatch.setattr(elevate, "RECONNECT_POLL_S", 0.0)
        monkeypatch.setattr(elevate, "RECONNECT_BUDGET_S", 0.05)
        settings = StubSettings({"ingest.adb_root": True, "ingest.quiet_radios": True})
        monkeypatch.setattr(elevate, "get_settings_service", lambda: settings)

        assert not await elevate.ensure_root("u:5555")
        assert timeouts, "the daemon was never polled for"
        assert all(t <= real_budget / 2 for t in timeouts), (
            f"an attempt could outlast the retry budget: {timeouts} against {real_budget}s"
        )

    async def test_the_uid_check_is_bounded_too(self, monkeypatch):
        seen: list[float] = []

        async def fake_shell(address, command, **kwargs):
            seen.append(kwargs.get("timeout", adb.CONTROL_TIMEOUT_S))
            return "0"

        monkeypatch.setattr(adb, "shell", fake_shell)
        assert await elevate._is_root("u:5555")
        assert seen == [elevate.ATTEMPT_TIMEOUT_S]
