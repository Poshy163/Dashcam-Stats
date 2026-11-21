"""Bounded integration of sparse MAF-derived fuel estimates, not measured fuel."""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .timing import RESPONSE_AGES_KEY, RateInterval, has_response_times, response_time


@dataclass(slots=True)
class EstimatedFuelAccumulator:
    nominal_cycle_s: float = 5.0
    maf_cadence_s: float = 15.0
    require_timing: bool = False
    litres: float = 0.0
    has_evidence: bool = False
    previous_at: datetime | None = None
    previous_sequence: int | None = None
    previous_drive: int | str | None = None
    rate_at: datetime | None = None
    rate: float | None = None
    previous_effective_at: datetime | None = None
    previous_timed: bool | None = None

    def observe(
        self,
        *,
        drive: int | str | None,
        sequence: int | None,
        captured_at: datetime,
        estimated_rate: float | None,
        ecu_data_status: str | None,
        quality: object,
    ) -> RateInterval | None:
        quality = quality if isinstance(quality, dict) else {}
        covered = None
        missing_pids = quality.get("missing_pids")
        invalid = (
            ecu_data_status != "live"
            or quality.get("transport", "ok") != "ok"
            or (isinstance(missing_pids, list) and 0x10 in missing_pids)
            or (
                estimated_rate is not None
                and (not math.isfinite(estimated_rate) or estimated_rate < 0)
            )
        )
        interval = (
            (captured_at - self.previous_at).total_seconds()
            if self.previous_at is not None
            else 0.0
        )
        contiguous = (
            0 < interval <= self.nominal_cycle_s * 1.5
            and self.previous_drive == drive
            and self.previous_sequence is not None
            and sequence == self.previous_sequence + 1
        )
        timed = has_response_times(quality) or self.require_timing
        invalid = invalid or (timed and not isinstance(quality.get(RESPONSE_AGES_KEY), dict))
        effective_at = (
            response_time(captured_at, quality, 0x10, require_timing=self.require_timing)
            if timed and estimated_rate is not None
            else captured_at
        )
        if timed:
            invalid = invalid or effective_at is None
            if self.previous_timed is not None and not self.previous_timed:
                contiguous = False
            effective_interval = (
                (effective_at - self.previous_effective_at).total_seconds()
                if effective_at is not None and self.previous_effective_at is not None
                else 0.0
            )
            reversed_time = self.previous_effective_at is not None and effective_interval <= 0
            if reversed_time:
                invalid = True
                effective_at = captured_at
            if invalid or not contiguous or effective_interval <= 0:
                self.rate = None
                self.rate_at = None
            elif self.rate is not None and self.rate_at is not None:
                age_at_start = (self.previous_effective_at - self.rate_at).total_seconds()
                covered_s = min(
                    effective_interval, max(0.0, self.maf_cadence_s * 1.5 - age_at_start)
                )
                if covered_s > 0:
                    self.has_evidence = True
                    self.litres += self.rate * covered_s / 3600
                    covered = RateInterval(
                        self.previous_effective_at,
                        self.previous_effective_at + timedelta(seconds=covered_s),
                        self.rate,
                        self.rate,
                    )
            if estimated_rate is not None and not invalid:
                self.rate = float(estimated_rate)
                self.rate_at = effective_at
            self.previous_effective_at = effective_at
            self.previous_timed = True
            self.previous_at = captured_at
            self.previous_sequence = sequence
            self.previous_drive = drive
            return covered
        if self.previous_timed:
            contiguous = False
        if invalid or not contiguous:
            self.rate = None
            self.rate_at = None
        elif self.rate is not None and self.rate_at is not None:
            # MAF is polled every third cycle. Scheduled null rows do not erase its
            # estimate, but never bridge lost rows, failed reads, or an expired rate.
            age_at_start = (self.previous_at - self.rate_at).total_seconds()
            covered_s = min(interval, max(0.0, self.nominal_cycle_s * 3 * 1.5 - age_at_start))
            if covered_s > 0:
                self.has_evidence = True
                self.litres += self.rate * covered_s / 3600
                covered = RateInterval(
                    self.previous_at,
                    self.previous_at + timedelta(seconds=covered_s),
                    self.rate,
                    self.rate,
                )
        if estimated_rate is not None and not invalid:
            self.rate = float(estimated_rate)
            self.rate_at = captured_at
        self.previous_at = captured_at
        self.previous_sequence = sequence
        self.previous_drive = drive
        self.previous_effective_at = captured_at
        self.previous_timed = False
        return covered


@dataclass(slots=True)
class MatchedFuelEconomy:
    """Join independently timed speed and fuel intervals without extending either signal."""

    distance_km: float = 0.0
    litres: float = 0.0
    duration_s: float = 0.0
    speeds: deque[RateInterval] = field(default_factory=deque)
    fuels: deque[RateInterval] = field(default_factory=deque)
    previous: tuple[object, int | None, bool] | None = None
    last_match_end: datetime | None = None
    not_before: datetime | None = None

    def observe(
        self,
        drive: object,
        sequence: int | None,
        captured_at: datetime,
        timed: bool,
        speed: RateInterval | None,
        fuel: RateInterval | None,
    ) -> None:
        previous = self.previous
        self.previous = (drive, sequence, timed)
        if sequence is None or previous != (drive, sequence - 1, timed):
            self.speeds.clear()
            self.fuels.clear()
            return
        # A valid reply can predate the row by 60 seconds, and its interval begins
        # at the previous reply (at most another 15 seconds earlier).
        cutoff = captured_at - timedelta(seconds=75)
        for queue, interval in ((self.speeds, speed), (self.fuels, fuel)):
            while queue and queue[0].end <= cutoff:
                queue.popleft()
            if interval is not None:
                queue.append(interval)
        while self.speeds and self.fuels:
            moving, burning = self.speeds[0], self.fuels[0]
            start, end = max(moving.start, burning.start), min(moving.end, burning.end)
            if self.not_before is not None:
                start = max(start, self.not_before)
            if self.last_match_end is not None:
                start = max(start, self.last_match_end)
            if start < end:
                self.distance_km += moving.integral(start, end)
                self.litres += burning.integral(start, end)
                self.duration_s += (end - start).total_seconds()
                self.last_match_end = end
            if moving.end <= burning.end:
                self.speeds.popleft()
            if burning.end <= moving.end:
                self.fuels.popleft()

    @property
    def consumption(self) -> float | None:
        return fuel_consumption(self.litres, self.distance_km)


def fuel_consumption(litres: float | None, distance_km: float | None) -> float | None:
    if litres is None or distance_km is None or distance_km <= 0:
        return None
    ratio = 100 * litres / distance_km
    return ratio if math.isfinite(ratio) else None


def economy_coverage(matched: float | None, total: float | None) -> float | None:
    if matched is None or total is None or total <= 0:
        return None
    if math.isclose(matched, total, rel_tol=1e-12):
        return 100.0
    return min(100.0, 100 * matched / total)
