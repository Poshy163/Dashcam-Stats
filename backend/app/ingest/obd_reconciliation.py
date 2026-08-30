"""Idempotent OBD lifecycle projection and tier-aware completeness analysis.

Immutable manifests and raw samples remain source evidence.  This module only rebuilds
the relational projection and its derived JSON, so it is safe to run after every import,
at process startup, and through the operator API.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import OBDBundle, OBDDiagnostic, OBDDrive, OBDSample, utcnow
from app.db.session import get_session_factory

log = structlog.get_logger(__name__)

POLL_PLAN_VERSION = 2
PROJECTION_VERSION = 2
NOMINAL_CYCLE_S = 5.0
GAP_TOLERANCE = 1.5
MAX_RECORDED_GAPS = 100
MAX_CADENCE_VALUES = 20_000
MAX_EXPECTED_CYCLES = 2**63 - 1
MAX_USABLE_ROLLUP_GAP_S = NOMINAL_CYCLE_S * 3


@dataclass(frozen=True, slots=True)
class SignalSpec:
    name: str
    attribute: str
    label: str
    pid: int | None
    tier: str
    every: int
    provenance: str = "measured"
    discrete: bool = False

    @property
    def cadence_s(self) -> float:
        return NOMINAL_CYCLE_S * self.every


# This is byte-for-byte the logger's ServicePolicies.kt v1 schedule. Keep the version in
# stored output so a later logger plan can coexist with historical drives.
SIGNALS: tuple[SignalSpec, ...] = (
    SignalSpec("engine_load", "engine_load_pct", "Engine load", 0x04, "fast", 1),
    SignalSpec("engine_rpm", "engine_rpm", "Engine RPM", 0x0C, "fast", 1),
    SignalSpec("vehicle_speed", "vehicle_speed_kmh", "Vehicle speed", 0x0D, "fast", 1),
    SignalSpec("timing_advance", "timing_advance_deg", "Timing advance", 0x0E, "fast", 1),
    SignalSpec("mass_air_flow", "mass_air_flow_g_s", "Mass air flow", 0x10, "fast", 1),
    SignalSpec("throttle_position", "throttle_position_pct", "Throttle", 0x11, "fast", 1),
    SignalSpec(
        "fuel_system_1", "fuel_system_status", "Fuel system", 0x03, "medium", 3, discrete=True
    ),
    SignalSpec("coolant_temperature", "coolant_temperature_c", "Coolant", 0x05, "medium", 3),
    SignalSpec(
        "short_term_fuel_trim_bank_1",
        "short_term_fuel_trim_bank_1_pct",
        "Short-term fuel trim",
        0x06,
        "medium",
        3,
    ),
    SignalSpec(
        "long_term_fuel_trim_bank_1",
        "long_term_fuel_trim_bank_1_pct",
        "Long-term fuel trim",
        0x07,
        "medium",
        3,
    ),
    SignalSpec(
        "intake_air_temperature", "intake_air_temperature_c", "Intake air", 0x0F, "medium", 3
    ),
    SignalSpec(
        "oxygen_sensor_1_voltage",
        "oxygen_sensor_1_voltage_v",
        "Oxygen sensor 1 voltage",
        0x14,
        "medium",
        3,
    ),
    SignalSpec(
        "oxygen_sensor_1_short_term_fuel_trim",
        "oxygen_sensor_1_short_term_fuel_trim_pct",
        "Oxygen sensor 1 trim",
        0x14,
        "medium",
        3,
    ),
    SignalSpec(
        "oxygen_sensor_2_voltage",
        "oxygen_sensor_2_voltage_v",
        "Oxygen sensor 2 voltage",
        0x15,
        "medium",
        3,
    ),
    SignalSpec(
        "oxygen_sensor_2_short_term_fuel_trim",
        "oxygen_sensor_2_short_term_fuel_trim_pct",
        "Oxygen sensor 2 trim",
        0x15,
        "medium",
        3,
    ),
    SignalSpec(
        "oxygen_sensors_present",
        "oxygen_sensors_present",
        "Oxygen sensors present",
        0x13,
        "slow",
        12,
        discrete=True,
    ),
    SignalSpec("obd_standard", "obd_standard", "OBD standard", 0x1C, "slow", 12, discrete=True),
    SignalSpec("distance_with_mil", "distance_with_mil_km", "Distance with MIL", 0x21, "slow", 12),
    SignalSpec("adapter_voltage", "adapter_voltage_v", "Adapter voltage", None, "fast", 1),
    SignalSpec(
        "estimated_fuel_rate",
        "estimated_fuel_rate_l_h",
        "Estimated fuel rate",
        None,
        "fast",
        1,
        provenance="derived",
    ),
    SignalSpec(
        "estimated_fuel_consumption",
        "estimated_fuel_consumption_l_100km",
        "Estimated fuel consumption",
        None,
        "fast",
        1,
        provenance="derived",
    ),
)

POLL_PHASES_V2: dict[int, int] = {
    # medium.filterIndexed { index % 3 == sequence % 3 }
    0x03: 0,
    0x05: 1,
    0x06: 2,
    0x07: 0,
    0x0F: 1,
    0x14: 2,
    0x15: 0,
    # slow.filterIndexed { index * 4 == sequence % 12 }
    0x13: 0,
    0x1C: 4,
    0x21: 8,
}


def lifecycle_status(*, clean_end: bool, stop_reason: str | None, producer: str | None) -> str:
    """Return the truthful server lifecycle while accepting legacy v1 manifests."""
    if clean_end:
        return "complete"
    if stop_reason == "device_restart" or producer == "recovered":
        return "recovered"
    return "interrupted"


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value is not None else None


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(len(ordered) - 1, lower + 1)
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


@dataclass(slots=True)
class _Cadence:
    count: int = 0
    values: list[float] = field(default_factory=list)
    stride: int = 1
    last_at: datetime | None = None
    first_at: datetime | None = None
    gap_count: int = 0
    total_gap_duration_s: float = 0.0
    longest_gap_s: float | None = None
    maximum_interval_s: float | None = None
    out_of_order_count: int = 0
    gaps: list[dict[str, Any]] = field(default_factory=list)

    def observe(self, captured_at: datetime, expected_s: float) -> None:
        self.count += 1
        if self.first_at is None:
            self.first_at = captured_at
        if self.last_at is not None:
            interval = (captured_at - self.last_at).total_seconds()
            if interval < 0:
                self.out_of_order_count += 1
            else:
                self.maximum_interval_s = (
                    interval
                    if self.maximum_interval_s is None
                    else max(self.maximum_interval_s, interval)
                )
                # Exact for ordinary drives and deterministic/bounded for pathological
                # multi-million-sample archives.
                if self.count % self.stride == 0:
                    self.values.append(interval)
                if len(self.values) > MAX_CADENCE_VALUES:
                    self.values = self.values[::2]
                    self.stride *= 2
                if interval > expected_s * GAP_TOLERANCE:
                    self.gap_count += 1
                    excess = max(0.0, interval - expected_s)
                    self.total_gap_duration_s += excess
                    self.longest_gap_s = (
                        interval
                        if self.longest_gap_s is None
                        else max(self.longest_gap_s, interval)
                    )
                    if len(self.gaps) < MAX_RECORDED_GAPS:
                        self.gaps.append(
                            {
                                "start_at": _iso(self.last_at),
                                "end_at": _iso(captured_at),
                                "duration_s": interval,
                                "excess_s": excess,
                            }
                        )
        self.last_at = captured_at

    def result(self, expected_s: float) -> dict[str, Any]:
        return {
            "observation_count": self.count,
            "first_observed_at": _iso(self.first_at),
            "last_observed_at": _iso(self.last_at),
            "expected_cadence_s": expected_s,
            "gap_threshold_s": expected_s * GAP_TOLERANCE,
            "median_cadence_s": _percentile(self.values, 0.5),
            "p95_cadence_s": _percentile(self.values, 0.95),
            "p99_cadence_s": _percentile(self.values, 0.99),
            "maximum_cadence_s": self.maximum_interval_s,
            "cadence_is_sampled": self.stride > 1,
            "gap_count": self.gap_count,
            "total_gap_duration_s": self.total_gap_duration_s,
            "longest_gap_s": self.longest_gap_s,
            "out_of_order_count": self.out_of_order_count,
            "gaps": self.gaps,
            "gaps_truncated": self.gap_count > len(self.gaps),
        }


@dataclass(slots=True)
class _SignalState:
    spec: SignalSpec
    cadence: _Cadence = field(default_factory=_Cadence)
    received_count: int = 0
    missing_run_count: int = 0
    longest_missing_run: int = 0
    _current_missing_run: int = 0
    _next_opportunity: int = 0

    def _add_missing(self, count: int) -> None:
        """Append a missing scheduled block without iterating across absent cycles."""
        if count <= 0:
            return
        if self._current_missing_run == 0:
            self.missing_run_count += 1
        self._current_missing_run = min(
            MAX_EXPECTED_CYCLES,
            self._current_missing_run + count,
        )
        self.longest_missing_run = max(self.longest_missing_run, self._current_missing_run)

    def add(self, row: OBDSample, *, phase: int = 0) -> None:
        value = getattr(row, self.spec.attribute)
        if value is not None:
            self.cadence.observe(row.captured_at, self.spec.cadence_s)
        if row.sequence < phase or row.sequence % self.spec.every != phase:
            return
        opportunity = (row.sequence - phase) // self.spec.every
        # A corrupt historical sequence can reach SQLite's signed-integer ceiling. Keep
        # the projection bounded while still reporting that its expected counts were
        # capped; ordinary logger sequences never approach this branch.
        if opportunity >= MAX_EXPECTED_CYCLES:
            return
        self._add_missing(opportunity - self._next_opportunity)
        if value is None:
            self._add_missing(1)
        else:
            self.received_count += 1
            self._current_missing_run = 0
        self._next_opportunity = opportunity + 1

    def finish(
        self,
        *,
        supported: bool,
        expected_cycles: int,
        phase: int,
    ) -> dict[str, Any]:
        expected = _phase_opportunities(expected_cycles, self.spec.every, phase) if supported else 0
        if supported:
            self._add_missing(expected - self._next_opportunity)
        received = self.received_count if supported else self.cadence.count
        missing = max(0, expected - received)
        result = {
            "name": self.spec.name,
            "label": self.spec.label,
            "pid": f"{self.spec.pid:02X}" if self.spec.pid is not None else None,
            "tier": self.spec.tier,
            "provenance": self.spec.provenance,
            "discrete": self.spec.discrete,
            "supported": supported,
            "expected_observation_count": expected,
            "received_observation_count": received,
            "missing_observation_count": missing,
            "missing_run_count": self.missing_run_count if supported else 0,
            "longest_missing_run": self.longest_missing_run if supported else 0,
            "coverage_percentage": (100.0 * received / expected if expected else None),
        }
        result.update(self.cadence.result(self.spec.cadence_s))
        return result


@dataclass(slots=True)
class _Rollup:
    """Streaming canonical summary arithmetic over immutable typed sample rows."""

    sample_count: int = 0
    speed_sum: float = 0.0
    speed_count: int = 0
    rpm_sum: float = 0.0
    rpm_count: int = 0
    maximum_speed_kmh: float | None = None
    maximum_rpm: float | None = None
    maximum_coolant_temperature_c: float | None = None
    maximum_engine_load_pct: float | None = None
    distance_km: float = 0.0
    distance_intervals: int = 0
    idle_duration_s: float = 0.0
    idle_evidence: bool = False
    estimated_fuel_used_l: float = 0.0
    fuel_evidence: bool = False
    previous_at: datetime | None = None
    previous_speed: float | None = None
    previous_rpm: float | None = None
    previous_fuel_rate: float | None = None

    def observe(self, row: OBDSample) -> None:
        self.sample_count += 1
        speed = row.vehicle_speed_kmh
        rpm = row.engine_rpm
        coolant = row.coolant_temperature_c
        engine_load = row.engine_load_pct
        fuel_rate = row.estimated_fuel_rate_l_h

        if speed is not None:
            speed = float(speed)
            self.speed_sum += speed
            self.speed_count += 1
            self.maximum_speed_kmh = (
                speed if self.maximum_speed_kmh is None else max(self.maximum_speed_kmh, speed)
            )
        if rpm is not None:
            rpm = float(rpm)
            self.rpm_sum += rpm
            self.rpm_count += 1
            self.maximum_rpm = rpm if self.maximum_rpm is None else max(self.maximum_rpm, rpm)
        if coolant is not None:
            coolant = float(coolant)
            self.maximum_coolant_temperature_c = (
                coolant
                if self.maximum_coolant_temperature_c is None
                else max(self.maximum_coolant_temperature_c, coolant)
            )
        if engine_load is not None:
            engine_load = float(engine_load)
            self.maximum_engine_load_pct = (
                engine_load
                if self.maximum_engine_load_pct is None
                else max(self.maximum_engine_load_pct, engine_load)
            )
        if fuel_rate is not None:
            fuel_rate = float(fuel_rate)

        if self.previous_at is not None:
            interval = (row.captured_at - self.previous_at).total_seconds()
            if 0 < interval <= MAX_USABLE_ROLLUP_GAP_S:
                if self.previous_speed is not None and speed is not None:
                    self.distance_km += (self.previous_speed + speed) / 2 * interval / 3600
                    self.distance_intervals += 1
                if self.previous_rpm is not None and self.previous_speed is not None:
                    self.idle_evidence = True
                    if self.previous_rpm > 300 and self.previous_speed < 1:
                        self.idle_duration_s += interval
                if self.previous_fuel_rate is not None and self.previous_fuel_rate >= 0:
                    self.fuel_evidence = True
                    self.estimated_fuel_used_l += self.previous_fuel_rate * interval / 3600

        self.previous_at = row.captured_at
        self.previous_speed = speed
        self.previous_rpm = rpm
        self.previous_fuel_rate = fuel_rate


def _phase_opportunities(cycles: int, every: int, phase: int) -> int:
    """Count scheduled indices in ``range(cycles)`` in constant time."""
    if cycles <= phase:
        return 0
    return 1 + (cycles - 1 - phase) // every


def _expected_cycles(
    *,
    started_at: datetime,
    finished_at: datetime,
    sample_count: int,
    maximum_sequence: int | None,
) -> tuple[int, bool]:
    """Combine logger sequence and wall time without materialising absent cycle rows."""
    duration_s = max(0.0, (finished_at - started_at).total_seconds())
    from_time = int(duration_s // NOMINAL_CYCLE_S) + 1 if duration_s > 0 else 0
    from_sequence = maximum_sequence + 1 if maximum_sequence is not None else 0
    uncapped = max(sample_count, from_time, from_sequence)
    return min(uncapped, MAX_EXPECTED_CYCLES), uncapped > MAX_EXPECTED_CYCLES


def _supported_pids(diagnostics: list[OBDDiagnostic]) -> tuple[bool, set[int]]:
    scans = [row for row in diagnostics if row.kind == "mode01_support"]
    supported: set[int] = set()
    for row in scans:
        values = (
            row.payload_json.get("supported_pids") if isinstance(row.payload_json, dict) else None
        )
        if isinstance(values, list):
            supported.update(
                value for value in values if isinstance(value, int) and not isinstance(value, bool)
            )
    return bool(scans), supported


def _has_successful_measurement(row: OBDSample) -> bool:
    return any(
        getattr(row, spec.attribute) is not None
        for spec in SIGNALS
        if spec.provenance == "measured"
    )


def _latest_time(*values: datetime | None) -> datetime | None:
    present = [value for value in values if value is not None]
    return max(present) if present else None


def _summary_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _observed_dtcs(diagnostics: list[OBDDiagnostic]) -> list[str]:
    result: set[str] = set()
    for row in diagnostics:
        if row.kind not in {"confirmed_dtcs", "pending_dtcs", "permanent_dtcs"}:
            continue
        codes = row.payload_json.get("codes") if isinstance(row.payload_json, dict) else None
        if isinstance(codes, list):
            result.update(code for code in codes if isinstance(code, str))
    return sorted(result)


def _projection_fingerprint(value: dict[str, Any]) -> str:
    canonical = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def reconcile_drive_projection(
    session: AsyncSession,
    drive: OBDDrive,
    *,
    summary_source: str | None = None,
) -> dict[str, Any]:
    """Rebuild one drive's canonical projection from immutable samples and diagnostics."""
    now = utcnow()
    try:
        # Retained only so existing callers do not need a coordinated signature change.
        # Producer summaries are evidence, never the canonical relational projection.
        _ = summary_source
        bundle = await session.get(OBDBundle, drive.bundle_id)
        if bundle is None:
            raise ValueError("drive references a missing OBD bundle")
        diagnostics = (
            (
                await session.execute(
                    select(OBDDiagnostic)
                    .where(OBDDiagnostic.drive_db_id == drive.id)
                    .order_by(OBDDiagnostic.observed_at.asc(), OBDDiagnostic.id.asc())
                )
            )
            .scalars()
            .all()
        )
        has_support_scan, advertised_pids = _supported_pids(diagnostics)
        raw_poll_plan = (
            drive.manifest_json.get("poll_plan_version")
            if isinstance(drive.manifest_json, dict)
            else None
        )
        poll_plan_version = raw_poll_plan if raw_poll_plan == POLL_PLAN_VERSION else 1
        missing_pids: set[int] = set()
        signal_states = {spec.name: _SignalState(spec) for spec in SIGNALS}
        transport = _Cadence()
        rollup = _Rollup()
        first_sample: datetime | None = None
        last_sample: datetime | None = None
        last_success: datetime | None = None
        sequence_gaps = 0
        previous_sequence: int | None = None
        maximum_sequence: int | None = None

        stream = await session.stream_scalars(
            select(OBDSample)
            .where(OBDSample.drive_db_id == drive.id)
            .order_by(OBDSample.sequence.asc(), OBDSample.id.asc())
        )
        async for sample in stream:
            first_sample = (
                sample.captured_at
                if first_sample is None
                else min(first_sample, sample.captured_at)
            )
            last_sample = (
                sample.captured_at if last_sample is None else max(last_sample, sample.captured_at)
            )
            transport.observe(sample.captured_at, NOMINAL_CYCLE_S)
            rollup.observe(sample)
            if previous_sequence is None:
                if sample.sequence > 0:
                    sequence_gaps = min(MAX_EXPECTED_CYCLES, sample.sequence)
            elif sample.sequence > previous_sequence + 1:
                sequence_gaps = min(
                    MAX_EXPECTED_CYCLES,
                    sequence_gaps + sample.sequence - previous_sequence - 1,
                )
            previous_sequence = sample.sequence
            maximum_sequence = (
                sample.sequence
                if maximum_sequence is None
                else max(maximum_sequence, sample.sequence)
            )
            if _has_successful_measurement(sample):
                last_success = _latest_time(last_success, sample.captured_at)
            quality = sample.quality_json if isinstance(sample.quality_json, dict) else {}
            raw_missing = quality.get("missing_pids")
            if isinstance(raw_missing, list):
                missing_pids.update(
                    value
                    for value in raw_missing
                    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 255
                )
            for state in signal_states.values():
                phase = (
                    POLL_PHASES_V2.get(state.spec.pid, 0)
                    if poll_plan_version == POLL_PLAN_VERSION
                    else 0
                )
                state.add(sample, phase=phase)

        lifecycle = lifecycle_status(
            clean_end=bool(drive.clean_end),
            stop_reason=drive.stop_reason,
            producer=drive.completion_status,
        )
        producer_finished = bundle.drive_finished_at

        def manifest_time(name: str) -> datetime | None:
            value = drive.manifest_json.get(name) if isinstance(drive.manifest_json, dict) else None
            if not isinstance(value, str):
                return None
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return None
            return parsed.astimezone(UTC) if parsed.tzinfo is not None else None

        manifest_last_sample = manifest_time("last_sample_at_utc")
        manifest_last_response = manifest_time("last_successful_obd_response_at_utc")
        manifest_noticed = manifest_time("termination_noticed_at_utc")
        manifest_finalised = manifest_time("finalised_at_utc")
        finalization_observed = manifest_finalised or manifest_noticed or producer_finished
        successful_response = _latest_time(last_success, manifest_last_response)
        if lifecycle in {"interrupted", "recovered"}:
            effective_finished = last_sample or successful_response or producer_finished
        else:
            effective_finished = producer_finished
        if effective_finished < drive.started_at:
            effective_finished = drive.started_at
        duration_s = max(0.0, (effective_finished - drive.started_at).total_seconds())
        expected_cycles, expected_cycles_capped = _expected_cycles(
            started_at=drive.started_at,
            finished_at=effective_finished,
            sample_count=rollup.sample_count,
            maximum_sequence=maximum_sequence,
        )

        signals: list[dict[str, Any]] = []
        received_total = 0
        expected_total = 0
        for state in signal_states.values():
            spec = state.spec
            if spec.pid is None:
                supported = spec.name == "adapter_voltage" or state.cadence.count > 0
            elif has_support_scan:
                supported = spec.pid in advertised_pids
            else:
                supported = state.cadence.count > 0 or spec.pid in missing_pids
            phase = POLL_PHASES_V2.get(spec.pid, 0) if poll_plan_version == POLL_PLAN_VERSION else 0
            result = state.finish(
                supported=supported,
                expected_cycles=expected_cycles,
                phase=phase,
            )
            signals.append(result)
            if supported and spec.provenance == "measured":
                received_total += int(result["received_observation_count"])
                expected_total += int(result["expected_observation_count"])

        transport_result = transport.result(NOMINAL_CYCLE_S)
        received_percentage = (
            min(100.0, 100.0 * transport.count / expected_cycles) if expected_cycles else 0.0
        )
        transport_result.update(
            {
                "expected_observation_count": expected_cycles,
                "received_observation_count": transport.count,
                "missing_observation_count": max(0, expected_cycles - transport.count),
                "sequence_gap_count": sequence_gaps,
                "coverage_percentage": received_percentage,
            }
        )
        completeness = 100.0 * received_total / expected_total if expected_total else None
        raw_interruption = (
            drive.manifest_json.get("interruption_reason")
            if isinstance(drive.manifest_json, dict)
            else None
        )
        interruption_reason = (
            None
            if lifecycle == "complete"
            else (
                (raw_interruption if isinstance(raw_interruption, str) else None)
                or drive.stop_reason
                or "unclean_end"
            )
        )
        connection_loss_count = max(
            sum(1 for row in diagnostics if row.kind == "connection_failure"),
            1 if drive.stop_reason == "connection_lost" else 0,
        )
        dtcs = _observed_dtcs(diagnostics)
        distance_km = rollup.distance_km if rollup.distance_intervals else None
        fuel_used_l = rollup.estimated_fuel_used_l if rollup.fuel_evidence else None
        summary: dict[str, Any] = {
            "schema_version": 1,
            "drive_id": drive.drive_id,
            "start_time_utc": _summary_time(drive.started_at),
            "finish_time_utc": _summary_time(effective_finished),
            "duration_s": duration_s,
            "distance_km": distance_km,
            "average_speed_kmh": (
                rollup.speed_sum / rollup.speed_count if rollup.speed_count else None
            ),
            "maximum_speed_kmh": rollup.maximum_speed_kmh,
            "average_rpm": rollup.rpm_sum / rollup.rpm_count if rollup.rpm_count else None,
            "maximum_rpm": rollup.maximum_rpm,
            "idle_duration_s": rollup.idle_duration_s if rollup.idle_evidence else None,
            "estimated_fuel_used_l": fuel_used_l,
            "average_fuel_consumption_l_per_100km": (
                fuel_used_l * 100.0 / distance_km
                if fuel_used_l is not None and distance_km is not None and distance_km > 0
                else None
            ),
            "maximum_coolant_temperature_c": rollup.maximum_coolant_temperature_c,
            "maximum_engine_load_pct": rollup.maximum_engine_load_pct,
            "dtcs_observed": dtcs,
            "sample_count": rollup.sample_count,
            "missing_data_duration_s": float(transport_result["total_gap_duration_s"]),
            "expected_sample_count": expected_cycles,
            "received_sample_percentage": received_percentage,
            "clean_end": bool(drive.clean_end),
        }
        if raw_poll_plan == POLL_PLAN_VERSION:
            summary.update(
                {
                    "last_sample_at_utc": _summary_time(last_sample) if last_sample else None,
                    "termination_noticed_at_utc": (
                        _summary_time(manifest_noticed) if manifest_noticed else None
                    ),
                    "finalised_at_utc": (
                        _summary_time(manifest_finalised) if manifest_finalised else None
                    ),
                    "completion_status": lifecycle,
                    "interruption_reason": interruption_reason,
                }
            )

        manifest_last_sample_matches = (
            None if manifest_last_sample is None else manifest_last_sample == last_sample
        )
        gap_analysis: dict[str, Any] = {
            "schema_version": 1,
            "projection_version": PROJECTION_VERSION,
            "poll_plan_version": poll_plan_version,
            "nominal_cycle_s": NOMINAL_CYCLE_S,
            "gap_tolerance": GAP_TOLERANCE,
            "expected_cycle_count": expected_cycles,
            "expected_cycle_count_capped": expected_cycles_capped,
            "supported_pid_source": "mode01_support" if has_support_scan else "inferred",
            "supported_pids": [f"{pid:02X}" for pid in sorted(advertised_pids)],
            "transport": transport_result,
            "signals": signals,
            "aggregate_signal_completeness_percentage": completeness,
            "manifest_consistency": {
                "manifest_last_sample_at": _iso(manifest_last_sample),
                "raw_last_sample_at": _iso(last_sample),
                "last_sample_matches_manifest": manifest_last_sample_matches,
            },
        }
        projection_document = {
            "projection_version": PROJECTION_VERSION,
            "lifecycle_status": lifecycle,
            "interruption_reason": interruption_reason,
            "first_sample_at": _iso(first_sample),
            "last_sample_at": _iso(last_sample),
            "last_successful_response_at": _iso(successful_response),
            "finalization_observed_at": _iso(finalization_observed),
            "connection_loss_count": connection_loss_count,
            "summary": summary,
            "gap_analysis": gap_analysis,
        }
        fingerprint = _projection_fingerprint(projection_document)
        gap_analysis["projection_fingerprint"] = fingerprint
        canonical_values: dict[str, Any] = {
            "finished_at": effective_finished,
            "lifecycle_status": lifecycle,
            "interruption_reason": interruption_reason,
            "first_sample_at": first_sample,
            "last_sample_at": last_sample,
            "last_successful_response_at": successful_response,
            "finalization_observed_at": finalization_observed,
            "connection_loss_count": connection_loss_count,
            "gap_count": int(transport_result["gap_count"]),
            "longest_gap_s": transport_result["longest_gap_s"],
            "data_completeness_percentage": completeness,
            "gap_analysis_json": gap_analysis,
            "processing_status": "ready",
            "last_processing_error": None,
            "summary_source": "derived",
            "duration_s": summary["duration_s"],
            "distance_km": summary["distance_km"],
            "average_speed_kmh": summary["average_speed_kmh"],
            "maximum_speed_kmh": summary["maximum_speed_kmh"],
            "average_rpm": summary["average_rpm"],
            "maximum_rpm": summary["maximum_rpm"],
            "idle_duration_s": summary["idle_duration_s"],
            "estimated_fuel_used_l": summary["estimated_fuel_used_l"],
            "average_fuel_consumption_l_100km": summary["average_fuel_consumption_l_per_100km"],
            "maximum_coolant_temperature_c": summary["maximum_coolant_temperature_c"],
            "maximum_engine_load_pct": summary["maximum_engine_load_pct"],
            "missing_data_duration_s": summary["missing_data_duration_s"],
            "expected_sample_count": summary["expected_sample_count"],
            "received_sample_percentage": summary["received_sample_percentage"],
            "sample_count": summary["sample_count"],
            "dtcs_observed": summary["dtcs_observed"],
            "summary_json": summary,
        }
        prior_analysis = (
            drive.gap_analysis_json if isinstance(drive.gap_analysis_json, dict) else {}
        )
        projection_changed = (
            prior_analysis.get("projection_fingerprint") != fingerprint
            or drive.summary_generated_at is None
            or any(getattr(drive, key) != value for key, value in canonical_values.items())
        )
        for key, value in canonical_values.items():
            setattr(drive, key, value)
        if projection_changed:
            drive.summary_generated_at = now
        await session.flush()
        return {
            "drive_id": drive.drive_id,
            "status": "ready",
            "changed": projection_changed,
            "projection_fingerprint": fingerprint,
            "lifecycle_status": lifecycle,
            "gap_count": drive.gap_count,
            "longest_gap_s": drive.longest_gap_s,
            "data_completeness_percentage": completeness,
        }
    except Exception as exc:
        drive.processing_status = "error"
        drive.last_processing_error = f"{type(exc).__name__}: {exc}"[:2048]
        await session.flush()
        log.exception("OBD drive reconciliation failed", drive_id=drive.drive_id)
        return {
            "drive_id": drive.drive_id,
            "status": "error",
            "error": drive.last_processing_error,
        }


async def reconcile_all_drives(
    factory: async_sessionmaker[AsyncSession] | None = None,
) -> dict[str, int]:
    """Reconcile all stored drives in small transactions for restart safety."""
    session_factory = factory or get_session_factory()
    async with session_factory() as session:
        drive_ids = list(
            (await session.execute(select(OBDDrive.id).order_by(OBDDrive.id.asc()))).scalars()
        )
    ready = errors = 0
    for drive_db_id in drive_ids:
        async with session_factory() as session:
            drive = await session.get(OBDDrive, drive_db_id)
            if drive is None:
                continue
            result = await reconcile_drive_projection(session, drive)
            await session.commit()
            if result["status"] == "ready":
                ready += 1
            else:
                errors += 1
    log.info("OBD reconciliation complete", drives=ready + errors, ready=ready, errors=errors)
    return {"drives": ready + errors, "ready": ready, "errors": errors}


__all__ = [
    "GAP_TOLERANCE",
    "NOMINAL_CYCLE_S",
    "POLL_PLAN_VERSION",
    "SIGNALS",
    "lifecycle_status",
    "reconcile_all_drives",
    "reconcile_drive_projection",
]
