"""Where was the vehicle at *this* moment in the recording?

Detections, tracks and plate readings all happen at a frame offset, and the only positions
that exist are the once-per-second overlay samples either side of it. Turning one into the
other was a single ``ORDER BY abs(t_offset_s - ?) LIMIT 1``, which is wrong in three ways
that all produce a confident answer rather than an error:

* **No tolerance.** A recording whose GPS locked for the first ten seconds and then lost
  it for the remaining two minutes still had a "nearest" fix for every detection in it —
  the one from the start of the clip, up to 110 seconds and a couple of kilometres away.
  That is the stale-fix reuse that puts a sighting on the wrong street.
* **No interpolation.** Between two fixes a second apart it picked one of them rather than
  the point between, which at 60 km/h is up to 8 m of avoidable error on every sighting.
* **No validation.** ``has_fix`` was the only filter, so a sample carrying the flag with a
  corrupt coordinate was as eligible as any other.

What replaces it answers the same question with a fourth possible answer: *no idea*. A
sighting with no location is honest and the UI already has a place for it; a sighting with
someone else's location is a wrong answer that looks exactly like a right one.

Nothing here knows anything about the queue, the database or where in the world the camera
is. It takes samples and an offset and returns a position or nothing, so it is testable
against the exact case that went wrong without a recording, a decoder or a share.
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.osd.geo import haversine_m
from app.osd.validate import is_plausible_step, is_valid_coordinate

#: How far from the requested moment a single fix may sit and still be used as-is.
#:
#: The overlay updates once per second, so on a recording with a lock there is always a
#: sample within half a second and this never binds. It binds precisely when the lock was
#: lost, which is when using the last known position is wrong. At 60 km/h three seconds is
#: 50 m -- inside the width of an intersection, and about five times the overlay's own
#: 11 m coordinate quantisation.
DEFAULT_MAX_FIX_AGE_S = 3.0

#: The widest pair of fixes that may be interpolated between.
#:
#: Interpolation assumes the vehicle went roughly straight between two known points, which
#: holds for a few seconds of driving and stops holding across a dropout. Fifteen seconds
#: at urban speed is a couple of hundred metres -- far enough to be worth interpolating
#: rather than discarding, close enough that a corner cannot hide a road inside it. Beyond
#: this the honest answer is that the position is unknown.
DEFAULT_MAX_BRACKET_S = 15.0

#: Below this the requested offset is treated as landing on the sample itself.
_EXACT_S = 0.05


@dataclass(frozen=True, slots=True)
class FixSample:
    """One validated overlay position, ready to place something in time."""

    t_offset_s: float
    lat: float
    lon: float
    captured_at: datetime | None = None
    speed_kmh: float | None = None


@dataclass(frozen=True, slots=True)
class Located:
    """A position for a moment, and the full account of how it was arrived at."""

    lat: float
    lon: float
    captured_at: datetime | None
    #: ``exact`` (landed on a sample), ``interpolated`` (between two), ``nearest`` (one
    #: sample within tolerance). Stored in the log so a suspicious coordinate can be traced
    #: back to the decision that produced it rather than guessed at.
    source: str
    #: Seconds between the requested moment and the closest supporting sample.
    dt_s: float
    #: Width of the pair interpolated across; equals ``dt_s`` when a single fix was used.
    span_s: float
    before_offset_s: float | None = None
    after_offset_s: float | None = None

    def as_log(self) -> dict[str, object]:
        return {
            "gps_source": self.source,
            "gps_lat": round(self.lat, 6),
            "gps_lon": round(self.lon, 6),
            "gps_sample_at": self.captured_at.isoformat() if self.captured_at else None,
            "gps_dt_s": round(self.dt_s, 3),
            "gps_span_s": round(self.span_s, 3),
            "gps_before_offset_s": self.before_offset_s,
            "gps_after_offset_s": self.after_offset_s,
        }


class FixTrack:
    """The valid fixes of one recording, indexed for repeated lookup by offset.

    Built once per recording and asked once per track, rather than a database round trip
    per track. On a busy clip that is one query instead of four hundred, and it is the
    only way the tolerance rules can be applied consistently -- an ``ORDER BY abs(...)``
    cannot express "and only if it is close enough, and interpolate when it is not".
    """

    __slots__ = ("_max_bracket_s", "_max_fix_age_s", "_offsets", "_samples")

    def __init__(
        self,
        samples: list[FixSample],
        *,
        max_fix_age_s: float = DEFAULT_MAX_FIX_AGE_S,
        max_bracket_s: float = DEFAULT_MAX_BRACKET_S,
    ) -> None:
        # Invalid coordinates are dropped on the way in rather than checked on the way
        # out, so nothing downstream can reach one by any route.
        clean = [s for s in samples if is_valid_coordinate(s.lat, s.lon)]
        clean.sort(key=lambda s: s.t_offset_s)
        self._samples = clean
        self._offsets = [s.t_offset_s for s in clean]
        self._max_fix_age_s = max(0.0, max_fix_age_s)
        self._max_bracket_s = max(0.0, max_bracket_s)

    def __len__(self) -> int:
        return len(self._samples)

    @property
    def samples(self) -> list[FixSample]:
        return self._samples

    @classmethod
    def from_rows(cls, rows, **kwargs) -> FixTrack:
        """Build from database rows carrying ``t_offset_s``, ``lat``, ``lon``, ``captured_at``."""
        return cls(
            [
                FixSample(
                    t_offset_s=float(row.t_offset_s or 0.0),
                    lat=float(row.lat),
                    lon=float(row.lon),
                    captured_at=row.captured_at,
                    speed_kmh=getattr(row, "speed_kmh", None),
                )
                for row in rows
                if row.lat is not None and row.lon is not None
            ],
            **kwargs,
        )

    def at(self, offset_s: float) -> Located | None:
        """The vehicle's position at *offset_s*, or ``None`` if it cannot be established.

        ``None`` is a real answer and the most important one here. It is what a detection
        during a satellite outage gets, and what a detection gets when the fixes either
        side of it contradict each other.
        """
        if not self._samples:
            return None

        index = bisect_left(self._offsets, offset_s)
        before = self._samples[index - 1] if index > 0 else None
        after = self._samples[index] if index < len(self._samples) else None

        for candidate in (after, before):
            if candidate is not None and abs(candidate.t_offset_s - offset_s) <= _EXACT_S:
                return Located(
                    lat=candidate.lat,
                    lon=candidate.lon,
                    captured_at=candidate.captured_at,
                    source="exact",
                    dt_s=abs(candidate.t_offset_s - offset_s),
                    span_s=0.0,
                    before_offset_s=candidate.t_offset_s,
                    after_offset_s=candidate.t_offset_s,
                )

        if before is not None and after is not None:
            bracketed = self._interpolate(before, after, offset_s)
            if bracketed is not None:
                return bracketed

        nearest = min(
            (s for s in (before, after) if s is not None),
            key=lambda s: abs(s.t_offset_s - offset_s),
            default=None,
        )
        if nearest is None:
            return None
        dt = abs(nearest.t_offset_s - offset_s)
        if dt > self._max_fix_age_s:
            # The heart of it. Beyond the tolerance the last known position is not where
            # the vehicle was, and saying so is the whole point of this module.
            return None
        return Located(
            lat=nearest.lat,
            lon=nearest.lon,
            captured_at=nearest.captured_at,
            source="nearest",
            dt_s=dt,
            span_s=dt,
            before_offset_s=before.t_offset_s if before is not None else None,
            after_offset_s=after.t_offset_s if after is not None else None,
        )

    def _interpolate(self, before: FixSample, after: FixSample, offset_s: float) -> Located | None:
        span = after.t_offset_s - before.t_offset_s
        if span <= 0 or span > self._max_bracket_s:
            return None

        # Two fixes that disagree with each other cannot be interpolated between: one of
        # them is wrong and there is no way to tell which, so anywhere on the line joining
        # them is as likely to be wrong as right. Refusing leaves the sighting unplaced,
        # which is recoverable; splitting the difference between a real position and a
        # misread one is a coordinate halfway to nowhere that nothing downstream can
        # recognise as suspect.
        if not is_plausible_step(haversine_m(before.lat, before.lon, after.lat, after.lon), span):
            return None

        ratio = (offset_s - before.t_offset_s) / span
        captured = None
        if before.captured_at is not None and after.captured_at is not None:
            captured = before.captured_at + timedelta(
                seconds=(after.captured_at - before.captured_at).total_seconds() * ratio
            )
        elif before.captured_at is not None:
            captured = before.captured_at + timedelta(seconds=offset_s - before.t_offset_s)

        return Located(
            lat=before.lat + (after.lat - before.lat) * ratio,
            lon=before.lon + (after.lon - before.lon) * ratio,
            captured_at=captured,
            source="interpolated",
            dt_s=min(offset_s - before.t_offset_s, after.t_offset_s - offset_s),
            span_s=span,
            before_offset_s=before.t_offset_s,
            after_offset_s=after.t_offset_s,
        )


__all__ = [
    "DEFAULT_MAX_BRACKET_S",
    "DEFAULT_MAX_FIX_AGE_S",
    "FixSample",
    "FixTrack",
    "Located",
]
