"""Turning stored fixes into lines a map can draw.

Two operations, both shared by the journey view and the route overlay so the same drive
cannot be drawn differently depending on which page asked for it.

**Splitting comes before simplifying.** A route is not one unbroken line: the camera loses
its lock in tunnels and under cover, readings that fail validation are discarded, and a
journey stitches together clips that may have minutes between them. Joining the fix before
a gap to the fix after it draws a road that does not exist — a straight line through
whatever the vehicle actually went around. On a single journey that is a visible glitch; on
an overlay of every drive ever recorded it is a spray of false chords across the city, and
they are indistinguishable from real roads.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.osd.engine import haversine_m

#: Metres per degree of latitude. Longitude shrinks with latitude, but Douglas-Peucker
#: only needs a consistent scale to compare distances against, not a projection.
_METRES_PER_DEGREE = 111_320.0

#: Seconds between consecutive fixes beyond which the line is broken rather than joined.
#:
#: The overlay reports at 1 Hz, so anything past a few seconds is signal that was lost or
#: readings that were rejected. Ten is loose enough to ride out a brief dropout and tight
#: enough that a tunnel does not become a road.
DEFAULT_MAX_GAP_S = 10.0

#: Metres between consecutive fixes beyond which the line is broken. At 1 Hz this is
#: roughly 720 km/h, so it only ever catches a jump that is not travel.
DEFAULT_MAX_JUMP_M = 200.0


def simplify_indices(points: Sequence[tuple[float, float]], tolerance_m: float) -> list[int]:
    """Which points Douglas-Peucker keeps, as indices into *points*.

    Indices rather than points, because the caller often has more to say about a fix than
    where it was — which recording it came from and how far into it — and that has to
    survive simplification if a click on the map is to lead anywhere. See :func:`simplify`
    for the algorithm and why it is iterative.
    """
    if len(points) < 3 or tolerance_m <= 0:
        return list(range(len(points)))

    tolerance = tolerance_m / _METRES_PER_DEGREE
    keep = [False] * len(points)
    keep[0] = keep[-1] = True

    stack: list[tuple[int, int]] = [(0, len(points) - 1)]
    while stack:
        first, last = stack.pop()
        if last <= first + 1:
            continue

        ax, ay = points[first]
        bx, by = points[last]
        dx, dy = bx - ax, by - ay
        norm = (dx * dx + dy * dy) ** 0.5
        cross = bx * ay - by * ax

        worst_index, worst = first, -1.0
        for index in range(first + 1, last):
            px, py = points[index]
            if norm == 0:
                distance = ((px - ax) ** 2 + (py - ay) ** 2) ** 0.5
            else:
                distance = abs(dy * px - dx * py + cross) / norm
            if distance > worst:
                worst_index, worst = index, distance

        if worst > tolerance:
            keep[worst_index] = True
            stack.append((first, worst_index))
            stack.append((worst_index, last))

    return [index for index, kept in enumerate(keep) if kept]


def simplify(
    points: Sequence[tuple[float, float]], tolerance_m: float
) -> list[tuple[float, float]]:
    """Douglas-Peucker with the tolerance given in metres.

    A long journey is tens of thousands of 1 Hz fixes, and sending them all makes the map
    sluggish to draw a line that is identical at any zoom a person can read.

    Iterative rather than recursive. The textbook formulation recurses once per split, and
    on a trace that is nearly straight — a motorway, exactly the case that produces the
    most points — it degenerates towards one frame per point. A continuous twenty-minute
    drive is over a thousand fixes, which is close enough to CPython's default limit that
    a route overlay of a whole library would eventually raise ``RecursionError`` while
    rendering a map.
    """
    return [points[index] for index in simplify_indices(points, tolerance_m)]


def split_indices(
    samples: Sequence[tuple[float, float, float | None]],
    *,
    max_gap_s: float = DEFAULT_MAX_GAP_S,
    max_jump_m: float = DEFAULT_MAX_JUMP_M,
) -> list[list[int]]:
    """Runs of indices that should each be drawn as one unbroken line.

    See :func:`split_at_gaps`, which is this with the indices resolved to points.
    """
    runs: list[list[int]] = []
    current: list[int] = []
    previous: tuple[float, float, float | None] | None = None

    for index, sample in enumerate(samples):
        lat, lon, seconds = sample
        if previous is not None:
            prev_lat, prev_lon, prev_seconds = previous
            gapped = (
                seconds is not None
                and prev_seconds is not None
                and (seconds - prev_seconds) > max_gap_s
            )
            jumped = haversine_m(prev_lat, prev_lon, lat, lon) > max_jump_m
            if gapped or jumped:
                if len(current) > 1:
                    runs.append(current)
                current = []
        current.append(index)
        previous = sample

    if len(current) > 1:
        runs.append(current)
    return runs


def build_polyline_indices(
    samples: Sequence[tuple[float, float, float | None]],
    *,
    tolerance_m: float,
    max_gap_s: float = DEFAULT_MAX_GAP_S,
    max_jump_m: float = DEFAULT_MAX_JUMP_M,
) -> list[list[int]]:
    """:func:`build_polylines` as indices, for callers carrying more than coordinates."""
    lines: list[list[int]] = []
    for run in split_indices(samples, max_gap_s=max_gap_s, max_jump_m=max_jump_m):
        points = [(samples[i][0], samples[i][1]) for i in run]
        kept = [run[i] for i in simplify_indices(points, tolerance_m)]
        if len(kept) > 1:
            lines.append(kept)
    return lines


def split_at_gaps(
    samples: Sequence[tuple[float, float, float | None]],
    *,
    max_gap_s: float = DEFAULT_MAX_GAP_S,
    max_jump_m: float = DEFAULT_MAX_JUMP_M,
) -> list[list[tuple[float, float]]]:
    """Break an ordered fix sequence wherever the line should not be drawn.

    Each sample is ``(lat, lon, seconds)``, where ``seconds`` is a monotonic clock for the
    ordering and may be ``None`` when a reading carried a position but no timestamp. A
    missing clock is not treated as a gap on its own — the position was still good, and
    breaking the line there would punch a hole in a real route to no purpose — so distance
    is what decides in that case.
    """
    return [
        [(samples[i][0], samples[i][1]) for i in run]
        for run in split_indices(samples, max_gap_s=max_gap_s, max_jump_m=max_jump_m)
    ]


def build_polylines(
    samples: Sequence[tuple[float, float, float | None]],
    *,
    tolerance_m: float,
    max_gap_s: float = DEFAULT_MAX_GAP_S,
    max_jump_m: float = DEFAULT_MAX_JUMP_M,
) -> list[list[tuple[float, float]]]:
    """Ordered fixes to drawable lines: split first, then simplify each piece.

    The order is not interchangeable. Simplifying first lets Douglas-Peucker treat the two
    ends of a gap as collinear with everything between them and collapse a genuine dropout
    into a single straight segment, which is precisely the artefact splitting exists to
    prevent.
    """
    lines = []
    for segment in split_at_gaps(samples, max_gap_s=max_gap_s, max_jump_m=max_jump_m):
        simplified = simplify(segment, tolerance_m)
        if len(simplified) > 1:
            lines.append(simplified)
    return lines


__all__ = [
    "DEFAULT_MAX_GAP_S",
    "DEFAULT_MAX_JUMP_M",
    "build_polyline_indices",
    "build_polylines",
    "simplify",
    "simplify_indices",
    "split_at_gaps",
    "split_indices",
]
