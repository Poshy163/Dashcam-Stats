"""Coordinate in-process recording summaries until their journey membership is committed."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from weakref import WeakKeyDictionary

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings_service import get_settings_service
from app.db.models import Journey, Recording


@dataclass
class _SummaryWindow:
    start: datetime
    end: datetime
    finished: asyncio.Event


_active: WeakKeyDictionary[asyncio.AbstractEventLoop, list[_SummaryWindow]] = WeakKeyDictionary()


@asynccontextmanager
async def summary_turn(session: AsyncSession, recording: Recording):
    start = recording.started_at
    if start is None:
        yield
        return

    gap = timedelta(minutes=float(get_settings_service().get_nowait("journeys.gap_minutes")))
    # Include possible membership for an existing long journey, or for paired or
    # adjoining clips that do not yet have a journey id.
    candidates = await session.execute(
        select(Journey.started_at, Journey.ended_at).where(
            Journey.started_at <= start + gap, Journey.ended_at >= start - gap
        )
    )
    end = recording.ended_at or start
    for journey_start, journey_end in candidates:
        start = min(start, journey_start)
        end = max(end, journey_end)
    window = _SummaryWindow(start, end + gap, asyncio.Event())
    # All preceding pipeline stages have committed. End this lookup's read transaction
    # before waiting, so a waiter can see its predecessor's newly attached recording.
    await session.commit()
    active = _active.setdefault(asyncio.get_running_loop(), [])
    while predecessor := next(
        (w for w in active if w.start <= window.end and window.start <= w.end), None
    ):
        await predecessor.finished.wait()
    # Check and register without yielding to another worker on this loop.
    active.append(window)
    try:
        yield
    finally:
        active.remove(window)
        window.finished.set()
