"""Is this position consistent with the ones around it?

Every check that existed before this module asked about one sample, or about one *pair* of
samples. Both are blind to the failure that actually puts coordinates in the wrong place.

A per-point check asks "is this a legal coordinate", and a truncated OCR read always is.
The overlay prints ``E:138.6510``; a pixel gap swallows three digits and the parser sees
``E:138.6``, which is a perfectly ordinary longitude 4.7 km west of the drive.

A pair check asks "could the vehicle have got here from the previous fix", and that
question has no good answer when one of the two is the bad one. It cannot tell which end is
wrong, and whichever it blames it is right half the time. Worse, the previous version
answered it with a 400 km/h ceiling — which at the overlay's 1 Hz sampling licenses a
111 m step every second, and over a 15 s bracket licenses 1.6 km. Measured on this library
that ceiling let 84 impossible steps through while the genuinely impossible ones reached
20,857 km/h.

So the question here is posed against a *neighbourhood*: the median position of the samples
shortly before and shortly after, which a single bad reading cannot drag. A point is an
outlier when it disagrees with that median by more than the vehicle could have travelled in
the time separating them. That is what distinguishes the two cases that look identical to a
pair check — a sharp turn moves the whole neighbourhood, while a corrupt digit moves one
sample away from a neighbourhood that agrees with itself.

**On not being too clever.** The thresholds below are deliberately loose. Measured across
92,312 stored fixes this rejects 87 of them — 0.094% — and every recording of sustained
motorway driving in the library comes through untouched. That asymmetry is the design: a
wrongly kept outlier is a visible line across the map that someone will report, while a
wrongly rejected fix is a silent hole in a route that nobody ever notices.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from statistics import median

from app.osd.geo import haversine_m
from app.osd.reasons import GpsQuality, GpsReason
from app.osd.validate import coordinate_problem, is_no_fix_placeholder

#: Fastest a road vehicle is presumed to travel, in km/h.
#:
#: Not a guess at the speed limit — a ceiling chosen so that no legitimate driving reaches
#: it. Australian freeways are 110, the highest limit anywhere in the country is 130, and
#: this sits above both with room for a descent and for the overlay's own coarse clock.
#: Contrast the 400 km/h it replaces, which was chosen to catch a coordinate in the wrong
#: hemisphere and therefore caught nothing smaller.
MAX_ROAD_SPEED_KMH = 160.0
MAX_ROAD_SPEED_MS = MAX_ROAD_SPEED_KMH * 1000.0 / 3600.0

#: Slack added to every distance comparison, in metres, for the overlay's own imprecision.
#:
#: Coordinates are printed to four decimal places, so each is already quantised to about
#: 11 m of latitude and 9 m of longitude at this latitude. Two endpoints each landing in
#: the far corner of their cell puts roughly 17 m of pure arithmetic between two samples
#: that never moved, and at 1 Hz that alone reads as 61 km/h. Without this allowance the
#: check would spend its time rejecting a stationary car.
QUANTISATION_M = 17.0

#: Samples either side of a point that form the neighbourhood judging it.
#:
#: Four is enough that two or three consecutive corrupt readings cannot outvote the good
#: ones, and short enough that the median still describes *where the vehicle was* rather
#: than the average of a whole manoeuvre. At 1 Hz this is a four-second view either way.
NEIGHBOURHOOD = 4

#: Fewest samples in a neighbourhood before its median is worth believing. Below this the
#: check declines to act, which is the same refusal :mod:`app.osd.outliers` makes and for
#: the same reason: two points cannot vote on which of them is wrong.
MIN_NEIGHBOURS = 3

#: Seconds without a trusted position beyond which the route is broken rather than joined.
#:
#: The overlay reports every second, so anything past a few seconds is signal that was lost
#: or readings that were refused. Ten seconds is far enough to ride out a brief dropout and
#: close enough that a tunnel does not become a road across the park above it.
MAX_GAP_S = 10.0

#: Metres between consecutive trusted positions beyond which the route is broken, whatever
#: the clock says. A gap can be short in time and still be a gap: the clock is read by the
#: same OCR as everything else, so distance gets an independent say.
MAX_GAP_M = 250.0


@dataclass(slots=True)
class Verdict:
    """What became of one sample, and why."""

    quality: GpsQuality
    reasons: list[GpsReason] = field(default_factory=list)
    #: Human sentence for a log line or a job result. The codes are for counting; this is
    #: for the person reading the counts.
    detail: str = ""
    #: True when this sample begins a new drawable segment — the route must not be joined
    #: back to whatever came before it.
    breaks_segment: bool = False

    @property
    def drawable(self) -> bool:
        return self.quality in {GpsQuality.VALID, GpsQuality.INTERPOLATED}

    def as_log(self) -> dict[str, object]:
        return {
            "gps_quality": str(self.quality),
            "gps_reasons": [str(r) for r in self.reasons],
            "gps_detail": self.detail,
            "gps_breaks_segment": self.breaks_segment,
        }


@dataclass(frozen=True, slots=True)
class Fix:
    """One candidate position on a timeline, as this module wants to see it."""

    t_s: float
    lat: float | None
    lon: float | None
    #: Already known to be synthetic (interpolated, or copied from the other camera).
    synthetic: bool = False
    #: The camera said it had no lock here.
    no_fix: bool = False


def reachable_radius_m(dt_s: float) -> float:
    """How far the vehicle could legitimately be after *dt_s* seconds.

    Floored at one second because that is the overlay's update rate: two samples stamped
    the same second are evidence of a coarse clock, not of teleportation.
    """
    return MAX_ROAD_SPEED_MS * max(1.0, abs(dt_s)) + QUANTISATION_M


def _neighbourhood_centre(
    fixes: Sequence[Fix], index: int, usable: Sequence[int]
) -> tuple[float, float, float] | None:
    """Median position of the samples around *index*, and the time to the furthest of them.

    ``usable`` is the indices still in play, so a sample already condemned cannot help
    condemn the next one. Points are taken from both sides on purpose: an excursion that
    leaves and returns agrees with the samples before it exactly as well as a real turn
    does, and only the samples *after* it tell the two apart.
    """
    position = None
    for slot, candidate in enumerate(usable):
        if candidate == index:
            position = slot
            break
    if position is None:
        return None

    lo = max(0, position - NEIGHBOURHOOD)
    hi = min(len(usable), position + NEIGHBOURHOOD + 1)
    neighbours = [fixes[usable[slot]] for slot in range(lo, hi) if usable[slot] != index]
    if len(neighbours) < MIN_NEIGHBOURS:
        return None

    centre_lat = median(n.lat for n in neighbours)  # type: ignore[misc]
    centre_lon = median(n.lon for n in neighbours)  # type: ignore[misc]
    span_s = max(abs(fixes[index].t_s - n.t_s) for n in neighbours)
    return centre_lat, centre_lon, span_s


def classify(fixes: Sequence[Fix]) -> list[Verdict]:
    """Judge every sample in one recording's timeline, in order.

    Returns one :class:`Verdict` per input, so callers can line the results up against
    their own rows without matching on anything.

    The passes are ordered cheapest-and-most-certain first. A coordinate that is not a
    place cannot take part in judging its neighbours, so those are removed before any
    geometry happens; only then does the neighbourhood pass run, and only then the
    forward walk that decides where segments break.
    """
    verdicts: list[Verdict] = []

    # --- pass 1: is this a place at all? ----------------------------------------------
    for fix in fixes:
        if fix.no_fix:
            verdicts.append(
                Verdict(
                    GpsQuality.NO_FIX,
                    [GpsReason.NO_FIX],
                    "the camera reported no satellite lock",
                )
            )
            continue
        problem = coordinate_problem(fix.lat, fix.lon)
        if problem is not None:
            if fix.lat is None or fix.lon is None:
                # Nothing was read. That is a failure of the OCR, not a coordinate that
                # turned out to be impossible, and conflating the two makes "how many
                # positions did we refuse" unanswerable.
                reason, quality = GpsReason.COORDINATE_PARSE_FAILURE, GpsQuality.REJECTED
            elif is_no_fix_placeholder(float(fix.lat), float(fix.lon)):
                reason, quality = GpsReason.NO_FIX, GpsQuality.NO_FIX
            else:
                reason, quality = GpsReason.INVALID_LAT_LON, GpsQuality.REJECTED
            verdicts.append(Verdict(quality, [reason], problem))
            continue
        verdicts.append(Verdict(GpsQuality.INTERPOLATED if fix.synthetic else GpsQuality.VALID))

    # --- pass 2: does each point agree with its own neighbourhood? --------------------
    # Iterated: removing an outlier sharpens the median, which can expose a second one
    # that was hiding behind it. Two rounds is enough in practice and bounds the cost;
    # a third has never changed the answer on this library.
    for _ in range(2):
        usable = [i for i, v in enumerate(verdicts) if v.drawable]
        if len(usable) <= MIN_NEIGHBOURS:
            break
        condemned: list[tuple[int, float, float]] = []
        for index in usable:
            centre = _neighbourhood_centre(fixes, index, usable)
            if centre is None:
                continue
            centre_lat, centre_lon, span_s = centre
            distance = haversine_m(
                centre_lat,
                centre_lon,
                float(fixes[index].lat),  # type: ignore[arg-type]
                float(fixes[index].lon),  # type: ignore[arg-type]
            )
            radius = reachable_radius_m(span_s)
            if distance > radius:
                condemned.append((index, distance, radius))

        # Refuse to act if most of what we can see looks wrong. The neighbourhood median
        # is then describing the corruption rather than the drive, and discarding the
        # majority of a recording on that authority is the more expensive mistake. This
        # mirrors the guard in :mod:`app.osd.outliers` deliberately.
        if not condemned or len(condemned) * 2 >= len(usable):
            break
        for index, distance, radius in condemned:
            verdicts[index] = Verdict(
                GpsQuality.REJECTED,
                [GpsReason.ISOLATED_POSITION_OUTLIER],
                (
                    f"{distance:.0f} m from the median of the samples around it, which is "
                    f"more than the {radius:.0f} m the vehicle could have covered"
                ),
            )

    # --- pass 3: forward walk for implied speed and segment breaks --------------------
    # The anchor is the last position still believed, so one bad reading cannot condemn
    # the good one that follows it.
    anchor: int | None = None
    for index, fix in enumerate(fixes):
        verdict = verdicts[index]
        if not verdict.drawable:
            continue
        if anchor is None:
            anchor = index
            verdict.breaks_segment = True
            continue

        previous = fixes[anchor]
        dt = fix.t_s - previous.t_s
        distance = haversine_m(
            float(previous.lat),  # type: ignore[arg-type]
            float(previous.lon),  # type: ignore[arg-type]
            float(fix.lat),  # type: ignore[arg-type]
            float(fix.lon),  # type: ignore[arg-type]
        )

        if distance > reachable_radius_m(dt):
            # Unreachable from the last trusted point. Whether this sample or the anchor
            # is the liar was already settled by pass 2, which had both sides to look at;
            # by here the anchor has survived that and this one has not.
            verdicts[index] = Verdict(
                GpsQuality.REJECTED,
                [GpsReason.IMPLIED_SPEED_OUTLIER],
                (
                    f"{distance:.0f} m in {dt:.1f} s implies "
                    f"{distance / max(1.0, dt) * 3.6:.0f} km/h"
                ),
            )
            continue

        if dt > MAX_GAP_S or distance > MAX_GAP_M:
            # Not a fault — a hole. The sample is kept and drawn, but the line is cut so
            # nothing pretends the vehicle drove the straight bit in between.
            verdict.breaks_segment = True
            verdict.reasons.append(GpsReason.GPS_GAP)
            verdict.detail = (
                f"{dt:.0f} s and {distance:.0f} m since the last trusted position; "
                "route broken rather than joined"
            )
        anchor = index

    return verdicts


def segments(verdicts: Sequence[Verdict]) -> list[list[int]]:
    """Indices grouped into runs that may each be drawn as one unbroken line."""
    runs: list[list[int]] = []
    current: list[int] = []
    for index, verdict in enumerate(verdicts):
        if not verdict.drawable:
            continue
        if verdict.breaks_segment and current:
            runs.append(current)
            current = []
        current.append(index)
    if current:
        runs.append(current)
    return [run for run in runs if len(run) > 1]


__all__ = [
    "MAX_GAP_M",
    "MAX_GAP_S",
    "MAX_ROAD_SPEED_KMH",
    "MAX_ROAD_SPEED_MS",
    "NEIGHBOURHOOD",
    "QUANTISATION_M",
    "Fix",
    "Verdict",
    "classify",
    "reachable_radius_m",
    "segments",
]
