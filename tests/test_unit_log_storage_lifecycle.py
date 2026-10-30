"""Large log imports must preserve evidence without pinning SQLite one await per line."""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError

from app.db.models import UnitLogEntry, utcnow
from app.db.session import get_engine, session_scope
from app.ingest import unit_logs


def line(index, **changes):
    values = {
        "occurred_at": utcnow(),
        "pid": 1,
        "tid": 2,
        "level": "E",
        "tag": "Vendor",
        "message": f"line-{index}",
    }
    return unit_logs.ParsedLine(**(values | changes))


async def test_large_import_uses_bounded_executemany_and_retains_every_distinct_line(db_session):
    entries = [line(index) for index in range(1_001)]
    inserts = []

    def observe(_connection, _cursor, statement, parameters, _context, executemany):
        if statement.startswith("INSERT INTO unit_log_entries"):
            inserts.append((len(parameters), executemany))

    engine = get_engine()
    event.listen(engine.sync_engine, "before_cursor_execute", observe)
    try:
        assert await unit_logs.store(entries) == (1_001, 0)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", observe)
    assert len(inserts) == 5, "a 1,001-line capture must not make 1,001 awaited INSERTs"
    assert all(count <= 250 for count, _ in inserts)
    assert all(many for _, many in inserts[:-1])
    assert await unit_logs.store(entries) == (0, 1_001)
    async with session_scope() as session:
        saved = (await session.scalars(select(UnitLogEntry).order_by(UnitLogEntry.id))).all()
    assert [(row.line_hash, row.message) for row in saved] == [
        (entry.line_hash, entry.message) for entry in entries
    ]


async def test_prefix_enrichment_preserves_input_order_and_original_metadata_across_batches(
    db_session,
):
    short = line(0, tag="CarPlayTiming", message="sample=batch-boundary event=started")
    longer = replace(short, pid=99, tid=100, message=short.message + " elapsed_ms=100")
    conflict = replace(longer, message=longer.message.replace("started", "different"))
    entries = [line(index) for index in range(249)] + [short, longer, conflict, short]
    assert await unit_logs.store(entries) == (251, 2)
    async with session_scope() as session:
        original = await session.scalar(
            select(UnitLogEntry).where(UnitLogEntry.line_hash == short.line_hash)
        )
        before = (
            original.id,
            original.occurred_at,
            original.received_at,
            original.pid,
            original.tid,
            original.level,
            original.tag,
            original.line_hash,
        )
        assert original.message == longer.message
    complete = replace(
        longer, occurred_at=utcnow() + timedelta(days=1), message=longer.message + " complete=1"
    )
    assert await unit_logs.store([complete]) == (1, 0)
    async with session_scope() as session:
        saved = await session.get(UnitLogEntry, before[0])
        assert (
            saved.id,
            saved.occurred_at,
            saved.received_at,
            saved.pid,
            saved.tid,
            saved.level,
            saved.tag,
            saved.line_hash,
        ) == before
        assert saved.message == complete.message


async def test_prune_failure_rolls_back_new_rows_and_keeps_original_log_evidence(
    db_session, monkeypatch
):
    original = line(0)
    await unit_logs.store([original])
    prune = unit_logs._prune
    monkeypatch.setattr(unit_logs, "MAX_SERVER_ROWS", 1)

    async def fail_after_prune(session):
        await prune(session)
        raise RuntimeError("prune failed")

    monkeypatch.setattr(unit_logs, "_prune", fail_after_prune)
    with pytest.raises(RuntimeError, match="prune failed"):
        await unit_logs.store([line(1)])
    async with session_scope() as session:
        saved = (await session.scalars(select(UnitLogEntry))).all()
    assert [(row.line_hash, row.message) for row in saved] == [
        (original.line_hash, original.message)
    ]


async def test_retention_subquery_handles_more_than_999_victims_without_bind_growth(
    db_session, monkeypatch
):
    await unit_logs.store([line(index) for index in range(1_001)])
    monkeypatch.setattr(unit_logs, "MAX_SERVER_ROWS", 1)
    counts = []

    def observe(_connection, _cursor, statement, parameters, _context, _executemany):
        if statement.startswith("DELETE FROM unit_log_entries"):
            counts.append(len(parameters))

    engine = get_engine()
    event.listen(engine.sync_engine, "before_cursor_execute", observe)
    try:
        assert await unit_logs.store([]) == (0, 0)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", observe)
    assert max(counts) < 10
    async with session_scope() as session:
        assert len((await session.scalars(select(UnitLogEntry))).all()) == 1


@pytest.fixture
def isolated_tasks(monkeypatch):
    monkeypatch.setattr(unit_logs, "_enabled", lambda: True)
    monkeypatch.setattr(unit_logs, "_tasks", set())
    monkeypatch.setattr(unit_logs, "_last_refresh", {})
    monkeypatch.setattr(unit_logs, "_tasks_by_address", {})
    monkeypatch.setattr(unit_logs, "_pending_arrivals", set())
    monkeypatch.setattr(unit_logs, "_arrival_requests", {})
    monkeypatch.setattr(unit_logs, "_needs_collection", set())
    monkeypatch.setattr(unit_logs, "_shutting_down", False)


@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError("private captured log content"),
        OperationalError(
            "INSERT",
            {"message": "private captured log content"},
            sqlite3.OperationalError("database is locked"),
        ),
    ],
)
async def test_background_failure_is_retrieved_and_reports_no_log_content(
    monkeypatch, isolated_tasks, failure
):
    reports = []
    monkeypatch.setattr(
        unit_logs,
        "log",
        SimpleNamespace(warning=lambda message, **fields: reports.append((message, fields))),
    )
    monkeypatch.setattr(unit_logs, "refresh", AsyncMock(side_effect=failure))
    unit_logs.on_unit_present("unit")
    [task] = list(unit_logs._tasks)
    await asyncio.wait((task,), timeout=1)
    await asyncio.sleep(0)
    assert not task._log_traceback, "the done callback must retrieve a background exception"
    assert not unit_logs._tasks
    assert len(reports) == 1
    assert reports[0][1]["error_type"] == type(failure).__name__
    assert reports[0][1]["category"] == (
        "storage_busy" if isinstance(failure, OperationalError) else "internal_error"
    )
    assert "private" not in str(reports)


async def test_slow_refresh_cannot_accumulate_tasks_or_lose_a_queued_arrival(
    monkeypatch, isolated_tasks
):
    release = asyncio.Event()
    started = asyncio.Event()

    async def blocked(_address):
        started.set()
        await release.wait()

    refresh = AsyncMock(side_effect=blocked)
    arrival = AsyncMock()
    monkeypatch.setattr(unit_logs, "refresh", refresh)
    monkeypatch.setattr(unit_logs, "_collect_then_arm", arrival)
    monkeypatch.setattr(unit_logs, "REFRESH_S", 0)
    unit_logs.on_unit_present("unit")
    await asyncio.wait_for(started.wait(), timeout=1)
    for _ in range(5):
        unit_logs.on_unit_present("unit")
        unit_logs.on_unit_seen("unit")
    assert len(unit_logs._tasks) == 1
    assert arrival.await_count == 0
    release.set()
    for _ in range(4):
        await asyncio.sleep(0)
    assert refresh.await_count == arrival.await_count == 1
    await unit_logs.shutdown()


async def test_repeated_held_arrival_ticks_do_not_reconcile_or_restart_capture(
    monkeypatch, isolated_tasks
):
    collect, arm = AsyncMock(return_value=(1, 0)), AsyncMock(return_value=True)
    monkeypatch.setattr(unit_logs, "collect", collect)
    monkeypatch.setattr(unit_logs, "arm", arm)
    visit = object()
    for _ in range(5):
        unit_logs.on_unit_seen("unit", visit_token=visit)
        if unit_logs._tasks:
            await asyncio.wait(tuple(unit_logs._tasks), timeout=1)
        await asyncio.sleep(0)
    assert collect.await_count == arm.await_count == 1
    await unit_logs.shutdown()


async def test_inflight_arrival_coalesces_held_ticks_and_preserves_latest_new_visit(
    monkeypatch, isolated_tasks
):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def collect(_address):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
        return 1, 0

    arm = AsyncMock(return_value=True)
    monkeypatch.setattr(unit_logs, "collect", collect)
    monkeypatch.setattr(unit_logs, "arm", arm)
    initial, middle, latest = object(), object(), object()
    unit_logs.on_unit_seen("unit", visit_token=initial)
    await asyncio.wait_for(entered.wait(), timeout=1)
    for _ in range(5):
        unit_logs.on_unit_seen("unit", visit_token=initial)
    assert not unit_logs._pending_arrivals
    for visit in (middle, latest, latest):
        unit_logs.on_unit_seen("unit", visit_token=visit)
    assert len(unit_logs._tasks) == 1
    assert unit_logs._pending_arrivals == {"unit"}
    assert unit_logs._arrival_requests["unit"][0] is latest
    release.set()
    for _ in range(8):
        await asyncio.sleep(0)
    assert calls == arm.await_count == 2
    assert not unit_logs._tasks and not unit_logs._pending_arrivals
    unit_logs.on_unit_seen("unit", visit_token=latest)
    assert not unit_logs._tasks
    await unit_logs.shutdown()


async def test_quick_reconnect_policy_change_and_distinct_addresses_are_not_suppressed(
    monkeypatch, isolated_tasks
):
    collect, arm = AsyncMock(return_value=(1, 0)), AsyncMock(return_value=True)
    levels = {"WindowManager": "F"}
    monkeypatch.setattr(unit_logs, "collect", collect)
    monkeypatch.setattr(unit_logs, "arm", arm)
    monkeypatch.setattr(unit_logs, "writer_levels", lambda: levels)
    monkeypatch.setattr(unit_logs, "capture_command", lambda: "capture")

    async def arrival(address, visit):
        unit_logs.on_unit_seen(address, visit_token=visit)
        await asyncio.wait(tuple(unit_logs._tasks), timeout=1)
        await asyncio.sleep(0)

    first, second = object(), object()
    await arrival("unit", first)
    await arrival("unit", second)  # No elapsed debounce period or delay is required.
    levels["WindowManager"] = "E"
    await arrival("unit", second)
    await arrival("other-unit", second)
    assert [call.args[0] for call in arm.await_args_list] == ["unit", "unit", "unit", "other-unit"]
    assert collect.await_count == 4
    await unit_logs.shutdown()


@pytest.mark.parametrize("failure", ["collect", "arm"])
async def test_failed_arrival_retries_on_bounded_presence_without_repeated_held_rearms(
    monkeypatch, isolated_tasks, failure
):
    now = [1000.0]
    collect = AsyncMock(
        side_effect=[RuntimeError("import failed"), (1, 0)] if failure == "collect" else None,
        return_value=(1, 0),
    )
    arm = AsyncMock(side_effect=[False, True] if failure == "arm" else None, return_value=True)
    monkeypatch.setattr(unit_logs, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(unit_logs, "collect", collect)
    monkeypatch.setattr(unit_logs, "arm", arm)
    visit = object()
    unit_logs.on_unit_seen("unit", visit_token=visit)
    await asyncio.wait(tuple(unit_logs._tasks), timeout=1)
    await asyncio.sleep(0)
    assert "unit" in unit_logs._needs_collection
    for _ in range(5):
        unit_logs.on_unit_seen("unit", visit_token=visit)
    assert not unit_logs._tasks
    now[0] += unit_logs.REFRESH_S - 1
    unit_logs.on_unit_seen("unit", visit_token=visit)
    assert not unit_logs._tasks
    now[0] += 1
    unit_logs.on_unit_seen("unit", visit_token=visit)
    assert unit_logs._tasks
    await asyncio.wait(tuple(unit_logs._tasks), timeout=1)
    await asyncio.sleep(0)
    assert collect.await_count == 2
    assert arm.await_count == (1 if failure == "collect" else 2)
    assert "unit" not in unit_logs._needs_collection
    await unit_logs.shutdown()


async def test_invalid_policy_fingerprint_does_not_fail_the_poller(monkeypatch, isolated_tasks):
    def invalid_levels():
        raise ValueError("too many managed unit-log tags")

    monkeypatch.setattr(unit_logs, "writer_levels", invalid_levels)
    monkeypatch.setattr(unit_logs, "collect", AsyncMock(return_value=(0, 0)))
    monkeypatch.setattr(unit_logs, "arm", AsyncMock(return_value=False))
    unit_logs.on_unit_seen("unit", visit_token=object())
    await asyncio.wait(tuple(unit_logs._tasks), timeout=1)
    await asyncio.sleep(0)
    assert "unit" in unit_logs._needs_collection
    await unit_logs.shutdown()


async def test_shutdown_cancels_owned_work_without_starting_queued_arrival(
    monkeypatch, isolated_tasks
):
    started = asyncio.Event()

    async def blocked(_address):
        started.set()
        await asyncio.Event().wait()

    arrival = AsyncMock()
    monkeypatch.setattr(unit_logs, "refresh", blocked)
    monkeypatch.setattr(unit_logs, "_collect_then_arm", arrival)
    unit_logs.on_unit_present("unit")
    await asyncio.wait_for(started.wait(), timeout=1)
    unit_logs.on_unit_seen("unit")
    await unit_logs.shutdown()
    assert not unit_logs._tasks
    arrival.assert_not_awaited()


@pytest.mark.parametrize("failure", ["database", "missing_eof", "adb"])
async def test_failed_consuming_import_never_acknowledges_or_rearms_source(
    monkeypatch, isolated_tasks, failure
):
    commands = []

    async def shell(_address, command, **_kwargs):
        commands.append(command)
        if failure == "adb":
            raise unit_logs.adb.AdbError("device unavailable")
        raw = "2026-10-19 00:00:00.000 +0000 1 2 E Vendor: retained source"
        return raw if failure == "missing_eof" else raw + "\n" + unit_logs._READ_COMPLETE

    store = AsyncMock(side_effect=RuntimeError("database unavailable"))
    arm = AsyncMock(return_value=True)
    monkeypatch.setattr(unit_logs.adb, "shell", shell)
    monkeypatch.setattr(unit_logs, "store", store)
    monkeypatch.setattr(unit_logs, "arm", arm)
    with pytest.raises(RuntimeError):
        await unit_logs._collect_then_arm("unit")
    assert len(commands) == 1
    assert "rm -f" not in commands[0]
    arm.assert_not_awaited()
    assert "unit" in unit_logs._needs_collection
    if failure != "database":
        store.assert_not_awaited()


async def test_rearm_acknowledges_only_after_the_host_import_is_committed(
    db_session, monkeypatch, isolated_tasks
):
    actions = []
    stamp = utcnow().strftime("%Y-%m-%d %H:%M:%S")
    raw = f"{stamp}.000 +0000 1 2 E Vendor: committed before acknowledgement"

    async def shell(_address, command, **_kwargs):
        if 'cat "$unitlog_file"' in command:
            actions.append("read")
            assert "rm -f" not in command
            return raw + "\n" + unit_logs._READ_COMPLETE
        actions.append("rearm")
        assert "rm -f" in command
        async with session_scope() as session:
            saved = (await session.scalars(select(UnitLogEntry))).all()
        assert [row.message for row in saved] == ["committed before acknowledgement"]
        return ""

    monkeypatch.setattr(unit_logs.adb, "shell", shell)
    monkeypatch.setattr(unit_logs, "ensure_logging", AsyncMock(return_value=True))
    await unit_logs._collect_then_arm("unit")
    assert actions == ["read", "rearm"]
    assert "unit" not in unit_logs._needs_collection


async def test_failed_rearm_is_retried_on_later_presence_without_losing_collection(
    monkeypatch, isolated_tasks
):
    collect = AsyncMock(return_value=(1, 0))
    arm = AsyncMock(side_effect=[False, True])
    monkeypatch.setattr(unit_logs, "collect", collect)
    monkeypatch.setattr(unit_logs, "arm", arm)
    monkeypatch.setattr(unit_logs, "REFRESH_S", 0)
    await unit_logs._collect_then_arm("unit")
    assert "unit" in unit_logs._needs_collection
    unit_logs.on_unit_present("unit")
    await asyncio.wait(tuple(unit_logs._tasks), timeout=1)
    await asyncio.sleep(0)
    assert collect.await_count == arm.await_count == 2
    assert "unit" not in unit_logs._needs_collection


async def test_empty_completed_snapshot_can_be_acknowledged(monkeypatch, isolated_tasks):
    monkeypatch.setattr(unit_logs.adb, "shell", AsyncMock(return_value=unit_logs._READ_COMPLETE))
    arm = AsyncMock(return_value=True)
    monkeypatch.setattr(unit_logs, "arm", arm)
    assert await unit_logs.collect("unit") == (0, 0)
    await unit_logs._collect_then_arm("unit")
    arm.assert_awaited_once_with("unit")
