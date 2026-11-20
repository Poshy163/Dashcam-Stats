"""Closed tar members can be filed while the receiver is still reading later members."""

import asyncio
import io
import socket
import tarfile
import threading
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from app.ingest import adb, puller, transport
from app.ingest.models import RemoteFile, RunResult, RunState, UnitInfo
from app.ingest.status import IngestStatus


@pytest.fixture
def start_move(monkeypatch, tmp_path):
    status = IngestStatus()
    monkeypatch.setattr(puller, "get_status", lambda: status)
    monkeypatch.setattr(puller, "_get", lambda key, default: default)
    monkeypatch.setattr(adb, "clear_listener", AsyncMock())
    monkeypatch.setattr(adb, "launch_listener", AsyncMock())
    monkeypatch.setattr(adb, "stop_listener", AsyncMock(return_value=True))

    async def start(files, submit):
        return await puller._move(
            UnitInfo("unit:5555", source="/card"),
            files,
            staging=tmp_path / "staging",
            host="unit",
            port=9000,
            timeout_s=10,
            chunk_size=12,
            on_chunk_completed=submit,
        )

    return start


async def test_filing_runs_inside_one_live_tar_stream(start_move, tmp_path, monkeypatch):
    files = [RemoteFile(f"clip{i}.ts", 32_768, 0) for i in range(9)]
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w") as archive:
        for item in files:
            entry = tarfile.TarInfo(item.name)
            entry.size = item.size
            archive.addfile(entry, io.BytesIO(b"x" * item.size))
    reader, writer = socket.socketpair()
    monkeypatch.setattr(transport, "_connect", lambda *args: reader)
    groups = []
    first_group_filed = threading.Event()
    later_member_started = asyncio.Event()
    loop = asyncio.get_running_loop()
    status = puller.get_status()
    file_started = status.file_started

    def progress(name):
        file_started(name)
        if name == files[5].name:
            loop.call_soon_threadsafe(later_member_started.set)

    monkeypatch.setattr(status, "file_started", progress)

    def send():
        with writer:
            writer.settimeout(5)
            # Include part of member five for the streaming tar reader's read-ahead.
            cut = 4 * (512 + files[0].size) + 16_384
            writer.sendall(data.getvalue()[:cut])
            assert first_group_filed.wait(5)
            writer.sendall(data.getvalue()[cut:])

    async def file_group(group):
        wanted = {item.name: item.size for item in group}
        assert set(puller.commit(tmp_path / "staging", tmp_path, wanted)) == set(wanted)
        groups.append(list(wanted))
        if len(groups) == 1:
            first_group_filed.set()
            await asyncio.wait_for(later_member_started.wait(), timeout=5)

    pipeline = puller._CommitPipeline(file_group, depth=1)
    pipeline.start()
    try:
        _, result = await asyncio.gather(
            asyncio.to_thread(send), start_move(files, pipeline.submit)
        )
    finally:
        await pipeline.close()
    assert result.complete
    adb.launch_listener.assert_awaited_once()
    assert groups == [[item.name for item in files[i : i + 4]] for i in (0, 4, 8)]


async def test_retry_submits_only_completed_members_once(start_move, monkeypatch):
    files = [RemoteFile(f"clip{i}.ts", 100, 0) for i in range(8)]
    calls = []
    submitted = []
    monkeypatch.setattr(puller, "_can_resume_stream", AsyncMock(return_value=True))

    def receive(*args, expected, on_file_done, **kwargs):
        names = list(expected)
        calls.append(names)
        # Each attempt ends while reading its last incomplete member. One can retry.
        closed = names[:5] if len(calls) == 1 else names[:2]
        for name in closed:
            on_file_done(name)
        return transport.TransferResult(files=closed, error="disconnected", retryable=True)

    async def submit(group):
        submitted.append([item.name for item in group])

    monkeypatch.setattr(transport, "receive", receive)
    result = await start_move(files, submit)
    names = [item.name for item in files]
    assert calls == [names, names[5:]]
    assert submitted == [names[:4], names[4:7]]
    assert not result.complete and result.files == names[:7]


async def test_slow_filing_backpressures_the_active_receiver(start_move, monkeypatch):
    files = [RemoteFile(f"clip{i}.ts", 100, 0) for i in range(12)]
    release_first = asyncio.Event()
    submitting_last = asyncio.Event()
    receiver_finished = threading.Event()

    async def file_group(group):
        if group[0] == files[0]:
            await release_first.wait()

    pipeline = puller._CommitPipeline(file_group, depth=1)

    async def submit(group):
        if group[-1] == files[-1]:
            submitting_last.set()
        await pipeline.submit(group)

    def receive(*args, expected, on_file_done, **kwargs):
        for name in expected:
            on_file_done(name)
        receiver_finished.set()
        return transport.TransferResult(files=list(expected), complete=True)

    monkeypatch.setattr(transport, "receive", receive)
    pipeline.start()
    task = asyncio.create_task(start_move(files, submit))
    try:
        await asyncio.wait_for(submitting_last.wait(), timeout=5)
        assert not receiver_finished.is_set()
    finally:
        release_first.set()
        result = await task
        await pipeline.close()
    assert result.complete and receiver_finished.is_set()


async def test_cancel_joins_reader_before_releasing_staging(start_move, monkeypatch):
    entered = asyncio.Event()
    stopping = asyncio.Event()
    release = threading.Event()
    exited = threading.Event()
    loop = asyncio.get_running_loop()

    def receive(*args, cancel, **kwargs):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5)
        assert cancel.is_set()
        exited.set()
        return transport.TransferResult(error="cancelled")

    async def stop(listener):
        stopping.set()
        return True

    monkeypatch.setattr(transport, "receive", receive)
    monkeypatch.setattr(adb, "stop_listener", stop)
    submit = AsyncMock()
    task = asyncio.create_task(start_move([RemoteFile("clip.ts", 100, 0)], submit))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        task.cancel()
        await asyncio.wait_for(stopping.wait(), timeout=5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not exited.is_set()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert exited.is_set()
    submit.assert_not_awaited()


async def test_filing_error_propagates_without_transport_retry(start_move, monkeypatch):
    files = [RemoteFile(f"clip{i}.ts", 100, 0) for i in range(5)]

    def receive(*args, expected, on_file_done, **kwargs):
        for name in expected:
            on_file_done(name)
        pytest.fail("receiver should stop when the filing queue refuses a completed group")

    submit = AsyncMock(side_effect=OSError("archive unavailable"))
    monkeypatch.setattr(transport, "receive", receive)
    with pytest.raises(OSError, match="archive unavailable"):
        await start_move(files, submit)
    submit.assert_awaited_once_with(files[:4])
    adb.launch_listener.assert_awaited_once()
    adb.stop_listener.assert_awaited_once()


@pytest.mark.parametrize("lost_counters", [False, True])
async def test_history_counts_all_measured_footage_attempts(client, monkeypatch, lost_counters):
    status = IngestStatus()
    monkeypatch.setattr(adb, "stop_listener", AsyncMock(return_value=True))
    lifecycle = puller._RunLifecycle(started_at=datetime.now(UTC))
    token = puller._run_lifecycle.set(lifecycle)

    def interrupted():
        if lost_counters:
            raise OSError("staging failed")
        return transport.TransferResult(bytes_received=1_000_000, seconds=2, retryable=True)

    try:
        if lost_counters:
            with pytest.raises(OSError, match="staging failed"):
                await puller._receive_footage(interrupted, None, status)
        else:
            await puller._receive_footage(interrupted, None, status)
        await puller._receive_footage(
            lambda: transport.TransferResult(bytes_received=5_000_000, seconds=1, complete=True),
            None,
            status,
        )
        await puller._persist(RunResult(state=RunState.PARTIAL, bytes=5_000_000), "manual")
    finally:
        puller._run_lifecycle.reset(token)

    history = (await client.get("/api/ingest/history")).json()["items"]
    assert len(history) == 1
    assert history[0]["footage_receive_mbs"] == (None if lost_counters else 2.0)
