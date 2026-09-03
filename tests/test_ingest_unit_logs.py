"""The head unit's own log: what we ask it to capture, and how we read it back.

No device here. The capture command's load-bearing properties are asserted as text --
bounded rotation, absolute timestamps, the measured noise tags silenced *on the unit* so
they never cross the link -- and the parser is exercised on lines copied verbatim from the
live unit, including the shapes that must be rejected rather than half-stored.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.ingest import unit_logs

#: Copied verbatim from the unit, with `-v threadtime -v year -v UTC` applied.
REAL_LINES = """--------- beginning of main
2026-09-01 13:19:41.979 +0000   971  1476 E BatteryService: [20250926]mIsAccCable=true
2026-09-01 13:19:42.497 +0000  6630  7136 E ZQC-CamSubStream0: (6630-7136)ObtainYuvRate:16/s.cameraId 0 SleepTime 30
2026-09-01 13:19:43.501 +0000   557  1445 E GNSSMGT : nmea_reader_handle_gsa: modified to MAX_FIX_SVS
2026-09-01 13:19:44.010 +0000   971  1852 W OverlayManager: service 'idmap' died
"""


class TestTheCaptureCommand:
    def test_it_bounds_its_own_size_on_the_unit(self):
        """An unbounded capture on a car's SD card is a slow way to fill it."""
        command = unit_logs.capture_command()
        assert f"-r {unit_logs.ROTATE_KIB}" in command
        assert f"-n {unit_logs.ROTATE_COUNT}" in command
        assert f"-f {unit_logs.REMOTE_LOG}" in command

    def test_it_asks_for_absolute_timestamps(self):
        """Without `year` the parser would have to guess across a New Year boundary, and
        without `UTC` it would have to know the unit's timezone. Both are avoidable."""
        command = unit_logs.capture_command()
        assert "-v year" in command
        assert "-v UTC" in command

    def test_it_starts_at_the_present(self):
        """Re-arming on every arrival must not replay the ring buffer we just consumed."""
        assert "-T 1" in unit_logs.capture_command()

    def test_it_silences_the_measured_noise_on_the_unit(self):
        """Filtering here rather than after transfer is the difference between about a
        megabyte an hour and about eighty."""
        command = unit_logs.capture_command()
        for tag in ("ParamSet", "isp_alg_fw"):
            assert f"{tag}:S" in command

    def test_it_reads_the_buffers_that_carry_failures(self):
        command = unit_logs.capture_command()
        for buffer in ("crash", "kernel"):
            assert f"-b {buffer}" in command


class TestTheSilencedTagList:
    def test_it_falls_back_to_the_measured_defaults_when_unset(self, monkeypatch):
        monkeypatch.setattr(unit_logs, "get_settings_service", lambda: _Settings(""))
        assert unit_logs.silenced_tags() == unit_logs.DEFAULT_DENY_TAGS

    def test_it_takes_an_operator_list(self, monkeypatch):
        monkeypatch.setattr(unit_logs, "get_settings_service", lambda: _Settings("Foo, Bar"))
        assert unit_logs.silenced_tags() == ("Foo", "Bar")

    def test_it_drops_shell_metacharacters_rather_than_quoting_them(self, monkeypatch):
        """This string is interpolated into a remote shell command. A tag list has no
        legitimate need for a semicolon, so an odd entry is refused, not escaped."""
        monkeypatch.setattr(
            unit_logs, "get_settings_service", lambda: _Settings("Good, evil; rm -rf /")
        )
        assert unit_logs.silenced_tags() == ("Good",)

    def test_an_entirely_unsafe_list_falls_back_rather_than_capturing_everything(self, monkeypatch):
        """Dropping every entry must not silently turn into 'silence nothing', which is
        the 80 MB/hour case."""
        monkeypatch.setattr(unit_logs, "get_settings_service", lambda: _Settings("$(x), `y`"))
        assert unit_logs.silenced_tags() == unit_logs.DEFAULT_DENY_TAGS


class TestParsing:
    def test_it_reads_a_real_line(self):
        entries = unit_logs.parse(REAL_LINES)
        first = entries[0]
        assert first.occurred_at == datetime(2026, 9, 1, 13, 19, 41, 979000, tzinfo=UTC)
        assert (first.pid, first.tid) == (971, 1476)
        assert first.level == "E"
        assert first.tag == "BatteryService"
        assert "mIsAccCable=true" in first.message

    def test_it_keeps_the_recorders_own_lines(self):
        """ZQC-CamSubStream is the built-in recorder reporting real per-camera frame
        rate -- the whole reason for collecting this at all."""
        entries = unit_logs.parse(REAL_LINES)
        recorder = [e for e in entries if e.tag.startswith("ZQC-Cam")]
        assert len(recorder) == 1
        assert "ObtainYuvRate:16/s" in recorder[0].message

    def test_it_handles_a_tag_with_a_trailing_space(self):
        """`GNSSMGT ` really does print that way on this firmware."""
        tags = {e.tag for e in unit_logs.parse(REAL_LINES)}
        assert "GNSSMGT" in tags

    def test_it_skips_logcat_banners(self):
        assert all("beginning of" not in e.message for e in unit_logs.parse(REAL_LINES))

    def test_it_drops_a_line_cut_in_half_by_rotation(self):
        """Half a line is not evidence, and storing it as a mystery is worse than
        dropping it."""
        assert unit_logs.parse("2026-09-01 13:19:41.9") == []

    def test_it_truncates_a_vendor_stack_dump(self):
        giant = "2026-09-01 13:19:41.979 +0000 1 2 E Tag: " + ("x" * 9000)
        entry = unit_logs.parse(giant)[0]
        assert len(entry.message) == unit_logs.MAX_MESSAGE_CHARS


class TestDeduplicationIdentity:
    def test_the_same_line_hashes_the_same(self):
        [a] = unit_logs.parse(REAL_LINES.splitlines()[1])
        [b] = unit_logs.parse(REAL_LINES.splitlines()[1])
        assert a.line_hash == b.line_hash

    def test_two_processes_saying_the_same_thing_stay_distinct(self):
        """The parked refresh re-reads the same tail every minute, so identity has to be
        the whole tuple -- otherwise a genuine second occurrence is silently swallowed."""
        base = "2026-09-01 13:19:41.979 +0000 {pid} 2 E Tag: identical"
        [a] = unit_logs.parse(base.format(pid=111))
        [b] = unit_logs.parse(base.format(pid=222))
        assert a.line_hash != b.line_hash


class TestServerSideEnforcement:
    """The unit's filterspec silently ignores long tags, so the deny list is applied twice."""

    def test_a_denied_tag_is_dropped_even_if_the_unit_sent_it(self, monkeypatch):
        """Measured on the unit: a 32-character deny entry is accepted on the command line
        and then does nothing, so the line arrives anyway. Without this the setting would
        look configured and have no effect."""
        monkeypatch.setattr(
            unit_logs, "get_settings_service", lambda: _Settings("SprdActivityDebugConfigsUtilImpl")
        )
        entries = unit_logs.parse(
            "2026-09-01 13:19:41.979 +0000 1 2 E SprdActivityDebugConfigsUtilImpl: noise"
        )
        assert len(entries) == 1
        assert unit_logs.drop_silenced(entries) == []

    def test_an_undenied_tag_survives(self, monkeypatch):
        monkeypatch.setattr(unit_logs, "get_settings_service", lambda: _Settings("Other"))
        entries = unit_logs.parse(
            "2026-09-01 13:19:41.979 +0000 1 2 E ZQC-CamSubStream0: ObtainYuvRate:16/s"
        )
        assert unit_logs.drop_silenced(entries) == entries

    def test_every_privacy_tag_is_short_enough_for_the_unit_to_honour(self):
        """The networking tags name the hosts the unit contacts. Those must be stopped ON
        the unit, where the line is never written -- server-side dropping would be too
        late, because the data would already have crossed the link. Keeping them inside
        the length the filterspec honours is what makes that guarantee real."""
        privacy = (
            "dips_net",
            "resolv",
            "NETD_SEND_DNS_SOCK",
            "NETD_CREATE_SOCK",
            "dnsmasq2.89",
            "mDNSResponder",
        )
        for tag in privacy:
            assert tag in unit_logs.DEFAULT_DENY_TAGS
            assert len(tag) <= unit_logs.MAX_UNIT_FILTER_TAG, (
                f"{tag} is too long for the unit to actually silence, "
                "so hostnames would reach the server"
            )


class TestReadingTheCapture:
    @pytest.mark.asyncio
    async def test_a_consuming_read_stops_the_writer_before_clearing(self, monkeypatch):
        """logcat holds the current file open. Clearing it underneath a non-append writer
        pads the file with NULs, destroying the evidence we came for -- so the kill has to
        come first and the delete last."""
        seen: list[str] = []

        async def fake_shell(address, command, timeout=None):
            seen.append(command)
            return ""

        monkeypatch.setattr(unit_logs.adb, "shell", fake_shell)
        await unit_logs._read_capture("unit", consume=True)
        command = seen[0]
        assert command.index("kill") < command.index("cat")
        assert command.index("cat") < command.index("rm -f")

    @pytest.mark.asyncio
    async def test_a_refresh_never_consumes(self, monkeypatch):
        """The parked glance must leave the capture intact for the arrival collect."""
        seen: list[str] = []

        async def fake_shell(address, command, timeout=None):
            seen.append(command)
            return ""

        monkeypatch.setattr(unit_logs.adb, "shell", fake_shell)
        await unit_logs._read_capture("unit", consume=False)
        assert "rm -f" not in seen[0]
        assert "kill" not in seen[0]

    @pytest.mark.asyncio
    async def test_it_reads_rotations_oldest_first(self, monkeypatch):
        """logcat renames the current file to .1 and shifts existing rotations up, so the
        highest suffix is the oldest. Reading in that order keeps the tail chronological."""
        seen: list[str] = []

        async def fake_shell(address, command, timeout=None):
            seen.append(command)
            return ""

        monkeypatch.setattr(unit_logs.adb, "shell", fake_shell)
        await unit_logs._read_capture("unit", consume=False)
        command = seen[0]
        highest = command.index(f"{unit_logs.REMOTE_LOG}.{unit_logs.ROTATE_COUNT}")
        first = command.index(f"{unit_logs.REMOTE_LOG}.1")
        assert highest < first


class TestReenablingLogging:
    @pytest.mark.asyncio
    async def test_it_clears_suppression_and_starts_the_daemon(self, monkeypatch):
        """Two separate levers: persist.log.tag survives a reboot, ctl.start does not --
        which is why this runs on every arming rather than once."""
        seen: list[str] = []

        async def fake_shell(address, command, timeout=None):
            seen.append(command)
            return "running"

        monkeypatch.setattr(unit_logs.adb, "shell", fake_shell)
        assert await unit_logs.ensure_logging("unit") is True
        joined = " ".join(seen)
        assert "persist.log.tag" in joined
        assert "ctl.start logd" in joined

    @pytest.mark.asyncio
    async def test_it_reports_failure_when_logd_stays_down(self, monkeypatch):
        async def fake_shell(address, command, timeout=None):
            return "stopped"

        monkeypatch.setattr(unit_logs.adb, "shell", fake_shell)
        assert await unit_logs.ensure_logging("unit") is False

    @pytest.mark.asyncio
    async def test_arming_is_abandoned_when_logging_cannot_be_enabled(self, monkeypatch):
        """Starting a capture against a dead logd would leave a process producing nothing
        and a status line implying it works."""

        async def fake_shell(address, command, timeout=None):
            return "stopped"

        monkeypatch.setattr(unit_logs.adb, "shell", fake_shell)
        assert await unit_logs.arm("unit") is False


class TestArming:
    @pytest.mark.asyncio
    async def test_it_replaces_rather_than_stacks(self, monkeypatch):
        """Re-arming on every arrival must not leave one capture per visit running."""
        seen: list[str] = []

        async def fake_shell(address, command, timeout=None):
            seen.append(command)
            return "running"

        monkeypatch.setattr(unit_logs.adb, "shell", fake_shell)
        await unit_logs.arm("unit")
        launch = seen[-1]
        assert "kill $(cat" in launch
        assert unit_logs.REMOTE_PID in launch

    @pytest.mark.asyncio
    async def test_it_detaches_so_it_survives_the_car_driving_away(self, monkeypatch):
        """adbd SIGHUPs the process group when the transport drops, which is exactly the
        moment the capture needs to keep running."""
        seen: list[str] = []

        async def fake_shell(address, command, timeout=None):
            seen.append(command)
            return "running"

        monkeypatch.setattr(unit_logs.adb, "shell", fake_shell)
        await unit_logs.arm("unit")
        launch = seen[-1]
        assert launch.startswith("[ -f") and "setsid" in launch
        assert "</dev/null >/dev/null 2>&1 &" in launch

    @pytest.mark.asyncio
    async def test_the_recorded_pid_is_the_capture_itself(self, monkeypatch):
        """`exec` replaces the wrapper shell, so the pid file points at logcat rather than
        at a shell that has already exited -- otherwise the kill on the next arrival
        misses and captures stack up."""
        seen: list[str] = []

        async def fake_shell(address, command, timeout=None):
            seen.append(command)
            return "running"

        monkeypatch.setattr(unit_logs.adb, "shell", fake_shell)
        await unit_logs.arm("unit")
        assert "exec logcat" in seen[-1]


class _Settings:
    """Minimal stand-in for the settings service."""

    def __init__(self, deny: str):
        self._deny = deny

    def get_nowait(self, key, default=None):
        if key == unit_logs.DENY_KEY:
            return self._deny
        return True


class _AllowSettings:
    def __init__(self, allowed: str = "") -> None:
        self.allowed = allowed

    def get_nowait(self, key: str, default=None):
        if key == unit_logs.ALLOW_KEY:
            return self.allowed
        return default


class TestTheAllowedTagList:
    def test_the_defaults_name_the_evidence_worth_having(self, monkeypatch):
        monkeypatch.setattr(unit_logs, "get_settings_service", lambda: _AllowSettings(""))
        tags = unit_logs.allowed_tags()
        for wanted in ("AndroidRuntime", "ZQC-CamSubStream0", "UnisocWatchdog", "CarPlayTiming"):
            assert wanted in tags

    def test_it_takes_an_operator_list(self, monkeypatch):
        monkeypatch.setattr(unit_logs, "get_settings_service", lambda: _AllowSettings("Foo, Bar"))
        assert unit_logs.allowed_tags() == ("Foo", "Bar")

    def test_it_drops_shell_metacharacters_rather_than_quoting_them(self, monkeypatch):
        monkeypatch.setattr(
            unit_logs, "get_settings_service", lambda: _AllowSettings("Good, $(rm -rf /), Fine")
        )
        assert unit_logs.allowed_tags() == ("Good", "Fine")


class TestEnsureLogging:
    """The measured lesson: silence the writers, raise only what is wanted."""

    async def test_it_keeps_the_vendor_silence_and_raises_only_the_allow_list(self, monkeypatch):
        from app.ingest import adb

        shells: list[str] = []

        async def fake_shell(address, command, **kwargs):
            shells.append(command)
            return "running"

        monkeypatch.setattr(adb, "shell", fake_shell)
        monkeypatch.setattr(unit_logs, "get_settings_service", lambda: _AllowSettings(""))

        assert await unit_logs.ensure_logging("u:5555")

        setup = shells[0]
        assert "setprop persist.log.tag S" in setup
        assert 'persist.log.tag ""' not in setup, "clearing it is what unleashed the flood"
        assert "setprop log.tag.CarPlayTiming E;" in setup
        assert "setprop log.tag.AndroidRuntime E;" in setup
        assert "setprop ctl.start logd" in setup

    async def test_it_reports_logd_not_coming_up(self, monkeypatch):
        from app.ingest import adb

        async def fake_shell(address, command, **kwargs):
            return "stopped"

        monkeypatch.setattr(adb, "shell", fake_shell)
        monkeypatch.setattr(unit_logs, "get_settings_service", lambda: _AllowSettings(""))
        assert not await unit_logs.ensure_logging("u:5555")
