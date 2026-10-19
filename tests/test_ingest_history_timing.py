"""History retains the claimed run's start after live status is cleared."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.db.models import IngestRun
from app.ingest import puller
from app.ingest import status as status_module
from app.ingest.models import RunState, UnitInfo, UnitState


@pytest.mark.parametrize(
    ("ending", "expected_state"),
    [
        ("completed", RunState.UNAUTHORIZED),
        ("cancelled", RunState.CANCELLED),
        ("initialization_error", RunState.ERROR),
        ("initialization_cancelled", RunState.CANCELLED),
    ],
)
async def test_history_uses_original_start_for_completion_and_fallback(
    db_session, monkeypatch, tmp_path, ending, expected_state
):
    started = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
    finished = started + timedelta(minutes=7)

    class FixedClock(datetime):
        current = started

        @classmethod
        def now(cls, tz=None):
            return cls.current if tz is not None else cls.current.replace(tzinfo=None)

    monkeypatch.setattr(puller, "datetime", FixedClock)
    monkeypatch.setattr(status_module, "datetime", FixedClock)
    status_module.reset_status_for_tests()
    status = status_module.get_status()
    monkeypatch.setattr(puller, "_get", lambda key, default=None: key == "enabled")
    monkeypatch.setattr(puller, "report_event", AsyncMock())
    monkeypatch.setattr(puller, "_sleep_window_may_close", AsyncMock(return_value=False))
    monkeypatch.setattr(puller, "close_sleep_window", AsyncMock())

    async def footage_dir():
        assert status.snapshot()["started_at"] == started.isoformat()
        if ending.startswith("initialization_"):
            FixedClock.current = finished
            if ending == "initialization_cancelled":
                raise asyncio.CancelledError
            raise OSError("synthetic settings initialization failure")
        return tmp_path

    async def probe():
        assert status.snapshot()["started_at"] == started.isoformat()
        FixedClock.current = finished
        if ending == "cancelled":
            raise asyncio.CancelledError
        return UnitInfo("synthetic-unit:5555", UnitState.UNAUTHORIZED)

    monkeypatch.setattr(
        puller, "get_settings_service", lambda: SimpleNamespace(footage_dir=footage_dir)
    )
    monkeypatch.setattr(puller, "probe_unit", probe)
    try:
        if "cancelled" in ending:
            with pytest.raises(asyncio.CancelledError):
                await puller.run_pull(trigger="manual")
        else:
            result = await puller.run_pull(trigger="manual")
            assert result.state is expected_state

        assert not status.running
        assert status.snapshot()["started_at"] is None
        rows = list((await db_session.scalars(select(IngestRun))).all())
        assert len(rows) == 1, "normal and outer cleanup must not both persist the same run"
        row = rows[0]
        assert row.state == expected_state.value
        assert row.trigger == "manual"
        assert row.started_at.replace(tzinfo=UTC) == started
        assert row.finished_at.replace(tzinfo=UTC) == finished
        assert row.finished_at - row.started_at == timedelta(minutes=7)
    finally:
        status_module.reset_status_for_tests()


async def test_previous_history_write_cannot_finish_a_newer_claimed_run(
    db_session, monkeypatch, tmp_path
):
    from app.db import session as session_module

    started = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)

    class FixedClock(datetime):
        current = started

        @classmethod
        def now(cls, tz=None):
            return cls.current if tz is not None else cls.current.replace(tzinfo=None)

    monkeypatch.setattr(puller, "datetime", FixedClock)
    monkeypatch.setattr(status_module, "datetime", FixedClock)
    status_module.reset_status_for_tests()
    status = status_module.get_status()
    monkeypatch.setattr(puller, "_get", lambda key, default=None: key == "enabled")
    monkeypatch.setattr(puller, "report_event", AsyncMock())
    monkeypatch.setattr(puller, "_sleep_window_may_close", AsyncMock(return_value=False))
    monkeypatch.setattr(puller, "close_sleep_window", AsyncMock())
    monkeypatch.setattr(
        puller,
        "get_settings_service",
        lambda: SimpleNamespace(footage_dir=AsyncMock(return_value=tmp_path)),
    )
    history_waiting, release_history = asyncio.Event(), asyncio.Event()
    second_running, release_second = asyncio.Event(), asyncio.Event()
    real_scope = session_module.session_scope
    opened = 0

    @asynccontextmanager
    async def delayed_first_write():
        nonlocal opened
        opened += 1
        if opened == 1:
            history_waiting.set()
            await release_history.wait()
        async with real_scope() as session:
            yield session

    probes = 0

    async def probe():
        nonlocal probes
        probes += 1
        if probes == 1:
            FixedClock.current = started + timedelta(minutes=7)
        else:
            second_running.set()
            await release_second.wait()
            FixedClock.current = started + timedelta(minutes=12)
        return UnitInfo("synthetic-unit:5555", UnitState.UNAUTHORIZED)

    monkeypatch.setattr(session_module, "session_scope", delayed_first_write)
    monkeypatch.setattr(puller, "probe_unit", probe)
    first = asyncio.create_task(puller.run_pull(trigger="auto"))
    second = None
    try:
        await asyncio.wait_for(history_waiting.wait(), timeout=2)
        FixedClock.current = started + timedelta(minutes=8)
        second = asyncio.create_task(puller.run_pull(trigger="manual"))
        await asyncio.wait_for(second_running.wait(), timeout=2)
        release_history.set()
        await asyncio.wait_for(first, timeout=2)
        assert status.running, "the old run's outer fallback must not finish its successor"
        assert status.snapshot()["started_at"] == (started + timedelta(minutes=8)).isoformat()
        release_second.set()
        await asyncio.wait_for(second, timeout=2)
        rows = list((await db_session.scalars(select(IngestRun).order_by(IngestRun.id))).all())
        assert len(rows) == 2
        assert [row.started_at.replace(tzinfo=UTC) for row in rows] == [
            started,
            started + timedelta(minutes=8),
        ]
        assert [row.finished_at.replace(tzinfo=UTC) for row in rows] == [
            started + timedelta(minutes=7),
            started + timedelta(minutes=12),
        ]
    finally:
        release_history.set()
        release_second.set()
        await asyncio.gather(
            *[task for task in (first, second) if task is not None], return_exceptions=True
        )
        status_module.reset_status_for_tests()
