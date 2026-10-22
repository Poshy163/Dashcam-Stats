"""Bounded integration of sparse MAF-derived fuel estimates, not measured fuel."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from .timing import RESPONSE_AGES_KEY, has_response_times, response_time


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
    ) -> None:
        quality = quality if isinstance(quality, dict) else {}
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
            if estimated_rate is not None and not invalid:
                self.rate = float(estimated_rate)
                self.rate_at = effective_at
            self.previous_effective_at = effective_at
            self.previous_timed = True
            self.previous_at = captured_at
            self.previous_sequence = sequence
            self.previous_drive = drive
            return
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
        if estimated_rate is not None and not invalid:
            self.rate = float(estimated_rate)
            self.rate_at = captured_at
        self.previous_at = captured_at
        self.previous_sequence = sequence
        self.previous_drive = drive
        self.previous_effective_at = captured_at
        self.previous_timed = False
