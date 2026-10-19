"""Presence work must retry failed diagnostics without accumulating background tasks."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.ingest import carplay_timing as timing


@pytest.fixture(autouse=True)
async def isolated_presence(monkeypatch):
    await timing.shutdown()
    timing.reset_for_tests()
    monkeypatch.setattr(timing, "_enabled", lambda: True)
    monkeypatch.setattr(timing, "get_status", lambda: SimpleNamespace(ignition_state="off"))
    monkeypatch.setattr(timing.adb, "optional_requests_deferred", lambda _address: False)
    yield
    await timing.shutdown()
    timing.reset_for_tests()


async def settle():
    for _ in range(5):
        await asyncio.sleep(0)


async def test_failed_recovery_retries_after_30_seconds_success_waits_300(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(timing, "time", SimpleNamespace(monotonic=lambda: now[0]))
    recover = AsyncMock(side_effect=[timing.adb.AdbError("error: closed"), (2, 5), (0, 7)])
    monkeypatch.setattr(timing, "recover_sampler_file", recover)
    timing.recover_on_unit_present("unit")
    await settle()
    assert recover.await_count == 1
    now[0] += 29
    timing.recover_on_unit_present("unit")
    await settle()
    assert recover.await_count == 1
    now[0] += 1
    timing.recover_on_unit_present("unit")
    await settle()
    assert recover.await_count == 2
    now[0] += 299
    timing.recover_on_unit_present("unit")
    await settle()
    assert recover.await_count == 2
    now[0] += 1
    timing.recover_on_unit_present("unit")
    await settle()
    assert recover.await_count == 3


@pytest.mark.parametrize("operation", ["arm", "recovery"])
async def test_pending_presence_work_keeps_one_slot_past_debounce(monkeypatch, operation):
    now = [1000.0]
    monkeypatch.setattr(timing, "time", SimpleNamespace(monotonic=lambda: now[0]))
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def pending(_address):
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return True if operation == "arm" else (0, 0)

    if operation == "arm":
        monkeypatch.setattr(timing, "arm", pending)
        monkeypatch.setattr(timing, "recover_on_unit_present", lambda _address: None)
        schedule = timing.on_unit_present
    else:
        monkeypatch.setattr(timing, "recover_sampler_file", pending)
        schedule = timing.recover_on_unit_present
    try:
        schedule("unit")
        await asyncio.wait_for(entered.wait(), timeout=1)
        now[0] += 1000
        for _ in range(5):
            schedule("unit")
        await settle()
        assert calls == 1
        assert len(timing._tasks) == 1
    finally:
        release.set()
        await settle()


async def test_arm_callback_retrieves_unexpected_failure_without_payload(monkeypatch):
    reports = []
    monkeypatch.setattr(
        timing,
        "log",
        SimpleNamespace(warning=lambda message, **fields: reports.append((message, fields))),
    )
    monkeypatch.setattr(timing, "recover_on_unit_present", lambda _address: None)
    monkeypatch.setattr(
        timing, "arm", AsyncMock(side_effect=RuntimeError("private payload sample=do-not-log"))
    )
    timing.on_unit_present("unit")
    [task] = list(timing._tasks)
    await asyncio.wait((task,), timeout=1)
    await settle()
    assert not task._log_traceback
    assert not timing._tasks
    assert reports and "private" not in str(reports) and "sample=" not in str(reports)
    assert reports[0][1]["error_type"] == "RuntimeError"


async def test_arm_transport_failure_does_not_log_remote_payload(monkeypatch):
    reports = []
    monkeypatch.setattr(
        timing,
        "log",
        SimpleNamespace(warning=lambda message, **fields: reports.append((message, fields))),
    )
    monkeypatch.setattr(
        timing,
        "_deploy_bundle",
        AsyncMock(side_effect=timing.adb.AdbError("error: closed; private sample=do-not-log")),
    )
    assert await timing.arm("unit") is False
    assert reports[0][1] == {"category": "transport_closed", "error_type": "AdbError"}
    assert "private" not in str(reports) and "sample=" not in str(reports)


async def test_total_recovery_deadline_covers_storage_after_snapshot_read(monkeypatch):
    from app.ingest import unit_logs

    cancelled = asyncio.Event()

    async def blocked_store(_entries):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(timing, "RECOVERY_TIMEOUT_S", 0.02)
    monkeypatch.setattr(timing, "_read_sampler_snapshot", AsyncMock(return_value="valid snapshot"))
    monkeypatch.setattr(timing, "parse_sampler_file", lambda _raw: [object()])
    monkeypatch.setattr(unit_logs, "store", blocked_store)
    task = asyncio.create_task(timing.recover_sampler_file("unit"))
    try:
        done, _ = await asyncio.wait((task,), timeout=0.5)
        assert task in done, "storage was outside the purported total recovery deadline"
        with pytest.raises(timing.adb.AdbError, match="total time limit"):
            await task
        assert cancelled.is_set()
    finally:
        if not task.done():
            task.cancel()
            await asyncio.wait((task,), timeout=1)


async def test_timed_out_recovery_owns_slot_until_cleanup_finishes(monkeypatch):
    from app.ingest import unit_logs

    cleanup_started, release_cleanup = asyncio.Event(), asyncio.Event()

    async def slow_rollback(_entries):
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            await release_cleanup.wait()

    read = AsyncMock(return_value="complete snapshot")
    monkeypatch.setattr(timing, "RECOVERY_TIMEOUT_S", 0.02)
    monkeypatch.setattr(timing, "_read_sampler_snapshot", read)
    monkeypatch.setattr(timing, "parse_sampler_file", lambda _raw: [object()])
    monkeypatch.setattr(unit_logs, "store", slow_rollback)
    shutdown_task = None
    try:
        timing.recover_on_unit_present("unit")
        await asyncio.wait_for(cleanup_started.wait(), timeout=1)
        task = timing._recovery_tasks["unit"]
        assert task.cancelling() == 1
        # Even an elapsed retry/debounce cannot release a live SQLite rollback.
        timing._last_recovered["unit"] = 0
        for _ in range(3):
            timing.recover_on_unit_present("unit")
        await settle()
        assert read.await_count == 1
        assert timing._recovery_tasks["unit"] is task
        shutdown_task = asyncio.create_task(timing.shutdown())
        await settle()
        assert not shutdown_task.done()
        shutdown_task.cancel()
        await settle()
        assert task.cancelling() == 1
        assert timing._recovery_tasks["unit"] is task
        assert not shutdown_task.done()
        release_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(shutdown_task, timeout=1)
        assert task.done()
        assert not timing._tasks and not timing._recovery_tasks
    finally:
        release_cleanup.set()
        if shutdown_task is not None:
            await asyncio.gather(shutdown_task, return_exceptions=True)


async def test_storage_timeout_rolls_back_new_rows_and_preserves_prior_capture(
    db_session, monkeypatch
):
    from sqlalchemy import select

    from app.db.models import UnitLogEntry, utcnow
    from app.db.session import session_scope
    from app.ingest import unit_logs

    old = unit_logs.ParsedLine(utcnow(), 0, 0, "E", "CarPlayTiming", "sample=committed old")
    new = unit_logs.ParsedLine(utcnow(), 0, 0, "E", "CarPlayTiming", "sample=cancelled new")
    assert await unit_logs.store([old]) == (1, 0)
    inserted = asyncio.Event()

    async def blocked_prune(_session):
        inserted.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(timing, "RECOVERY_TIMEOUT_S", 0.05)
    monkeypatch.setattr(timing, "_read_sampler_snapshot", AsyncMock(return_value="complete"))
    monkeypatch.setattr(timing, "parse_sampler_file", lambda _raw: [new])
    monkeypatch.setattr(unit_logs, "_prune", blocked_prune)
    with pytest.raises(timing.adb.AdbError, match="total time limit"):
        await timing.recover_sampler_file("unit")
    assert inserted.is_set()
    async with session_scope() as session:
        stored_hashes = (await session.execute(select(UnitLogEntry.line_hash))).scalars().all()
    assert stored_hashes == [old.line_hash]


async def test_ignition_and_optional_request_gates_still_prevent_recovery(monkeypatch):
    recover = AsyncMock(return_value=(0, 0))
    monkeypatch.setattr(timing, "recover_sampler_file", recover)
    monkeypatch.setattr(timing, "get_status", lambda: SimpleNamespace(ignition_state="unknown"))
    timing.recover_on_unit_present("unit")
    await settle()
    monkeypatch.setattr(timing, "get_status", lambda: SimpleNamespace(ignition_state="off"))
    monkeypatch.setattr(timing.adb, "optional_requests_deferred", lambda _address: True)
    timing.recover_on_unit_present("unit")
    await settle()
    recover.assert_not_awaited()
