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

#: Anything faster is an OCR error. Overridable via ``telemetry.max_speed_kmh``.
DEFAULT_MAX_SPEED_KMH = 300.0

#: The camera clock is not going to be meaningfully outside this window, and a wild date
#: is a strong signal that the digits were misread.
MIN_YEAR = 2000
MAX_YEAR = 2100

#: Fraction digits the overlay always prints for a coordinate.
#:
#: Fixing this is what makes whitespace-insensitive parsing safe. Glyph segmentation infers
#: spaces from pixel gaps, and a slightly wide gap inside a number used to split it: ``61``
#: became ``6 1`` and the speed was read as 6, while ``-34.7763`` became ``-34.7 763`` and
#: the latitude truncated to ``-34.7``. Both then failed validation or, worse, passed with
#: a wrong value. Stripping whitespace fixes that, but only if the fraction length is
#: pinned -- otherwise ``-34.776361`` reads as one long latitude that swallows the speed.
_COORD_DECIMALS = 4

#: Glyphs that a misread ``-`` lands on. All are small, low-ink marks with almost no
#: structure to tell them apart at this resolution, so a minus sign that decodes as one of
#: these is genuinely ambiguous rather than merely noisy — see :func:`_sign_is_ambiguous`.
#:
#: ``:`` is pointedly *not* in this set. It is the overlay's own separator — every reading
#: contains ``E:`` and ``N:`` — so treating it as a suspicious sign condemns every frame.
_DOTLIKE = ".,_'`~"

#: Separators inside the date and time. Deliberately any non-digit rather than the literal
#: ``-`` and ``:`` the overlay prints: those two glyphs are thin, adjacent in shape, and
#: swap for each other often enough to matter. Insisting on them threw away whole readings
#: over a single cosmetic character — ``2026-07:28 13:49:03 E:138.7035 N:-34.7956`` was
#: discarded outright despite every digit, both coordinates and the speed being correct.
#: The digits are what carry meaning here; the punctuation only has to be *something*.
_SEP = r"\D{1,2}"

#: The overlay's three pieces are matched **independently**, and that is the single most
#: important decision in this module.
#:
#: They used to be one pattern, so a reading was all-or-nothing: no timestamp meant no
#: position, no matter how clean the coordinates were. On the rear camera, where something
#: bright sits behind the left of the strip and eats the date, that threw away run after
#: run of perfectly legible fixes::
#:
#:     '202670 13:49:22 E:138.7075 N:-34.7955 61 km/h'   -> discarded
#:     '-07-28 13:49:25 E:138.7078 N:-34.7955 57 km/h'   -> discarded
#:
#: Nothing about those coordinates is doubtful, and the clock is the *least* valuable
#: field of the three: the recording's start time comes from its filename and the sample's
#: own offset, so a lost timestamp costs almost nothing while a lost fix leaves a hole in
#: the map. Matching each field on its own lets every frame contribute whatever it can.
#:
#: Each is matched on field *order*, not on the ``E:``/``N:`` literals. Requiring those
#: letters would make every reading hostage to two glyphs the classifier sees far less
#: often than digits — one misread ``N`` and an otherwise perfect line is thrown away.
#: The two lookarounds are load-bearing, not decoration. Without the trailing ``(?!\d)``
#: the seconds field will happily eat the leading digits of the longitude that follows it:
#: on ``2026-07-28 m:49:19 E:138.7067`` a misread hour let the pattern take ``13`` out of
#: ``138`` as its seconds, and the longitude that came back was ``8.7067`` — a coordinate
#: off the coast of Nigeria, sitting in the middle of an otherwise correct drive.
_TIMESTAMP_RE = re.compile(
    rf"(?<!\d)(?P<year>\d{{4}}){_SEP}(?P<month>\d{{2}}){_SEP}(?P<day>\d{{2}})"
    r"\D{0,4}"
    rf"(?P<hour>\d{{2}}){_SEP}(?P<minute>\d{{2}}){_SEP}(?P<second>\d{{2}})(?!\d)"
)

#: Longitude, then latitude, then speed. Applied to the compacted text.
#:
#: ``(?<!\d)`` stops a coordinate being read out of the middle of a longer number, which
#: is the same truncation the timestamp guard prevents from the other side.
_FIX_RE = re.compile(
    r"(?<!\d)(?P<lonsep>[^\d+-]{0,8})"
    rf"(?P<lon>[-+]?\d{{1,3}}\.\d{{{_COORD_DECIMALS}}})"
    r"(?P<latsep>[^\d+-]{0,8})"
    rf"(?P<lat>[-+]?\d{{1,3}}\.\d{{{_COORD_DECIMALS}}})"
    r"(?:[^\d]{0,6}(?P<speed>\d{1,3}))?"
    r"(?:km/?h)?",
    re.IGNORECASE | re.DOTALL,
)

#: Tolerant fallback for a camera whose overlay prints a different precision. Run against
#: the same compacted text and from the same offset as the strict pattern, which is what
#: keeps it honest: an earlier version matched the spaced text and allowed whitespace
#: around the decimal point, so on ``22:52:13 .6848`` it spliced the timestamp's seconds
#: onto the fraction and reported a longitude of ``13.6848`` — ten thousand kilometres
#: from the drive it belonged to.
_FIX_LOOSE_RE = re.compile(
    r"(?<!\d)(?P<lonsep>[^\d+-]{0,8})"
    r"(?P<lon>[-+]?\d{1,3}\.\d{1,6})"
    r"(?P<latsep>[^\d+-]{0,8})"
    r"(?P<lat>[-+]?\d{1,3}\.\d{1,6})"
    r"(?:[^\d]{0,8}(?P<speed>\d{1,3}))?"
    r"(?:km/?h)?",
    re.IGNORECASE | re.DOTALL,
)

#: Speed on its own, anchored on the trailing ``km/h``. That literal is four glyphs at a
#: fixed place on the line and decodes reliably, which makes it a dependable anchor when
#: the coordinates in front of it did not survive.
_SPEED_RE = re.compile(r"(?P<speed>\d{1,3})\s*k\s*m\s*/?\s*h", re.IGNORECASE)


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
        """A reading is useful if it recovered *any* of the three fields.

        This deliberately does not require a timestamp. Each sample carries its own decode
        offset, so a frame that yields coordinates but no clock still places a point on the
        map; insisting on the clock discarded long runs of clean fixes whenever something
        bright in the scene sat behind the date.
        """
        return self.captured_at is not None or self.has_position or self.speed_kmh is not None

    @property
    def has_position(self) -> bool:
        return self.has_fix and self.lat is not None and self.lon is not None


def _sign_is_ambiguous(separator: str | None, value: str | None) -> bool:
    """Could this coordinate's minus sign have been eaten by the separator?

    The overlay prints ``N:`` then, in the southern hemisphere, a minus. When that minus
    decodes as a dot the string becomes ``N:.34.7956``, the sign vanishes, and the reading
    lands 34 degrees *north* — a point in Iraq recorded for a drive through Adelaide. The
    two cases are not distinguishable from the text alone: a dot sitting in the sign
    position is equally consistent with a misread minus and with a misread colon in front
    of a genuinely positive coordinate.

    So the reading is refused rather than guessed. A frame without a fix costs nothing —
    the overlay repeats itself every second — whereas a point in the wrong hemisphere
    drags journey bounds across the planet and is plainly visible on the map.
    """
    if not separator or not value:
        return False
    if value[0] in "+-":
        # The sign decoded explicitly; whatever precedes it is just punctuation.
        return False
    # Nothing dot-shaped belongs between ``N:`` and the digits, so anything found there is
    # a glyph that decoded wrongly, and the sign is the most likely casualty.
    return separator[-1] in _DOTLIKE


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

    # Whitespace in the decoded text is inferred from pixel gaps, not read from the image,
    # so it is the least trustworthy thing in the string. The compacted form is what the
    # strict patterns run against; the fixed fraction length keeps the fields apart without
    # needing the spaces to be right.
    compact = re.sub(r"\s+", "", text)

    # --- timestamp --------------------------------------------------------------------
    stamp = _TIMESTAMP_RE.search(compact)
    if stamp is not None:
        reading.captured_at = _parse_timestamp(stamp.groupdict(), problems)
    else:
        problems.append("timestamp unreadable")

    # --- position and speed -----------------------------------------------------------
    # Searching from the end of the timestamp keeps a mangled date out of the coordinate
    # match: a ``-`` that decoded as ``.`` can turn ``2026-07-31`` into something with the
    # shape of a coordinate, and matching that would invent a position from the clock.
    search_from = stamp.end() if stamp else 0
    match = _FIX_RE.search(compact, search_from) or _FIX_LOOSE_RE.search(compact, search_from)
    groups = match.groupdict() if match else {}
    if match is None:
        problems.append("GPS fields unreadable")

    lat = lon = None
    if groups:
        try:
            lon = float(groups["lon"].replace(" ", ""))
            lat = float(groups["lat"].replace(" ", ""))
        except (TypeError, ValueError, AttributeError):
            problems.append("coordinates could not be parsed")
            lon = lat = None

    if lat is not None and lon is not None:
        if _sign_is_ambiguous(groups.get("latsep"), groups.get("lat")) or _sign_is_ambiguous(
            groups.get("lonsep"), groups.get("lon")
        ):
            problems.append(
                "coordinate sign ambiguous; refused rather than risk a wrong hemisphere"
            )
        elif abs(lat) < NO_FIX_EPSILON and abs(lon) < NO_FIX_EPSILON:
            # The no-fix placeholder. Deliberately not stored as (0, 0).
            reading.has_fix = False
        elif abs(lat) > MAX_LATITUDE or abs(lon) > MAX_LONGITUDE:
            problems.append(f"coordinates out of range (lat={lat}, lon={lon})")
            reading.has_fix = False
        else:
            reading.lat = lat
            reading.lon = lon
            reading.has_fix = True

    # Speed comes from the coordinate match when the line held together that far, and from
    # the ``km/h`` anchor when it did not.
    raw_speed = groups.get("speed")
    if raw_speed is None:
        anchored = _SPEED_RE.search(text)
        raw_speed = anchored.group("speed") if anchored else None

    if raw_speed is None:
        problems.append("speed could not be parsed")
    else:
        speed = float(raw_speed)
        if 0 <= speed <= max_speed_kmh:
            reading.speed_kmh = speed
        else:
            problems.append(f"implausible speed {speed} km/h")

    if not reading.valid:
        problems.append("no recognisable overlay content")
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
