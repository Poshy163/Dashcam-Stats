"""Bounded source-view selection and exact, temporally independent plate agreement."""

from __future__ import annotations

import math
from dataclasses import dataclass
from fractions import Fraction

import numpy as np

from app.ai.normalise_au import normalise, patterns_for_region
from app.ai.plates import PlateReading, PlateVote

RECOGNITION_METHOD = "distinct-frames-v1"
MIN_FRAME_SEPARATION_S = 0.5


@dataclass(frozen=True, slots=True)
class VehicleView:
    offset_s: float
    bbox: tuple[float, float, float, float]
    confidence: float

    @property
    def score(self) -> float:
        x1, y1, x2, y2 = self.bbox
        width, height = x2 - x1, y2 - y1
        if width <= 0 or height <= 0:
            return 0.0
        clipped = x1 <= 0.003 or x2 >= 0.997 or y1 <= 0.003 or y2 >= 0.997
        return width * height * self.confidence * (0.15 if clipped else 1.0)


def select_vehicle_views(
    views: list[VehicleView], limit: int, *, baseline: VehicleView | None = None
) -> list[VehicleView]:
    """Prefer visible vehicles without spending every read on adjacent frames."""
    valid = [
        v
        for v in views
        if math.isfinite(v.offset_s)
        and v.offset_s >= 0
        and all(math.isfinite(c) and 0 <= c <= 1 for c in v.bbox)
        and v.score > 0
    ]
    # Historical JPEGs can retain evidence lost in a re-decode. Reserve one slot for
    # that view, including when a sparse detection lies just 0.25 seconds away. Those
    # nearby alternatives must not silently displace the saved comparison frame.
    selected: list[VehicleView] = (
        [baseline] if baseline is not None and baseline in valid and limit > 1 else []
    )
    for view in sorted(valid, key=lambda v: (-v.score, v.offset_s)):
        if all(abs(view.offset_s - other.offset_s) >= MIN_FRAME_SEPARATION_S for other in selected):
            selected.append(view)
            if len(selected) >= max(1, limit):
                break
    return sorted(selected, key=lambda v: v.offset_s)


def sampling_rate(views: list[VehicleView]) -> float:
    """Recover the stored sample clock instead of assuming the current FPS setting.

    Usual quarter-second and whole-second offsets need only 4/1 FPS. Nonstandard old
    clocks are bounded at 30 FPS; the caller uses the actual decoded timestamp.
    """
    rate = 1
    for view in views:
        rate = math.lcm(rate, Fraction(view.offset_s).limit_denominator(60).denominator)
        if rate >= 30:
            return 30.0
    return float(rate)


def temporal_vote(
    readings: list[PlateReading], *, region: str, min_confidence: float, store_unmatched: bool
) -> PlateVote | None:
    """Choose an observed registration, never synthesize characters from different plates.

    One timestamp contributes at most one vote per identity. Generic shapes require
    corroboration. A strong named single-frame read remains useful, but a material
    conflict must abstain instead of manufacturing certainty from a higher OCR score.
    """
    groups: dict[str, list[PlateReading]] = {}
    has_patterns = bool(patterns_for_region(region))
    for reading in readings:
        if not reading.usable:
            continue
        result = normalise(reading.raw_text, region=region)
        if has_patterns and not result.matched and not store_unmatched:
            continue
        named = bool(result.plausible_states) or not has_patterns or store_unmatched
        floor = max(min_confidence, 0.80 if named else 0.90)
        if reading.ocr_confidence < floor:
            continue
        group = groups.setdefault(result.normalised, [])
        duplicate_index = next(
            (
                index
                for index, r in enumerate(group)
                if abs(r.offset_s - reading.offset_s) < MIN_FRAME_SEPARATION_S
            ),
            None,
        )
        if duplicate_index is None:
            group.append(reading)
        elif reading.ocr_confidence > group[duplicate_index].ocr_confidence:
            group[duplicate_index] = reading
    if not groups:
        return None
    ranked = sorted(
        groups.items(),
        key=lambda item: (len(item[1]), sum(r.ocr_confidence for r in item[1])),
        reverse=True,
    )
    text, supporters = ranked[0]
    best = max(supporters, key=lambda r: r.ocr_confidence)
    result = normalise(best.raw_text, region=region)
    if len(supporters) == 1:
        named = bool(result.plausible_states) or not has_patterns or store_unmatched
        if not named or best.ocr_confidence < max(min_confidence, 0.92):
            return None
    if len(ranked) > 1:
        other = ranked[1][1]
        # Two near-equal character variants across a few frames are uncertain, even if
        # one has a 99% character score. Multiple boxes in one frame cannot break a tie.
        if len(supporters) <= 2 * len(other):
            return None
    return PlateVote(
        text=text,
        ocr_confidence=round(float(np.mean([r.ocr_confidence for r in supporters])), 4),
        detection_confidence=round(float(np.mean([r.detection_confidence for r in supporters])), 4),
        vote_count=len(supporters),
        best=best,
        alternatives=[(name, len(group)) for name, group in ranked[1:4]],
        supporting_offsets=sorted(r.offset_s for r in supporters),
    )
