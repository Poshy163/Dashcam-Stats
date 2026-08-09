"""Rejecting coordinates that cannot belong to the drive they were recorded in.

Per-point validation and consecutive-step checks both miss the failure mode that does the
most damage here, because both ask only whether a reading is *internally* consistent.

Measured on a real 674-recording library, 65 stored fixes were in the wrong place, and 48
of them came from recordings where **every single fix agreed with every other one**. The
rear camera's minus sign is thin enough that segmentation sometimes loses it for a whole
clip, so ``N:-34.7612`` becomes ``N:34.7612`` — a perfectly legal coordinate, at 0.95
confidence, 7,700 km away in China. Nothing inside that recording disagrees. A per-point
range check passes it, a jump check between consecutive fixes passes it, and a median taken
over the recording itself is flipped too, so that passes it as well.

What does disagree is the rest of the journey. A drive is one continuous thing spanning
many recordings, and a flipped clip sits an implausible distance from the others. So the
question this module asks is deliberately posed at the journey level: *given how long this
drive lasted, could the vehicle possibly have been here?*

The centre is a median rather than a mean so that the outliers being hunted cannot drag the
reference toward themselves, and the radius comes from physics — elapsed time times a
sustained road speed — rather than from a tuned constant, so a genuine interstate haul is
never mistaken for corruption.
"""

from __future__ import annotations

from collections.abc import Sequence
from statistics import median

from app.osd.engine import haversine_m

#: Sustained speed used to bound how far a drive can reach in its own duration.
#:
#: Not a limit on any single reading — :mod:`app.osd.engine` already rejects steps implying
#: more than 400 km/h between consecutive fixes. This is the *average* a vehicle could hold
#: across an entire journey including stops, and it is generous on purpose: the job here is
#: to catch coordinates thousands of kilometres out of place, not to second-guess a fast
#: drive. Adelaide to Sydney is 1,160 km, which needs about nine hours at this rate.
SUSTAINED_SPEED_KMH = 130.0

#: Floor on the radius, so a short clip is not held to an unreasonably tight circle. GPS
#: drift, a stationary fix wandering, and the overlay's own quantisation all live well
#: inside this.
MIN_RADIUS_M = 5_000.0

#: Half-width of the box around (0, 0) that is the camera's no-fix marker rather than a
#: place. Mirrors ``parser.NO_FIX_EPSILON`` and exists separately so callers that do not
#: hold a parsed reading -- a migration cleaning rows already in the database -- can apply
#: the same rule without importing the parser.
NO_FIX_EPSILON_FALLBACK = 0.1


def plausible_radius_m(span_s: float) -> float:
    """How far from its own centre a drive of this length could possibly reach."""
    reach = max(0.0, span_s) / 3600.0 * SUSTAINED_SPEED_KMH * 1000.0
    # Half the total reach: the centre sits in the middle of the drive, not at one end.
    return max(MIN_RADIUS_M, reach / 2.0)


def robust_centre(points: Sequence[tuple[float, float]]) -> tuple[float, float] | None:
    """Median latitude and longitude, or ``None`` for an empty set.

    A median rather than a mean because the whole point is to be unmoved by the outliers
    being looked for. An average of twenty good fixes and one that is 7,700 km away sits
    370 km from the truth, which is enough to start rejecting the good ones instead.

    Latitude and longitude are taken independently. That is not a true geometric median,
    and near the poles or the antimeridian it would not be defensible — but a dashcam
    library covers a driveable region, and within one the componentwise median is both
    correct and far cheaper.
    """
    if not points:
        return None
    return (median(p[0] for p in points), median(p[1] for p in points))


def spatial_outliers(
    points: Sequence[tuple[float, float]],
    *,
    span_s: float,
) -> set[int]:
    """Indices of coordinates that cannot belong to a drive of ``span_s`` seconds.

    Returns an empty set when there is too little to judge with. Two fixes cannot vote on
    which of them is wrong, and guessing between them would be worse than keeping both:
    a wrongly rejected fix is a hole in a route, and holes are hard to notice.
    """
    if len(points) < 3:
        return set()

    centre = robust_centre(points)
    if centre is None:
        return set()

    radius = plausible_radius_m(span_s)
    outliers = {
        index
        for index, (lat, lon) in enumerate(points)
        if haversine_m(centre[0], centre[1], lat, lon) > radius
    }

    # Refuse to act if most of the set looks wrong. That means the centre is not what was
    # assumed -- the drive genuinely moved a long way, or the majority is corrupt and the
    # minority is right -- and either way this is the wrong tool. Discarding most of a
    # journey's positions on a guess is far worse than leaving them alone for a human to
    # notice.
    if len(outliers) * 2 >= len(points):
        return set()
    return outliers


def looks_like_sign_loss(
    point: tuple[float, float], centre: tuple[float, float], radius_m: float
) -> bool:
    """Would negating the latitude bring this coordinate back inside the radius?

    Reported, never applied. Knowing that a rejected fix is a dropped minus sign rather
    than a mangled digit is worth surfacing in a job log, because it points at the overlay
    region or the glyph templates rather than at the weather. Repairing it automatically is
    a different matter: the evidence is circumstantial, and inventing a position is the one
    outcome worse than having none.
    """
    if haversine_m(centre[0], centre[1], point[0], point[1]) <= radius_m:
        return False
    return haversine_m(centre[0], centre[1], -point[0], point[1]) <= radius_m


def is_no_fix_placeholder(lat: float, lon: float, epsilon: float) -> bool:
    """Is this the camera's ``E:00.0000 N:00.0000`` marker rather than a coordinate?

    Both components have to be near zero. A real coordinate with one component genuinely
    close to zero exists — the equator and the Greenwich meridian are real places — but
    both at once is 600 km off the African coast in the Gulf of Guinea, which no dashcam
    is driving through.
    """
    return abs(lat) < epsilon and abs(lon) < epsilon


__all__ = [
    "MIN_RADIUS_M",
    "NO_FIX_EPSILON_FALLBACK",
    "SUSTAINED_SPEED_KMH",
    "is_no_fix_placeholder",
    "looks_like_sign_loss",
    "plausible_radius_m",
    "robust_centre",
    "spatial_outliers",
]
