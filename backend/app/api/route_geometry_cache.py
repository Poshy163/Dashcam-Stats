"""Build the geometry stored for one complete journey in the route overlay."""

from __future__ import annotations

from datetime import datetime

from app.api.geometry import build_polylines

RouteFix = tuple[float, float, str | None, int, bool]
Lines = list[list[tuple[float, float]]]

_UTC_EPOCH_NAIVE = datetime(1970, 1, 1)


def simplify_journey(rows: list[RouteFix], tolerance_m: float) -> Lines:
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
