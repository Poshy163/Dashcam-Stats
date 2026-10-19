"""Event mirroring must stay bounded under a full snapshot and SQLite contention."""

from __future__ import annotations

import asyncio
import sqlite3
import time
from datetime import UTC, datetime

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import OperationalError

from app.db.models import OBDLoggerEvent
from app.db.session import get_engine
from app.ingest import obd_events, puller


def snapshot(count=512):
    now = datetime.now(UTC)
    return obd_events.ValidatedEventSnapshot(
        source_id_hash="a" * 64,
        generated_at=now,
        app_version_name="0.3.0",
        app_version_code=8,
        build_git_sha="0123456789ab",
        events=tuple(
            obd_events.ValidatedLoggerEvent(
                sequence=index,
                occurred_at=now,
                session_id_hash="b" * 64,
                kind="app.boot",
                level="info",
                outcome="observed",
                reason_code=None,
                drive_id=None,
                metrics={},
            )
            for index in range(1, count + 1)
        ),
    )


async def test_full_snapshot_respects_sqlite_variable_limit_and_stays_idempotent(db_session):
    engine = get_engine()

    # SQLite builds can impose a 999-variable statement ceiling. Test the actual driver,
    # not just the number of rows the Python writer intends to send.
    async def set_limit(driver):
        await driver._execute(driver._conn.setlimit, sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)

    def configure(dbapi_connection, _record):
        dbapi_connection.run_async(set_limit)

    await engine.dispose()
    event.listen(engine.sync_engine, "connect", configure)
    try:
        full = snapshot()
        assert await obd_events.store_event_snapshot(full) == (512, 0, 0)
        assert await obd_events.store_event_snapshot(full) == (0, 512, 0)
        async with engine.connect() as connection:
            assert await connection.scalar(select(func.count(OBDLoggerEvent.id))) == 512
            assert await connection.scalar(text("PRAGMA busy_timeout")) == 30_000
    finally:
        event.remove(engine.sync_engine, "connect", configure)


async def test_pruning_excess_rows_does_not_build_an_unbounded_bind_list(db_session, monkeypatch):
    await obd_events.store_event_snapshot(snapshot(1_100))
    monkeypatch.setattr(obd_events, "MAX_SERVER_EVENTS", 1)
    engine = get_engine()
    parameter_counts = []

    def observe(_connection, _cursor, statement, parameters, _context, executemany):
        if statement.lstrip().upper().startswith("DELETE"):
            parameter_counts.append(len(parameters))

    event.listen(engine.sync_engine, "before_cursor_execute", observe)
    try:
        assert await obd_events.store_event_snapshot(snapshot(0)) == (0, 0, 0)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", observe)
    assert max(parameter_counts) < 10
    async with engine.connect() as connection:
        assert await connection.scalar(select(func.count(OBDLoggerEvent.id))) == 1


async def test_event_write_lock_does_not_wait_past_its_observability_budget(
    db_session, app_config, monkeypatch
):
    await obd_events.store_event_snapshot(snapshot(1))
    blocker = sqlite3.connect(app_config.db_path, timeout=0.1, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        started = time.monotonic()
        task = asyncio.create_task(obd_events.store_event_snapshot(snapshot(2)))
        done, _ = await asyncio.wait((task,), timeout=1.5)
        assert task in done, "optional event writer is still inside SQLite's 30-second wait"
        with pytest.raises(OperationalError, match="database is locked"):
            await task
        assert time.monotonic() - started < 1.5
    finally:
        blocker.rollback()
        blocker.close()
        if not task.done():
            await asyncio.wait((task,), timeout=2)
    # Its connection-level timeout must not change unrelated writers after pool return.
    async with get_engine().connect() as connection:
        assert await connection.scalar(text("PRAGMA busy_timeout")) == 30_000
    assert await obd_events.store_event_snapshot(snapshot(2)) == (1, 1, 0)


async def test_cancelling_a_contended_event_write_settles_and_restores_pool_policy(
    db_session, app_config
):
    blocker = sqlite3.connect(app_config.db_path, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    engine = get_engine()
    started = asyncio.Event()

    def observe(_connection, _cursor, statement, _parameters, _context, _executemany):
        if statement == "BEGIN IMMEDIATE":
            started.set()

    event.listen(engine.sync_engine, "before_cursor_execute", observe)
    task = asyncio.create_task(obd_events.store_event_snapshot(snapshot(2)))
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", observe)
        blocker.rollback()
        blocker.close()
        if not task.done():
            await asyncio.wait((task,), timeout=2)
    async with engine.connect() as connection:
        assert await connection.scalar(text("PRAGMA busy_timeout")) == 30_000
        assert await connection.scalar(select(func.count(OBDLoggerEvent.id))) == 0


async def test_pruning_failure_rolls_back_inserts_and_retains_previous_events(
    db_session, monkeypatch
):
    await obd_events.store_event_snapshot(snapshot(2))
    monkeypatch.setattr(obd_events, "MAX_SERVER_EVENTS", 1)
    prune = obd_events._prune_server_events

    async def fail_after_pruning(connection):
        await prune(connection)
        raise RuntimeError("retention failed")

    monkeypatch.setattr(obd_events, "_prune_server_events", fail_after_pruning)
    with pytest.raises(RuntimeError, match="retention failed"):
        await obd_events.store_event_snapshot(snapshot(3))
    async with get_engine().connect() as connection:
        rows = await connection.scalars(
            select(OBDLoggerEvent.sequence).order_by(OBDLoggerEvent.sequence)
        )
        assert list(rows) == [1, 2]


async def test_retained_mirror_cleanup_and_skipped_attempts_are_visible_without_new_tasks(
    monkeypatch,
):
    status = obd_events.LoggerEventStatus()
    monkeypatch.setattr(obd_events, "_status", status)
    monkeypatch.setattr(puller, "_event_mirror_task", None)
    storing, cleanup_started, release_cleanup = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def read(*args):
        return "{}"

    async def stalled_store(_snapshot):
        storing.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            await release_cleanup.wait()

    monkeypatch.setattr(obd_events, "read_remote_event_snapshot", read)
    monkeypatch.setattr(obd_events, "validate_event_snapshot", lambda _raw: snapshot(1))
    monkeypatch.setattr(obd_events, "store_event_snapshot", stalled_store)
    task = puller._start_event_mirror("unit", "/safe/events.json")
    assert task is not None
    try:
        await asyncio.wait_for(storing.wait(), timeout=1)
        active = status.snapshot()
        assert active["sync_active"] is True
        assert active["sync_phase"] == "storing"
        assert active["sync_started_at"] is not None
        assert active["sync_age_s"] is not None
        assert active["checked_at"] is None
        await puller._await_event_mirror(task, deadline=asyncio.get_running_loop().time())
        await asyncio.wait_for(cleanup_started.wait(), timeout=1)
        assert status.snapshot()["sync_phase"] == "cleanup_pending"
        assert puller._start_event_mirror("unit", "/safe/events.json") is None
        puller._cancel_event_mirror(task)
        pending = status.snapshot()
        assert pending["sync_active"] is True
        assert pending["skipped_attempts"] == 1
        assert pending["last_skipped_at"] is not None
        assert pending["checked_at"] is None
        assert task.cancelling() == 1
        assert puller._event_mirror_task is task
    finally:
        release_cleanup.set()
        await asyncio.wait((task,), timeout=1)
        await asyncio.sleep(0)
    finished = status.snapshot()
    assert finished["sync_active"] is False
    assert finished["sync_phase"] == "idle"
    assert finished["sync_age_s"] is None
    assert finished["last_error"] == "cancelled"
    assert finished["checked_at"] is not None
    assert puller._event_mirror_task is None
