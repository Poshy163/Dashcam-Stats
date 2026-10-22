"""Bounded temperature observations recovered from existing device diagnostic logs.

This projection does not probe the camera or establish continuous capture. Historical
sampler heads identify the SoC sensor by the sampler's fixed source, not by a per-line
sensor field. Direct context/idle heads take precedence over their copied frame heads.
"""

from __future__ import annotations

import hashlib
import math
import re
from bisect import bisect_left
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

MAX_INPUT_ROWS = 20_000
MAX_OUTPUT_POINTS = 1_500
MAX_CONTEXT_AGE_MS = 120_000
GAP_THRESHOLD_S = 90
BUCKET_SECONDS = {1: 15, 6: 60, 24: 300, 168: 1800}
_FIELD = re.compile(r"(?:^|\s)([A-Za-z0-9_]+)=([^\s]*)")
_SESSION = re.compile(r"[A-Za-z0-9_-]{1,96}\Z")
_VOLATILE = {"sample", "context_age_ms", "frame_poll_gap_ms"}


@dataclass(frozen=True, slots=True)
class Observation:
    occurred_at: datetime
    soc_c: float | None
    session_id: str | None
    quality: str
    signature: str
    copied: bool

    def public(self) -> dict[str, Any]:
        return {
            "occurred_at": self.occurred_at,
            "soc_c": self.soc_c,
            "session_id": self.session_id,
            "quality": self.quality,
        }


def parse_observation(occurred_at: datetime, message: str) -> Observation | None:
    """Read only a sampler head; unrelated events and other sensors are not readings."""
    if " | " not in message:
        return None
    head, tail = message.split(" | ", 1)
    pairs = _FIELD.findall(head)
    fields = dict(pairs)
    if "soc" not in fields:
        return None
    if any(
        fields.get(key, "soc-thmzone") != "soc-thmzone"
        for key in ("sensor", "soc_sensor", "temperature_sensor")
    ):
        return None
    raw = fields["soc"]
    quality = "unavailable" if raw == "na" else "valid"
    value = None
    if quality == "valid":
        try:
            number = float(raw)
            # Telemetry bounds are independent of the decoder's admission/stop limits.
            if not math.isfinite(number) or not -40 <= number <= 200:
                raise ValueError
            value = number
        except ValueError:
            quality = "invalid"
    duplicates = {key for key, count in Counter(key for key, _value in pairs).items() if count > 1}
    if duplicates & {
        "soc",
        "session",
        "sensor",
        "soc_sensor",
        "temperature_sensor",
        "context_age_ms",
    }:
        quality, value = "invalid", None
    session = fields.get("session")
    session = session if session is not None and _SESSION.fullmatch(session) else None
    captured = occurred_at.astimezone(UTC)
    if "context_age_ms" in fields:
        age = fields["context_age_ms"]
        if not age.isascii() or not age.isdigit() or len(age) > 9 or int(age) > MAX_CONTEXT_AGE_MS:
            quality, value = "invalid", None
        else:
            captured -= timedelta(milliseconds=int(age))
    signature = hashlib.sha256(
        repr(
            (quality, sorted((key, text) for key, text in fields.items() if key not in _VOLATILE))
        ).encode()
    ).hexdigest()
    return Observation(
        captured,
        value,
        session,
        quality,
        signature,
        "context_age_ms" in fields or tail.startswith("layer=") or tail == "no video surface",
    )


def _unique(observations: list[Observation]) -> list[Observation]:
    primary: dict[str, list[Observation]] = defaultdict(list)
    copies: dict[str, list[Observation]] = defaultdict(list)
    for observation in sorted(observations, key=lambda item: item.occurred_at):
        target = copies if observation.copied else primary
        previous = target[observation.signature][-1:]
        if previous and (observation.occurred_at - previous[0].occurred_at).total_seconds() <= 2:
            continue
        target[observation.signature].append(observation)
    result = [item for entries in primary.values() for item in entries]
    primary_times = {
        signature: [item.occurred_at.timestamp() for item in entries]
        for signature, entries in primary.items()
    }
    for signature, entries in copies.items():
        times = primary_times.get(signature, ())
        for item in entries:
            # One context is repeated on every surface and frame poll. Prefer its
            # original direct head, whose source timestamp may follow slow probes.
            # Frame age is captured before its probes, so probe/emit delay may move
            # the corrected copy to either side of the direct report. Legacy heads
            # have no temperature-read ID: conservatively prefer nearby originals.
            index = bisect_left(times, item.occurred_at.timestamp() - 120)
            if index < len(times) and times[index] <= item.occurred_at.timestamp() + 120:
                continue
            result.append(item)
    return sorted(result, key=lambda item: (item.occurred_at, item.session_id or ""))


def history(
    rows: list[Any],
    *,
    hours: int,
    range_start: datetime,
    range_end: datetime,
    input_truncated: bool = False,
) -> dict[str, Any]:
    observations = _unique(
        [
            item
            for row in rows
            if (item := parse_observation(row.occurred_at, row.message)) is not None
        ]
    )
    observations = [item for item in observations if range_start <= item.occurred_at <= range_end]
    valid = [item for item in observations if item.soc_c is not None]
    minimum = min(valid, key=lambda item: item.soc_c) if valid else None
    maximum = max(valid, key=lambda item: item.soc_c) if valid else None
    latest = observations[-1] if observations else None
    bucket_s = BUCKET_SECONDS[hours]
    buckets: dict[tuple[int, int], list[Observation]] = defaultdict(list)
    previous = None
    segment = 0
    for item in observations:
        if previous is not None and (
            item.session_id is None
            or item.session_id != previous.session_id
            or item.soc_c is None
            or previous.soc_c is None
            or (item.occurred_at - previous.occurred_at).total_seconds() > GAP_THRESHOLD_S
        ):
            segment += 1
        buckets[(int(item.occurred_at.timestamp()) // bucket_s, segment)].append(item)
        previous = item
    points: list[dict[str, Any]] = []
    for (_bucket, segment), items in buckets.items():
        values = [item for item in items if item.soc_c is not None]
        chosen = {}
        if values:
            chosen[items.index(min(values, key=lambda item: item.soc_c))] = "min"
            chosen[items.index(max(values, key=lambda item: item.soc_c))] = "max"
        chosen[len(items) - 1] = "last"
        for index, kind in sorted(chosen.items()):
            points.append(
                {
                    **items[index].public(),
                    "segment_id": f"segment-{segment}",
                    "samples": len(items),
                    "kind": kind,
                }
            )
    points.sort(key=lambda point: point["occurred_at"])
    output_truncated = len(points) > MAX_OUTPUT_POINTS
    if output_truncated:
        # Retain global extremes even if they precede the retained recent tail.
        extremes = []
        for item in (minimum, maximum):
            representative = next(
                (
                    point
                    for point in points
                    if item is not None
                    and point["occurred_at"] == item.occurred_at
                    and point["soc_c"] == item.soc_c
                    and point["session_id"] == item.session_id
                ),
                None,
            )
            if representative is not None and representative not in extremes:
                extremes.append(representative)
        remaining = max(0, MAX_OUTPUT_POINTS - len(extremes))
        tail = points[-remaining:] if remaining else []
        points = sorted(
            [*extremes, *(point for point in tail if point not in extremes)],
            key=lambda point: point["occurred_at"],
        )
    return {
        "source": "retained_carplay_timing",
        "sensor": "soc-thmzone",
        "hours": hours,
        "range_start": range_start,
        "range_end": range_end,
        "bucket_s": bucket_s,
        "gap_threshold_s": GAP_THRESHOLD_S,
        "latest": latest.public() if latest else None,
        "minimum_c": minimum.soc_c if minimum else None,
        "maximum_c": maximum.soc_c if maximum else None,
        "unique_observation_count": len(observations),
        "source_row_count": len(rows),
        "input_truncated": input_truncated,
        "output_truncated": output_truncated,
        "truncated": input_truncated or output_truncated,
        "points": points,
    }
