"""Task cancellation retains OBD ownership until its workers and listener settle."""

import asyncio
import threading
from unittest.mock import AsyncMock

import pytest

from app.core.process_lock import try_acquire
from app.ingest import obd_transfer
from app.ingest.models import RemoteFile, UnitInfo, UnitState
from app.ingest.status import IngestStatus
from app.ingest.transport import TransferResult


def setup_transfer(monkeypatch):
    listener = object()
    monkeypatch.setattr(obd_transfer, "read_logger_status", AsyncMock(return_value=None))
    monkeypatch.setattr(obd_transfer, "_already_verified", AsyncMock(return_value=None))
    monkeypatch.setattr(obd_transfer, "_already_rejected", AsyncMock(return_value=None))
    monkeypatch.setattr(obd_transfer.adb, "clear_listener", AsyncMock())
    monkeypatch.setattr(obd_transfer.adb, "launch_listener", AsyncMock(return_value=listener))
    monkeypatch.setattr(obd_transfer.adb, "stop_listener", AsyncMock())
    return listener


def start_transfer(app_config, status=None):
    return asyncio.create_task(
        obd_transfer.sync_remote_bundles(
            UnitInfo("192.0.2.10:5555", UnitState.DEVICE, "/card"),
            ingest_status=status,
            remote=[RemoteFile("drive_lifecycle.obd2.zip", 5, 1, "/safe/ready")],
            config=app_config,
        )
    )


async def cancel_again(task):
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done(), "cancellation must wait for the owned worker"


def assert_lock_released(app_config):
    lock = try_acquire(obd_transfer._sync_lock_path(app_config))
    assert lock is not None
    lock.release()


async def test_cancel_joins_staging_cleanup_before_releasing_fence(app_config, monkeypatch):
    setup_transfer(monkeypatch)
    entered, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()

    def clean(_config):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5)

    monkeypatch.setattr(obd_transfer, "_clean_staging", clean)
    task = start_transfer(app_config)
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await cancel_again(task)
        assert try_acquire(obd_transfer._sync_lock_path(app_config)) is None
        obd_transfer.read_logger_status.assert_not_called()
        obd_transfer.adb.launch_listener.assert_not_called()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
    assert_lock_released(app_config)


@pytest.mark.parametrize("with_status", [False, True])
async def test_cancel_stops_listener_but_waits_for_receiver_before_cleanup(
    app_config, monkeypatch, with_status
):
    listener = setup_transfer(monkeypatch)
    entered, stopped, release = asyncio.Event(), asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    state = {}
    status = IngestStatus() if with_status else None
    if status is not None:
        status.try_begin()

    def receive(_host, _port, staging, *, cancel, **_kwargs):
        state.update(staging=staging, cancel=cancel)
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5)
        assert cancel.is_set()
        # A noncancellable write finishing after Stop still owns this directory.
        (staging / "last-write").write_bytes(b"owned")
        return TransferResult(complete=False, error="cancelled")

    async def stop(proc):
        assert proc is listener
        stopped.set()

    monkeypatch.setattr(obd_transfer.transport, "receive", receive)
    monkeypatch.setattr(obd_transfer.adb, "stop_listener", AsyncMock(side_effect=stop))
    task = start_transfer(app_config, status)
    try:
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        await asyncio.wait_for(stopped.wait(), 5)
        await cancel_again(task)
        assert state["cancel"].is_set()
        assert state["staging"].is_dir()
        assert try_acquire(obd_transfer._sync_lock_path(app_config)) is None
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
    assert not state["staging"].exists()
    obd_transfer.adb.stop_listener.assert_awaited_once_with(listener)
    assert_lock_released(app_config)


async def test_cancel_during_listener_launch_collects_handle_without_starting_receiver(
    app_config, monkeypatch
):
    listener = setup_transfer(monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()

    async def launch(*_args, **_kwargs):
        entered.set()
        await release.wait()
        return listener

    receive = AsyncMock(side_effect=AssertionError("cancelled launch must not start a receiver"))
    monkeypatch.setattr(obd_transfer.adb, "launch_listener", launch)
    monkeypatch.setattr(obd_transfer.transport, "receive", receive)
    task = start_transfer(app_config)
    await asyncio.wait_for(entered.wait(), 5)
    await cancel_again(task)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    receive.assert_not_called()
    obd_transfer.adb.stop_listener.assert_awaited_once_with(listener)
    assert not list(app_config.obd_staging_dir.glob(".transfer-*.partial"))
    assert_lock_released(app_config)


async def test_final_directory_cleanup_keeps_fence_when_caller_is_cancelled(
    app_config, monkeypatch
):
    setup_transfer(monkeypatch)
    entered, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    original = obd_transfer.shutil.rmtree

    def remove(path):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5)
        original(path)

    monkeypatch.setattr(obd_transfer.shutil, "rmtree", remove)
    monkeypatch.setattr(
        obd_transfer.transport, "receive", lambda *_args, **_kwargs: TransferResult(complete=False)
    )
    task = start_transfer(app_config)
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await cancel_again(task)
        assert try_acquire(obd_transfer._sync_lock_path(app_config)) is None
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
    assert not list(app_config.obd_staging_dir.glob(".transfer-*.partial"))
    assert_lock_released(app_config)


async def test_unexpected_receiver_error_still_stops_listener_and_releases_fence(
    app_config, monkeypatch
):
    listener = setup_transfer(monkeypatch)

    def receive(*_args, **_kwargs):
        raise RuntimeError("receiver failed")

    monkeypatch.setattr(obd_transfer.transport, "receive", receive)
    with pytest.raises(RuntimeError, match="receiver failed"):
        await start_transfer(app_config)
    obd_transfer.adb.stop_listener.assert_awaited_once_with(listener)
    assert not list(app_config.obd_staging_dir.glob(".transfer-*.partial"))
    assert_lock_released(app_config)
