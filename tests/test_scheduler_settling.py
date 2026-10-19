"""Settling files get bounded scheduler-owned retries instead of a full scan interval."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.scanner.discovery import ScanSummary
from app.workers import scheduler as module
from app.workers.scheduler import Scheduler, TaskState


@pytest.fixture
def scheduled(monkeypatch):
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock.now))
    settings = SimpleNamespace(
        scan_enabled=AsyncMock(return_value=True),
        auto_process=AsyncMock(return_value=True),
        settle_seconds=AsyncMock(return_value=90.0),
    )
    monkeypatch.setattr(module, "get_settings_service", lambda: settings)

    @asynccontextmanager
    async def session_scope():
        yield object()

    monkeypatch.setattr(module, "session_scope", session_scope)
    queued = AsyncMock(return_value=2)
    monkeypatch.setattr(module, "queue_unprocessed", queued)
    scheduler = Scheduler()
    scanner = AsyncMock(return_value=ScanSummary(unsettled=2))
    scheduler._scanner = SimpleNamespace(scan=scanner)
    monkeypatch.setattr(scheduler, "_post_scan_maintenance", AsyncMock())
    return SimpleNamespace(
        scheduler=scheduler, clock=clock, settings=settings, scan=scanner, queued=queued
    )


@pytest.mark.parametrize("trigger", ["scheduled", "manual"])
async def test_successful_walk_arms_a_settle_followup_and_queues_newly_ready_files(
    scheduled, trigger
):
    state = scheduled
    state.scan.side_effect = [ScanSummary(unsettled=2), ScanSummary(unsettled=0)]
    if trigger == "manual":
        await state.scheduler.scan_now()
    else:
        await state.scheduler._run_scan()
    assert state.scan.call_args.kwargs["trigger"] == trigger
    assert state.scheduler._settling_scan_due_at == 190.0
    assert state.scheduler._settling_scan_remaining == 3
    state.clock.now = 189.0
    assert not await state.scheduler._run_due_settling_scan()
    state.clock.now = 190.0
    assert await state.scheduler._run_due_settling_scan()
    assert state.scan.await_count == 2
    assert state.queued.await_count == 2, "the second stable observation must reach auto-processing"
    assert state.scheduler._settling_scan_due_at is None
    assert state.scheduler._settling_scan_remaining == 0


async def test_continuously_changing_file_gets_only_three_extra_walks_then_normal_cadence(
    scheduled,
):
    state = scheduled
    await state.scheduler._run_scan()
    for remaining in (2, 1, 0):
        state.clock.now += 90
        assert await state.scheduler._run_due_settling_scan()
        assert state.scheduler._settling_scan_remaining == remaining
    assert state.scan.await_count == 4
    assert state.scheduler._settling_scan_due_at is None
    state.clock.now += 1_800
    assert not await state.scheduler._run_due_settling_scan()
    assert state.scan.await_count == 4
    await state.scheduler._run_scan()
    assert state.scheduler._settling_scan_remaining == 3, (
        "a new regular scan starts a new bounded chain"
    )
    assert state.scheduler._settling_scan_due_at == state.clock.now + 90


@pytest.mark.parametrize(
    "summary",
    [
        ScanSummary(),
        ScanSummary(unsettled=2, errors=1),
        ScanSummary(unsettled=2, error_message="share unavailable"),
    ],
)
async def test_empty_or_failed_scan_summary_does_not_arm_a_followup(scheduled, summary):
    state = scheduled
    state.scan.return_value = summary
    await state.scheduler.scan_now()
    assert state.scheduler._settling_scan_due_at is None
    assert state.scheduler._settling_scan_remaining == 0


@pytest.mark.parametrize("flag", ["scan_enabled", "auto_process"])
async def test_automatic_followups_respect_disabled_settings_when_armed_or_due(scheduled, flag):
    state = scheduled
    getattr(state.settings, flag).return_value = False
    await state.scheduler.scan_now()
    assert state.scan.await_count == 1, "explicit manual scans retain their existing behavior"
    assert state.scheduler._settling_scan_due_at is None
    assert state.queued.await_count == (0 if flag == "auto_process" else 1)
    getattr(state.settings, flag).return_value = True
    await state.scheduler._run_scan()
    getattr(state.settings, flag).return_value = False
    state.clock.now = 190
    assert not await state.scheduler._run_due_settling_scan()
    assert state.scan.await_count == 2
    assert state.scheduler._settling_scan_due_at is None


async def test_failed_followup_consumes_the_deadline_without_rapid_retries(scheduled):
    state = scheduled
    state.scan.side_effect = [ScanSummary(unsettled=2), RuntimeError("share vanished")]
    await state.scheduler._run_scan()
    state.clock.now = 190
    with pytest.raises(RuntimeError, match="share vanished"):
        await state.scheduler._run_due_settling_scan()
    assert state.scheduler._settling_scan_due_at is None
    assert state.scheduler._settling_scan_remaining == 0
    assert not await state.scheduler._run_due_settling_scan(now=1_000_000)
    assert state.scan.await_count == 2


async def test_failed_due_setting_read_does_not_retry_on_each_scheduler_tick(scheduled):
    state = scheduled
    await state.scheduler.scan_now()
    state.clock.now = 190
    state.settings.scan_enabled.side_effect = RuntimeError("settings unavailable")
    with pytest.raises(RuntimeError, match="settings unavailable"):
        await state.scheduler._run_due_settling_scan()
    assert state.scheduler._settling_scan_due_at is None
    assert state.scheduler._settling_scan_remaining == 0
    assert not await state.scheduler._run_due_settling_scan()
    assert state.scan.await_count == 1


async def test_failed_initial_scan_does_not_create_a_background_walk(scheduled):
    state = scheduled
    state.scan.side_effect = RuntimeError("share unavailable")
    with pytest.raises(RuntimeError, match="share unavailable"):
        await state.scheduler.scan_now()
    assert state.scheduler._settling_scan_due_at is None
    assert state.queued.await_count == 0


async def test_repeated_manual_results_coalesce_without_postponing_the_first_deadline(scheduled):
    state = scheduled
    await state.scheduler.scan_now()
    state.clock.now = 110
    await state.scheduler.scan_now()
    assert state.scheduler._settling_scan_due_at == 190
    assert state.scheduler._settling_scan_remaining == 3


async def test_frequent_regular_scans_subsume_older_followups_instead_of_adding_walks(scheduled):
    state = scheduled
    await state.scheduler._run_scan()
    for now in (160, 220, 280):
        state.clock.now = now
        await state.scheduler._run_scan()
        assert state.scheduler._settling_scan_due_at == now + 90
        assert not await state.scheduler._run_due_settling_scan()
    assert state.scan.await_count == 4


async def test_long_maintenance_does_not_restart_the_settle_wait(scheduled, monkeypatch):
    state = scheduled

    async def maintenance(summary):
        state.clock.now = 500

    monkeypatch.setattr(state.scheduler, "_post_scan_maintenance", maintenance)
    await state.scheduler.scan_now()
    assert state.scheduler._settling_scan_due_at == 190
    assert await state.scheduler._run_due_settling_scan(), (
        "already settled files must not wait another 90 seconds"
    )


async def test_periodic_finally_preserves_a_manual_deadline_and_reports_its_actual_eta(
    scheduled, monkeypatch
):
    state = scheduled

    async def interval_after_manual_request():
        await state.scheduler.scan_now()
        return 1_800.0

    task = TaskState(
        name="scan",
        interval_s=interval_after_manual_request,
        run=AsyncMock(),
        enabled=AsyncMock(return_value=True),
    )
    state.scheduler._tasks = [task]
    state.scheduler._running = True

    async def stop_after_cycle(_delay):
        state.scheduler._running = False

    monkeypatch.setattr(
        module,
        "asyncio",
        SimpleNamespace(sleep=stop_after_cycle, CancelledError=asyncio.CancelledError),
    )
    await state.scheduler._loop()
    assert task.next_run == 1_900
    assert state.scheduler._settling_scan_due_at == 190
    assert state.scheduler.describe()[0]["seconds_until_next"] == 90


async def test_new_manual_scan_wins_over_an_older_followup_completion(scheduled, monkeypatch):
    state = scheduled
    await state.scheduler.scan_now()
    state.clock.now = 190
    old_generation = state.scheduler._settling_scan_generation
    # A newer explicit scan can finish while the previous follow-up is doing maintenance.
    await state.scheduler.scan_now()
    new_deadline = state.scheduler._settling_scan_due_at
    await state.scheduler._schedule_settling_scan(ScanSummary(), old_generation, None, True)
    assert state.scheduler._settling_scan_due_at == new_deadline
    assert state.scheduler._settling_scan_remaining == 3


async def test_zero_settle_policy_still_has_a_bounded_minimum_delay(scheduled):
    state = scheduled
    state.settings.settle_seconds.return_value = 0
    await state.scheduler.scan_now()
    assert state.scheduler._settling_scan_due_at == 130
