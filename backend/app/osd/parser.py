"""Parse and validate a decoded overlay line.

The overlay this was built against reads::

    2026-08-04 17:44:38   E:138.6769 N:-34.8088  68 km/h

``E:`` is longitude and ``N:`` is latitude — both signed decimal degrees, four decimal
places, so roughly 11 m of resolution. The southern hemisphere shows as a negative
latitude rather than an ``S`` prefix.

The single most important rule in this module: **``E:00.0000 N:00.0000`` means the camera
had no satellite fix.** It is a placeholder, not a coordinate. Storing it literally would
drop every parked moment into the Gulf of Guinea, drag journey bounds across the planet,
and wreck every distance calculation. It becomes ``has_fix=False`` with null coordinates.

Parsing is tolerant — OCR drops a space or mangles a separator often enough that a strict
match would throw away good readings — but validation is strict. A reading that survives
here is one we are willing to plot on a map.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

#: Latitude/longitude below this magnitude are treated as the no-fix placeholder. The
#: overlay prints exactly 0.0000, and genuine coordinates that close to Null Island are
#: 600 km off the African coast — not somewhere a dashcam is driving.
NO_FIX_EPSILON = 1e-6

MAX_LATITUDE = 90.0
MAX_LONGITUDE = 180.0

#: Anything faster is an OCR error. Overridable via ``telemetry.max_speed_kmh``.
DEFAULT_MAX_SPEED_KMH = 300.0

#: The camera clock is not going to be meaningfully outside this window, and a wild date
#: is a strong signal that the digits were misread.
MIN_YEAR = 2000
MAX_YEAR = 2100

#: Matched on field *order*, not on the ``E:``/``N:`` literals.
#:
#: Requiring those letters would make every reading hostage to two glyphs the classifier
#: sees far less often than digits — one misread ``N`` and an otherwise perfect line is
#: thrown away. The layout is fixed, so position carries the meaning instead: after the
#: timestamp, the first decimal number is longitude, the second is latitude, and the
#: trailing integer is speed. Non-digit runs between fields are skipped whatever they
#: decoded to.
_OSD_RE = re.compile(
    r"(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})"
    r"\D{0,4}"
    r"(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})"
    r"[^\d+-]{0,8}"
    # Whitespace is permitted around the decimal point. Glyph segmentation infers spaces
    # from pixel gaps, and a slightly wide gap inside a coordinate must not cost the whole
    # reading — the digits either side are unambiguous.
    r"(?P<lon>[-+]?\d{1,3}\s?\.\s?\d{1,6})"
    r"[^\d+-]{0,8}"
    r"(?P<lat>[-+]?\d{1,3}\s?\.\s?\d{1,6})"
    r"[^\d]{0,8}"
    r"(?P<speed>\d{1,3})"
    r"\s*(?:k\s*m\s*/?\s*h)?",
    re.IGNORECASE | re.DOTALL,
)

#: Fallback when the GPS fields are unreadable but the clock is not. A timestamp alone is
#: still worth keeping: it anchors the recording's true start time, which the filename
#: only approximates.
_TIME_ONLY_RE = re.compile(
    r"(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})"
    r"\s*"
    r"(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})"
)


@dataclass(slots=True)
class OsdReading:
    """One parsed overlay sample.

    ``captured_at`` is naive local time — the overlay carries no zone information, so it
    is localised later using the ``general.timezone`` setting.
    """

    captured_at: datetime | None = None
    lat: float | None = None
    lon: float | None = None
    has_fix: bool = False
    speed_kmh: float | None = None

    raw_text: str = ""
    #: Confidence from the glyph classifier, carried through so the UI can show it.
    confidence: float = 0.0
    #: Why a field was rejected, for the job log. Empty on a clean read.
    problems: list[str] | None = None

    @property
    def valid(self) -> bool:
        """A reading is useful if it produced a timestamp, with or without a fix."""
        return self.captured_at is not None

    @property
    def has_position(self) -> bool:
        return self.has_fix and self.lat is not None and self.lon is not None


def _parse_timestamp(groups: dict[str, str], problems: list[str]) -> datetime | None:
    try:
        year = int(groups["year"])
        if not (MIN_YEAR <= year <= MAX_YEAR):
            problems.append(f"implausible year {year}")
            return None
        return datetime(
            year,
            int(groups["month"]),
            int(groups["day"]),
            int(groups["hour"]),
            int(groups["minute"]),
            int(groups["second"]),
        )
    except (KeyError, ValueError) as exc:
        # ValueError covers month 13, day 32, hour 25 — all normal OCR misreads.
        problems.append(f"invalid date/time: {exc}")
        return None


def parse_osd_text(
    text: str,
    *,
    confidence: float = 0.0,
    max_speed_kmh: float = DEFAULT_MAX_SPEED_KMH,
) -> OsdReading:
    """Turn a decoded overlay string into a validated reading.

    Never raises. An unparseable line comes back with ``valid`` False so the caller can
    count it and move on — one bad frame must not stop a recording being processed.
    """
    problems: list[str] = []
    reading = OsdReading(raw_text=text, confidence=confidence, problems=problems)

    match = _OSD_RE.search(text)
    if match is None:
        fallback = _TIME_ONLY_RE.search(text)
        if fallback is None:
            problems.append("no recognisable overlay content")
            return reading
        problems.append("GPS and speed fields unreadable; kept timestamp only")
        reading.captured_at = _parse_timestamp(fallback.groupdict(), problems)
        return reading

    groups = match.groupdict()
    reading.captured_at = _parse_timestamp(groups, problems)

    # --- position -------------------------------------------------------------------
    try:
        lon = float(groups["lon"].replace(" ", ""))
        lat = float(groups["lat"].replace(" ", ""))
    except (TypeError, ValueError, AttributeError):
        problems.append("coordinates could not be parsed")
        lon = lat = None  # type: ignore[assignment]

    if lat is not None and lon is not None:
        if abs(lat) < NO_FIX_EPSILON and abs(lon) < NO_FIX_EPSILON:
            # The no-fix placeholder. Deliberately not stored as (0, 0).
            reading.has_fix = False
        elif abs(lat) > MAX_LATITUDE or abs(lon) > MAX_LONGITUDE:
            problems.append(f"coordinates out of range (lat={lat}, lon={lon})")
            reading.has_fix = False
        else:
            reading.lat = lat
            reading.lon = lon
            reading.has_fix = True

    # --- speed ----------------------------------------------------------------------
    try:
        speed = float(groups["speed"])
    except (TypeError, ValueError):
        problems.append("speed could not be parsed")
    else:
        if 0 <= speed <= max_speed_kmh:
            reading.speed_kmh = speed
        else:
            problems.append(f"implausible speed {speed} km/h")

    return reading


def enforce_monotonic(readings: list[OsdReading]) -> list[OsdReading]:
    """Drop readings whose clock runs backwards within a segment.

    Time inside one recording only moves forward. A timestamp that jumps backwards is a
    misread digit, and it is cheaper and safer to discard that sample than to let it
    reorder a journey or invent a negative time delta.

    Equal timestamps are kept: the overlay updates at 1 Hz, so sampling at exactly 1 fps
    legitimately lands twice on the same second at segment boundaries.
    """
    out: list[OsdReading] = []
    last: datetime | None = None
    for reading in readings:
        if reading.captured_at is None:
            out.append(reading)
            continue
        if last is not None and reading.captured_at < last:
            if reading.problems is None:
                reading.problems = []
            reading.problems.append("timestamp moved backwards; discarded")
            continue
        last = reading.captured_at
        out.append(reading)
    return out
