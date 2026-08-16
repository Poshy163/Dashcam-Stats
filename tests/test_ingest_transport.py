"""The head-unit ingest: delta, transport and the staged commit.

No device and no ADB anywhere in here. The control channel is mocked, but the *transport*
is exercised for real over a loopback socket serving a genuine tar stream, because that is
the part with the interesting failure: the car pulls out of the driveway mid-file and the
question is what the footage directory looks like afterwards.

Why the transport is shaped the way it is, measured against the live unit: everything
routed through ``adbd`` capped at ~10 MB/s no matter how many parallel streams it was given
(``adb pull`` managed 4), while the TF card reads at 60 MB/s and the WiFi link is good for
~50. A plain socket carrying ``tar c | nc`` measured 34.3 MB/s. The unit has no battery, so
it is on the network only while the engine runs -- one to two minutes -- and that
difference is 1.2 GB per window against 4 GB.
"""

from __future__ import annotations

import asyncio
import io
import socket
import tarfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.ingest import transport
from app.ingest.models import RemoteFile
from app.ingest.puller import commit, delta


def _tar_bytes(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, payload in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


class FakeProcess:
    """Stands in for the `adb shell` child that carries the listener."""

    def __init__(self, *, dies_on_kill: bool = True) -> None:
        self.pid = 4321
        self.returncode: int | None = None
        self.killed = False
        self._dies_on_kill = dies_on_kill

    def kill(self) -> None:
        self.killed = True
        if self._dies_on_kill:
            self.returncode = -9

    async def wait(self) -> int:
        if self.returncode is None:
            await asyncio.sleep(3600)
        return self.returncode or 0


async def _settle() -> None:
    """Let the run's fire-and-forget side effects finish.

    Reporting and the head unit's screen are deliberately not awaited by the transfer, so a
    test that wants to see them has to say so rather than race them.
    """
    from app.ingest import puller

    pending = list(puller._side_tasks)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


def _serve(payload: bytes, *, truncate_at: int | None = None) -> int:
    """Serve *payload* once on a loopback port, like the unit's `nc -l`. Returns the port."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def run() -> None:
        with listener:
            conn, _ = listener.accept()
            with conn:
                data = payload if truncate_at is None else payload[:truncate_at]
                try:
                    conn.sendall(data)
                except OSError:
                    pass

    threading.Thread(target=run, daemon=True).start()
    return port


class TestTheDelta:
    """Size-based, never checksummed: a truncated arrival re-fetches itself next window."""

    def test_missing_and_wrong_sized_files_are_fetched(self, tmp_path):
        (tmp_path / "20260812120000_camera_0.ts").write_bytes(b"x" * 100)
        (tmp_path / "20260812120100_camera_0.ts").write_bytes(b"x" * 50)  # truncated locally

        remote = [
            RemoteFile("20260812120000_camera_0.ts", 100, 0),  # already have it, same size
            RemoteFile("20260812120100_camera_0.ts", 100, 0),  # short locally -> refetch
            RemoteFile("20260812120200_camera_0.ts", 100, 0),  # absent locally
        ]
        plan = delta(remote, tmp_path, skip_active_s=15, camera="both")

        assert [item.name for item in plan.files] == [
            "20260812120100_camera_0.ts",
            "20260812120200_camera_0.ts",
        ]
        assert plan.bytes == 200
        assert plan.backlog_files == 2

    def test_the_segment_still_being_recorded_is_left_alone(self, tmp_path):
        """Both lenses write continuously; the newest file of each is open in the recorder.

        Copying it produces a short file that looks complete, and the size-based delta
        would then consider it done.
        """
        now = int(time.time())
        remote = [
            RemoteFile("20260812120000_camera_0.ts", 100, now - 600),
            RemoteFile("20260812120100_camera_0.ts", 100, now - 2),  # being written now
        ]
        plan = delta(remote, tmp_path, skip_active_s=15, camera="both")

        assert [item.name for item in plan.files] == ["20260812120000_camera_0.ts"]
        assert plan.active_skipped == 1

    def test_the_copy_order_can_be_reversed_so_a_big_backlog_does_not_starve(self, tmp_path):
        """Oldest-first is only right while the backlog fits in a window.

        Once it does not, every window goes on the oldest recordings and today's drive is
        never reached at all.
        """
        remote = [
            RemoteFile("20260812120000_camera_0.ts", 100, 0),
            RemoteFile("20260812130000_camera_0.ts", 100, 0),
            RemoteFile("20260812140000_camera_0.ts", 100, 0),
        ]
        names = sorted(item.name for item in remote)

        oldest = delta(remote, tmp_path, skip_active_s=15, camera="both")
        newest = delta(remote, tmp_path, skip_active_s=15, camera="both", newest_first=True)

        assert [item.name for item in oldest.files] == names
        assert [item.name for item in newest.files] == list(reversed(names))

    def test_the_camera_filter_still_counts_the_backlog(self, tmp_path):
        """Filtering changes what is fetched, not what is known to be outstanding."""
        remote = [
            RemoteFile("20260812120000_camera_0.ts", 100, 0),
            RemoteFile("20260812120000_camera_1.ts", 100, 0),
        ]
        plan = delta(remote, tmp_path, skip_active_s=15, camera="camera_0")

        assert [item.name for item in plan.files] == ["20260812120000_camera_0.ts"]
        assert plan.backlog_files == 2, "the interior lens is still sitting on the card"


class TestTheStagedCommit:
    """Nothing reaches the footage directory until it is byte-complete."""

    def test_only_whole_files_are_published(self, tmp_path):
        staging, footage = tmp_path / "staging", tmp_path / "footage"
        staging.mkdir()
        footage.mkdir()
        (staging / "whole.ts").write_bytes(b"x" * 100)
        (staging / "short.ts").write_bytes(b"x" * 40)  # the window closed mid-file

        committed = commit(staging, footage, {"whole.ts": 100, "short.ts": 100})

        assert committed == ["whole.ts"]
        assert (footage / "whole.ts").exists()
        assert not (footage / "short.ts").exists(), "a truncated recording was published"
        assert not (staging / "short.ts").exists(), "the truncated file was left to rot"
        assert list(staging.iterdir()) == []

    def test_an_unexpected_file_is_never_published(self, tmp_path):
        staging, footage = tmp_path / "staging", tmp_path / "footage"
        staging.mkdir()
        footage.mkdir()
        (staging / "surprise.ts").write_bytes(b"x" * 10)

        assert commit(staging, footage, {}) == []
        assert not (footage / "surprise.ts").exists()

    def test_an_unmounted_share_commits_nothing(self, tmp_path):
        """The mount point inside the container is an ordinary empty directory.

        Creating it would write a whole card into the container's writable layer, report
        success, and -- with delete-after-verify on -- then erase the originals.
        """
        staging, footage = tmp_path / "staging", tmp_path / "not-mounted"
        staging.mkdir()
        (staging / "clip.ts").write_bytes(b"x" * 100)

        assert commit(staging, footage, {"clip.ts": 100}) == []
        assert not footage.exists(), "the footage root was created behind a missing mount"
        assert (staging / "clip.ts").exists(), "the only copy was thrown away"

    def test_a_complete_recording_already_in_the_library_is_not_replaced(self, tmp_path):
        staging, footage = tmp_path / "staging", tmp_path / "footage"
        staging.mkdir()
        footage.mkdir()
        (footage / "clip.ts").write_bytes(b"original" * 20)
        (staging / "clip.ts").write_bytes(b"x" * 160)

        assert commit(staging, footage, {"clip.ts": 160}) == []
        assert (footage / "clip.ts").read_bytes() == b"original" * 20

    def test_a_truncated_local_copy_is_completed(self, tmp_path):
        """This is what the size-based delta is for, so it must still work."""
        staging, footage = tmp_path / "staging", tmp_path / "footage"
        staging.mkdir()
        footage.mkdir()
        (footage / "clip.ts").write_bytes(b"x" * 40)  # a window that closed early
        (staging / "clip.ts").write_bytes(b"y" * 100)

        assert commit(staging, footage, {"clip.ts": 100}) == ["clip.ts"]
        assert (footage / "clip.ts").read_bytes() == b"y" * 100


class TestTheTransport:
    async def test_a_complete_stream_lands_every_file(self, tmp_path):
        payload = {f"2026081212{i:02d}00_camera_0.ts": bytes([i]) * 4096 for i in range(5)}
        port = _serve(_tar_bytes(payload))

        seen: list[str] = []
        result = transport.receive("127.0.0.1", port, tmp_path, on_file_done=seen.append)

        assert result.complete
        assert sorted(result.files) == sorted(payload)
        assert sorted(seen) == sorted(payload)
        assert result.bytes_received > 0
        for name, body in payload.items():
            assert (tmp_path / name).read_bytes() == body

    async def test_a_window_that_closes_mid_file_keeps_what_arrived(self, tmp_path):
        """The normal ending, not an error: the engine stopped and the unit went away."""
        payload = {f"2026081212{i:02d}00_camera_0.ts": bytes([i]) * 8192 for i in range(6)}
        blob = _tar_bytes(payload)
        port = _serve(blob, truncate_at=len(blob) // 3)

        result = transport.receive("127.0.0.1", port, tmp_path)

        assert not result.complete
        assert result.error, "an interrupted transfer should say why it stopped"
        assert 0 < len(result.files) < len(payload)
        # Everything it *claims* to have finished really is whole; commit() checks sizes
        # again anyway, which is what keeps a partial tail out of the library.
        for name in result.files:
            assert (tmp_path / name).read_bytes() == payload[name]

    async def test_cancellation_stops_the_transfer(self, tmp_path):
        payload = {f"2026081212{i:02d}00_camera_0.ts": bytes([i]) * 65536 for i in range(20)}
        port = _serve(_tar_bytes(payload))

        cancel = threading.Event()
        cancel.set()  # already cancelled: the first read must refuse

        result = transport.receive("127.0.0.1", port, tmp_path, cancel=cancel)

        assert not result.complete
        assert result.files == []
        assert "cancel" in (result.error or "").lower()

    async def test_a_refused_connection_is_reported_not_raised(self, tmp_path, monkeypatch):
        monkeypatch.setattr(transport, "CONNECT_RETRY_S", 0.2)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        listener.close()

        result = transport.receive("127.0.0.1", port, tmp_path)

        assert not result.complete
        assert result.error
        assert result.files == []

    async def test_a_member_with_a_path_is_refused(self, tmp_path):
        """The unit is asked for a flat list of its own recordings and nothing else."""
        port = _serve(_tar_bytes({"../../etc/passwd": b"nope", "good.ts": b"yes"}))

        result = transport.receive("127.0.0.1", port, tmp_path)

        assert result.files == ["good.ts"]
        assert not (tmp_path.parent / "passwd").exists()
        assert not (Path("/etc") / "passwd").is_symlink()


class TestSingleFlight:
    async def test_a_second_pull_cannot_start_on_top_of_the_first(self):
        """The poll fires every few seconds while the unit sits on the network."""
        from app.ingest.models import RunResult, RunState
        from app.ingest.status import IngestStatus

        status = IngestStatus()
        assert status.try_begin()
        assert not status.try_begin(), "two pullers would extract into the same staging dir"

        status.finish(RunResult(state=RunState.OK, files=1, bytes=1, seconds=1.0))
        assert status.try_begin()

    async def test_cancel_only_applies_while_running(self):
        from app.ingest.status import IngestStatus

        status = IngestStatus()
        assert not status.cancel()
        assert status.try_begin()
        assert status.cancel()
        assert status.cancel_event.is_set()


class TestTheStatusSnapshot:
    """One snapshot feeds the API, the UI, Home Assistant's sensor and the webhook."""

    def test_it_carries_everything_the_sensor_needs(self):
        from app.ingest.models import DeltaPlan
        from app.ingest.status import IngestStatus

        status = IngestStatus()
        status.try_begin()
        status.plan(
            DeltaPlan(
                files=[RemoteFile("a.ts", 1000, 0), RemoteFile("b.ts", 2000, 0)],
                backlog_files=2,
                backlog_bytes=3000,
            )
        )
        status.add_bytes(1000)
        status.file_done("a.ts")

        snapshot = status.snapshot()
        for key in (
            "state",
            "unit_online",
            "files_total",
            "files_done",
            "bytes_total",
            "bytes_done",
            "throughput_mbs",
            "current_file",
            "backlog_files",
            "backlog_bytes",
            "last_success_ts",
            "last_error",
        ):
            assert key in snapshot, f"the Home Assistant REST sensor reads {key}"
        assert snapshot["files_total"] == 2
        assert snapshot["files_done"] == 1
        assert snapshot["bytes_total"] == 3000
        assert snapshot["bytes_done"] == 1000


class TestTheAdbControlChannel:
    def test_the_source_probe_prefers_the_stable_symlink(self):
        """The volume id changes at every reformat; the symlink does not."""
        from app.ingest.adb import SOURCE_PROBE

        assert SOURCE_PROBE.index("/storage/Tfcard/DCIM/Video") < SOURCE_PROBE.index("/storage/*")

    async def test_the_inventory_parses_stat_output(self, monkeypatch):
        from app.ingest import adb

        async def fake_shell(address, command, **kwargs):
            assert "stat -c" in command
            return "104857600|20260812120000_camera_0.ts|1786000000\nrubbish\n"

        monkeypatch.setattr(adb, "shell", fake_shell)
        files = await adb.inventory("unit:5555", "/storage/Tfcard/DCIM/Video")

        assert len(files) == 1
        assert files[0] == RemoteFile("20260812120000_camera_0.ts", 104857600, 1786000000)

    async def test_the_card_is_listed_with_one_process_not_one_per_file(self, monkeypatch):
        """A full card is ~140 recordings, and this runs inside the driveway window.

        The loop this replaced spawned a `stat` per recording on a SoC that is not quick
        at spawning anything.
        """
        from app.ingest import adb

        captured: list[str] = []

        async def fake_shell(address, command, **kwargs):
            captured.append(command)
            return ""

        monkeypatch.setattr(adb, "shell", fake_shell)
        await adb.inventory("unit:5555", "/src")

        assert "for f in" not in captured[0], "the per-file loop is back"
        assert captured[0].count("stat -c") == 1
        assert captured[0].rstrip().endswith("exit 0"), "an empty card must not fail the call"

    async def test_the_listener_is_launched_without_waiting_for_adb(self, monkeypatch):
        """`adb shell` does not return for this, and waiting for it is what broke.

        Measured against the live head unit: the call had not returned after twenty
        seconds, while a connection to port 9000 was answered immediately with a valid tar
        header naming the first file. The listener was up and serving the whole time; only
        the local wait was stuck, and the run aborted around a transfer that was ready to
        go. So nothing is backgrounded remotely and nothing is awaited locally -- the adb
        session becomes the listener's lifetime.
        """
        from app.ingest import adb

        captured: list[list[str]] = []

        async def fake_spawn(*args, **kwargs):
            captured.append(list(args))
            return FakeProcess()

        async def forbidden_shell(address, command, **kwargs):
            raise AssertionError("the listener must not be launched through a waited call")

        monkeypatch.setattr(adb.asyncio, "create_subprocess_exec", fake_spawn)
        monkeypatch.setattr(adb, "shell", forbidden_shell)
        monkeypatch.setattr(adb, "adb_path", lambda: "adb")

        proc = await adb.launch_listener(
            "unit:5555", "/src", ["a.ts", "b.ts"], port=9000, timeout_s=180
        )

        assert proc is not None, "the caller needs the handle to end the session afterwards"
        argv = captured[0]
        assert argv[:4] == ["adb", "-s", "unit:5555", "shell"]
        command = argv[4]
        assert "tar c a.ts b.ts" in command
        assert "timeout 180 nc -l -p 9000" in command
        # Nothing backgrounded remotely: that is exactly what adb would not return from.
        assert not command.rstrip().endswith("&")
        assert "setsid" not in command

    async def test_the_listener_session_is_ended_after_the_stream(self):
        """The adb session is the listener's lifetime, so it has to be closed explicitly."""
        from app.ingest import adb

        proc = FakeProcess(dies_on_kill=True)
        was_serving = await adb.stop_listener(proc)

        assert proc.killed
        assert proc.returncode is not None
        assert was_serving, "a session we had to kill means the unit was still serving"

    async def test_a_listener_that_died_on_its_own_is_reported_as_such(self):
        """Which side stopped first is the difference between a fault and a normal ending.

        Still serving when we stopped reading = the car pulled away. Already gone = the
        unit gave up, and that is somebody's problem.
        """
        from app.ingest import adb

        proc = FakeProcess()
        proc.returncode = 1  # `tar` failed, or the remote `timeout` fired

        assert await adb.stop_listener(proc) is False
        assert not proc.killed, "an exited session must not be killed again"

    async def test_a_stale_listener_is_cleared_first(self, monkeypatch):
        """An orphan is serving a *previous* run's file list, not this one's."""
        from app.ingest import adb

        captured: list[str] = []

        async def fake_shell(address, command, **kwargs):
            captured.append(command)
            return ""

        monkeypatch.setattr(adb, "shell", fake_shell)
        await adb.clear_listener("unit:5555")

        assert "pidof nc" in captured[0]
        assert captured[0].rstrip().endswith("exit 0"), "an empty pidof must not fail the call"

    @pytest.mark.parametrize(
        "hostile",
        ["a b.ts", "*.ts", "'; rm -rf /; '", "../../etc/passwd", "a$(id).ts", "a'b.ts"],
    )
    async def test_a_hostile_card_filename_never_reaches_a_shell(self, monkeypatch, hostile):
        """Card contents are whatever happens to be on a removable card.

        Unquoted and unchecked, one odd filename turns `rm -f` into a broad delete and a
        `*` into "erase the card".
        """
        from app.ingest import adb

        async def fail(*args, **kwargs):
            raise AssertionError(f"{hostile!r} was put into a shell command")

        monkeypatch.setattr(adb, "shell", fail)

        with pytest.raises(adb.AdbError):
            await adb.launch_listener("u:5555", "/src", [hostile], port=9000, timeout_s=60)
        with pytest.raises(adb.AdbError):
            await adb.delete("u:5555", "/src", [hostile])

    async def test_an_empty_card_is_not_a_control_failure(self, monkeypatch):
        """Once delete-after-verify is on, an empty card is the steady state.

        The inventory loop's last statement fails when the glob matches nothing, so
        without the explicit `exit 0` every run after the first would have reported a
        control-channel failure instead of "nothing to do".
        """
        from app.ingest import adb

        captured: list[str] = []

        async def fake_shell(address, command, **kwargs):
            captured.append(command)
            return ""

        monkeypatch.setattr(adb, "shell", fake_shell)
        assert await adb.inventory("u:5555", "/src") == []
        assert captured[0].rstrip().endswith("exit 0")

    async def test_an_unlocatable_card_is_its_own_failure(self, monkeypatch):
        """ "The card is missing" and "the car is not here" need different answers."""
        from app.ingest import adb

        async def fake_shell(address, command, **kwargs):
            return ""

        async def fake_state(address):
            from app.ingest.models import UnitState

            return UnitState.DEVICE

        monkeypatch.setattr(adb, "shell", fake_shell)
        monkeypatch.setattr(adb, "state", fake_state)
        monkeypatch.setattr(adb, "reconnect", lambda address: asyncio.sleep(0))

        info = await adb.describe("u:5555")

        assert info.online
        assert info.source is None
        assert info.card_error, "the operator would have been told the unit was offline"

    async def test_no_listener_is_launched_for_an_empty_plan(self, monkeypatch):
        from app.ingest import adb

        async def fail(*args, **kwargs):
            raise AssertionError("a listener was started with nothing to send")

        monkeypatch.setattr(adb, "shell", fail)
        await adb.launch_listener("unit:5555", "/src", [], port=9000, timeout_s=180)


class TestThePresenceProbe:
    """The tick that runs all day. It has to be cheap, and it has to be right about absence."""

    async def test_an_open_port_is_seen(self):
        from app.ingest import adb

        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        try:
            assert await adb.is_listening(f"127.0.0.1:{listener.getsockname()[1]}")
        finally:
            listener.close()

    async def test_a_closed_port_reads_as_the_car_being_out(self, monkeypatch):
        from app.ingest import adb

        monkeypatch.setattr(adb, "PRESENCE_TIMEOUT_S", 0.3)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        listener.close()

        assert not await adb.is_listening(f"127.0.0.1:{port}")

    @pytest.mark.parametrize("address", ["", "   ", ":5555"])
    async def test_an_unconfigured_address_never_connects_to_ourselves(self, address):
        """An empty host is not localhost, whatever asyncio would otherwise make of it."""
        from app.ingest import adb

        assert not await adb.is_listening(address)

    async def test_it_spawns_nothing(self, monkeypatch):
        """The entire point: the car is absent for all but a few minutes of the day.

        This used to be three `adb` process spawns per tick, which is what kept the
        interval long enough to lose a meaningful slice of the window to it.
        """
        from app.ingest import adb

        async def forbidden(*args, **kwargs):
            raise AssertionError("the cheap presence check spawned a subprocess")

        monkeypatch.setattr(adb.asyncio, "create_subprocess_exec", forbidden)
        monkeypatch.setattr(adb, "PRESENCE_TIMEOUT_S", 0.3)

        await adb.is_listening("127.0.0.1:1")


class TestTheUnitDisplay:
    """Putting the Backup page on the head unit's own screen while a transfer runs.

    The only way to show a real progress bar on the car without installing anything: AOSP's
    `cmd notification post` has no setProgress, no setOngoing and no setOnlyAlertOnce, so a
    notification could only ever be text that re-alerts on every update.
    """

    @pytest.mark.parametrize(
        "hostile",
        [
            "http://h:8098/b'; rm -rf /storage/Tfcard; echo '",
            "http://h:8098/$(reboot)",
            "http://h:8098/`id`",
            "http://h:8098/a b",
            "http://h:8098/x\nam force-stop com.example",
            "http://h:8098/x; reboot",
            "file:///etc/passwd",
            "javascript:alert(1)",
            "",
            "http://" + "a" * 400,
        ],
    )
    def test_only_a_plain_web_address_is_accepted(self, hostile):
        from app.ingest.adb import is_safe_url

        assert not is_safe_url(hostile)

    @pytest.mark.parametrize(
        "good",
        [
            "http://192.168.1.10:8098/backup",
            "https://dashcam.example.com/backup",
            "http://nas:8098/backup?tab=history",
            "http://nas:8098",
        ],
    )
    def test_an_ordinary_lan_address_is_allowed(self, good):
        from app.ingest.adb import is_safe_url

        assert is_safe_url(good)

    async def test_a_refused_url_never_reaches_a_shell(self, monkeypatch):
        from app.ingest import adb

        async def fail(*args, **kwargs):
            raise AssertionError("a URL that is not a plain web address reached a shell")

        monkeypatch.setattr(adb, "shell", fail)
        await adb.show_url("u:5555", "http://h/'; reboot; echo '")

    async def test_a_unit_with_no_browser_is_not_a_transfer_failure(self, monkeypatch):
        """`am` reports this on stdout with a zero exit status, so it is easy to miss."""
        from app.ingest import adb

        async def fake_shell(address, command, **kwargs):
            assert "am start -a android.intent.action.VIEW" in command
            return "Error: Activity not started, unable to resolve Intent"

        monkeypatch.setattr(adb, "shell", fake_shell)
        await adb.show_url("u:5555", "http://nas:8098/backup")

    async def test_a_control_channel_failure_is_swallowed(self, monkeypatch):
        from app.ingest import adb

        async def fake_shell(address, command, **kwargs):
            raise adb.AdbError("device offline")

        monkeypatch.setattr(adb, "shell", fake_shell)
        await adb.show_url("u:5555", "http://nas:8098/backup")


class TestTheAppsOwnAddress:
    """Learned from the browser, because a bridged container cannot know it.

    The addresses on this app's own interfaces are the container's; the one that reaches it
    is the host's LAN address and the *published* port, and neither exists inside the
    container. Whatever address the dashboard was opened on is, by definition, one that
    works on this network.
    """

    def setup_method(self):
        from app.ingest import origin

        origin.reset_for_tests()

    async def test_the_dashboard_address_becomes_the_cars_address(self):
        from app.ingest import origin

        await origin.remember("http", "192.168.1.16:8199")

        assert origin.backup_url() == "http://192.168.1.16:8199/backup"

    async def test_it_is_a_url_the_control_channel_will_accept(self):
        """Whatever is learned still has to survive the allowlist before it is used."""
        from app.ingest import origin
        from app.ingest.adb import is_safe_url

        await origin.remember("http", "192.168.1.16:8199")

        assert is_safe_url(origin.backup_url())

    @pytest.mark.parametrize(
        "host",
        ["localhost:8199", "127.0.0.1:8199", "[::1]:8199", "0.0.0.0:8199", "", "   "],
    )
    async def test_an_address_only_this_machine_can_use_is_refused(self, host):
        """The head unit resolving "localhost" would reach itself, not this app."""
        from app.ingest import origin

        await origin.remember("http", host)

        assert origin.backup_url() == ""

    async def test_moving_the_app_is_picked_up_on_the_next_page_load(self):
        from app.ingest import origin

        await origin.remember("http", "192.168.1.16:8199")
        await origin.remember("http", "192.168.1.20:9000")

        assert origin.backup_url() == "http://192.168.1.20:9000/backup"

    async def test_nothing_is_known_before_anybody_opens_the_dashboard(self):
        from app.ingest import origin

        assert origin.backup_url() == ""


class TestLearningTheAddressFromTheDashboard:
    """The hook sits on the route that serves the dashboard, and deliberately nowhere else."""

    @pytest.fixture(autouse=True)
    def _forget(self):
        from app.ingest import origin

        origin.reset_for_tests()
        yield
        origin.reset_for_tests()

    async def test_an_api_call_never_teaches_it_an_address(self, client):
        """Home Assistant polls this app under whatever name *it* was configured with.

        Usually a container name or a Docker-internal host that nothing in a car could
        resolve, and inheriting it would send the head unit somewhere unreachable while
        looking entirely correct.
        """
        from app.ingest import origin

        await client.get("/api/ingest/status")

        assert origin.backup_url() == ""

    async def test_opening_the_dashboard_teaches_it(self, client):
        from app.ingest import origin
        from app.main import FRONTEND_DIST

        if not FRONTEND_DIST.is_dir():
            pytest.skip("the SPA is not built in this tree")

        await client.get("/backup")

        assert origin.backup_url() == "http://test/backup"

    async def test_it_survives_a_restart(self, client):
        """The reason this feature never once fired on the deployment it was written for.

        The dashboard is opened when somebody wants to look at footage; the car arrives
        when somebody comes home. There is no reason the first has happened since the last
        restart, and on the real unit it had not — two windows of sixty-two files each ran
        with an empty address and nothing on the screen, and the app only learned where it
        lived ninety-three seconds after the second one had started moving.
        """
        from app.ingest import origin
        from app.main import FRONTEND_DIST

        if not FRONTEND_DIST.is_dir():
            pytest.skip("the SPA is not built in this tree")

        await client.get("/backup")
        # What a restart actually does: process memory goes, the database stays.
        origin.reset_for_tests()

        assert origin.backup_url() == "http://test/backup"

    async def test_a_later_page_load_overwrites_a_stored_address(self, client):
        """Staleness is what memory-only was protecting against, and it still is."""
        from app.ingest import origin
        from app.main import FRONTEND_DIST

        if not FRONTEND_DIST.is_dir():
            pytest.skip("the SPA is not built in this tree")

        await origin.remember("http", "192.168.1.16:8199")
        await origin.remember("http", "192.168.1.20:9000")
        origin.reset_for_tests()

        assert origin.backup_url() == "http://192.168.1.20:9000/backup"

    async def test_the_car_is_sent_the_api_key_when_one_is_set(self, client):
        """The head unit has no other way to present it — no keyboard, and no header on a
        browser's first navigation."""
        from app.auth.service import MIN_API_KEY_LENGTH
        from app.ingest import origin

        key = "iL9nQm3xWvB7tR2kZ4pY6hJ8sD5fG1aC"
        assert len(key) >= MIN_API_KEY_LENGTH
        await client.put("/api/settings", json={"values": {"security.api_key": key}})
        await origin.remember("http", "192.168.1.16:8199")

        assert origin.backup_url() == f"http://192.168.1.16:8199/backup?k={key}"

    async def test_the_url_with_a_key_still_survives_the_control_channel_allowlist(self, client):
        """`show_url` refuses anything that is not a plain web address, key or no key."""
        from app.ingest import origin
        from app.ingest.adb import is_safe_url

        await client.put(
            "/api/settings",
            json={"values": {"security.api_key": "iL9nQm3xWvB7tR2kZ4pY6hJ8sD5fG1aC"}},
        )
        await origin.remember("http", "192.168.1.16:8199")

        assert is_safe_url(origin.backup_url())

    async def test_a_key_too_short_to_be_accepted_is_not_appended(self, client):
        """Appending it would send the car to a login form by way of a URL that looks
        like it should have worked."""
        from app.ingest import origin

        await client.put("/api/settings", json={"values": {"security.api_key": "short"}})
        await origin.remember("http", "192.168.1.16:8199")

        assert origin.backup_url() == "http://192.168.1.16:8199/backup"


class TestTheLiveNumbers:
    """What the Backup page reads while a window is open."""

    def test_the_speed_shown_is_recent_rather_than_the_run_average(self, monkeypatch):
        """The average is dragged down for the whole run by the seconds before any bytes.

        That is exactly when somebody is deciding whether this is working.
        """
        from app.ingest.status import IngestStatus

        clock = {"now": 1000.0}
        monkeypatch.setattr("app.ingest.status.time.monotonic", lambda: clock["now"])

        status = IngestStatus()
        status.try_begin()
        clock["now"] += 10.0  # ten seconds of connecting and listing, no bytes
        for _ in range(4):
            clock["now"] += 1.0
            status.add_bytes(30_000_000)

        assert status.throughput_mbs() < 10.0, "the run average is the misleading one"
        assert 29.0 <= status.speed_recent_mbs() <= 31.0

    def test_an_eta_is_only_offered_while_bytes_are_actually_moving(self, monkeypatch):
        from app.ingest.models import DeltaPlan, Phase
        from app.ingest.status import IngestStatus

        clock = {"now": 0.0}
        monkeypatch.setattr("app.ingest.status.time.monotonic", lambda: clock["now"])

        status = IngestStatus()
        status.try_begin()
        status.plan(DeltaPlan(files=[RemoteFile("a.ts", 400_000_000, 0)]))

        assert status.eta_seconds() is None, "an estimate during the connect is fiction"

        status.set_phase(Phase.TRANSFERRING)
        for _ in range(4):
            clock["now"] += 1.0
            status.add_bytes(20_000_000)

        eta = status.eta_seconds()
        assert eta is not None
        assert 15.0 <= eta <= 17.0, "320 MB left at ~20 MB/s"

    def test_the_current_file_is_cleared_when_it_finishes(self):
        """It used to hold the file that had just *finished*.

        So between files the page named the previous recording, which reads to anyone
        watching as though the transfer had gone backwards.
        """
        from app.ingest.status import IngestStatus

        status = IngestStatus()
        status.try_begin()
        status.file_started("a.ts")
        assert status.snapshot()["current_file"] == "a.ts"

        status.file_done("a.ts")
        assert status.snapshot()["current_file"] is None
        assert status.snapshot()["files_done"] == 1

    def test_the_phase_leaves_the_state_the_sensor_reads_alone(self):
        """`state` is the Home Assistant contract and keeps exactly its old meaning."""
        from app.ingest.models import Phase
        from app.ingest.status import IngestStatus

        status = IngestStatus()
        status.try_begin()
        status.set_phase(Phase.SCANNING)

        snapshot = status.snapshot()
        assert snapshot["state"] == "running", "an established consumer would have broken"
        assert snapshot["phase"] == "scanning"

    def test_recordings_left_alone_are_visible(self):
        """Otherwise "2 of 5 files" looks like three went missing."""
        from app.ingest.models import DeltaPlan
        from app.ingest.status import IngestStatus

        status = IngestStatus()
        status.try_begin()
        status.plan(DeltaPlan(files=[RemoteFile("a.ts", 10, 0)], active_skipped=2))

        assert status.snapshot()["active_skipped"] == 2


class TestTheScannerIgnoresStaging:
    def test_dot_directories_are_not_walked(self, tmp_path):
        """Half-written arrivals live in `<footage>/.ingest_staging` on the same filesystem."""
        from app.ingest.puller import STAGING_DIRNAME
        from app.scanner.discovery import Scanner

        (tmp_path / STAGING_DIRNAME).mkdir()
        (tmp_path / STAGING_DIRNAME / "half.ts").write_bytes(b"x" * 10)
        (tmp_path / "done.ts").write_bytes(b"x" * 10)

        scanner = Scanner(tmp_path)
        scanner._walk_errors = 0
        found = {entry.rel_path for entry in scanner._walk(tmp_path, frozenset({".ts"}), False)}

        assert found == {"done.ts"}, "the scanner indexed a file that was still arriving"


@pytest.mark.parametrize(
    ("state", "expected"),
    [("device", True), ("offline", False), ("unauthorized", False)],
)
def test_unit_online_only_means_device(state, expected):
    from app.ingest.models import UnitInfo, UnitState

    assert UnitInfo(address="x", state=UnitState(state)).online is expected


class TestARunEndToEnd:
    """The whole orchestration against a fake unit: delta, transfer, commit, history.

    ADB is mocked because there is no car in CI, but the socket, the tar stream, the
    staging directory and the database are all real.
    """

    @pytest.fixture
    def unit(self, monkeypatch, app_config):
        """A head unit holding two recordings, serving them over a loopback socket."""
        from app.ingest import adb, puller
        from app.ingest.models import UnitInfo, UnitState
        from app.ingest.status import reset_status_for_tests

        reset_status_for_tests()
        payload = {
            "20260812120000_camera_0.ts": b"a" * 4096,
            "20260812120100_camera_0.ts": b"b" * 4096,
        }
        served: dict[str, object] = {"port": None, "names": None, "truncate": None}

        async def describe(address, override=""):
            return UnitInfo(
                address="127.0.0.1:5555",
                state=UnitState.DEVICE,
                source="/storage/Tfcard/DCIM/Video",
            )

        async def inventory(address, source):
            return [RemoteFile(name, len(body), 0) for name, body in payload.items()]

        async def launch_listener(address, source, names, *, port, timeout_s):
            served["names"] = list(names)
            blob = _tar_bytes({name: payload[name] for name in names})
            served["port"] = _serve(blob, truncate_at=served["truncate"])

        deleted: list[str] = []

        async def delete(address, source, names):
            deleted.extend(names)
            return len(names)

        monkeypatch.setattr(adb, "describe", describe)
        monkeypatch.setattr(adb, "inventory", inventory)
        monkeypatch.setattr(adb, "launch_listener", launch_listener)
        monkeypatch.setattr(adb, "delete", delete)

        # The listener chooses its own ephemeral port, so the receiver must be told it.
        real_receive = transport.receive

        def receive(host, port, staging, **kwargs):
            return real_receive(host, served["port"], staging, **kwargs)

        monkeypatch.setattr(puller.transport, "receive", receive)
        return SimpleNamespace(payload=payload, served=served, deleted=deleted)

    async def _enable(self, **overrides):
        from app.core.settings_service import get_settings_service

        values = {
            "ingest.enabled": True,
            "ingest.unit_adb_address": "127.0.0.1:5555",
            # The test footage directory is an ordinary tmpdir, not a mount, which is the
            # supported "footage on a local disk" configuration.
            "storage.require_mountpoint": False,
        }
        values.update(overrides)
        await get_settings_service().set_many(values)

    async def test_a_successful_window_lands_the_footage_and_is_recorded(
        self, db_session, unit, app_config
    ):
        from sqlalchemy import select

        from app.db.models import IngestRun
        from app.db.session import session_scope
        from app.ingest.models import RunState
        from app.ingest.puller import STAGING_DIRNAME, run_pull

        await self._enable()
        result = await run_pull(trigger="manual")

        assert result.state is RunState.OK, result.error
        assert result.files == 2
        footage = app_config.footage_dir
        for name, body in unit.payload.items():
            assert (footage / name).read_bytes() == body
        # Staging is left clean, and it is a dot-directory the scanner skips anyway.
        assert list((footage / STAGING_DIRNAME).iterdir()) == []
        assert unit.deleted == [], "nothing may be removed from the card by default"

        async with session_scope() as session:
            runs = list((await session.execute(select(IngestRun))).scalars())
        assert len(runs) == 1
        assert runs[0].state == "ok"
        assert runs[0].files_transferred == 2

    async def test_a_second_run_finds_nothing_to_do(self, db_session, unit, app_config):
        from app.ingest.models import RunState
        from app.ingest.puller import run_pull

        await self._enable()
        assert (await run_pull(trigger="manual")).state is RunState.OK

        again = await run_pull(trigger="manual")

        assert again.state is RunState.IDLE
        assert again.files == 0
        assert unit.served["names"] is not None

    async def test_a_window_that_closes_early_commits_only_whole_files(
        self, db_session, unit, app_config
    ):
        """The car pulled away mid-transfer. Whatever arrived whole is kept."""
        from app.ingest.models import RunState
        from app.ingest.puller import STAGING_DIRNAME, run_pull

        blob = _tar_bytes(unit.payload)
        unit.served["truncate"] = len(blob) // 2
        await self._enable()

        result = await run_pull(trigger="manual")

        assert result.state is RunState.PARTIAL
        footage = app_config.footage_dir
        landed = sorted(p.name for p in footage.glob("*.ts"))
        assert 0 < len(landed) < 2
        for name in landed:
            assert (footage / name).read_bytes() == unit.payload[name], "a partial file landed"
        assert list((footage / STAGING_DIRNAME).iterdir()) == [], "staging was left dirty"

    async def test_an_unmounted_share_stops_the_run_before_anything_moves(
        self, db_session, unit, app_config
    ):
        """The one path in the app that can destroy footage with no dry run.

        If the share is not mounted, the mount point inside the container is an ordinary
        empty directory. Writing a card there would report success, and with
        delete-after-verify on would then erase the originals -- leaving no copy anywhere
        once the real share mounts back over the top.
        """
        from app.ingest.models import RunState
        from app.ingest.puller import run_pull

        # `storage.require_mountpoint` is the operator saying "footage lives on a mount".
        await self._enable(
            **{"storage.require_mountpoint": True, "ingest.delete_after_verify": True}
        )

        result = await run_pull(trigger="manual")

        assert result.state is RunState.ERROR
        assert "mount" in (result.error or "").lower()
        assert unit.served["names"] is None, "a listener was started against an unsafe share"
        assert unit.deleted == [], "the card was cleared against an unmounted share"
        assert list(app_config.footage_dir.glob("*.ts")) == []

    async def test_a_window_with_nothing_new_notifies_nobody(
        self, db_session, unit, app_config, monkeypatch
    ):
        """The car arriving with nothing new is not an event.

        Everything that is not OK reports as an error, so without this an ordinary "up to
        date" window would put an error notification on a phone every time the engine
        started.
        """
        from app.ingest import puller
        from app.ingest.models import RunState

        events: list[str] = []

        async def record(event, **kwargs):
            events.append(event)

        monkeypatch.setattr(puller, "report_event", record)
        await self._enable()

        first = await puller.run_pull(trigger="manual")
        await _settle()
        assert first.state is RunState.OK
        assert sorted(events) == ["finished", "started"]

        events.clear()
        second = await puller.run_pull(trigger="manual")
        await _settle()

        assert second.state is RunState.IDLE
        assert events == [], f"an idle window announced {events}"

    async def test_a_slow_webhook_does_not_delay_the_first_byte(
        self, db_session, unit, app_config, monkeypatch
    ):
        """A ten-second httpx timeout used to sit between a decided transfer and its start.

        The one failure this has in practice -- an unreachable webhook host -- is precisely
        the one that takes the full ten seconds, and at 34 MB/s that is 340 MB of footage
        left on the card so a notification could go out marginally sooner.
        """
        from app.ingest import adb, puller
        from app.ingest.models import RunState

        order: list[str] = []
        launched = adb.launch_listener

        async def slow_report(event, **kwargs):
            if event != "started":
                return
            await asyncio.sleep(0.3)
            order.append("webhook")

        async def launch(address, source, names, *, port, timeout_s):
            order.append("listener")
            return await launched(address, source, names, port=port, timeout_s=timeout_s)

        monkeypatch.setattr(puller, "report_event", slow_report)
        monkeypatch.setattr(adb, "launch_listener", launch)
        await self._enable()

        result = await puller.run_pull(trigger="manual")
        await _settle()

        assert result.state is RunState.OK
        assert order == ["listener", "webhook"], "the transfer waited for the notification"

    async def test_the_local_checks_overlap_the_card_listing(
        self, db_session, unit, app_config, monkeypatch
    ):
        """Not one of them needs the card, and they used to wait for it anyway.

        Two database reads and a walk of the staging directory on what is a hard NFS mount
        in the deployment, run in series between the listing and the first byte purely
        because of the order they were written in.
        """
        from app.ingest import adb, puller
        from app.ingest.models import RunState

        order: list[str] = []
        listed = adb.inventory
        looked_up = puller._deliberately_removed

        async def inventory(address, source):
            order.append("card:start")
            await asyncio.sleep(0.05)
            order.append("card:end")
            return await listed(address, source)

        async def removed():
            order.append("database")
            return await looked_up()

        monkeypatch.setattr(adb, "inventory", inventory)
        monkeypatch.setattr(puller, "_deliberately_removed", removed)
        await self._enable()

        assert (await puller.run_pull(trigger="manual")).state is RunState.OK
        assert order.index("database") < order.index("card:end"), (
            "the local checks waited for the head unit to answer"
        )

    async def test_an_idle_window_collects_the_checks_it_never_needed(
        self, db_session, unit, app_config
    ):
        """Nothing to copy, so the safety check and the staging clean are never awaited.

        They still have to be collected, or a short run leaves tasks complaining about
        results nobody retrieved.
        """
        from app.ingest import puller
        from app.ingest.models import RunState

        await self._enable()
        assert (await puller.run_pull(trigger="manual")).state is RunState.OK

        second = await puller.run_pull(trigger="manual")

        assert second.state is RunState.IDLE
        assert not [task for task in puller._side_tasks if not task.done()]

    async def test_the_unit_is_not_described_twice_when_the_poll_already_did(
        self, db_session, unit, app_config, monkeypatch
    ):
        """The poll describes the unit one line before it starts the run.

        Re-deriving it inside the run meant a second `disconnect`/`connect` against a link
        that had only just been proven good, at the most expensive moment of the day.
        """
        from app.ingest import adb, puller
        from app.ingest.models import RunState, UnitInfo, UnitState

        calls = 0

        async def counting(address, override=""):
            nonlocal calls
            calls += 1
            return UnitInfo(address=address, state=UnitState.DEVICE, source="/src")

        monkeypatch.setattr(adb, "describe", counting)
        await self._enable()

        known = UnitInfo(
            address="127.0.0.1:5555",
            state=UnitState.DEVICE,
            source="/storage/Tfcard/DCIM/Video",
        )
        result = await puller.run_pull(trigger="manual", info=known)

        assert result.state is RunState.OK
        assert calls == 0, "the run re-described a unit the caller had already described"

    async def test_the_car_is_sent_to_the_address_the_dashboard_was_opened_on(
        self, db_session, unit, app_config, monkeypatch
    ):
        """The one address a bridged container cannot discover about itself.

        Also: coming home with nothing new must not hijack the head unit's display.
        """
        from app.ingest import adb, origin, puller
        from app.ingest.models import RunState

        shown: list[str] = []

        async def record(address, url):
            shown.append(url)

        monkeypatch.setattr(adb, "show_url", record)
        origin.reset_for_tests()
        await origin.remember("http", "192.168.1.16:8199")
        await self._enable(**{"ingest.show_on_unit": True})

        assert (await puller.run_pull(trigger="manual")).state is RunState.OK
        await _settle()
        assert shown == ["http://192.168.1.16:8199/backup"]

        shown.clear()
        assert (await puller.run_pull(trigger="manual")).state is RunState.IDLE
        await _settle()
        assert shown == [], "an idle window took the car's screen over for nothing"

    async def test_an_explicit_address_wins_over_the_learned_one(
        self, db_session, unit, app_config, monkeypatch
    ):
        """For a reverse proxy whose hostname the head unit cannot resolve."""
        from app.ingest import adb, origin, puller
        from app.ingest.models import RunState

        shown: list[str] = []

        async def record(address, url):
            shown.append(url)

        monkeypatch.setattr(adb, "show_url", record)
        origin.reset_for_tests()
        await origin.remember("https", "dashcam.example.com")
        await self._enable(
            **{
                "ingest.show_on_unit": True,
                "ingest.unit_display_url": "http://192.168.1.16:8199/backup",
            }
        )

        assert (await puller.run_pull(trigger="manual")).state is RunState.OK
        await _settle()
        assert shown == ["http://192.168.1.16:8199/backup"]

    async def test_a_transfer_still_runs_when_the_address_is_not_known_yet(
        self, db_session, unit, app_config, monkeypatch
    ):
        """Nobody has opened the dashboard since this process started. Copy anyway."""
        from app.ingest import adb, origin, puller
        from app.ingest.models import RunState

        async def fail(address, url):
            raise AssertionError("the car was sent to an address nobody knows")

        monkeypatch.setattr(adb, "show_url", fail)
        origin.reset_for_tests()
        await self._enable(**{"ingest.show_on_unit": True})

        assert (await puller.run_pull(trigger="manual")).state is RunState.OK
        await _settle()

    async def test_the_car_screen_is_left_alone_unless_asked_for(
        self, db_session, unit, app_config, monkeypatch
    ):
        from app.ingest import adb, origin, puller
        from app.ingest.models import RunState

        async def fail(address, url):
            raise AssertionError("the head unit's screen was taken over without being asked")

        monkeypatch.setattr(adb, "show_url", fail)
        origin.reset_for_tests()
        await origin.remember("http", "192.168.1.16:8199")
        await self._enable()

        assert (await puller.run_pull(trigger="manual")).state is RunState.OK
        await _settle()

    async def test_disabled_does_nothing_at_all(self, db_session, unit):
        from app.ingest.models import RunState
        from app.ingest.puller import run_pull

        result = await run_pull(trigger="manual")

        assert result.state is RunState.DISABLED
        assert unit.served["names"] is None, "the head unit was contacted while disabled"

    async def test_deleting_from_the_card_only_covers_committed_files(
        self, db_session, unit, app_config
    ):
        from app.ingest.models import RunState
        from app.ingest.puller import run_pull

        await self._enable(**{"ingest.delete_after_verify": True})
        result = await run_pull(trigger="manual")

        assert result.state is RunState.OK
        assert sorted(unit.deleted) == sorted(unit.payload)


class TestTheShellIsNotCachedIntoABlankScreen:
    """`index.html` is the only unhashed file in the build, and that makes it dangerous.

    Nothing here used to send `Cache-Control` at all, and "no header" does not mean "do not
    cache" — browsers fall back to a heuristic fraction of the file's age. So a shell that
    had been deployed a while was served from cache without revalidating, and since every
    page is a content-hashed lazy chunk, it asked for filenames the new build no longer
    had. The 404 unmounted React to a white screen, and a manual refresh "fixed" it only
    because refreshing forces the revalidation that should never have been skipped.
    """

    @pytest.fixture(autouse=True)
    def _needs_spa(self):
        from app.main import FRONTEND_DIST

        if not FRONTEND_DIST.is_dir():
            pytest.skip("the SPA is not built in this tree")

    async def test_the_shell_must_be_revalidated(self, client):
        response = await client.get("/backup")

        assert response.headers["cache-control"] == "no-cache"

    async def test_the_root_is_the_same(self, client):
        assert (await client.get("/")).headers["cache-control"] == "no-cache"

    async def test_hashed_assets_are_cached_hard(self, client):
        """Otherwise revalidating the shell every navigation refetches the whole app."""
        import re

        shell = (await client.get("/")).text
        match = re.search(r"/assets/(index-[A-Za-z0-9_-]+\.js)", shell)
        assert match, "no hashed entry chunk in the shell"

        response = await client.get(f"/assets/{match.group(1)}")

        assert response.status_code == 200
        assert response.headers["cache-control"] == "public, max-age=31536000, immutable"

    async def test_revalidating_costs_about_a_kilobyte(self, client):
        """`no-cache` is not `no-store`, and what it costs has to stay negligible.

        Only a full page load fetches the shell at all — moving between pages is
        client-side routing — so this is the whole price of never seeing a white screen.
        """
        response = await client.get("/")

        assert response.status_code == 200
        assert len(response.content) < 8_000, "the shell has grown enough to be worth caching"


class TestTheCarScreenTestButton:
    """Firing the head unit's screen by hand, because the real thing is unobservable.

    It only fires when a transfer has files to copy, and a card with nothing new on it is
    the steady state — so confirming it works otherwise means waiting for the car to arrive
    carrying footage and catching a sixty-second window.
    """

    async def _enable(self, **values):
        from app.core.settings_service import get_settings_service

        await get_settings_service().set_many({"ingest.enabled": True, **values})

    async def test_it_reports_success_and_hides_the_key(self, db_session, monkeypatch, client):
        from app.ingest import adb, origin

        shown: list[str] = []

        async def record(address, url):
            shown.append(url)
            return ""

        monkeypatch.setattr(adb, "show_url", record)
        monkeypatch.setattr(adb, "is_listening", lambda address: _true())
        origin.reset_for_tests()
        await self._enable(**{"ingest.unit_adb_address": "10.0.0.5:5555"})
        await client.put(
            "/api/settings",
            json={"values": {"security.api_key": "iL9nQm3xWvB7tR2kZ4pY6hJ8sD5fG1aC"}},
        )
        await origin.remember("http", "192.168.1.16:8199")

        response = await client.post("/api/ingest/show-test")

        assert response.status_code == 200
        # The unit gets the real key...
        assert shown == ["http://192.168.1.16:8199/backup?k=iL9nQm3xWvB7tR2kZ4pY6hJ8sD5fG1aC"]
        # ...and the screen the operator is looking at does not.
        assert response.json()["url"] == "http://192.168.1.16:8199/backup?k=<key>"

    async def test_it_says_so_when_the_address_is_not_known(self, db_session, monkeypatch, client):
        from app.ingest import origin

        origin.reset_for_tests()
        await self._enable()
        await client.put("/api/settings", json={"values": {"ingest.learned_origin": ""}})

        response = await client.post("/api/ingest/show-test")

        assert response.status_code == 409
        assert "does not know its own address" in response.json()["detail"]

    async def test_it_says_so_when_the_car_is_not_here(self, db_session, monkeypatch, client):
        from app.ingest import adb, origin

        monkeypatch.setattr(adb, "is_listening", lambda address: _false())
        origin.reset_for_tests()
        await self._enable(**{"ingest.unit_adb_address": "10.0.0.5:5555"})
        await origin.remember("http", "192.168.1.16:8199")

        response = await client.post("/api/ingest/show-test")

        assert response.status_code == 409
        assert "Nothing is answering" in response.json()["detail"]

    async def test_a_unit_with_no_browser_is_reported_rather_than_swallowed(
        self, db_session, monkeypatch, client
    ):
        """The transfer path deliberately ignores this. The test button must not."""
        from app.ingest import adb, origin

        async def refuse(address, url):
            return "The head unit refused to open it: Error: Activity not started"

        monkeypatch.setattr(adb, "show_url", refuse)
        monkeypatch.setattr(adb, "is_listening", lambda address: _true())
        origin.reset_for_tests()
        await self._enable(**{"ingest.unit_adb_address": "10.0.0.5:5555"})
        await origin.remember("http", "192.168.1.16:8199")

        response = await client.post("/api/ingest/show-test")

        assert response.status_code == 502
        assert "Activity not started" in response.json()["detail"]


async def _true() -> bool:
    return True


async def _false() -> bool:
    return False
