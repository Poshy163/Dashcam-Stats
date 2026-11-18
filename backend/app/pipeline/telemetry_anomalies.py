"""Find sparse GPS reads contradicted by the overlay on both sides of the frame."""

from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import TelemetryPoint
from app.osd.geo import haversine_m
from app.osd.track_quality import MAX_GAP_S, reachable_radius_m

# Read one explicitly labelled, full-precision field even if the other one was lost.
# Keep spaces inside a number significant: guessing missing digits would manufacture
# evidence for a replay. Zero is ambiguous with the camera's no-satellite marker.
_AXIS = re.compile(r"([EN]):([+-]?\d{1,3}\.\d{4})(?![\d.])", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class _Sample:
    offset: float
    lat: float | None
    lon: float | None
    speed: float | None
    direct: bool
    damaged_gps: bool
    interpolated: bool
    raw: str | None


def _raw_axis_conflict(anchor: _Sample, neighbours: list[_Sample]) -> bool:
    if anchor.lat is None or anchor.lon is None:
        return False
    # An ordinary, independently supported track can have bad raw reads too.
    if sum(row.direct for row in neighbours) >= 3:
        return False

    before: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for row in neighbours:  # Sorted by offset, excluding the anchor itself.
        elapsed = row.offset - anchor.offset
        for match in _AXIS.finditer(row.raw or ""):
            axis = match[1].upper()
            value = float(match[2])
            if value == 0 or abs(value) > (90 if axis == "N" else 180):
                continue
            # Compare just the observed axis at the anchor's latitude, consistent with
            # the spacing of the overlay's longitude cells at this recording's location.
            if axis == "N":
                displacement = haversine_m(anchor.lat, anchor.lon, value, anchor.lon)
            else:
                displacement = haversine_m(anchor.lat, anchor.lon, anchor.lat, value)
            # The fault is a nearby false decimal coordinate. Repeated, grossly damaged
            # degree digits or a dropped hemisphere sign in the raw text are common.
            if not max(1_000.0, reachable_radius_m(elapsed)) < displacement <= 10_000.0:
                continue
            if elapsed < 0:
                before[axis].append((row.offset, value))
                continue
            for previous_offset, previous in before[axis]:
                if axis == "N":
                    gap = haversine_m(previous, anchor.lon, value, anchor.lon)
                else:
                    gap = haversine_m(anchor.lat, previous, anchor.lat, value)
                if gap <= reachable_radius_m(row.offset - previous_offset):
                    return True
    return False


def _damaged_bridge(clip: list[_Sample], offsets: list[float], direct: list[_Sample]) -> bool:
    """A largely unreadable overlay produced a moving line amongst low speed reads."""
    for start, end in pairwise(direct):
        if not (start.damaged_gps and end.damaged_gps):
            continue
        if start.speed is not None and end.speed is not None:
            continue
        if (start.speed or 0) > 5 or (end.speed or 0) > 5:
            continue
        elapsed = end.offset - start.offset
        if not 1 <= elapsed <= 15:
            continue
        if start.lat is None or end.lat is None or start.lon is None or end.lon is None:
            continue
        # The unaffected axis stays exactly on its original overlay grid cell.
        if (start.lat == end.lat) == (start.lon == end.lon):
            continue
        distance = haversine_m(start.lat, start.lon, end.lat, end.lon)
        implied_speed = 3.6 * distance / elapsed
        if distance < 75 or implied_speed < 25:
            continue
        interior = clip[bisect_right(offsets, start.offset) : bisect_left(offsets, end.offset)]
        if not any(point.interpolated for point in interior):
            continue
        # A straight road between unreadable frames also moves on one axis. Speed
        # corroborating the movement anywhere inside the bridge is enough to pass it.
        if any(
            point.speed is not None and point.speed >= implied_speed * 0.5 for point in interior
        ):
            continue
        nearby = clip[
            bisect_left(offsets, start.offset - 15) : bisect_right(offsets, end.offset + 15)
        ]
        if any(point.speed is not None and point.speed <= 5 for point in nearby):
            return True
    return False


async def suspect_telemetry_recording_ids(
    session: AsyncSession, recording_ids: Sequence[int]
) -> set[int]:
    """Return source IDs worth decoding again, without changing any stored results.

    Call with a bounded batch of IDs. The first query uses only map fields. Read the
    wider raw OCR rows only for clips with an accepted position whose speed was lost.
    Paired copies have their own provenance and are scheduled with their source.
    """
    if not recording_ids:
        return set()
    point = TelemetryPoint
    weak_sources = (
        select(point.recording_id)
        .where(
            point.recording_id.in_(recording_ids),
            point.has_fix.is_(True),
            point.lat.is_not(None),
            point.lon.is_not(None),
            func.coalesce(point.gps_quality, "valid") == "valid",
            point.speed_kmh.is_(None),
        )
        .distinct()
    )
    weak_ids = (await session.scalars(weak_sources)).all()
    if not weak_ids:
        return set()

    # One damaged JSON payload cannot abort the remaining backlog. Preserve historical
    # SQL/JSON nulls, but return a non-object sentinel for invalid JSON.
    provenance_json = case(
        (func.json_valid(func.coalesce(point.quality_json, "null")), point.quality_json),
        else_="[]",
    )
    rows = await session.execute(
        select(
            point.recording_id,
            point.t_offset_s,
            point.lat,
            point.lon,
            point.speed_kmh,
            point.has_fix,
            point.gps_quality,
            provenance_json,
            point.raw_text,
        )
        .where(point.recording_id.in_(weak_ids))
        .order_by(point.recording_id, point.t_offset_s)
    )
    clips: dict[int, list[_Sample]] = defaultdict(list)
    malformed: set[int] = set()
    for rid, offset, lat, lon, speed, has_fix, quality, provenance, raw in rows:
        if provenance is None:
            provenance = {}
        if not isinstance(provenance, dict):
            malformed.add(rid)
            continue
        source = provenance.get("gps_source")
        is_direct = has_fix and quality in (None, "valid") and source in (None, "direct")
        problems = provenance.get("problems")
        damaged = (
            provenance.get("ocr_status") == "partial"
            and isinstance(problems, list)
            and "GPS fields unreadable" in problems
        )
        interpolated = source == "interpolated" and quality == "interpolated"
        clips[rid].append(
            _Sample(
                offset,
                lat,
                lon,
                speed,
                is_direct,
                damaged,
                interpolated,
                raw if isinstance(raw, str) else None,
            )
        )

    suspects: set[int] = set()
    for rid, clip in clips.items():
        if rid in malformed:
            continue
        offsets = [row.offset for row in clip]
        direct = [row for row in clip if row.direct]
        if _damaged_bridge(clip, offsets, direct):
            suspects.add(rid)
            continue
        if len(direct) * 10 > len(clip):
            continue
        for anchor in direct:
            if anchor.speed is not None:
                continue
            lo = bisect_left(offsets, anchor.offset - MAX_GAP_S)
            hi = bisect_right(offsets, anchor.offset + MAX_GAP_S)
            neighbours = [row for row in clip[lo:hi] if row.offset != anchor.offset]
            if _raw_axis_conflict(anchor, neighbours):
                suspects.add(rid)
                break
    return suspects
