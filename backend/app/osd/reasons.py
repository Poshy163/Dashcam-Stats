"""Why a coordinate was not trusted, in one vocabulary shared by every layer.

Before this existed each layer said "rejected" in its own words, or said nothing at all.
The parser appended a sentence to ``problems``, the extractor logged a count, the journey
builder cleared the row and moved on, and the heat map simply filtered on ``has_fix``. When
a position turned up in the wrong place there was no way to ask *which* check should have
caught it, so every investigation started by re-deriving the pipeline from source.

These codes are that missing answer. They are stable identifiers rather than prose, so they
can be counted, grouped and filtered — "how many fixes did we lose to truncated OCR this
week" is a query, not an afternoon. The human sentence still travels alongside them; this
is the part a machine reads.

The set is deliberately small and describes *observations*, not verdicts. ``gps_gap`` is not
a fault — it is the honest statement that nothing was recorded for a while, and the route
layer draws a break rather than a line because of it.
"""

from __future__ import annotations

from enum import StrEnum


class GpsReason(StrEnum):
    """Why a GPS sample is not a trusted position."""

    #: The coordinate text could not be read as the overlay's fixed format: the wrong
    #: number of fraction digits, the two fields disagreeing about precision, or digits
    #: lost to a pixel gap. The commonest fault in this library by a wide margin — and
    #: the most dangerous, because a truncated coordinate is still a legal one.
    COORDINATE_PARSE_FAILURE = "coordinate_parse_failure"

    #: Not a place: out of range, NaN, infinite, or missing entirely.
    INVALID_LAT_LON = "invalid_lat_lon"

    #: The camera itself reported no satellite lock (the ``00.0000/00.0000`` marker).
    #: Not damage, and never something to interpolate over.
    NO_FIX = "no_fix"

    #: Reaching this point from the last trusted one would need a speed no road vehicle
    #: sustains. Judged against elapsed time, so it catches both a wild single-second
    #: jump and a slow drift that accumulates.
    IMPLIED_SPEED_OUTLIER = "implied_speed_outlier"

    #: The point disagrees with the samples on *both* sides of it while those agree with
    #: each other — the out-and-back excursion that a single corrupt digit produces. This
    #: is the check that a per-step comparison cannot make, because a bad point is
    #: "reachable" from its predecessor exactly as often as it is not.
    ISOLATED_POSITION_OUTLIER = "isolated_position_outlier"

    #: The sample's own overlay clock is too far from where its offset in the recording
    #: says it should be. A single misread digit moves the clock by an hour or a day, and
    #: the sample then sorts into the wrong place in its journey.
    TIMESTAMP_MISMATCH = "timestamp_mismatch"

    #: No trustworthy position for long enough that the route between the two ends cannot
    #: be reconstructed. An observation about coverage, not a fault in any one sample:
    #: the sample after the gap may be perfectly good.
    GPS_GAP = "gps_gap"

    #: The position was not read here but carried in — interpolated across a short hole,
    #: or copied from the other camera. Trustworthy enough to draw, never counted as
    #: independent evidence.
    SYNTHESISED = "synthesised"


class GpsQuality(StrEnum):
    """How much weight a stored position carries.

    Four states rather than the boolean ``has_fix`` that preceded them, because the three
    ways of not having a position call for different handling downstream and used to be
    indistinguishable once written: a rejected reading and a satellite outage both arrived
    as ``has_fix=False`` with null coordinates, so nothing could tell "the camera could not
    see the sky" from "we did not believe what it printed".
    """

    #: Read directly from the overlay and agreed with by its neighbours.
    VALID = "valid"

    #: Reconstructed from surrounding trusted fixes. Real enough to draw a line through,
    #: but never evidence for distance travelled or for placing a sighting.
    INTERPOLATED = "interpolated"

    #: Read, but not believed. The coordinate is discarded; the rest of the reading
    #: (speed, clock, raw text) is kept, because only the position was ever in doubt.
    REJECTED = "rejected"

    #: The camera reported no lock. Nothing was misread; there was nothing to read.
    NO_FIX = "no_fix"


#: Quality states whose coordinate may be drawn on a map or fed to a heat layer.
DRAWABLE = frozenset({GpsQuality.VALID, GpsQuality.INTERPOLATED})

#: Quality states that count as independent evidence — distance, speed, placing a
#: sighting. Interpolation is excluded on purpose: measuring a line we drew ourselves
#: inflates every total that uses it.
TRUSTED = frozenset({GpsQuality.VALID})


__all__ = ["DRAWABLE", "TRUSTED", "GpsQuality", "GpsReason"]
