"""Reuse simplified journey lines when a new recording invalidates the whole map."""

from __future__ import annotations

import hashlib
import marshal
from collections import OrderedDict
from datetime import datetime

from app.api.geometry import build_polylines

RouteFix = tuple[float, float, str | None, int, bool]
Lines = list[list[tuple[float, float]]]

# A mostly stationary library can contain many journeys with no drawable lines. Bound
# both their entry count and the memory consumed by busy journeys with many vertices.
_MAX_JOURNEYS = 2048
_MAX_POINTS = 250_000
_UTC_EPOCH_NAIVE = datetime(1970, 1, 1)
_entries: OrderedDict[tuple[float, bytes], tuple[Lines, int]] = OrderedDict()
_points = 0


def clear() -> None:
    global _points
    _entries.clear()
    _points = 0


def _simplify(rows: list[RouteFix], tolerance_m: float) -> Lines:
    fixes = []
    breaks = []
    previous_recording = None
    for lat, lon, captured_at, recording_id, marked in rows:
        # UtcDateTime stores UTC in SQLite's timezone-less ISO format. Defer conversion
        # until a journey actually needs its gaps checked, and retain subsecond precision.
        seconds = None
        if captured_at is not None:
            instant = datetime.fromisoformat(captured_at)
            seconds = (
                (instant - _UTC_EPOCH_NAIVE).total_seconds()
                if instant.tzinfo is None
                else instant.timestamp()
            )
        fixes.append((lat, lon, seconds))
        # The extractor marks the first drawable fix in every clip. The journey view also
        # suppresses this sentinel at clip boundaries; real time/distance gaps still split.
        clip_start = previous_recording is not None and recording_id != previous_recording
        breaks.append(marked and not clip_start)
        previous_recording = recording_id
    return build_polylines(fixes, tolerance_m=tolerance_m, breaks=breaks)


def journey_lines(rows: list[RouteFix], tolerance_m: float, *, cacheable: bool) -> Lines:
    global _points
    if not cacheable:
        return _simplify(rows, tolerance_m)

    # Hash every ordered input, including clip boundaries and timestamps. A reanalysis
    # can change positions or breaks without changing row counts or the journey ID.
    # marshal's v2 encoding writes these primitive tuples by value, independent of how
    # Python happened to share objects in a cursor batch. Only the digest is retained.
    key = (tolerance_m, hashlib.sha256(marshal.dumps(rows, 2)).digest())
    hit = _entries.get(key)
    if hit is not None:
        _entries.move_to_end(key)
        return hit[0]

    lines = _simplify(rows, tolerance_m)
    count = sum(len(line) for line in lines)
    if count <= _MAX_POINTS:
        _entries[key] = (lines, count)
        _points += count
        while len(_entries) > _MAX_JOURNEYS or _points > _MAX_POINTS:
            _, (_, evicted_points) = _entries.popitem(last=False)
            _points -= evicted_points
    return lines
