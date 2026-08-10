"""What makes a coordinate fit to store, and to attach to something.

Every layer that handles a position needed its own version of this question and each grew
its own answer: the parser tested magnitude, the extractor tested consecutive steps, the
journey builder tested distance from a median, the heat map re-tested for nulls because it
did not trust ``has_fix``. Four implementations of "is this a real place" is four chances
to disagree, and they did — a row could carry ``has_fix`` with null coordinates, and a NaN
would have passed every one of them.

So the rules live here once, as pure functions over numbers, and everything upstream of a
stored coordinate calls them. Deliberately free of any notion of *where* this dashcam
drives: a fixed region would work beautifully for one library and silently discard every
fix from anyone else's. What is checked is only what is impossible anywhere on Earth.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

#: Latitude/longitude below this magnitude are treated as the no-fix placeholder.
#:
#: The overlay prints exactly ``00.0000`` when it has no satellite lock, so an exact-zero
#: test looks sufficient and is not. The placeholder goes through the same OCR as
#: everything else, and one misread digit turns it into ``00.0001`` — still obviously the
#: placeholder, but no longer within a hair of zero. On a real 674-recording library that
#: let fourteen "coordinates" through: ``0.0001, 0.0000``, ``0.0000, 0.0009``,
#: ``0.0100, 0.0000`` at 77 km/h. Each was stored as a genuine position in the Gulf of
#: Guinea, and each added roughly 14,000 km to whatever journey it landed in.
#:
#: A tenth of a degree is about 11 km, which is a wide net for a placeholder and no net at
#: all for a real coordinate: the nearest land to (0, 0) is 600 km away. Nothing legitimate
#: is being discarded, because nothing legitimate is there.
NO_FIX_EPSILON = 0.1

MAX_LATITUDE = 90.0
MAX_LONGITUDE = 180.0

#: Ceiling on how fast the vehicle could actually be moving between two fixes. Well above
#: any road speed, so it only ever catches OCR damage rather than fast driving. Used both
#: to reject a misread coordinate and to refuse to interpolate across two fixes that
#: disagree with each other.
MAX_PLAUSIBLE_SPEED_KMH = 400.0

#: The same in metres per second, which is the form every caller actually wants.
MAX_PLAUSIBLE_SPEED_MS = MAX_PLAUSIBLE_SPEED_KMH * 1000.0 / 3600.0


#: How far a sample's overlay clock may sit from where its own offset says it should be.
#:
#: The clock is read glyph by glyph like everything else, so a single misread digit moves it
#: by a day, a month or an hour. One fix in the live library reads ``2026-08-08 05:49:20``
#: inside a recording made on ``2026-08-05`` -- one wrong day digit, three days out. Nothing
#: compared a sample's time against the clip it came from, so it sorted after every other
#: point in its journey and became that journey's *end marker*, 3 km from where the drive
#: actually finished.
#:
#: Fifteen minutes matches the tolerance already used to decide whether the overlay clock
#: may override the filename: generous enough for a camera whose clock drifts, far tighter
#: than any misread digit can produce.
MAX_CLOCK_DRIFT_S = 15 * 60


def clock_is_plausible(
    captured_at: datetime | None,
    expected_at: datetime | None,
    tolerance_s: float = MAX_CLOCK_DRIFT_S,
) -> bool:
    """Could this sample really have been taken when its overlay says it was?

    ``expected_at`` is where the sample sits by construction -- the recording's start plus
    the sample's own offset -- which is independent of the glyphs and therefore the thing
    worth checking against. Missing values are not implausible, merely unknown, and are
    left for the caller to handle.
    """
    if captured_at is None or expected_at is None:
        return True
    if captured_at.tzinfo is None:
        captured_at = captured_at.replace(tzinfo=UTC)
    if expected_at.tzinfo is None:
        expected_at = expected_at.replace(tzinfo=UTC)
    return abs((captured_at - expected_at).total_seconds()) <= tolerance_s


def coordinate_problem(lat: object, lon: object) -> str | None:
    """Why this pair may not be stored as a position, or ``None`` if it may.

    Returns a sentence rather than a boolean because every caller wants to say *why* in a
    log line or a job result — "rejected" on its own has repeatedly sent people looking in
    the wrong place.
    """
    if lat is None or lon is None:
        return "no coordinate"
    try:
        latitude = float(lat)  # type: ignore[arg-type]
        longitude = float(lon)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return f"coordinate is not a number (lat={lat!r}, lon={lon!r})"

    # NaN and infinity are checked explicitly. They survive every comparison below --
    # `abs(nan) > 90` is False, so a NaN latitude passes a range check and is then stored,
    # serialised as `null` by JSON and drawn as nothing at all, which is indistinguishable
    # from having no fix except that everything downstream believes there is one.
    if not math.isfinite(latitude) or not math.isfinite(longitude):
        return f"coordinate is not finite (lat={latitude}, lon={longitude})"

    if abs(latitude) > MAX_LATITUDE or abs(longitude) > MAX_LONGITUDE:
        return f"coordinate out of range (lat={latitude}, lon={longitude})"

    if is_no_fix_placeholder(latitude, longitude):
        return "the camera reported no satellite fix (00.0000/00.0000)"

    return None


def is_valid_coordinate(lat: object, lon: object) -> bool:
    """True when this pair is a place a vehicle could be."""
    return coordinate_problem(lat, lon) is None


def is_no_fix_placeholder(lat: float, lon: float, epsilon: float = NO_FIX_EPSILON) -> bool:
    """Is this the camera's ``E:00.0000 N:00.0000`` marker rather than a place?

    Both components have to be near zero. A real coordinate with one component genuinely
    close to zero exists — the equator and the Greenwich meridian are real places — but
    both at once is 600 km off the African coast in the Gulf of Guinea, which no dashcam
    is driving through.
    """
    return abs(lat) < epsilon and abs(lon) < epsilon


def implied_speed_ms(distance_m: float, seconds: float) -> float:
    """Speed a vehicle would need to cover *distance_m* in *seconds*.

    Zero or negative elapsed time yields infinity for any real distance, which is the
    honest answer: two different places at the same instant is not a speed, it is a
    contradiction, and the callers all treat it as one.
    """
    if seconds <= 0:
        return 0.0 if distance_m <= 0 else math.inf
    return distance_m / seconds


def is_plausible_step(distance_m: float, seconds: float) -> bool:
    """Could a vehicle have covered this distance in this time?

    The elapsed time is floored at one second because that is the overlay's own update
    rate: two fixes stamped the same second are not evidence of teleportation, they are
    evidence of a 1 Hz clock.
    """
    return implied_speed_ms(distance_m, max(1.0, seconds)) <= MAX_PLAUSIBLE_SPEED_MS


__all__ = [
    "MAX_CLOCK_DRIFT_S",
    "MAX_LATITUDE",
    "MAX_LONGITUDE",
    "MAX_PLAUSIBLE_SPEED_KMH",
    "MAX_PLAUSIBLE_SPEED_MS",
    "NO_FIX_EPSILON",
    "clock_is_plausible",
    "coordinate_problem",
    "implied_speed_ms",
    "is_no_fix_placeholder",
    "is_plausible_step",
    "is_valid_coordinate",
]
