"""Preparation deadlines must not abandon staging ownership or gate on telemetry."""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.ingest import puller, transport
from app.ingest.models import RemoteFile, RunState, UnitInfo, UnitState
from app.ingest.obd_events import EventSyncResult
from app.ingest.status import get_status, reset_status_for_tests


@pytest.fixture
def isolated_mirror(monkeypatch):
    monkeypatch.setattr(puller, "_event_mirror_task", None)


@pytest.fixture
def prepared_unit(monkeypatch, tmp_path, isolated_mirror):
    """Real staging/commit pipeline, with all device and database I/O replaced."""
    reset_status_for_tests()
    settings = SimpleNamespace(footage_dir=AsyncMock(return_value=tmp_path))
    monkeypatch.setattr(puller, "get_settings_service", lambda: settings)
    monkeypatch.setattr(
        puller,
        "get_config",
        lambda: SimpleNamespace(
            obd_remote_ready_dir="/ready",
            obd_remote_status_file="/status",
            obd_remote_events_file="/events",
        ),
    )
    options = {
        "enabled": True,
        "only_when_parked": False,
        "include_locked": False,
        "quiet_radios": False,
        "show_on_unit": False,
        "delete_after_verify": False,
        "sweep_passes": 0,
        "rescue_partials": False,
    }
    monkeypatch.setattr(puller, "_get", lambda key, default=None: options.get(key, default))
    monkeypatch.setattr(puller, "_recover_before_run", AsyncMock(return_value=True))
    monkeypatch.setattr(puller, "_deliberately_removed", AsyncMock(return_value=set()))
    monkeypatch.setattr(puller, "_footage_is_safe_to_write", AsyncMock(return_value=(True, None)))
    monkeypatch.setattr(puller.adb, "unit_clock", AsyncMock(return_value=None))
    monkeypatch.setattr(puller, "inventory_remote_bundles", AsyncMock(return_value=[]))
    monkeypatch.setattr(puller, "read_logger_status", AsyncMock(return_value=None))
    monkeypatch.setattr(puller, "sync_remote_events", AsyncMock(return_value=EventSyncResult()))
    monkeypatch.setattr(puller, "_drop_still_growing", AsyncMock())
    monkeypatch.setattr(puller.band, "gate", AsyncMock(return_value=True))
    monkeypatch.setattr(puller.elevate, "ensure_root", AsyncMock(return_value=False))
    monkeypatch.setattr(puller.elevate, "channel_lost", lambda: False)
    monkeypatch.setattr(puller, "report_event", AsyncMock())
    monkeypatch.setattr(puller, "_record_run_completion", AsyncMock())
    monkeypatch.setattr(puller, "_sleep_window_may_close", AsyncMock(return_value=False))
    monkeypatch.setattr(puller, "close_sleep_window", AsyncMock())
    filename = "20260812120000_camera_0.ts"
    payload = b"verified footage" * 20
    inventory = AsyncMock(return_value=[RemoteFile(filename, len(payload), 0)])
    monkeypatch.setattr(puller.adb, "inventory_all", inventory)
    transferred = asyncio.Event()

    async def move(info, files, *, staging, on_chunk_completed, **kwargs):
        staging.mkdir(exist_ok=True)
        (staging / filename).write_bytes(payload)
        await on_chunk_completed(files)
        transferred.set()
        return transport.TransferResult(
            files=[filename], bytes_received=len(payload), seconds=0.1, complete=True
        )

    monkeypatch.setattr(puller, "_move", move)
    return SimpleNamespace(
        options=options,
        info=UnitInfo("unit:5555", UnitState.DEVICE, "/card"),
        settings=settings,
        inventory=inventory,
        path=tmp_path / filename,
        payload=payload,
        transferred=transferred,
    )


@pytest.mark.parametrize("sleep_window", [None, 60])
async def test_unsafe_sleep_budget_holds_without_copying_claiming_or_quiescing(
    prepared_unit, monkeypatch, sleep_window
):
    from app.ingest.adb import RuntimeObservation

    prepared_unit.options["quiet_radios"] = True
    status = get_status()
    if sleep_window is not None:
        status.observe_unit_runtime(RuntimeObservation("boot", 100, "on", sleep_window))
        status.observe_unit_runtime(RuntimeObservation("boot", 101, "off", sleep_window))
    begin = AsyncMock(side_effect=AssertionError("must not claim radios"))
    monkeypatch.setattr(puller.radio_coordinator, "begin", begin)
    monkeypatch.setattr(
        puller,
        "read_logger_status",
        AsyncMock(
            return_value={
                "state": "ecu_online",
                "ownership_enabled": True,
                "capabilities": [puller.obd_control.CAPABILITY],
            }
        ),
    )
    result = await asyncio.wait_for(puller.run_pull(info=prepared_unit.info), timeout=1)
    assert result.state is RunState.IDLE
    assert not prepared_unit.path.exists()
    begin.assert_not_awaited()
    assert status.radio_quieting_hold
    assert "Waiting for radio shutdown" in status.radio_quieting_hold_reason


async def test_slow_mirror_cleanup_never_blocks_footage_or_starts_another_mirror(
    prepared_unit, monkeypatch
):
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    calls = 0

    async def mirror(*args):
        nonlocal calls
        calls += 1
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            await release_cleanup.wait()
            raise RuntimeError("late observability failure")

    monkeypatch.setattr(puller, "sync_remote_events", mirror)
    monkeypatch.setattr(puller, "EVENT_SYNC_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(puller, "_EVENT_SYNC_AWAIT_GRACE_SECONDS", 0.0)
    pending = None
    try:
        result = await asyncio.wait_for(puller.run_pull(info=prepared_unit.info), timeout=1)
        pending = puller._event_mirror_task
        await asyncio.wait_for(cleanup_started.wait(), timeout=0.5)
        assert result.state is RunState.OK
        assert prepared_unit.path.read_bytes() == prepared_unit.payload
        assert prepared_unit.transferred.is_set()
        assert not get_status().running
        assert pending is not None and not pending.done()

        # A later backup may proceed while cleanup drains, but may not add a DB job.
        second = await asyncio.wait_for(puller.run_pull(info=prepared_unit.info), timeout=1)
        assert second.state is RunState.IDLE
        assert calls == 1
        assert puller._event_mirror_task is pending
    finally:
        release_cleanup.set()
        if pending is not None:
            await asyncio.wait((pending,), timeout=0.5)
            await asyncio.sleep(0)
    assert puller._event_mirror_task is None
    # The done callback retrieved the late exception without depending on another run.
    assert pending is not None and not pending._log_traceback


async def test_mirror_deadline_does_not_cancel_database_cleanup_twice(monkeypatch, isolated_mirror):
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()

    async def mirror(*args):
        async with asyncio.timeout(0.01):
            try:
                await asyncio.Event().wait()
            finally:
                cleanup_started.set()
                await release_cleanup.wait()

    monkeypatch.setattr(puller, "sync_remote_events", mirror)
    task = puller._start_event_mirror("unit", "/events")
    assert task is not None
    try:
        await asyncio.wait_for(cleanup_started.wait(), timeout=0.5)
        assert task.cancelling() == 1
        started = time.monotonic()
        await puller._await_event_mirror(task, deadline=asyncio.get_running_loop().time())
        puller._cancel_event_mirror(task)  # The run's final cleanup also sees this task.
        assert time.monotonic() - started < 0.2
        assert task.cancelling() == 1
        assert not task.done()
        assert puller._start_event_mirror("unit", "/events") is None
    finally:
        release_cleanup.set()
        await asyncio.wait((task,), timeout=0.5)
        await asyncio.sleep(0)
    assert puller._event_mirror_task is None


async def test_cancelling_mirror_wait_does_not_wait_for_its_cleanup(monkeypatch, isolated_mirror):
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()

    async def mirror(*args):
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            await release_cleanup.wait()

    monkeypatch.setattr(puller, "sync_remote_events", mirror)
    task = puller._start_event_mirror("unit", "/events")
    assert task is not None
    waiter = asyncio.create_task(
        puller._await_event_mirror(task, deadline=asyncio.get_running_loop().time() + 30)
    )
    await asyncio.sleep(0)
    try:
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(waiter, timeout=0.5)
        await asyncio.wait_for(cleanup_started.wait(), timeout=0.5)
        assert not task.done()
        assert puller._event_mirror_task is task
    finally:
        release_cleanup.set()
        await asyncio.wait((task,), timeout=0.5)


async def test_path_initialization_failure_clears_run_ownership(prepared_unit):
    prepared_unit.settings.footage_dir.side_effect = OSError("storage unavailable")
    result = await puller.run_pull(info=prepared_unit.info)
    assert result.state is RunState.ERROR
    assert "storage unavailable" in result.error
    assert not get_status().running
    assert get_status().snapshot()["phase"] == "idle"
    puller._record_run_completion.assert_awaited_once_with(result, "auto", continuation=False)
    prepared_unit.settings.footage_dir.side_effect = None
    assert (await puller.run_pull(info=prepared_unit.info)).state is RunState.OK
    assert puller._record_run_completion.await_count == 2


async def test_cancellation_during_preflight_cleanup_clears_terminal_status(
    prepared_unit, monkeypatch
):
    cleanup_started = asyncio.Event()

    async def safety(*args):
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(puller, "_footage_is_safe_to_write", safety)
    prepared_unit.inventory.return_value = []
    run = asyncio.create_task(puller.run_pull(info=prepared_unit.info))
    try:
        await asyncio.wait_for(cleanup_started.wait(), timeout=1)
        assert get_status().running
        run.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(run, timeout=0.5)
        assert not get_status().running
        assert get_status().snapshot()["state"] == "cancelled"
    finally:
        if not run.done():
            run.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await run


async def test_cancelled_run_keeps_staging_owned_until_cleanup_thread_exits(
    prepared_unit, monkeypatch
):
    thread_started = threading.Event()
    release_thread = threading.Event()
    exited = threading.Event()

    def clean(staging):
        thread_started.set()
        assert release_thread.wait(timeout=3), "test failed to release staging cleanup"
        exited.set()

    monkeypatch.setattr(puller, "_clean", clean)
    run = asyncio.create_task(puller.run_pull(info=prepared_unit.info))
    try:
        assert await asyncio.to_thread(thread_started.wait, 1)
        run.cancel()
        await asyncio.sleep(0.03)
        assert not run.done()
        assert get_status().running
        # A second cancellation lands inside final cleanup, while the thread is
        # still capable of deleting staging. Ownership must survive this too.
        run.cancel()
        await asyncio.sleep(0.03)
        assert not run.done()
        assert get_status().running
        assert (await puller.run_pull(info=prepared_unit.info)).state is RunState.RUNNING
        assert not prepared_unit.transferred.is_set()
        release_thread.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(run, timeout=1)
        assert exited.is_set()
        assert not get_status().running
    finally:
        release_thread.set()
        if not run.done():
            with contextlib.suppress(asyncio.CancelledError):
                await run


async def test_reboot_cancellation_after_inventory_prevents_radio_or_transfer_work(
    prepared_unit, monkeypatch
):
    async def logger_status(*args):
        get_status().cancel()
        return None

    monkeypatch.setattr(puller, "read_logger_status", logger_status)
    result = await puller.run_pull(info=prepared_unit.info)
    assert result.state is RunState.CANCELLED
    assert not get_status().running
    assert not prepared_unit.transferred.is_set()
    puller.band.gate.assert_not_awaited()
    puller.elevate.ensure_root.assert_not_awaited()


async def test_staging_cleanup_failure_records_one_terminal_error(prepared_unit, monkeypatch):
    def clean(staging):
        raise OSError("cannot list staging directory")

    monkeypatch.setattr(puller, "_clean", clean)
    prepared_unit.inventory.return_value = []
    result = await puller.run_pull(info=prepared_unit.info)
    assert result.state is RunState.ERROR
    assert "cannot list staging directory" in result.error
    assert not get_status().running
    puller._record_run_completion.assert_awaited_once_with(result, "auto", continuation=False)


async def test_cancellation_during_preparation_records_cancelled_result(prepared_unit, monkeypatch):
    started = asyncio.Event()

    async def logger_status(*args):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(puller, "read_logger_status", logger_status)
    run = asyncio.create_task(puller.run_pull(info=prepared_unit.info))
    await asyncio.wait_for(started.wait(), timeout=1)
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(run, timeout=1)
    assert get_status().snapshot()["state"] == "cancelled"
    assert puller._record_run_completion.await_count == 1
    assert puller._record_run_completion.await_args.args[0].state is RunState.CANCELLED


@pytest.mark.parametrize("cancel_shutdown", [False, True])
async def test_shutdown_joins_retained_mirror_even_without_a_current_run(
    monkeypatch, isolated_mirror, cancel_shutdown
):
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()

    async def mirror(*args):
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            await release_cleanup.wait()

    monkeypatch.setattr(puller, "sync_remote_events", mirror)
    monkeypatch.setattr(puller, "_current", None)
    monkeypatch.setattr(puller, "_side_tasks", set())
    monkeypatch.setattr(puller.radios, "cancel_pending", AsyncMock())
    task = puller._start_event_mirror("unit", "/events")
    assert task is not None
    await asyncio.sleep(0)
    puller._cancel_event_mirror(task)
    await asyncio.wait_for(cleanup_started.wait(), timeout=0.5)
    shutdown = asyncio.create_task(puller.shutdown())
    try:
        await asyncio.sleep(0.02)
        assert not shutdown.done()
        assert not task.done()
        if cancel_shutdown:
            shutdown.cancel()
            await asyncio.sleep(0.02)
            assert not shutdown.done()
            assert task.cancelling() == 1
        release_cleanup.set()
        if cancel_shutdown:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(shutdown, timeout=0.5)
        else:
            await asyncio.wait_for(shutdown, timeout=0.5)
        assert task.done()
        assert puller._event_mirror_task is None
    finally:
        release_cleanup.set()
        with contextlib.suppress(asyncio.CancelledError):
            await shutdown
