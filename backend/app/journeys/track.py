"""The path a drive actually took, assembled once and used by everything.

A journey holds two positions for every second it lasted, because the front and rear
cameras both record the drive and both burn the same 1 Hz overlay. Every consumer had its
own way of coping with that and no two agreed:

* the distance walk used every stored fix, and measured the same ground twice -- across 59
  journeys on the live library the total came to a median of **2.05x** what the reported
  average speed and duration allow;
* the drawn route took the front camera only, so a stretch the front could not read simply
  vanished from the map;
* the start and end markers were the first and last row by ``captured_at``, which is a
  third population again -- and 10 of 59 journeys had a marker more than 500 m from the end
  of their own drawn route, the worst by 4.7 km.

So the three answers disagreed by construction, and the map showed a line, a pair of
markers and a distance that were each measuring something slightly different.

This module is the single answer: **one position per second of the drive, in time order,
from whichever camera saw it.** The front wins a tie because it is the more useful picture;
the rear fills any second the front could not read, which is the whole reason for not
simply dropping one camera. It is the same idea the heat map already relies on -- counting
distinct seconds rather than rows -- applied to everything else that describes a journey.

Pure functions over plain values: no database, no ORM, no settings. That is what lets the
builder and the API share it rather than growing a second copy each.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.osd.validate import clock_is_plausible


@dataclass(frozen=True, slots=True)
class TrackPoint:
    """One position on the drive, with everything a consumer needs to place it."""

    recording_id: int
    t_offset_s: float
    lat: float | None
    lon: float | None
    speed_kmh: float | None
    #: Wall-clock moment, resolved against the recording rather than trusted from the overlay.
    at: datetime


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def moment_of(
    captured_at: datetime | None, started_at: datetime | None, t_offset_s: float | None
) -> datetime | None:
    """When a sample was taken, preferring the overlay clock but not trusting it blindly.

    The clock is read glyph by glyph like everything else on the strip, so one misread digit
    moves a sample a day, a month or an hour out of the recording it belongs to. A single
    fix in the live library reads ``2026-08-08 05:49:20`` inside a clip recorded on
    ``2026-08-05`` -- one wrong day digit -- and because it then sorted after every other
    point in its journey it became that journey's *end marker*, 3 km from where the drive
    finished.

    The recording's own start plus this sample's offset is the cross-check, because it owes
    nothing to the glyphs. Where the two disagree by more than the clock could plausibly
    drift, the offset wins.
    """
    captured = _as_utc(captured_at)
    started = _as_utc(started_at)
    expected = started + timedelta(seconds=t_offset_s or 0.0) if started is not None else None
    if captured is not None and clock_is_plausible(captured, expected):
        return captured
    return expected


def single_track(
    points,
    starts: dict[int, datetime | None],
    front_ids: set[int],
) -> list[TrackPoint]:
    """One position per second of the drive, in time order.

    *points* is any iterable of rows carrying ``recording_id``, ``t_offset_s``, ``lat``,
    ``lon``, ``speed_kmh`` and ``captured_at``. *starts* maps a recording id to its start
    time and *front_ids* names the recordings from the forward-facing camera.

    A point that cannot be placed in time at all -- no readable clock and no start time for
    its recording -- is dropped, because it cannot be ordered against anything and a
    position with no moment is not a point on a path.
    """
    placed: list[tuple[datetime, bool, TrackPoint]] = []
    for row in points:
        at = moment_of(row.captured_at, starts.get(row.recording_id), row.t_offset_s)
        if at is None:
            continue
        placed.append(
            (
                at,
                row.recording_id in front_ids,
                TrackPoint(
                    recording_id=row.recording_id,
                    t_offset_s=float(row.t_offset_s or 0.0),
                    lat=row.lat,
                    lon=row.lon,
                    speed_kmh=row.speed_kmh,
                    at=at,
                ),
            )
        )

    # Front first within a second, so it wins the tie below without a second comparison.
    placed.sort(key=lambda item: (item[0], not item[1]))

    chosen: dict[int, tuple[bool, TrackPoint]] = {}
    for at, is_front, point in placed:
        second = int(at.timestamp())
        current = chosen.get(second)
        if current is None or (is_front and not current[0]):
            chosen[second] = (is_front, point)
    return [point for _, point in sorted(chosen.values(), key=lambda item: item[1].at)]


__all__ = ["TrackPoint", "moment_of", "single_track"]
