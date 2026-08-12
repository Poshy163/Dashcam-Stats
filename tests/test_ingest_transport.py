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

    async def test_the_listener_is_detached_and_time_limited(self, monkeypatch):
        """It has to outlive the shell call that starts it, and never outlive the window."""
        from app.ingest import adb

        captured: list[str] = []

        async def fake_shell(address, command, **kwargs):
            captured.append(command)
            return ""

        monkeypatch.setattr(adb, "shell", fake_shell)
        await adb.launch_listener("unit:5555", "/src", ["a.ts", "b.ts"], port=9000, timeout_s=180)

        command = captured[0]
        assert "setsid" in command, "the listener must survive the shell call returning"
        assert "tar c a.ts b.ts" in command
        assert "nc -l -p 9000" in command
        assert command.rstrip().endswith("&")
        # Two timeouts, and the inner one matters most: the outer only kills the wrapper
        # shell, so `nc` -- the process actually holding the port -- would outlive it.
        assert command.count("timeout 180") == 2
        assert "timeout 180 nc -l -p 9000" in command

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
        assert first.state is RunState.OK
        assert events == ["started", "finished"]

        events.clear()
        second = await puller.run_pull(trigger="manual")

        assert second.state is RunState.IDLE
        assert events == [], f"an idle window announced {events}"

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
