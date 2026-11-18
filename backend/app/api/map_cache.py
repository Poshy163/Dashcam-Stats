"""Cache map layers by their database revision and dashboard counts by their own stamps."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar
from weakref import WeakValueDictionary

from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import journey_snapshot, map_snapshot

T = TypeVar("T")
MapLayer = TypeVar("MapLayer", bound=BaseModel)

MAX_ENTRIES = 64
Fingerprint = tuple[Any, ...]
_entries: OrderedDict[tuple, tuple[Fingerprint, Any]] = OrderedDict()
# A waiter holds a strong reference until it leaves the lock. This also covers keys
# that never get cached because a request failed or was cancelled.
_locks: WeakValueDictionary[tuple, asyncio.Lock] = WeakValueDictionary()
_REVISION = text("SELECT revision FROM map_source_state WHERE id = 1")


async def _revision(session: AsyncSession) -> bytes | None:
    revision = await session.scalar(_REVISION)
    return revision if isinstance(revision, bytes) and len(revision) == 16 else None


async def cached(
    session: AsyncSession,
    key: tuple,
    compute: Callable[[], Awaitable[MapLayer]],
    *,
    snapshot: type[MapLayer] | None = None,
) -> MapLayer:
    """Read a layer for the current revision. Only the default layers also persist on disk."""
    if session.get_bind().dialect.name != "sqlite":
        return await compute()

    async with _locks.setdefault(key, asyncio.Lock()):
        revision = await _revision(session)
        if revision is None:
            return await compute()

        stamp = (revision,)
        hit = _entries.get(key)
        if hit is not None and hit[0] == stamp:
            _entries.move_to_end(key)
            return hit[1]

        if snapshot is not None:
            saved = await asyncio.to_thread(map_snapshot.load, key, revision, snapshot)
            if saved is not None:
                _remember(key, stamp, saved)
                return saved

        value = await compute()
        # Ingest may commit between reading the revision and streaming the layer.
        # Return this request's result, but only publish it if both belong to one revision.
        if await _revision(session) == revision:
            _remember(key, stamp, value)
            if snapshot is not None:
                await asyncio.to_thread(map_snapshot.save, key, revision, value)
        return value


async def cached_for(key: tuple, stamp: Fingerprint, compute: Callable[[], Awaitable[T]]) -> T:
    """Memoise a dashboard total for a caller that already holds its source stamp."""
    async with _locks.setdefault(key, asyncio.Lock()):
        hit = _entries.get(key)
        if hit is not None and hit[0] == stamp:
            _entries.move_to_end(key)
            return hit[1]
        value = await compute()
        _remember(key, stamp, value)
        return value


def _remember(key: tuple, stamp: Fingerprint, value: Any) -> None:
    _entries[key] = (stamp, value)
    _entries.move_to_end(key)
    while len(_entries) > MAX_ENTRIES:
        _entries.popitem(last=False)


def clear() -> None:
    """Release process-local results and geometry."""
    _entries.clear()
    _locks.clear()
    journey_snapshot.clear()


def size() -> int:
    return len(_entries)
