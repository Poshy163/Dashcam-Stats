"""Connection fault injection: preserve bytes and keep recovery bounded and parked."""

from __future__ import annotations

import errno
import io
import socket
import tarfile
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.ingest import adb, puller, transport
from app.ingest.models import DeltaPlan, RemoteFile, UnitInfo
from app.ingest.status import IngestStatus


@pytest.fixture
def status(monkeypatch):
    value = IngestStatus()
    monkeypatch.setattr(puller, "get_status", lambda: value)
    monkeypatch.setattr(adb, "_optional_until", {})
    return value


def _archive(payload):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for name, body in payload.items():
            info = tarfile.TarInfo(name)
            info.size = len(body)
            archive.addfile(info, io.BytesIO(body))
    return output.getvalue()


@pytest.mark.parametrize("failure", ["eof", "timeout"])
async def test_real_socket_interruption_resumes_only_unfinished_files(
    monkeypatch, tmp_path, status, failure
):
    payload = {"a.ts": b"a" * 16384, "b.ts": b"b" * 65536}
    files = [RemoteFile(name, len(body), 0) for name, body in payload.items()]
    status.plan(DeltaPlan(files=files))
    requests = []
    order = []
    servers = []
    release = threading.Event()
    port = 0

    async def launch(_address, _directory, names, **_kwargs):
        nonlocal port
        requests.append(names)
        if len(requests) == 2:
            assert status.bytes_total - status.bytes_done == len(payload["b.ts"])
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        server.settimeout(3)
        port = server.getsockname()[1]
        blob = _archive({name: payload[name] for name in names})
        interrupted = len(requests) == 1
        if interrupted:
            blob = blob[: 512 + len(payload["a.ts"]) + 512 + 16384]

        def serve():
            with server:
                connection, _ = server.accept()
                with connection:
                    connection.sendall(blob)
                    if interrupted and failure == "timeout":
                        release.wait(3)

        worker = threading.Thread(target=serve, daemon=True)
        worker.start()
        servers.append(worker)
        return object()

    async def stop(_listener):
        order.append("listener-stopped")
        release.set()
        return True

    async def probe(_address):
        assert order[-1] == "listener-stopped"
        return True

    receive = transport.receive

    def receive_actual(_host, _port, staging, **kwargs):
        return receive("127.0.0.1", port, staging, **kwargs)

    async def committed(chunk):
        order.append("commit")
        assert puller.commit(
            tmp_path / "staging", tmp_path, {item.name: item.size for item in chunk}
        ) == ["a.ts", "b.ts"]

    monkeypatch.setattr(adb, "clear_listener", AsyncMock())
    monkeypatch.setattr(adb, "launch_listener", launch)
    monkeypatch.setattr(adb, "stop_listener", stop)
    monkeypatch.setattr(adb, "is_listening", AsyncMock(return_value=True))
    monkeypatch.setattr(adb, "probe_shell", probe)
    monkeypatch.setattr(adb, "ignition_state", AsyncMock(return_value="off"))
    reconnect = AsyncMock(side_effect=AssertionError("must not disconnect a live ingest"))
    monkeypatch.setattr(adb, "reconnect", reconnect)
    monkeypatch.setattr(transport, "receive", receive_actual)
    monkeypatch.setattr(transport, "SOCKET_TIMEOUT_S", 0.1)
    try:
        result = await puller._move(
            UnitInfo("unit:5555", source="/card"),
            files,
            staging=tmp_path / "staging",
            host="127.0.0.1",
            port=1,
            timeout_s=60,
            on_chunk_completed=committed,
        )
    finally:
        release.set()
        for worker in servers:
            worker.join(3)
    assert result.complete and result.error is None
    assert result.files == ["a.ts", "b.ts"]
    assert requests == [["a.ts", "b.ts"], ["b.ts"]]
    assert status.files_done == 2
    assert {name: (tmp_path / name).read_bytes() for name in payload} == payload
    reconnect.assert_not_called()


@pytest.mark.parametrize(
    ("reachable", "responsive", "ignition", "remaining", "cancelled", "allowed"),
    [
        (False, False, "unknown", None, False, False),
        (True, False, "unknown", None, False, False),
        (True, True, "on", None, False, False),
        (True, True, "unknown", None, False, False),
        (True, True, "off", 30, False, False),
        (True, True, "off", 300, True, False),
        (True, True, "off", 300, False, True),
    ],
)
async def test_retry_requires_a_responsive_parked_unit_and_time_left(
    monkeypatch, status, reachable, responsive, ignition, remaining, cancelled, allowed
):
    monkeypatch.setattr(status, "sleep_countdown_remaining_s", lambda: remaining)
    monkeypatch.setattr(adb, "is_listening", AsyncMock(return_value=reachable))
    monkeypatch.setattr(adb, "probe_shell", AsyncMock(return_value=responsive))
    monkeypatch.setattr(adb, "ignition_state", AsyncMock(return_value=ignition))
    if cancelled:
        status.cancel_event.set()
    assert await puller._can_resume_stream("unit:5555") is allowed
    if cancelled or remaining == 30:
        adb.is_listening.assert_not_called()


@pytest.mark.parametrize("retryable,expected_attempts", [(True, 2), (False, 1)])
async def test_failed_recovery_is_bounded_and_unsafe_streams_are_not_retried(
    monkeypatch, tmp_path, status, retryable, expected_attempts
):
    def receive(*_args, **_kwargs):
        return transport.TransferResult(error="interrupted", retryable=retryable)

    launch = AsyncMock(return_value=None)
    monkeypatch.setattr(adb, "clear_listener", AsyncMock())
    monkeypatch.setattr(adb, "launch_listener", launch)
    monkeypatch.setattr(adb, "stop_listener", AsyncMock(return_value=True))
    monkeypatch.setattr(transport, "receive", receive)
    monkeypatch.setattr(puller, "_can_resume_stream", AsyncMock(return_value=True))
    result = await puller._move(
        UnitInfo("unit:5555", source="/card"),
        [RemoteFile("a.ts", 100, 0)],
        staging=tmp_path / "staging",
        host="unit",
        port=9000,
        timeout_s=60,
    )
    assert not result.complete
    assert launch.await_count == expected_attempts


async def test_lost_lease_prevents_recovery(monkeypatch, tmp_path, status):
    lost = False

    def check():
        if lost:
            raise RuntimeError("lease lost")

    def receive(*_args, **_kwargs):
        nonlocal lost
        lost = True
        return transport.TransferResult(error="timeout", retryable=True)

    probe = AsyncMock(return_value=True)
    monkeypatch.setattr(adb, "clear_listener", AsyncMock())
    monkeypatch.setattr(adb, "launch_listener", AsyncMock(return_value=None))
    monkeypatch.setattr(adb, "stop_listener", AsyncMock(return_value=True))
    monkeypatch.setattr(transport, "receive", receive)
    monkeypatch.setattr(puller, "_can_resume_stream", probe)
    with pytest.raises(RuntimeError, match="lease lost"):
        await puller._move(
            UnitInfo("unit:5555", source="/card"),
            [RemoteFile("a.ts", 100, 0)],
            staging=tmp_path / "staging",
            host="unit",
            port=9000,
            timeout_s=60,
            lease=SimpleNamespace(raise_if_lease_lost=check),
        )
    probe.assert_not_called()


async def test_timeout_backs_off_display_but_does_not_block_safety_shells(monkeypatch, status):
    now = [10.0]
    monkeypatch.setattr(adb.time, "monotonic", lambda: now[0])
    replies = [TimeoutError(), (b"restored", b"")]

    class Process:
        returncode = 0

        async def communicate(self):
            reply = replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply

        def kill(self):
            pass

        async def wait(self):
            return 0

    monkeypatch.setattr(adb, "adb_path", lambda: "adb")
    monkeypatch.setattr(adb.asyncio, "create_subprocess_exec", AsyncMock(return_value=Process()))
    with pytest.raises(adb.AdbError):
        await adb.shell("unit:5555", "dumpsys activity", timeout=1)
    foreground = AsyncMock(side_effect=AssertionError("optional work must yield"))
    monkeypatch.setattr(adb, "chrome_is_foreground", foreground)
    await puller._show_backup_page_during_transfer("unit:5555", "http://server/backup")
    assert adb.optional_requests_deferred("unit")
    assert not adb.optional_requests_deferred("another-unit")
    assert await adb.shell("unit:5555", "echo restored") == "restored"
    now[0] += adb.OPTIONAL_BACKOFF_S + 1
    assert not adb.optional_requests_deferred("unit")


@pytest.mark.parametrize(
    "error",
    [OSError(errno.ENOSPC, "disk full"), PermissionError("denied"), TimeoutError("NFS stalled")],
)
def test_local_storage_failure_is_not_retryable(monkeypatch, tmp_path, error):
    class Socket:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def makefile(self, *args, **kwargs):
            raise error

    monkeypatch.setattr(transport, "_connect", lambda *args: Socket())
    result = transport.receive("unit", 9000, tmp_path / "staging", expected={"a.ts": 100})
    assert not result.complete and not result.retryable
