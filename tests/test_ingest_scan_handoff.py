"""Committed footage wakes the normal serial scanner, including partial arrivals."""

from unittest.mock import Mock

import pytest
from test_ingest_preparation_lifecycle import isolated_mirror as isolated_mirror
from test_ingest_preparation_lifecycle import prepared_unit as prepared_unit
from test_scheduler_settling import scheduled as scheduled

from app.ingest import puller
from app.ingest.models import RunState
from app.scanner.discovery import ScanSummary
from app.workers import scheduler as module


async def test_ingest_batch_notifications_coalesce_and_reuse_settle_queue_chain(scheduled):
    state = scheduled
    state.scheduler._running = True
    state.scan.side_effect = [ScanSummary(new=2, unsettled=2), ScanSummary(unsettled=0)]
    assert state.scheduler.request_ingest_scan()
    state.clock.now = 101
    assert state.scheduler.request_ingest_scan()
    assert state.scheduler._ingest_scan_due_at == 102
    assert not await state.scheduler._run_due_ingest_scan()
    state.clock.now = 102
    assert await state.scheduler._run_due_ingest_scan()
    assert state.scan.call_args.kwargs["trigger"] == "ingest"
    assert state.scan.await_count == state.queued.await_count == 1
    assert state.scheduler._settling_scan_due_at == 192
    state.clock.now = 192
    assert await state.scheduler._run_due_settling_scan()
    assert state.scan.await_count == state.queued.await_count == 2


@pytest.mark.parametrize("enabled", [False, True])
async def test_ingest_scan_honors_disabled_scanner_and_automatic_processing(scheduled, enabled):
    state = scheduled
    state.scheduler._running = True
    state.settings.scan_enabled.return_value = enabled
    state.settings.auto_process.return_value = False
    state.scheduler.request_ingest_scan()
    state.clock.now = 102
    assert await state.scheduler._run_due_ingest_scan() is enabled
    assert state.scan.await_count == int(enabled)
    state.queued.assert_not_awaited()
    assert state.scheduler._settling_scan_due_at is None


async def test_scheduled_scan_covers_pending_batch_without_an_extra_walk(scheduled):
    state = scheduled
    state.scheduler._running = True
    state.scheduler.request_ingest_scan()
    await state.scheduler._run_scan()
    state.clock.now = 102
    assert not await state.scheduler._run_due_ingest_scan()
    assert state.scan.await_count == 1


async def test_new_batch_during_scan_keeps_its_notification(scheduled):
    state = scheduled
    state.scheduler._running = True

    async def walk(**_kwargs):
        state.clock.now = 101
        state.scheduler.request_ingest_scan()
        return ScanSummary()

    state.scan.side_effect = walk
    await state.scheduler._run_scan()
    assert state.scheduler._ingest_scan_due_at == 103


async def test_failed_ingest_scan_is_consumed_without_per_second_retry(scheduled):
    state = scheduled
    state.scheduler._running = True
    state.scheduler.request_ingest_scan()
    state.clock.now = 102
    state.scan.side_effect = RuntimeError("share unavailable")
    with pytest.raises(RuntimeError, match="share unavailable"):
        await state.scheduler._run_due_ingest_scan()
    assert not await state.scheduler._run_due_ingest_scan(now=10000)
    assert state.scan.await_count == 1


@pytest.mark.parametrize("complete", [False, True])
async def test_completed_or_partial_footage_notifies_once_after_committing(
    prepared_unit, monkeypatch, complete
):
    unit = prepared_unit
    original_move = puller._move

    async def move(*args, **kwargs):
        arrived = await original_move(*args, **kwargs)
        arrived.complete = complete
        return arrived

    notify = Mock(return_value=True)
    monkeypatch.setattr(puller, "_move", move)
    monkeypatch.setattr(module, "request_ingest_scan", notify)
    result = await puller.run_pull(info=unit.info)
    assert result.state is (RunState.OK if complete else RunState.PARTIAL)
    assert unit.path.read_bytes() == unit.payload
    notify.assert_called_once_with()


async def test_no_committed_footage_does_not_request_scan(prepared_unit, monkeypatch):
    unit = prepared_unit
    unit.inventory.return_value = []
    notify = Mock(return_value=True)
    monkeypatch.setattr(module, "request_ingest_scan", notify)
    assert (await puller.run_pull(info=unit.info)).state is RunState.IDLE
    notify.assert_not_called()


def test_shutdown_scheduler_does_not_start_background_work(scheduled, monkeypatch):
    monkeypatch.setattr(module, "_scheduler", None)
    assert not module.request_ingest_scan()
    monkeypatch.setattr(module, "_scheduler", scheduled.scheduler)
    assert not module.request_ingest_scan()
