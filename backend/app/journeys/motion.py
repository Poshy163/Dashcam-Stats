"""Require temporal and positional evidence before treating GPS output as a drive.

A correctly recognised overlay is not necessarily correct sensor data. Keep the raw
observations available, but do not let isolated speed spikes or GPS wander prove motion.
This is a display/statistics assessment, never authority to delete parked footage.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass, replace
from itertools import pairwise
from statistics import median

from app.journeys.track import TrackPoint
from app.osd.geo import haversine_m

MOTION_REVISION = "motion-v1"


@dataclass
class MotionAssessment:
    track: list[TrackPoint]
    rejected: set[tuple[int, float]]
    evidence: dict


def assess_motion(track: list[TrackPoint]) -> MotionAssessment:
    """Inspect the chronological, camera-deduplicated track without changing raw data.

    The median test needs context on both sides. A rising/falling legitimate speed ramp
    survives; a brief high island in a low-speed neighbourhood does not. Time windows
    (not array offsets) prevent sparse samples across a GPS outage voting together.
    """
    times = [p.at.timestamp() for p in track]
    rejected: set[tuple[int, float]] = set()
    cleaned = []
    for index, point in enumerate(track):
        speed = point.speed_kmh
        if speed is not None:
            lo = bisect_left(times, times[index] - 10)
            hi = bisect_right(times, times[index] + 10)
            neighbours = [p.speed_kmh for p in track[lo:hi] if p.speed_kmh is not None]
            bounded = times[index] - times[lo] >= 3 and times[hi - 1] - times[index] >= 3
            if bounded and len(neighbours) >= 7:
                centre = median(neighbours)
                if speed > centre + max(20.0, centre * 0.5):
                    rejected.add((point.recording_id, point.t_offset_s))
                    point = replace(point, speed_kmh=None)
        cleaned.append(point)

    # A moving-average alone omits all the stationary seconds. Instead require a
    # contiguous stretch of speed and actual progress, beyond coordinate quantisation.
    # A 10-30 second window retains short trips and bends; gaps and rejected spikes reset
    # the evidence, so separate bursts cannot be glued into one sustained manoeuvre.
    run: list[TrackPoint] = []
    confirmed = False
    longest_s = 0.0
    greatest_progress = 0.0
    for point in cleaned:
        if point.lat is None or point.lon is None or point.speed_kmh is None or point.speed_kmh < 5:
            run = []
            continue
        if run and (point.breaks_segment or (point.at - run[-1].at).total_seconds() > 10):
            run = []
        run.append(point)
        run = [p for p in run if (point.at - p.at).total_seconds() <= 30]
        span = (point.at - run[0].at).total_seconds()
        progress = haversine_m(run[0].lat, run[0].lon, point.lat, point.lon)
        longest_s = max(longest_s, span)
        greatest_progress = max(greatest_progress, progress)
        # Three distinct observations also support telemetry sampled every ten seconds.
        if span >= 10 and len(run) >= 3 and progress >= 50:
            confirmed = True

    # A tunnel/dropout can leave only two fixes. Substantial displacement over a
    # plausible interval is independent evidence, provided both endpoint speeds agree
    # with that displacement. Small jumps, explicit breaks and impossible legs cannot
    # use this fallback. Do not manufacture samples across the missing interval.
    sparse_leg = False
    for before, after in pairwise(cleaned):
        if (
            after.breaks_segment
            or before.lat is None
            or before.lon is None
            or after.lat is None
            or after.lon is None
            or before.speed_kmh is None
            or after.speed_kmh is None
            or min(before.speed_kmh, after.speed_kmh) < 5
        ):
            continue
        elapsed = (after.at - before.at).total_seconds()
        if not 10 < elapsed <= 300:
            continue
        progress = haversine_m(before.lat, before.lon, after.lat, after.lon)
        implied = progress / elapsed * 3.6
        if (
            progress >= 500
            and implied <= 200
            and all(
                speed * 0.5 <= implied <= speed * 1.5
                for speed in (before.speed_kmh, after.speed_kmh)
            )
        ):
            sparse_leg = True

    status = "moving" if confirmed or sparse_leg else "unconfirmed" if track else "unknown"
    return MotionAssessment(
        cleaned,
        rejected,
        {
            "revision": MOTION_REVISION,
            "status": status,
            "reason": (
                "sustained_movement"
                if confirmed
                else "plausible_sparse_leg"
                if sparse_leg
                else "insufficient_movement_evidence"
            ),
            "samples": len(track),
            "rejected_speed_samples": len(rejected),
            "longest_movement_s": round(longest_s, 1),
            "max_window_progress_m": round(greatest_progress, 1),
        },
    )
