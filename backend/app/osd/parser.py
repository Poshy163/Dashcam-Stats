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
from functools import lru_cache

#: Re-exported rather than redefined. These are the rules for what may be stored as a
#: position anywhere in the application, and having the parser own its own copy is how a
#: coordinate could pass one layer's idea of valid and fail another's.
from app.osd.reasons import GpsReason
from app.osd.validate import (
    coordinate_problem,
    is_no_fix_placeholder,
)

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

#: The date half of the overlay clock, on its own.
#:
#: The coordinate patterns are deliberately run from the *end* of the timestamp, so that a
#: date whose separators decoded badly cannot be read as a position. That guard only ever
#: existed when the whole timestamp matched — and the frame that needs it most is the one
#: where the time is destroyed and the date survives. On ``2026-08-16 1 .0000 N:00.0000``
#: the clock did not match, the search restarted at zero, and the strict pattern spliced
#: the day digits onto the wreckage of the ``E:`` field to report ``161.0000``: a longitude
#: in the Pacific, at 0.96 confidence, from a car parked in a garage.
#:
#: Anchored on the first digit of the line rather than searched for anywhere in it. The
#: overlay prints the date leftmost, so a date-shaped run that starts where the digits
#: start is the date; one further in is just as likely to *be* the coordinates — a northern
#: hemisphere ``E:138.7078N:34.7955`` has the same shape — and skipping past those would
#: lose the fix this is meant to protect.
_DATE_SHAPE_RE = re.compile(rf"\d{{4}}{_SEP}\d{{2}}{_SEP}\d{{2}}")


#: Longitude, then latitude, then speed. Applied to the compacted text.
#:
#: ``(?<!\d)`` stops a coordinate being read out of the middle of a longer number, which
#: is the same truncation the timestamp guard prevents from the other side, and the
#: trailing ``(?!\d)`` on each fraction stops the converse: a fraction running on into the
#: digits of the field after it, which is how ``E:138.65124:-34.7981 0 km/h`` used to
#: yield a longitude one digit too long and a latitude that had swallowed the speed.
#:
#: Tolerant fallback for an overlay whose separators decoded badly. Run against the same
#: compacted text and from the same offset as the strict pattern, which is what keeps it
#: honest: an earlier version matched the spaced text and allowed whitespace around the
#: decimal point, so on ``22:52:13 .6848`` it spliced the timestamp's seconds onto the
#: fraction and reported a longitude of ``13.6848`` — ten thousand kilometres from the
#: drive it belonged to.
#:
#: It differs from the strict pattern only in how much punctuation it tolerates *between*
#: the fields. It deliberately does **not** relax the fraction width. An earlier version
#: accepted one to six digits there, on the reasoning that another camera might print a
#: different precision, and that single allowance was the largest source of wrong positions
#: in the live library — see :func:`_precision_is_expected`. A camera that really does print
#: a different width is served by the ``telemetry.coordinate_decimals`` setting, which
#: rebuilds both patterns, rather than by letting every damaged frame invent its own format.
@lru_cache(maxsize=8)
def _fix_patterns(decimals: int) -> tuple[re.Pattern[str], re.Pattern[str]]:
    """The strict and tolerant coordinate patterns for an overlay of this precision."""
    strict = re.compile(
        r"(?<!\d)(?P<lonsep>[^\d+-]{0,8})"
        rf"(?P<lon>[-+]?\d{{1,3}}\.\d{{{decimals}}})"
        r"(?P<latsep>[^\d+-]{0,8})"
        rf"(?P<lat>[-+]?\d{{1,3}}\.\d{{{decimals}}})"
        r"(?:[^\d]{0,6}(?P<speed>\d{1,3}))?"
        r"(?:km/?h)?",
        re.IGNORECASE | re.DOTALL,
    )
    loose = re.compile(
        r"(?<!\d)(?P<lonsep>[^\d+-]{0,8})"
        rf"(?P<lon>[-+]?\d{{1,3}}\.\d{{{decimals}}})(?!\d)"
        r"(?P<latsep>[^\d+-]{0,8})"
        rf"(?P<lat>[-+]?\d{{1,3}}\.\d{{{decimals}}})(?!\d)"
        r"(?:[^\d]{0,8}(?P<speed>\d{1,3}))?"
        r"(?:km/?h)?",
        re.IGNORECASE | re.DOTALL,
    )
    return strict, loose


#: A final, labelled fallback for the failure that dominates sunlit rear-camera footage.
#:
#: Road texture survives the white-text threshold and touches the overlay's thinnest marks,
#: so the classifier commonly reads a decimal point as ``:`` or ``k`` and a minus as ``h``::
#:
#:     E:138:7013 N:-34:8056
#:     E:h138.7014 N:-h34.8056
#:
#: The field labels and every digit are still present. Requiring ``E`` then ``N``, the
#: camera's fixed four fraction digits, and a non-digit in the decimal cell is much tighter
#: than globally replacing punctuation. The ordinary patterns always run first; this only
#: recovers an otherwise missing pair.
@lru_cache(maxsize=8)
def _labelled_damaged_pattern(decimals: int) -> re.Pattern[str]:
    return re.compile(
        r"E(?P<lonprefix>[^\d]{1,5})(?P<lonwhole>\d{1,3})(?P<lonpoint>[^\d]{1,2})"
        rf"(?P<lonfrac>\d{{{decimals}}})(?!\d).{{0,6}}?N"
        r"(?P<latprefix>[^\d]{1,6})(?P<latwhole>\d{1,3})(?P<latpoint>[^\d]{1,2})"
        # No ``(?!\d)`` after the latitude: in compacted text the speed's digits run
        # straight on from it, so requiring a non-digit here can never hold. The fixed
        # fraction width and the ``E``/``N`` labels are what keep this anchored.
        rf"(?P<latfrac>\d{{{decimals}}})",
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
    #: ``valid``, ``parse_failed`` or ``rejected``. Kept independently from GPS.
    time_status: str = "parse_failed"
    #: ``valid``, ``no_fix``, ``parse_failed`` or ``rejected``.
    gps_status: str = "parse_failed"
    #: Machine-readable counterpart to whichever ``problems`` entry explains the position.
    #: ``None`` on a clean read. See :class:`app.osd.reasons.GpsReason`.
    gps_reason: GpsReason | None = None

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
    if not value:
        return False
    if value[0] in "+-":
        # The sign decoded explicitly -- but only *believe* it when the label's colon is
        # still there beside it.
        #
        # The same two glyphs swap in the other direction, and that direction was
        # unguarded. `-` and `:` are thin and adjacent in shape (see `_SEP`), and the
        # separator pattern is `[^\d+-]{0,8}`, which cannot hold a `-` -- so a colon read
        # as a minus is absorbed into the coordinate's own optional sign instead.
        # `E-138.6769` then parses as longitude *minus* 138.6769 at full confidence, with
        # no problem recorded: a point in the Pacific for a drive through Adelaide, and the
        # mirror image of the case this function was written for. A genuine `N:-34.8088`
        # keeps its colon and is unaffected; so is the labelled-damage recovery path, whose
        # separator carries one too.
        return ":" not in (separator or "")
    if not separator:
        return False
    # Nothing dot-shaped belongs between ``N:`` and the digits, so anything found there is
    # a glyph that decoded wrongly, and the sign is the most likely casualty.
    return separator[-1] in _DOTLIKE


def _fraction_width(value: str | None) -> int | None:
    """How many digits this coordinate printed after the decimal point."""
    if not value or "." not in value:
        return None
    return len(value.rsplit(".", 1)[1])


def _precision_is_expected(lon: str | None, lat: str | None, decimals: int) -> bool:
    """Did both coordinates print the width this overlay is known to use?

    The camera formats both fields with one formatter, so the fraction width is a property
    of the *overlay*, not of any single frame. A read that disagrees with it did not
    observe a different place — it observed the same place badly.

    This is the check that the tolerant pattern used to lack, and its absence was the
    single largest source of wrong positions in the live library. Glyph segmentation infers
    spaces from pixel gaps, so a wide gap splits a number: ``E:138.6510`` decodes as
    ``E:138.6 510`` and a pattern willing to accept one fraction digit reports ``138.6`` —
    a legal longitude 4.7 km west, at 0.94 classifier confidence, indistinguishable
    downstream from a place the vehicle really was.

    Width alone also catches the greedy converse, which symmetry between the two fields
    does not: on ``E:138.65124:-34.7981 0 km/h`` a 1-to-6-digit pattern reads the longitude
    one digit too long *and* absorbs the speed's leading zero into the latitude, leaving
    both fields five digits wide and in perfect agreement with each other while the
    longitude is wrong.
    """
    return _fraction_width(lon) == decimals and _fraction_width(lat) == decimals


def _date_shape_end(compact: str) -> int:
    """Where to start looking for coordinates when the clock could not be read.

    Returns the offset just past a date-shaped run beginning at the line's first digit, or
    zero when there is no such run — in which case nothing is skipped and the behaviour is
    what it always was.
    """
    first_digit = next((i for i, ch in enumerate(compact) if ch.isdigit()), None)
    if first_digit is None:
        return 0
    date = _DATE_SHAPE_RE.match(compact, first_digit)
    return date.end() if date else 0


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


def _recover_labelled_coordinates(
    compact: str, start: int, decimals: int = _COORD_DECIMALS
) -> dict[str, str | None] | None:
    """Recover labelled coordinates whose punctuation cells were OCR'd as other glyphs.

    No digit is corrected, inserted or removed here. The only inference is that the one or
    two non-digits between the whole and fractional parts occupy the camera's decimal cell.
    That keeps this useful for ``138:7013`` without turning arbitrary numbers in the scene
    or timestamp into a location.
    """
    match = _labelled_damaged_pattern(decimals).search(compact, start)
    if match is None:
        return None
    found = match.groupdict()

    def number(prefix: str, whole: str, fraction: str) -> str:
        sign = "-" if "-" in prefix else "+" if "+" in prefix else ""
        return f"{sign}{whole}.{fraction}"

    return {
        "lon": number(found["lonprefix"], found["lonwhole"], found["lonfrac"]),
        "lat": number(found["latprefix"], found["latwhole"], found["latfrac"]),
        "lonsep": found["lonprefix"],
        "latsep": found["latprefix"],
        "speed": None,
    }


def parse_osd_text(
    text: str,
    *,
    confidence: float = 0.0,
    max_speed_kmh: float = DEFAULT_MAX_SPEED_KMH,
    coord_decimals: int = _COORD_DECIMALS,
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
        reading.time_status = "valid" if reading.captured_at is not None else "rejected"
    else:
        problems.append("timestamp unreadable")

    # --- position and speed -----------------------------------------------------------
    # Searching from the end of the timestamp keeps a mangled date out of the coordinate
    # match: a ``-`` that decoded as ``.`` can turn ``2026-07-31`` into something with the
    # shape of a coordinate, and matching that would invent a position from the clock.
    # When the clock did not parse at all the date is stepped over on its own, because
    # otherwise the one frame with no timestamp to hide behind is the one frame where the
    # date is fair game -- see :data:`_DATE_SHAPE_RE`.
    search_from = stamp.end() if stamp else _date_shape_end(compact)
    strict_re, loose_re = _fix_patterns(coord_decimals)
    match = strict_re.search(compact, search_from) or loose_re.search(compact, search_from)
    recovered_groups = (
        None if match else _recover_labelled_coordinates(compact, search_from, coord_decimals)
    )
    groups = match.groupdict() if match else recovered_groups or {}
    if match is None and recovered_groups is None:
        problems.append("GPS fields unreadable")
        # Covers the commonest damage of all: digits lost to a pixel gap, so the fields no
        # longer have the overlay's shape. Naming it here rather than leaving the reason
        # null is what makes "why did this recording lose its positions" answerable.
        reading.gps_reason = GpsReason.COORDINATE_PARSE_FAILURE
    elif recovered_groups is not None:
        problems.append("coordinate punctuation recovered from labelled overlay fields")

    lat = lon = None
    if groups:
        try:
            lon = float(groups["lon"].replace(" ", ""))
            lat = float(groups["lat"].replace(" ", ""))
        except (TypeError, ValueError, AttributeError):
            problems.append("coordinates could not be parsed")
            lon = lat = None

    if lat is not None and lon is not None:
        if not _precision_is_expected(groups.get("lon"), groups.get("lat"), coord_decimals):
            # A partial read is the one failure that produces a *legal* coordinate, so it
            # has to be caught here on the shape of the text. By the time it is two floats
            # nothing downstream can tell 138.6 from a place the vehicle really was.
            reading.gps_status = "rejected"
            reading.gps_reason = GpsReason.COORDINATE_PARSE_FAILURE
            problems.append(
                f"coordinate precision inconsistent (lon {groups.get('lon')!r}, "
                f"lat {groups.get('lat')!r}); digits were lost or gained in the read"
            )
        elif _sign_is_ambiguous(groups.get("latsep"), groups.get("lat")) or _sign_is_ambiguous(
            groups.get("lonsep"), groups.get("lon")
        ):
            reading.gps_status = "rejected"
            reading.gps_reason = GpsReason.COORDINATE_PARSE_FAILURE
            problems.append(
                "coordinate sign ambiguous; refused rather than risk a wrong hemisphere"
            )
        elif (problem := coordinate_problem(lat, lon)) is not None:
            # One gate for every way a pair of numbers can fail to be a place: the no-fix
            # placeholder, out of range, and -- the one no previous version caught -- NaN
            # or infinity, which pass every magnitude comparison ever written.
            if not is_no_fix_placeholder(lat, lon):
                problems.append(problem)
                reading.gps_status = "rejected"
                reading.gps_reason = GpsReason.INVALID_LAT_LON
            else:
                reading.gps_status = "no_fix"
                reading.gps_reason = GpsReason.NO_FIX
            reading.has_fix = False
        else:
            reading.lat = lat
            reading.lon = lon
            reading.has_fix = True
            reading.gps_status = "valid"

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
    """Reject clocks that run backwards without dropping their other fields.

    Time inside one recording only moves forward. A timestamp that jumps backwards is a
    misread digit, but the GPS and speed on that same overlay line are independent. The
    old implementation discarded the entire reading here, producing a missing database
    row and throwing away a valid position because only its clock was bad.

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
            reading.problems.append("timestamp moved backwards; clock rejected")
            reading.captured_at = None
            reading.time_status = "rejected"
            out.append(reading)
            continue
        last = reading.captured_at
        out.append(reading)
    return out
