"""The recording watcher: the script left on the unit, and the reading of its log.

No device here. The script's load-bearing properties are asserted as text — single
instance, rotation, no test-writes to a card already suspected of failing — and the
analysis rules are exercised on synthetic logs shaped like the real failures: a stall
mid-drive, the card flipping read-only, the boot-time age artifact that must never be
called a stall.
"""

from __future__ import annotations

import pytest

from app.ingest import adb, health

TICK = health.SAMPLE_INTERVAL_S


def _log(rows: list[tuple]) -> str:
    return "\n".join("|".join(str(f) for f in row) for row in rows)


def _trip(start: int, count: int, *, card="rw", age=30, avail=20_000_000, mem=500_000):
    """`count` healthy-looking samples spaced one interval apart."""
    return [(start + i * TICK, card, age, avail, mem) for i in range(count)]


class TestTheScript:
    def test_it_is_single_instance_by_pid_file(self):
        text = health.script()
        assert health.REMOTE_PID in text
        assert 'kill "$(cat "$PIDF")"' in text

    def test_it_rotates_its_own_log(self):
        text = health.script()
        assert "tail -n 2000" in text, "an unrotated log would grow for weeks off-driveway"

    def test_it_never_writes_to_the_card(self):
        """Writability comes from /proc/mounts, not a probe write: thousands of test
        writes a day at a card suspected of failing is an accelerant, not a diagnostic."""
        text = health.script()
        assert "/proc/mounts" in text
        assert "touch" not in text

    def test_the_sample_interval_is_the_configured_one(self):
        assert f"sleep {TICK}" in health.script()


class TestParsing:
    def test_ordinary_lines_parse(self):
        samples = health.parse("1755640000|rw|34|29000000|150000\n1755640020|ro|34|na|na\n")
        assert len(samples) == 2
        assert samples[0].card == "rw" and samples[0].age == 34
        assert samples[1].card == "ro" and samples[1].avail_kb is None

    def test_garbage_lines_are_dropped_not_fatal(self):
        samples = health.parse("hello\n1755640000|rw|34|1|1\n||||\n")
        assert len(samples) == 1

    def test_nodir_is_carried_as_its_own_signal(self):
        (sample,) = health.parse("1755640000|rw|nodir|na|na")
        assert sample.nodir and sample.age is None


class TestTheStallRule:
    def test_a_mid_drive_stall_is_one_incident_with_a_duration(self):
        rows = _trip(1_755_640_000, 10)
        # The recorder stops: age climbs past the threshold and keeps climbing.
        last = rows[-1][0]
        rows += [(last + TICK * (i + 1), "rw", 200 + TICK * i, 20_000_000, 500_000) for i in range(9)]
        report = health.analyze(health.parse(_log(rows)))
        assert len(report.incidents) == 1
        assert "STOPPED WRITING" in report.incidents[0]

    def test_the_boot_age_artifact_is_not_a_stall(self):
        """At power-on the newest recording is the previous drive's last, hours old. The
        first samples of a trip must never read that as the recorder being stalled."""
        start = 1_755_640_000
        rows = [
            (start, "rw", 7200, 20_000_000, 500_000),  # newest file is 2h old at key-on
            (start + TICK, "rw", 7200 + TICK, 20_000_000, 500_000),
            # ... camera closes its first segment and the age collapses:
            (start + 2 * TICK, "rw", 10, 20_000_000, 500_000),
            (start + 3 * TICK, "rw", 30, 20_000_000, 500_000),
        ]
        report = health.analyze(health.parse(_log(rows)))
        assert report.healthy, report.incidents

    def test_a_healthy_drive_reports_healthy(self):
        report = health.analyze(health.parse(_log(_trip(1_755_640_000, 30))))
        assert report.healthy
        assert report.trips == 1
        assert "healthy" in report.summary()


class TestTheOtherIncidents:
    def test_a_read_only_card_is_named_as_the_emergency_it_is(self):
        rows = [*_trip(1_755_640_000, 5), (1_755_640_000 + 5 * TICK, "ro", 30, 20_000_000, 500_000)]
        report = health.analyze(health.parse(_log(rows)))
        assert any("READ-ONLY" in i for i in report.incidents)

    def test_a_vanished_recording_folder_is_reported_after_the_mount_grace(self):
        start = 1_755_640_000
        rows = _trip(start, 5)
        rows += [(start + (5 + i) * TICK, "rw", "nodir", "na", 500_000) for i in range(5)]
        report = health.analyze(health.parse(_log(rows)))
        assert any("MISSING" in i for i in report.incidents)

    def test_low_space_is_flagged_from_the_last_sample(self):
        rows = _trip(1_755_640_000, 5, avail=100_000)  # ~100 MB free
        report = health.analyze(health.parse(_log(rows)))
        assert any("nearly full" in i for i in report.incidents)

    def test_two_trips_are_counted_as_two(self):
        first = _trip(1_755_640_000, 10)
        second = _trip(1_755_640_000 + 7200, 10)  # two hours later
        report = health.analyze(health.parse(_log(first + second)))
        assert report.trips == 2
        assert report.healthy


class StubSettings:
    def __init__(self, values):
        self.values = dict(values)

    def get_nowait(self, key, default=None):
        return self.values.get(key, default)

    async def set(self, key, value, *, internal=False):
        self.values[key] = value


@pytest.fixture(autouse=True)
def clean_module_state():
    health.reset_for_tests()
    yield
    health.reset_for_tests()


class TestArming:
    async def test_the_script_is_deployed_by_base64_and_started_detached(self, monkeypatch):
        shells: list[str] = []

        async def fake_shell(address, command, **kwargs):
            shells.append(command)
            return ""

        monkeypatch.setattr(adb, "shell", fake_shell)

        assert await health.arm("u:5555", "/storage/Tfcard/DCIM/Video")
        assert any("base64 -d" in c and health.REMOTE_SCRIPT in c for c in shells)
        launch = next(c for c in shells if "setsid" in c)
        # setsid for its own session (survives the car leaving); full redirection so the
        # launch returns rather than hanging like the tar|nc listener does.
        assert f"sh {health.REMOTE_SCRIPT} '/storage/Tfcard/DCIM/Video'" in launch
        assert "</dev/null >/dev/null 2>&1 &" in launch

    async def test_an_odd_directory_is_refused_outright(self, monkeypatch):
        async def fake_shell(address, command, **kwargs):
            raise AssertionError("nothing should reach the unit")

        monkeypatch.setattr(adb, "shell", fake_shell)
        assert not await health.arm("u:5555", "/storage/x'; rm -rf /; '")

    async def test_on_unit_seen_is_debounced(self, monkeypatch):
        calls: list[str] = []

        monkeypatch.setattr(
            health, "get_settings_service", lambda: StubSettings({health.ENABLED_KEY: True})
        )

        async def fake_work(address, source_dir):
            calls.append(address)

        monkeypatch.setattr(health, "_collect_then_arm", fake_work)

        import asyncio

        health.on_unit_seen("u:5555", "/storage/Tfcard/DCIM/Video")
        health.on_unit_seen("u:5555", "/storage/Tfcard/DCIM/Video")
        await asyncio.sleep(0)
        assert calls == ["u:5555"], "the arrival branch re-runs every tick; arming must not"

    async def test_switched_off_means_nothing_touches_the_unit(self, monkeypatch):
        monkeypatch.setattr(
            health, "get_settings_service", lambda: StubSettings({health.ENABLED_KEY: False})
        )
        started: list = []
        monkeypatch.setattr(
            health, "_collect_then_arm", lambda *a: started.append(a)
        )
        health.on_unit_seen("u:5555", "/storage/Tfcard/DCIM/Video")
        assert not started


class TestCollection:
    async def test_the_log_is_truncated_in_the_same_call_that_reads_it(self, monkeypatch):
        commands: list[str] = []

        async def fake_shell(address, command, **kwargs):
            commands.append(command)
            return _log(_trip(1_755_640_000, 5))

        monkeypatch.setattr(adb, "shell", fake_shell)
        monkeypatch.setattr(
            health, "get_settings_service", lambda: StubSettings({health.ENABLED_KEY: True})
        )

        report = await health.collect("u:5555")
        assert report is not None and report.healthy
        assert f": > {health.REMOTE_LOG}" in commands[0], "an untruncated log re-reports drives"

    async def test_incidents_reach_the_webhook_and_the_settings_page(self, monkeypatch):
        settings = StubSettings({health.ENABLED_KEY: True})
        monkeypatch.setattr(health, "get_settings_service", lambda: settings)

        rows = [*_trip(1_755_640_000, 5), (1_755_640_000 + 5 * TICK, "ro", 30, 1, 1)]

        async def fake_shell(address, command, **kwargs):
            return _log(rows)

        monkeypatch.setattr(adb, "shell", fake_shell)

        published: dict = {}

        async def fake_publish(event, **kwargs):
            published["event"] = event
            published.update(kwargs)

        import app.ingest.reporter as reporter

        monkeypatch.setattr(reporter, "publish", fake_publish)

        report = await health.collect("u:5555")
        assert report is not None and not report.healthy
        assert "READ-ONLY" in str(settings.values.get(health.REPORT_KEY))
        assert published.get("event") == "health"
        assert published["extra"]["health_incidents"] == report.incidents
