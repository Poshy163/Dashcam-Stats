"""Radio debt is retried independently of footage copying and its visit limits."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.ingest import poller as poller_module
from app.ingest import puller
from app.ingest.models import RunState


@pytest.mark.parametrize("state", [RunState.PARTIAL, RunState.ERROR, RunState.CANCELLED])
async def test_online_radio_debt_retries_without_new_backup_or_budget_use(monkeypatch, state):
    poller = poller_module.IngestPoller()
    poller._running = poller._was_online = True
    poller._backups_this_visit = 2
    poller._error_retries_started = 3
    status = SimpleNamespace(running=False, state=state)
    clock = [0.0]
    times = iter([10.0, 29.9, 30.0])
    attempts = []
    pending = [True]

    async def debt():
        return "unit:5555" if pending[0] else None

    async def recover(address):
        attempts.append((clock[0], address))
        if len(attempts) == 2:
            pending[0] = False
            return True
        return False

    async def tick(_delay):
        try:
            clock[0] = next(times)
        except StopIteration:
            poller._running = False

    forbidden = Mock(side_effect=AssertionError("recovery must not schedule/probe a backup"))
    monkeypatch.setattr(poller_module, "get_status", lambda: status)
    monkeypatch.setattr(poller_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(poller_module.asyncio, "sleep", tick)
    monkeypatch.setattr(poller, "_enabled", lambda: True)
    monkeypatch.setattr(poller, "_address", lambda: "unit:5555")
    monkeypatch.setattr(poller, "_interval", lambda: 1.0)
    monkeypatch.setattr(poller, "_observe_unit_runtime", AsyncMock())
    monkeypatch.setattr(poller, "_should_drain_again", forbidden)
    monkeypatch.setattr(poller_module.adb, "is_listening", AsyncMock(return_value=True))
    monkeypatch.setattr(poller_module.radio_coordinator, "pending_recovery_address", debt)
    monkeypatch.setattr(puller, "reconcile_pending_in_awake_window", recover)
    monkeypatch.setattr(puller, "start_run", forbidden)
    monkeypatch.setattr(puller, "probe_unit", forbidden)

    await poller._loop()

    assert attempts == [(0.0, "unit:5555"), (30.0, "unit:5555")]
    assert not pending[0]
    assert poller._backups_this_visit == 2
    assert poller._error_retries_started == 3
    forbidden.assert_not_called()


async def test_recovery_cooldown_starts_after_slow_or_exceptional_attempt(monkeypatch):
    poller = poller_module.IngestPoller()
    clock = [10.0]
    monkeypatch.setattr(poller_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        poller_module.radio_coordinator,
        "pending_recovery_address",
        AsyncMock(return_value="unit:5555"),
    )

    async def failed(_address):
        clock[0] = 60.0
        raise RuntimeError("temporary recovery failure")

    recover = AsyncMock(side_effect=failed)
    monkeypatch.setattr(puller, "reconcile_pending_in_awake_window", recover)
    with pytest.raises(RuntimeError, match="temporary recovery failure"):
        await poller._recover_pending_while_online("unit:5555")
    clock[0] = 89.9
    assert await poller._recover_pending_while_online("unit:5555")
    assert recover.await_count == 1
    recover.side_effect = None
    recover.return_value = False
    clock[0] = 90.0
    assert await poller._recover_pending_while_online("unit:5555")
    assert recover.await_count == 2


async def test_no_radio_debt_does_not_contact_unit_or_block_normal_polling(monkeypatch):
    poller = poller_module.IngestPoller()
    poller._radio_recovery_retry_at = float("inf")
    recover = AsyncMock()
    monkeypatch.setattr(
        poller_module.radio_coordinator, "pending_recovery_address", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(puller, "reconcile_pending_in_awake_window", recover)
    assert not await poller._recover_pending_while_online("unit:5555")
    assert poller._radio_recovery_retry_at == 0.0
    recover.assert_not_called()


async def test_active_backup_retains_radio_ownership(monkeypatch):
    poller = poller_module.IngestPoller()
    poller._running = poller._was_online = True
    recover = AsyncMock(side_effect=AssertionError("an active run owns its radio transition"))

    async def stop(_delay):
        poller._running = False

    monkeypatch.setattr(poller_module, "get_status", lambda: SimpleNamespace(running=True))
    monkeypatch.setattr(poller, "_address", lambda: "unit:5555")
    monkeypatch.setattr(poller, "_interval", lambda: 1.0)
    monkeypatch.setattr(poller, "_observe_unit_runtime", AsyncMock())
    monkeypatch.setattr(poller, "_recover_pending_while_online", recover)
    monkeypatch.setattr(poller_module.carplay_timing, "recover_on_unit_present", Mock())
    monkeypatch.setattr(poller_module.asyncio, "sleep", stop)
    await poller._loop()
    recover.assert_not_called()


async def test_run_exit_and_reconciliation_share_sufficient_restore_budget(monkeypatch):
    budgets = []
    transition = SimpleNamespace(restore=AsyncMock(return_value=True))

    async def wait(awaitable, *, timeout):
        budgets.append(timeout)
        return await awaitable

    monkeypatch.setattr(puller.asyncio, "wait_for", wait)
    monkeypatch.setattr(puller, "widen_sleep_window", AsyncMock(return_value=True))
    monkeypatch.setattr(puller.radio_coordinator, "reconcile_pending", AsyncMock(return_value=True))
    assert await puller._restore_radio_transition(transition)
    assert await puller.reconcile_pending_in_awake_window("unit:5555")
    # Three attestation reads plus Bluetooth/AP checks and logger resumption must fit
    # before whole-operation cancellation. The old total was shorter than attestation.
    assert budgets == [puller.RADIO_RESTORE_TIMEOUT_S] * 2
    assert 3 * 15 + 0.5 + 6 + 8 + 6 + 6 + 12 < budgets[0] <= 120


async def test_reconciliation_timeout_cancels_work_and_reports_pending(monkeypatch):
    cancelled = asyncio.Event()

    async def blocked(*, address):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(puller, "RADIO_RESTORE_TIMEOUT_S", 0.01)
    monkeypatch.setattr(puller, "widen_sleep_window", AsyncMock(return_value=True))
    monkeypatch.setattr(puller.radio_coordinator, "reconcile_pending", blocked)
    assert not await puller.reconcile_pending_in_awake_window("unit:5555")
    assert cancelled.is_set()


async def test_run_exit_external_cancellation_remains_cancellation(monkeypatch):
    entered = asyncio.Event()

    async def blocked(*, error=None):
        entered.set()
        await asyncio.Event().wait()

    transition = SimpleNamespace(restore=blocked, require_recovery=AsyncMock())
    task = asyncio.create_task(puller._restore_radio_transition(transition))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    transition.require_recovery.assert_awaited_once()
