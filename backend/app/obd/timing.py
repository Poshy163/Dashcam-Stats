"""Versioned observation-time helpers; receipt time is not ECU measurement time."""

from __future__ import annotations

from datetime import datetime, timedelta

RESPONSE_AGES_KEY = "pid_response_age_ms"
PID_FIELDS: dict[int, tuple[str, ...]] = {
    0x01: ("mil_on", "dtc_count"),
    0x03: ("fuel_system_1",),
    0x04: ("engine_load",),
    0x05: ("coolant_temperature",),
    0x06: ("short_term_fuel_trim_bank_1",),
    0x07: ("long_term_fuel_trim_bank_1",),
    0x0C: ("engine_rpm",),
    0x0D: ("vehicle_speed",),
    0x0E: ("timing_advance",),
    0x0F: ("intake_air_temperature",),
    0x10: ("mass_air_flow",),
    0x11: ("throttle_position",),
    0x14: ("oxygen_sensor_1_voltage", "oxygen_sensor_1_short_term_fuel_trim"),
    0x15: ("oxygen_sensor_2_voltage", "oxygen_sensor_2_short_term_fuel_trim"),
    0x21: ("distance_with_mil",),
}


def nominal_cycle_s(plan: object) -> float:
    return 3.0 if type(plan) is int and plan == 8 else 5.0


def maf_cadence_s(plan: object) -> float:
    # Untimed legacy calculations keep their existing hold separately.
    return 3.0 if type(plan) is int and plan == 8 else 15.0


def has_response_times(quality: object) -> bool:
    return isinstance(quality, dict) and RESPONSE_AGES_KEY in quality


def response_time(
    captured_at: datetime,
    quality: object,
    pid: int,
    *,
    require_timing: bool = False,
) -> datetime | None:
    """An explicit but incomplete map never falls back to a fabricated row-time read."""
    if not has_response_times(quality):
        return None if require_timing else captured_at
    ages = quality[RESPONSE_AGES_KEY]
    age = ages.get(f"{pid:02X}") if isinstance(ages, dict) else None
    if type(age) is not int or not 0 <= age <= 60_000:
        return None
    return captured_at - timedelta(milliseconds=age)


def row_is_live(status: object, quality: object) -> bool:
    return status == "live" and (
        not isinstance(quality, dict) or quality.get("transport", "ok") == "ok"
    )


def pid_is_missing(quality: object, pid: int) -> bool:
    return (
        isinstance(quality, dict)
        and isinstance(quality.get("missing_pids"), list)
        and (pid in quality["missing_pids"])
    )


class MotionAccumulator:
    """Speed receipt intervals for timed rows, with the historical untimed arithmetic."""

    def __init__(self, cycle_s: float = 5.0, *, require_timing: bool = False) -> None:
        self.cycle_s = cycle_s
        self.require_timing = require_timing
        self.distance_km = 0.0
        self.distance_intervals = 0
        self.idle_s = 0.0
        self.idle_evidence = False
        self.previous: (
            tuple[
                object,
                int | None,
                datetime,
                datetime | None,
                float | None,
                float | None,
                bool,
                bool,
            ]
            | None
        ) = None

    def observe(
        self,
        *,
        drive: object,
        sequence: int | None,
        captured_at: datetime,
        speed: float | None,
        rpm: float | None,
        status: object,
        quality: object,
    ) -> None:
        timed = has_response_times(quality) or self.require_timing
        live = row_is_live(status, quality)
        speed_at = (
            response_time(captured_at, quality, 0x0D, require_timing=self.require_timing)
            if speed is not None or not timed
            else None
        )
        rpm_at = (
            response_time(captured_at, quality, 0x0C, require_timing=self.require_timing)
            if rpm is not None
            else None
        )
        if timed and (not live or pid_is_missing(quality, 0x0D)):
            speed_at = None
        if timed and (not live or rpm_at is None or pid_is_missing(quality, 0x0C)):
            rpm = None
        if self.previous is not None:
            (
                prev_drive,
                prev_sequence,
                prev_row_at,
                prev_speed_at,
                prev_speed,
                prev_rpm,
                prev_timed,
                prev_live,
            ) = self.previous
            row_gap = (captured_at - prev_row_at).total_seconds()
            continuous = (not timed and not prev_timed) or (
                timed
                and prev_timed
                and live
                and prev_live
                and drive == prev_drive
                and prev_sequence is not None
                and sequence == prev_sequence + 1
                and 0 < row_gap <= self.cycle_s * 1.5
            )
            gap = (
                (speed_at - prev_speed_at).total_seconds()
                if speed_at is not None and prev_speed_at is not None
                else 0.0
            )
            limit = self.cycle_s * (1.5 if timed else 3)
            if continuous and 0 < gap <= limit:
                if prev_speed is not None and speed is not None:
                    self.distance_km += (prev_speed + speed) / 2 * gap / 3600
                    self.distance_intervals += 1
                if prev_rpm is not None and prev_speed is not None:
                    self.idle_evidence = True
                    if prev_rpm > 300 and prev_speed < 1:
                        self.idle_s += gap
        self.previous = (drive, sequence, captured_at, speed_at, speed, rpm, timed, live)
