"""Defensive drive aggregation; absent telemetry remains absent, never zero."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .schema import parse_utc, utc_text


def calculate_summary(
    drive: dict[str, Any],
    samples: Iterable[dict[str, Any]],
    diagnostics: list[dict[str, Any]],
    *,
    expected_interval_s: float = 5.0,
) -> dict[str, Any]:
    start = parse_utc(str(drive["start_time_utc"]))
    finish = parse_utc(str(drive["finish_time_utc"]))
    duration_s = max(0.0, (finish - start).total_seconds())
    distance_km = 0.0
    idle_s = 0.0
    fuel_l = 0.0
    missing_s = 0.0
    distance_intervals = 0
    idle_evidence = False
    fuel_evidence = False
    max_usable_gap = expected_interval_s * 3
    count = 0
    speed_sum = rpm_sum = 0.0
    speed_count = rpm_count = 0
    max_speed = max_rpm = max_coolant = max_load = None
    previous: dict[str, Any] | None = None
    for current in samples:
        count += 1
        speed = current.get("vehicle_speed")
        if isinstance(speed, int | float):
            speed_sum += float(speed)
            speed_count += 1
            max_speed = float(speed) if max_speed is None else max(max_speed, float(speed))
        rpm = current.get("engine_rpm")
        if isinstance(rpm, int | float):
            rpm_sum += float(rpm)
            rpm_count += 1
            max_rpm = float(rpm) if max_rpm is None else max(max_rpm, float(rpm))
        coolant = current.get("coolant_temperature")
        if isinstance(coolant, int | float):
            max_coolant = (
                float(coolant) if max_coolant is None else max(max_coolant, float(coolant))
            )
        load = current.get("engine_load")
        if isinstance(load, int | float):
            max_load = float(load) if max_load is None else max(max_load, float(load))
        if previous is not None:
            gap = (
                parse_utc(current["timestamp_utc"]) - parse_utc(previous["timestamp_utc"])
            ).total_seconds()
            if gap > 0:
                if gap > expected_interval_s:
                    missing_s += gap - expected_interval_s
                if gap <= max_usable_gap:
                    first_speed = previous.get("vehicle_speed")
                    if isinstance(first_speed, int | float) and isinstance(speed, int | float):
                        distance_km += (float(first_speed) + float(speed)) / 2 * gap / 3600
                        distance_intervals += 1
                    first_rpm = previous.get("engine_rpm")
                    if isinstance(first_rpm, int | float) and isinstance(first_speed, int | float):
                        idle_evidence = True
                        if first_rpm > 300 and first_speed < 1:
                            idle_s += gap
                    fuel_rate = previous.get("estimated_fuel_rate")
                    if isinstance(fuel_rate, int | float) and fuel_rate >= 0:
                        fuel_evidence = True
                        fuel_l += float(fuel_rate) * gap / 3600.0
        previous = current

    expected = max(int(duration_s // expected_interval_s) + 1 if duration_s else 0, count)
    received = min(100.0, count * 100.0 / expected) if expected else 0.0
    dtcs: set[str] = set()
    for event in diagnostics:
        if event.get("kind") in {"confirmed_dtcs", "pending_dtcs", "permanent_dtcs"}:
            codes = event.get("payload", {}).get("codes", [])
            if isinstance(codes, list):
                dtcs.update(str(code) for code in codes)

    summary: dict[str, Any] = {
        "schema_version": 1,
        "drive_id": drive["drive_id"],
        "start_time_utc": utc_text(start),
        "finish_time_utc": utc_text(finish),
        "duration_s": duration_s,
        "distance_km": distance_km if distance_intervals else None,
        "average_speed_kmh": speed_sum / speed_count if speed_count else None,
        "maximum_speed_kmh": max_speed,
        "average_rpm": rpm_sum / rpm_count if rpm_count else None,
        "maximum_rpm": max_rpm,
        "idle_duration_s": idle_s if idle_evidence else None,
        "estimated_fuel_used_l": fuel_l if fuel_evidence else None,
        "average_fuel_consumption_l_per_100km": (
            fuel_l * 100.0 / distance_km if distance_km > 0 and fuel_evidence else None
        ),
        "maximum_coolant_temperature_c": max_coolant,
        "maximum_engine_load_pct": max_load,
        "dtcs_observed": sorted(dtcs),
        "sample_count": count,
        "missing_data_duration_s": missing_s,
        "expected_sample_count": expected,
        "received_sample_percentage": received,
        "clean_end": bool(drive["clean_end"]),
    }
    return summary
