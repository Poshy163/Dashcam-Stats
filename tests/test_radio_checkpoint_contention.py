"""Radio lease renewals remain short and truthful under real SQLite contention."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import create_async_engine

from app.db.models import IngestRadioTransition
from app.db.session import get_engine, session_scope
from app.ingest import radio_coordinator


@pytest.fixture
async def transition(db_session, monkeypatch):
    controller = SimpleNamespace(
        report=None,
        claim=Mock(),
        release=AsyncMock(),
        watchdog_healthy=AsyncMock(return_value=True),
    )
    monkeypatch.setattr(radio_coordinator.radios, "RadioController", lambda *a, **k: controller)
    monkeypatch.setattr(radio_coordinator.radios, "new_quieting_allowed", lambda: True)
    monkeypatch.setattr(
        radio_coordinator.radios, "read_device_boot_id", AsyncMock(return_value="test-boot")
    )
    monkeypatch.setattr(radio_coordinator, "HEARTBEAT_INTERVAL_S", 60)
    value = await radio_coordinator.begin(
        trigger="test",
        address="test-unit:5555",
        logger_status=None,
        logger_status_path=None,
        watchdog_deadline_s=120,
    )
    await value._stop_heartbeat()
    try:
        yield value
    finally:
        await value.close()


def hold_writer(app_config):
    connection = sqlite3.connect(app_config.db_path, timeout=0)
    connection.execute("BEGIN IMMEDIATE")
    return connection


async def persisted(transition):
    async with session_scope() as session:
        return await session.get(IngestRadioTransition, transition.id)


async def assert_default_timeout():
    async with get_engine().connect() as connection:
        assert await connection.scalar(text("PRAGMA busy_timeout")) == 30_000


async def test_transient_writer_retries_and_dates_lease_after_lock_acquisition(
    transition, app_config, monkeypatch
):
    monkeypatch.setattr(radio_coordinator, "CHECKPOINT_BUSY_TIMEOUT_MS", 40)
    monkeypatch.setattr(radio_coordinator, "CHECKPOINT_LOCK_RETRY_S", 0.01)
    holder = hold_writer(app_config)
    try:
        task = asyncio.create_task(transition.checkpoint())
        await asyncio.sleep(0.12)
        assert not task.done()
        released = datetime.now(UTC)
        holder.rollback()
        await asyncio.wait_for(task, timeout=1)
    finally:
        holder.close()
    row = await persisted(transition)
    assert row.heartbeat_at >= released
    assert row.lease_expires_at - row.heartbeat_at == timedelta(
        seconds=radio_coordinator.LEASE_TTL_S
    )
    assert not transition.lease_lost
    await assert_default_timeout()


async def test_prolonged_writer_fails_heartbeat_closed_without_expiring_owner_silently(
    transition, app_config, monkeypatch
):
    before = await persisted(transition)
    cancelled = asyncio.Event()
    transition.lease_loss_callback = cancelled.set
    monkeypatch.setattr(radio_coordinator, "CHECKPOINT_BUSY_TIMEOUT_MS", 30)
    monkeypatch.setattr(radio_coordinator, "CHECKPOINT_MAX_RETRY_S", 0.12)
    monkeypatch.setattr(radio_coordinator, "CHECKPOINT_LOCK_RETRY_S", 0.01)
    monkeypatch.setattr(radio_coordinator, "HEARTBEAT_INTERVAL_S", 0.01)
    holder = hold_writer(app_config)
    started = asyncio.get_running_loop().time()
    try:
        transition.start_heartbeat()
        error = await asyncio.wait_for(transition.wait_for_lease_loss(), timeout=1)
        assert "database is locked" in str(error)
        assert cancelled.is_set()
        assert transition.process_fence.held
        transition.controller.watchdog_healthy.assert_not_awaited()
    finally:
        holder.rollback()
        holder.close()
    assert asyncio.get_running_loop().time() - started < 1
    after = await persisted(transition)
    assert after.active
    assert after.lease_owner == transition.lease_owner
    assert after.heartbeat_at == before.heartbeat_at
    assert after.lease_expires_at == before.lease_expires_at
    await assert_default_timeout()


async def test_cancelled_checkpoint_rolls_back_and_does_not_leave_short_timeout_in_pool(
    transition, app_config, monkeypatch
):
    before = await persisted(transition)
    monkeypatch.setattr(radio_coordinator, "CHECKPOINT_BUSY_TIMEOUT_MS", 40)
    holder = hold_writer(app_config)
    task = asyncio.create_task(transition.checkpoint())
    try:
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)
    finally:
        holder.rollback()
        holder.close()
    assert (await persisted(transition)).heartbeat_at == before.heartbeat_at
    await assert_default_timeout()
    # The pooled connection has no leaked transaction/lock after cancellation.
    await transition.checkpoint(last_error="post-cancellation checkpoint")
    assert (await persisted(transition)).last_error == "post-cancellation checkpoint"


async def test_changed_owner_is_never_renewed_or_retried(transition, monkeypatch):
    async with session_scope() as session:
        await session.execute(
            update(IngestRadioTransition)
            .where(IngestRadioTransition.id == transition.id)
            .values(lease_owner="another-owner")
        )
    with pytest.raises(radio_coordinator.RadioTransitionError, match="no longer owned"):
        await transition.checkpoint()
    assert transition.lease_lost
    assert (await persisted(transition)).lease_owner == "another-owner"
    await assert_default_timeout()


async def test_recovery_checkpoint_preserves_explicit_expiry(transition):
    expired = datetime.now(UTC) - timedelta(seconds=1)
    await transition.checkpoint(
        radio_coordinator.TransitionPhase.RECOVERY_REQUIRED,
        lease_expires_at=expired,
        recovery_required=True,
    )
    row = await persisted(transition)
    assert row.lease_expires_at == expired
    assert row.recovery_required
    await assert_default_timeout()


async def test_non_lock_failure_is_not_retried(transition, monkeypatch):
    attempt = AsyncMock(side_effect=RuntimeError("disk unavailable"))
    monkeypatch.setattr(radio_coordinator.RadioTransition, "_checkpoint_once", attempt)
    with pytest.raises(RuntimeError, match="disk unavailable"):
        await transition.checkpoint()
    attempt.assert_awaited_once()


async def test_six_second_writer_does_not_kill_a_lease_with_proven_headroom(transition, app_config):
    holder = hold_writer(app_config)
    task = asyncio.create_task(transition.checkpoint())
    try:
        await asyncio.sleep(6.1)
        assert not task.done()
        released = datetime.now(UTC)
        holder.rollback()
        await asyncio.wait_for(task, timeout=2)
    finally:
        holder.close()
    assert not transition.lease_lost
    assert (await persisted(transition)).heartbeat_at >= released
    await assert_default_timeout()


async def test_retry_budget_uses_last_committed_lease_and_preserves_recovery_headroom(
    transition, app_config, monkeypatch
):
    monkeypatch.setattr(radio_coordinator, "CHECKPOINT_BUSY_TIMEOUT_MS", 30)
    transition._lease_valid_until = (
        asyncio.get_running_loop().time() + radio_coordinator.CHECKPOINT_RECOVERY_HEADROOM_S + 0.12
    )
    holder = hold_writer(app_config)
    try:
        with pytest.raises(Exception, match=r"locked|budget"):
            await asyncio.wait_for(transition.checkpoint(), timeout=1)
        assert transition.lease_lost
        assert transition.process_fence.held
        assert transition._lease_valid_until - asyncio.get_running_loop().time() > 9
    finally:
        holder.rollback()
        holder.close()


async def test_expired_persisted_owner_is_not_resurrected_but_can_record_recovery(transition):
    expired = datetime.now(UTC) - timedelta(seconds=1)
    async with session_scope() as session:
        await session.execute(
            update(IngestRadioTransition)
            .where(IngestRadioTransition.id == transition.id)
            .values(lease_expires_at=expired)
        )
    with pytest.raises(radio_coordinator.RadioTransitionError, match="expired"):
        await transition.checkpoint()
    assert (await persisted(transition)).lease_expires_at == expired
    assert transition.lease_lost
    # A fenced cleanup checkpoint can expire recovery state; it cannot silently
    # renew an expired transfer owner and resume ingest.
    await transition.checkpoint(
        radio_coordinator.TransitionPhase.RECOVERY_REQUIRED,
        lease_expires_at=expired,
        recovery_required=True,
    )
    assert (await persisted(transition)).recovery_required


async def test_pool_checkout_is_bounded_by_the_same_confirmed_lease_budget(
    transition, app_config, monkeypatch
):
    engine = create_async_engine(
        app_config.sqlalchemy_url, pool_size=1, max_overflow=0, pool_timeout=30
    )
    monkeypatch.setattr(radio_coordinator, "get_engine", lambda: engine)
    monkeypatch.setattr(radio_coordinator, "CHECKPOINT_MAX_RETRY_S", 0.1)
    try:
        async with engine.connect():
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(transition.checkpoint(), timeout=1)
        assert transition.lease_lost
        assert transition.process_fence.held
    finally:
        await engine.dispose()
