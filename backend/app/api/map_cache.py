"""Memoisation for the map aggregates, keyed on what they are computed from.

``/api/map/routes`` walks every drawable fix in the library and simplifies it journey by
journey. On a library of a couple of hundred thousand fixes that is twenty-odd seconds of
single-writer SQLite time, and the Map page asks for it on every visit -- as does the
shell's post-job refresh, every thirty seconds, for as long as anything is in the queue.
The answer only changes when telemetry, journeys or recording revisions do, so it is kept
until one of them does.

A fingerprint rather than a TTL. A TTL either serves stale lines for a while after a
rebuild or expires during the hours in which nothing changed, and the thing being avoided
is the twenty-second query, not a stale picture. Counting rows and reading the newest id
and update time is a handful of index lookups, which next to the query is free.

Process-local and bounded. There is one backend process, so this is the only copy that
matters, and the entry count is capped so a date picker cannot grow it without limit.

The dashboard's heaviest totals (``/api/status``) use the same store with their own, much
cheaper stamp: that endpoint is polled every ten seconds, and counting a million-row
table to decide whether a count of it is still good costs as much as the count. See
``_recording_rollup`` in the system routes.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.visibility import visible_revision
from app.db.models import Journey, Recording, TelemetryPoint

T = TypeVar("T")

#: Distinct parameter combinations kept. Beyond this the least recently used is dropped.
MAX_ENTRIES = 64

Fingerprint = tuple[Any, ...]

_entries: OrderedDict[tuple, tuple[Fingerprint, Any]] = OrderedDict()
_locks: dict[tuple, asyncio.Lock] = {}


async def fingerprint(session: AsyncSession) -> Fingerprint:
    """What the map queries depend on, reduced to a few scalars.

    * Telemetry rows: count and newest id. Inserts and deletes (including the cascade
      from a retention delete) move one or both.
    * Journeys: count, newest id and latest ``updated_at``. A rebuild replaces the rows;
      a split, merge or motion re-check updates one in place.
    * Recordings: the latest ``processed_at`` and *which* recordings carry a current
      telemetry revision. Reprocessing and invalidation each move one of these without
      touching the telemetry table at all.

    "Which" is how many there are and the sum of their ids. The count alone cannot tell
    one recording being hidden from another being restored in the same interval -- same
    number, different lines on the map -- and nothing else here moves when that happens.
    Ids are distinct, so a one-for-one swap always moves their sum.
    """
    row = (
        await session.execute(
            select(
                func.count(TelemetryPoint.id),
                func.max(TelemetryPoint.id),
                select(func.count(Journey.id)).scalar_subquery(),
                select(func.max(Journey.id)).scalar_subquery(),
                select(func.max(Journey.updated_at)).scalar_subquery(),
                select(func.max(Recording.processed_at)).scalar_subquery(),
            )
        )
    ).one()
    visible = (
        await session.execute(
            select(func.count(Recording.id), func.coalesce(func.sum(Recording.id), 0)).where(
                visible_revision(Recording.telemetry_revision)
            )
        )
    ).one()
    return (*row, *visible)


async def cached(
    session: AsyncSession,
    key: tuple,
    compute: Callable[[], Awaitable[T]],
    *,
    fingerprint_of: Callable[[AsyncSession], Awaitable[Fingerprint]] = fingerprint,
) -> T:
    """Return the memoised value for ``key`` if the library is unchanged, else recompute.

    ``fingerprint_of`` names what the value depends on; the default is the map's.
    """
    return await cached_for(key, await fingerprint_of(session), compute)


async def cached_for(key: tuple, stamp: Fingerprint, compute: Callable[[], Awaitable[T]]) -> T:
    """:func:`cached` for a caller that already holds the stamp.

    Concurrent requests for the same key wait for the first to finish rather than each
    running the query -- two tabs opening the Map page should cost one computation.
    """
    hit = _lookup(key, stamp)
    if hit is not None:
        return hit[0]

    lock = _locks.setdefault(key, asyncio.Lock())
    try:
        async with lock:
            hit = _lookup(key, stamp)
            if hit is not None:
                return hit[0]
            value = await compute()
            _entries[key] = (stamp, value)
            _entries.move_to_end(key)
            while len(_entries) > MAX_ENTRIES:
                _entries.popitem(last=False)
            return value
    finally:
        if not lock.locked() and _locks.get(key) is lock:
            del _locks[key]


def _lookup(key: tuple, stamp: Fingerprint) -> tuple[Any] | None:
    entry = _entries.get(key)
    if entry is None or entry[0] != stamp:
        return None
    _entries.move_to_end(key)
    return (entry[1],)


def clear() -> None:
    """Forget everything. For tests, and for anything that swaps the database underneath."""
    _entries.clear()
    _locks.clear()


def size() -> int:
    return len(_entries)
