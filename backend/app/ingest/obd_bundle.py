"""Strict validation and transactional storage for dashcam OBD export bundles.

The server is the primary high-resolution history. External consumers can read the latest
state plus bounded hourly statistics, but every validated five-second sample is retained
here first.  Validation is deliberately a filesystem-only, streaming operation so it can
run in ``asyncio.to_thread`` and never hold up the API event loop.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import math
import re
import stat
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, BinaryIO

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import AppConfig, get_config
from app.db.models import (
    OBDBundle,
    OBDBundleState,
    OBDDiagnostic,
    OBDDrive,
    OBDSample,
    utcnow,
)

SCHEMA_VERSION = 1
BUNDLE_SUFFIX = ".obd2.zip"
PARTIAL_SUFFIX = ".partial"
MEMBERS = frozenset({"manifest.json", "samples.ndjson.gz", "diagnostics.json", "summary.json"})
CORE_MEMBERS = frozenset({"manifest.json", "samples.ndjson.gz", "diagnostics.json"})
PAYLOAD_MEMBERS = frozenset({"samples.ndjson.gz", "diagnostics.json", "summary.json"})
SAFE_DRIVE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
SAFE_VEHICLE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
SAFE_SAMPLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,95}$")
DTC_RE = re.compile(r"^[PCBU][0-9A-F]{4}$")
PID_HEX_RE = re.compile(r"^[0-9A-F]{2}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
PIPELINE_METRIC_FIELDS_V1 = frozenset(
    {
        "commands_requested",
        "commands_completed",
        "command_timeouts",
        "notifications_received",
        "notification_fragments_received",
        "frames_assembled",
        "checksum_failures",
        "parse_failures",
        "samples_created",
        "samples_queued",
        "samples_persisted",
        "samples_dropped",
        "database_write_failures",
        "ble_disconnects",
        "reconnect_attempts",
        "radio_shutdowns",
        "queue_depth",
        "maximum_queue_depth",
    }
)
# Android extended the diagnostic without changing the surrounding diagnostics-v1 envelope.
# Keep both exact shapes: already-exported legacy drives remain readable, while the current
# producer is still fail-closed for unknown fields. The timing medians are the only conditional
# keys -- JSONObject omits them when a rolling distribution has no samples.
PIPELINE_METRIC_COUNTER_FIELDS_CURRENT = frozenset(
    {
        "commands_requested",
        "commands_blocked",
        "commands_sent",
        "commands_completed",
        "command_timeouts",
        "adapter_local_commands",
        "vehicle_bus_commands",
        "ble_connection_attempts",
        "adapter_targets_resolved",
        "gatt_connections_established",
        "notification_subscriptions_enabled",
        "ble_connection_successes",
        "ble_connection_failures",
        "voltage_reads_successful",
        "voltage_reads_failed",
        "invalid_voltage_responses",
        "notifications_received",
        "notification_fragments_received",
        "frames_assembled",
        "checksum_failures",
        "parse_failures",
        "samples_created",
        "samples_queued",
        "samples_persisted",
        "samples_dropped",
        "database_write_failures",
        "ble_disconnects",
        "reconnect_attempts",
        "radio_shutdowns",
    }
)
PIPELINE_METRIC_TIMING_NAMES_CURRENT = frozenset(
    {
        "connection_time",
        "command_response_time",
        "voltage_response_time",
        "connected_time",
    }
)
PIPELINE_METRIC_FIELDS_CURRENT_REQUIRED = (
    PIPELINE_METRIC_COUNTER_FIELDS_CURRENT
    | frozenset(
        {
            "queue_depth",
            "maximum_queue_depth",
            "observation_window_ms",
            "polling_duty_cycle_percent",
        }
    )
    | frozenset(
        field_name
        for name in PIPELINE_METRIC_TIMING_NAMES_CURRENT
        for field_name in (
            f"{name}_samples",
            f"maximum_{name}_ms",
            f"total_{name}_ms",
        )
    )
)
PIPELINE_METRIC_FIELDS_CURRENT_OPTIONAL = frozenset(
    f"median_{name}_ms" for name in PIPELINE_METRIC_TIMING_NAMES_CURRENT
)
# Compatibility for code which imported the original constant before the two supported
# shapes were named explicitly.
PIPELINE_METRIC_FIELDS = PIPELINE_METRIC_FIELDS_V1
PIPELINE_METRIC_COUNTER_MAXIMUM = 2**31 - 1
PIPELINE_METRIC_TIMING_MAXIMUM_MS = 1_000_000_000_000
PIPELINE_METRIC_TIMING_SAMPLE_LIMIT = 256
MAX_JSON_MEMBER_BYTES = 2 * 1024 * 1024
MAX_SAMPLE_LINE_BYTES = 64 * 1024
MAX_DIAGNOSTIC_EVENTS = 4096
MAX_DRIVE_SPAN = timedelta(days=31)
HARDENED_SUMMARY_FIELDS = frozenset(
    {
        "last_sample_at_utc",
        "termination_noticed_at_utc",
        "finalised_at_utc",
        "completion_status",
        "interruption_reason",
    }
)
SUMMARY_FIELDS_V1 = frozenset(
    {
        "schema_version",
        "drive_id",
        "start_time_utc",
        "finish_time_utc",
        "duration_s",
        "distance_km",
        "average_speed_kmh",
        "maximum_speed_kmh",
        "average_rpm",
        "maximum_rpm",
        "idle_duration_s",
        "estimated_fuel_used_l",
        "average_fuel_consumption_l_per_100km",
        "maximum_coolant_temperature_c",
        "maximum_engine_load_pct",
        "dtcs_observed",
        "sample_count",
        "missing_data_duration_s",
        "expected_sample_count",
        "received_sample_percentage",
        "clean_end",
    }
)

# Byte-for-byte shared with the logger contract. Keeping this in
# one place makes a unit spelling change a schema version change rather than a silent data
# conversion during import.
UNITS_V1: dict[str, str] = {
    "engine_rpm": "rpm",
    "vehicle_speed": "km/h",
    "coolant_temperature": "°C",
    "intake_air_temperature": "°C",
    "engine_load": "%",
    "throttle_position": "%",
    "timing_advance": "°",
    "mass_air_flow": "g/s",
    "short_term_fuel_trim_bank_1": "%",
    "long_term_fuel_trim_bank_1": "%",
    "oxygen_sensor_1_voltage": "V",
    "oxygen_sensor_1_short_term_fuel_trim": "%",
    "oxygen_sensor_2_voltage": "V",
    "oxygen_sensor_2_short_term_fuel_trim": "%",
    "adapter_voltage": "V",
    "estimated_fuel_rate": "L/h",
    "estimated_fuel_consumption": "L/100 km",
    "distance_with_mil": "km",
}

SAMPLE_IDENTITY_FIELDS = frozenset(
    {"sample_id", "drive_id", "timestamp_utc", "sequence", "ecu_data_status"}
)
SAMPLE_TELEMETRY_FIELDS = frozenset(
    {
        *UNITS_V1,
        "fuel_system_1",
        "oxygen_sensors_present",
        "obd_standard",
        # Poll-plan v4: mode-01 PID 0x01, the check-engine lamp and stored-DTC count.
        "mil_on",
        "dtc_count",
    }
)
# Quality remains server-side. The names below are the v1 logger contract; accepting only
# bounded JSON values prevents a future logger bug from
# turning an export into an unbounded opaque payload.
SAMPLE_SERVER_FIELDS = frozenset({*SAMPLE_IDENTITY_FIELDS, *SAMPLE_TELEMETRY_FIELDS, "quality"})
SAMPLE_NUMERIC_RANGES: dict[str, tuple[float, float]] = {
    "engine_rpm": (0, 20000),
    "vehicle_speed": (0, 400),
    "coolant_temperature": (-80, 250),
    "intake_air_temperature": (-80, 200),
    "engine_load": (0, 100),
    "throttle_position": (0, 100),
    "timing_advance": (-90, 180),
    "mass_air_flow": (0, 2000),
    "short_term_fuel_trim_bank_1": (-100, 100),
    "long_term_fuel_trim_bank_1": (-100, 100),
    "oxygen_sensor_1_voltage": (0, 5),
    "oxygen_sensor_1_short_term_fuel_trim": (-100, 100),
    "oxygen_sensor_2_voltage": (0, 5),
    "oxygen_sensor_2_short_term_fuel_trim": (-100, 100),
    "adapter_voltage": (0, 40),
    "estimated_fuel_rate": (0, 1000),
    "estimated_fuel_consumption": (0, 10000),
    "distance_with_mil": (0, 65535),
}


class BundleError(ValueError):
    """A permanent integrity/schema failure.  The copy belongs in quarantine."""


class BundleConflict(BundleError):
    """A drive id was already stored from different immutable bytes."""


@dataclass(frozen=True, slots=True)
class ValidatedBundle:
    path: Path
    filename: str
    bundle_sha256: str
    size_bytes: int
    manifest: dict[str, Any]
    summary: dict[str, Any]
    diagnostics_document: dict[str, Any]
    summary_source: str = "producer"
    warnings: tuple[str, ...] = ()

    @property
    def drive_id(self) -> str:
        return str(self.manifest["drive_id"])

    @property
    def schema_version(self) -> int:
        return int(self.manifest["schema_version"])

    @property
    def vehicle_id(self) -> str:
        return str(self.manifest["vehicle_id"])


def is_bundle_name(name: str) -> bool:
    if not name.endswith(BUNDLE_SUFFIX) or name.endswith(BUNDLE_SUFFIX + PARTIAL_SUFFIX):
        return False
    drive_id = name[: -len(BUNDLE_SUFFIX)]
    return bool(SAFE_DRIVE_ID.fullmatch(drive_id)) and ".." not in name


def drive_id_from_name(name: str) -> str:
    if not is_bundle_name(name):
        raise BundleError("bundle filename must be <safe-drive-id>.obd2.zip")
    return name[: -len(BUNDLE_SUFFIX)]


def _reject_constant(value: str) -> None:
    raise BundleError(f"non-finite JSON number {value!r} is not allowed")


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BundleError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _json_bytes(raw: bytes, *, name: str) -> Any:
    if len(raw) > MAX_JSON_MEMBER_BYTES:
        raise BundleError(f"{name} exceeds the {MAX_JSON_MEMBER_BYTES}-byte JSON limit")
    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_pairs,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BundleError(f"{name} is not valid UTF-8 JSON: {exc}") from None


def _utc(value: object, *, field_name: str) -> datetime:
    if not isinstance(value, str) or len(value) > 64:
        raise BundleError(f"{field_name} must be a bounded ISO-8601 UTC string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise BundleError(f"{field_name} is not a valid ISO-8601 timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise BundleError(f"{field_name} must carry an explicit UTC offset")
    return parsed.astimezone(UTC)


def _text(
    value: object, *, field_name: str, maximum: int = 128, nullable: bool = False
) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value or len(value) > maximum or "\x00" in value:
        raise BundleError(f"{field_name} must be a non-empty string up to {maximum} characters")
    return value


def _integer(value: object, *, field_name: str, minimum: int = 0, maximum: int = 2**63 - 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise BundleError(f"{field_name} must be an integer in [{minimum}, {maximum}]")
    return value


def _number(
    value: object,
    *,
    field_name: str,
    minimum: float | None = None,
    maximum: float | None = None,
    nullable: bool = True,
) -> float | None:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BundleError(f"{field_name} must be numeric or null")
    result = float(value)
    if not math.isfinite(result):
        raise BundleError(f"{field_name} must be finite")
    if minimum is not None and result < minimum:
        raise BundleError(f"{field_name} is below {minimum}")
    if maximum is not None and result > maximum:
        raise BundleError(f"{field_name} is above {maximum}")
    return result


def _validate_pipeline_metrics(payload: dict[str, Any]) -> None:
    fields = frozenset(payload)
    queue_fields = {"queue_depth", "maximum_queue_depth"}

    if fields == PIPELINE_METRIC_FIELDS_V1:
        for field_name in PIPELINE_METRIC_FIELDS_V1:
            maximum = 1 if field_name in queue_fields else PIPELINE_METRIC_COUNTER_MAXIMUM
            _integer(
                payload[field_name],
                field_name=f"diagnostic.pipeline_metrics.{field_name}",
                maximum=maximum,
            )
    else:
        allowed = PIPELINE_METRIC_FIELDS_CURRENT_REQUIRED | PIPELINE_METRIC_FIELDS_CURRENT_OPTIONAL
        missing = PIPELINE_METRIC_FIELDS_CURRENT_REQUIRED - fields
        extras = fields - allowed
        if missing or extras:
            raise BundleError(
                "diagnostic pipeline_metrics fields do not match a supported shape "
                f"(missing={sorted(missing)}, extras={sorted(extras)})"
            )

        for field_name in PIPELINE_METRIC_COUNTER_FIELDS_CURRENT:
            _integer(
                payload[field_name],
                field_name=f"diagnostic.pipeline_metrics.{field_name}",
                maximum=PIPELINE_METRIC_COUNTER_MAXIMUM,
            )
        for field_name in queue_fields:
            _integer(
                payload[field_name],
                field_name=f"diagnostic.pipeline_metrics.{field_name}",
                maximum=1,
            )
        observation_window_ms = _integer(
            payload["observation_window_ms"],
            field_name="diagnostic.pipeline_metrics.observation_window_ms",
            minimum=1,
            maximum=PIPELINE_METRIC_TIMING_MAXIMUM_MS,
        )
        duty_cycle = _number(
            payload["polling_duty_cycle_percent"],
            field_name="diagnostic.pipeline_metrics.polling_duty_cycle_percent",
            minimum=0,
            maximum=100,
            nullable=False,
        )
        assert duty_cycle is not None

        timing_values: dict[str, tuple[int, float | None, int, int]] = {}
        for name in PIPELINE_METRIC_TIMING_NAMES_CURRENT:
            sample_count = _integer(
                payload[f"{name}_samples"],
                field_name=f"diagnostic.pipeline_metrics.{name}_samples",
                maximum=PIPELINE_METRIC_TIMING_SAMPLE_LIMIT,
            )
            maximum_ms = _integer(
                payload[f"maximum_{name}_ms"],
                field_name=f"diagnostic.pipeline_metrics.maximum_{name}_ms",
                maximum=PIPELINE_METRIC_TIMING_MAXIMUM_MS,
            )
            total_ms = _integer(
                payload[f"total_{name}_ms"],
                field_name=f"diagnostic.pipeline_metrics.total_{name}_ms",
                maximum=PIPELINE_METRIC_TIMING_MAXIMUM_MS,
            )
            median_field = f"median_{name}_ms"
            median_ms: float | None = None
            if sample_count == 0:
                if median_field in payload:
                    raise BundleError(
                        f"diagnostic pipeline_metrics {name} median requires timing samples"
                    )
                if maximum_ms != 0 or total_ms != 0:
                    raise BundleError(
                        f"diagnostic pipeline_metrics {name} empty timing is inconsistent"
                    )
            else:
                if median_field not in payload:
                    raise BundleError(f"diagnostic pipeline_metrics {name} median is missing")
                median_ms = _number(
                    payload[median_field],
                    field_name=f"diagnostic.pipeline_metrics.{median_field}",
                    minimum=0,
                    maximum=PIPELINE_METRIC_TIMING_MAXIMUM_MS,
                    nullable=False,
                )
                assert median_ms is not None
                if median_ms > maximum_ms or maximum_ms > total_ms:
                    raise BundleError(f"diagnostic pipeline_metrics {name} timing is inconsistent")
            timing_values[name] = (sample_count, median_ms, maximum_ms, total_ms)

        if (
            payload["commands_sent"] > payload["commands_requested"]
            or payload["commands_completed"] > payload["commands_sent"]
        ):
            raise BundleError("diagnostic pipeline_metrics command counts are inconsistent")
        if payload["command_timeouts"] > payload["commands_sent"]:
            raise BundleError("diagnostic pipeline_metrics timeout count is inconsistent")
        if (
            payload["adapter_local_commands"] > payload["commands_requested"]
            or payload["vehicle_bus_commands"] > payload["commands_requested"]
        ):
            raise BundleError("diagnostic pipeline_metrics command categories are inconsistent")
        if payload["checksum_failures"] > payload["parse_failures"]:
            raise BundleError("diagnostic pipeline_metrics parser counts are inconsistent")
        if payload["samples_queued"] > payload["samples_created"]:
            raise BundleError("diagnostic pipeline_metrics sample creation counts are inconsistent")

        sample_dependencies = {
            "connection_time": payload["ble_connection_successes"],
            "command_response_time": payload["commands_completed"],
            "connected_time": payload["ble_connection_successes"],
        }
        for name, counter in sample_dependencies.items():
            if timing_values[name][0] > counter:
                raise BundleError(
                    f"diagnostic pipeline_metrics {name} sample count is inconsistent"
                )
        if timing_values["voltage_response_time"][0] > timing_values["command_response_time"][0]:
            raise BundleError(
                "diagnostic pipeline_metrics voltage timing sample count is inconsistent"
            )

        connected_total_ms = timing_values["connected_time"][3]
        expected_duty_cycle = min(
            100.0,
            connected_total_ms / observation_window_ms * 100.0,
        )
        if not math.isclose(
            duty_cycle,
            expected_duty_cycle,
            rel_tol=1e-12,
            abs_tol=1e-9,
        ):
            raise BundleError("diagnostic pipeline_metrics polling duty cycle is inconsistent")

    if payload["queue_depth"] > payload["maximum_queue_depth"]:
        raise BundleError("diagnostic pipeline_metrics queue depth is inconsistent")
    if payload["commands_completed"] > payload["commands_requested"]:
        raise BundleError("diagnostic pipeline_metrics command counts are inconsistent")
    if payload["samples_persisted"] + payload["samples_dropped"] > payload["samples_queued"]:
        raise BundleError("diagnostic pipeline_metrics sample counts are inconsistent")


def _sha256_stream(source: BinaryIO) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    while chunk := source.read(1024 * 1024):
        size += len(chunk)
        digest.update(chunk)
    return digest.hexdigest(), size


def file_sha256(path: Path, *, maximum: int | None = None) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            size += len(chunk)
            if maximum is not None and size > maximum:
                raise BundleError(f"bundle exceeds the {maximum}-byte transfer limit")
            digest.update(chunk)
    return digest.hexdigest(), size


def _zip_infos(archive: zipfile.ZipFile, config: AppConfig) -> dict[str, zipfile.ZipInfo]:
    infos = archive.infolist()
    names = [item.filename for item in infos]
    if len(names) != len(set(names)):
        raise BundleError("ZIP contains duplicate member names")
    names_set = set(names)
    # summary.json is derived and is not allowed to make otherwise valid raw history
    # disappear. Unknown members and a missing manifest/sample/diagnostic remain fatal.
    if not CORE_MEMBERS.issubset(names_set) or not names_set.issubset(MEMBERS):
        missing = sorted(CORE_MEMBERS - names_set)
        unexpected = sorted(names_set - MEMBERS)
        raise BundleError(
            f"ZIP core members do not match v1 (missing={missing}, unexpected={unexpected})"
        )

    expanded = 0
    for info in infos:
        if info.is_dir() or Path(info.filename).name != info.filename:
            raise BundleError(f"ZIP member {info.filename!r} is not a safe root file")
        if "\\" in info.filename or info.filename.startswith(("/", ".")):
            raise BundleError(f"ZIP member {info.filename!r} is unsafe")
        if info.flag_bits & 0x1:
            raise BundleError("encrypted ZIP members are not supported")
        # V1 is stored, not deflated.  samples.ndjson.gz already carries its own bounded
        # compression and double-compressing it only creates another bomb surface.
        if info.compress_type != zipfile.ZIP_STORED:
            raise BundleError(f"ZIP member {info.filename!r} is not ZIP_STORED")
        mode = (info.external_attr >> 16) & 0xFFFF
        # ZIP writers commonly store only permission bits (0600) without S_IFREG, so
        # require "not a symlink/special entry" rather than a file-type bit many valid
        # archives do not carry.
        if stat.S_ISLNK(mode):
            raise BundleError(f"ZIP member {info.filename!r} is a symlink")
        expanded += info.file_size
        if info.file_size < 0 or expanded > config.obd_max_expanded_bytes:
            raise BundleError("ZIP expanded size exceeds the configured limit")
    return {info.filename: info for info in infos}


def _read_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo, *, maximum: int) -> bytes:
    if info.file_size > maximum:
        raise BundleError(f"{info.filename} exceeds its {maximum}-byte limit")
    with archive.open(info, "r") as source:
        data = source.read(maximum + 1)
    if len(data) > maximum or len(data) != info.file_size:
        raise BundleError(f"{info.filename} expanded beyond its declared/allowed size")
    return data


def _validate_manifest(
    manifest: Any,
    *,
    filename_drive_id: str,
    archive: zipfile.ZipFile,
    infos: dict[str, zipfile.ZipInfo],
    config: AppConfig,
) -> tuple[datetime, datetime, str | None]:
    if not isinstance(manifest, dict):
        raise BundleError("manifest.json must contain an object")
    manifest_fields = {
        "schema_version",
        "bundle_format",
        "drive_id",
        "vehicle_id",
        "adapter_id",
        "logger_id",
        "logger_version",
        "start_time_utc",
        "finish_time_utc",
        "original_timezone",
        "start_reason",
        "stop_reason",
        "obd_protocol",
        "completion_status",
        "clean_end",
        "sample_count",
        "diagnostic_count",
        "error_count",
        "created_at_utc",
        "included_filenames",
        "units",
        "files",
    }
    hardened_fields = {
        "last_sample_at_utc",
        "last_successful_obd_response_at_utc",
        "termination_noticed_at_utc",
        "finalised_at_utc",
        "interruption_reason",
        "poll_plan_version",
    }
    actual_fields = set(manifest)
    actual_shape = frozenset(actual_fields)
    if actual_shape not in {
        frozenset(manifest_fields),
        frozenset(manifest_fields | hardened_fields),
    }:
        expected = manifest_fields | (hardened_fields if actual_fields & hardened_fields else set())
        raise BundleError(
            f"manifest fields do not match a supported v1 shape "
            f"(missing={sorted(expected - actual_fields)}, extras={sorted(actual_fields - expected)})"
        )
    hardened = hardened_fields.issubset(actual_fields)
    if _integer(manifest["schema_version"], field_name="manifest.schema_version") != SCHEMA_VERSION:
        raise BundleError(f"unsupported bundle schema version {manifest['schema_version']!r}")
    if manifest["bundle_format"] != "dashcam-obd":
        raise BundleError("manifest.bundle_format must be dashcam-obd")
    drive_id = _text(manifest["drive_id"], field_name="manifest.drive_id", maximum=64)
    if drive_id != filename_drive_id or not SAFE_DRIVE_ID.fullmatch(str(drive_id)):
        raise BundleError("manifest drive_id does not match the safe bundle filename")
    vehicle_id = _text(manifest["vehicle_id"], field_name="manifest.vehicle_id", maximum=64)
    if not SAFE_VEHICLE_ID.fullmatch(str(vehicle_id)):
        raise BundleError("manifest.vehicle_id is not a canonical lower-case vehicle id")
    _text(manifest.get("adapter_id"), field_name="manifest.adapter_id", nullable=True)
    _text(manifest["logger_id"], field_name="manifest.logger_id")
    _text(manifest["logger_version"], field_name="manifest.logger_version", maximum=64)
    _text(
        manifest["original_timezone"],
        field_name="manifest.original_timezone",
        maximum=128,
        nullable=True,
    )
    _text(manifest["start_reason"], field_name="manifest.start_reason", maximum=128)
    _text(manifest["stop_reason"], field_name="manifest.stop_reason", maximum=128, nullable=True)
    _text(
        manifest["obd_protocol"],
        field_name="manifest.obd_protocol",
        maximum=256,
        nullable=True,
    )
    created = _utc(manifest["created_at_utc"], field_name="manifest.created_at_utc")
    if manifest["completion_status"] not in {"complete", "interrupted", "recovered"}:
        raise BundleError("completion_status must be complete, interrupted or recovered")
    if not isinstance(manifest["clean_end"], bool):
        raise BundleError("manifest.clean_end must be boolean")
    _integer(
        manifest["sample_count"],
        field_name="manifest.sample_count",
        maximum=config.obd_max_samples,
    )
    _integer(
        manifest["diagnostic_count"],
        field_name="manifest.diagnostic_count",
        maximum=MAX_DIAGNOSTIC_EVENTS,
    )
    _integer(manifest["error_count"], field_name="manifest.error_count")
    if manifest["units"] != UNITS_V1:
        raise BundleError("manifest.units does not exactly match the v1 explicit-unit contract")
    included = manifest["included_filenames"]
    if not isinstance(included, list) or len(included) != len(MEMBERS) or set(included) != MEMBERS:
        raise BundleError("manifest.included_filenames must name the four v1 members exactly")

    started = _utc(manifest["start_time_utc"], field_name="manifest.start_time_utc")
    finished = _utc(manifest["finish_time_utc"], field_name="manifest.finish_time_utc")
    if finished < started or finished - started > MAX_DRIVE_SPAN:
        raise BundleError("manifest drive time range is reversed or exceeds 31 days")
    if hardened:
        if _integer(manifest["poll_plan_version"], field_name="manifest.poll_plan_version") not in {
            2,
            3,
            4,
            5,
        }:
            raise BundleError("manifest.poll_plan_version is not supported")

        def lifecycle_time(name: str) -> datetime | None:
            value = manifest[name]
            if value is None:
                return None
            return _utc(value, field_name=f"manifest.{name}")

        last_sample = lifecycle_time("last_sample_at_utc")
        last_response = lifecycle_time("last_successful_obd_response_at_utc")
        noticed = lifecycle_time("termination_noticed_at_utc")
        finalised = lifecycle_time("finalised_at_utc")
        interruption_reason = _text(
            manifest["interruption_reason"],
            field_name="manifest.interruption_reason",
            maximum=128,
            nullable=True,
        )
        if last_sample is not None and not started <= last_sample <= finished:
            raise BundleError("manifest.last_sample_at_utc lies outside the drive window")
        response_upper = noticed or finalised or created
        if last_response is not None and not started <= last_response <= response_upper:
            raise BundleError(
                "manifest.last_successful_obd_response_at_utc lies outside the observed session"
            )
        if noticed is not None and noticed < finished:
            raise BundleError("manifest.termination_noticed_at_utc precedes the effective end")
        if finalised is not None and noticed is not None and finalised < noticed:
            raise BundleError("manifest.finalised_at_utc precedes termination observation")
        completion = manifest["completion_status"]
        if manifest["clean_end"]:
            if completion != "complete" or interruption_reason is not None:
                raise BundleError("a clean manifest must be complete without interruption_reason")
        elif completion == "complete":
            raise BundleError("a hardened unclean manifest cannot claim complete")
        elif not interruption_reason:
            raise BundleError("a hardened unclean manifest requires interruption_reason")

    files = manifest["files"]
    if not isinstance(files, dict) or set(files) != PAYLOAD_MEMBERS:
        raise BundleError("manifest.files must map the three payload members exactly")
    summary_problem: str | None = None
    for name in sorted(PAYLOAD_MEMBERS):
        expected = files[name]
        if not isinstance(expected, dict):
            raise BundleError(f"manifest.files.{name} must be an object")
        if set(expected) != {"size_bytes", "sha256", "record_count"}:
            raise BundleError(f"manifest.files.{name} fields do not match v1")
        expected_size = _integer(
            expected.get("size_bytes"),
            field_name=f"manifest.files.{name}.size_bytes",
            maximum=config.obd_max_expanded_bytes,
        )
        expected_hash = expected.get("sha256")
        if not isinstance(expected_hash, str) or not SHA256_RE.fullmatch(expected_hash):
            raise BundleError(f"manifest.files.{name}.sha256 must be lowercase SHA-256")
        _integer(
            expected.get("record_count"),
            field_name=f"manifest.files.{name}.record_count",
            maximum=max(config.obd_max_samples, MAX_DIAGNOSTIC_EVENTS),
        )
        info = infos.get(name)
        if info is None:
            if name == "summary.json":
                summary_problem = "summary.json is missing"
                continue
            raise BundleError(f"required payload member {name} is missing")
        with archive.open(info, "r") as source:
            actual_hash, actual_size = _sha256_stream(source)
        if actual_size != expected_size or actual_size != info.file_size:
            if name == "summary.json":
                summary_problem = "summary.json size does not match its manifest"
                continue
            raise BundleError(f"{name} size does not match its manifest")
        if actual_hash != expected_hash:
            if name == "summary.json":
                summary_problem = "summary.json SHA-256 does not match its manifest"
                continue
            raise BundleError(f"{name} SHA-256 does not match its manifest")
    return started, finished, summary_problem


def _validate_sample(
    value: Any,
    *,
    drive_id: str,
    started: datetime,
    finished: datetime,
    previous_sequence: int | None,
    previous_at: datetime | None,
) -> tuple[dict[str, Any], datetime, int]:
    if not isinstance(value, dict):
        raise BundleError("each samples.ndjson.gz line must be a JSON object")
    missing = (SAMPLE_IDENTITY_FIELDS | {"quality"}) - value.keys()
    extras = value.keys() - SAMPLE_SERVER_FIELDS
    if missing or extras:
        raise BundleError(
            f"sample fields invalid (missing={sorted(missing)}, extras={sorted(extras)})"
        )
    sample_id = _text(value["sample_id"], field_name="sample.sample_id", maximum=96)
    if not SAFE_SAMPLE_ID.fullmatch(str(sample_id)):
        raise BundleError("sample.sample_id is not a canonical safe id")
    if value["drive_id"] != drive_id:
        raise BundleError("sample drive_id does not match manifest")
    sequence = _integer(value["sequence"], field_name="sample.sequence")
    captured = _utc(value["timestamp_utc"], field_name="sample.timestamp_utc")
    if captured < started or captured > finished:
        raise BundleError("sample timestamp lies outside the manifest drive window")
    if previous_sequence is not None and sequence <= previous_sequence:
        raise BundleError("sample sequence numbers must be strictly increasing")
    if previous_at is not None and captured < previous_at:
        raise BundleError("samples must be ordered by their original UTC timestamp")
    if value["ecu_data_status"] not in {"live", "last_known"}:
        raise BundleError("sample.ecu_data_status is not a supported v1 value")

    for key, (minimum, maximum) in SAMPLE_NUMERIC_RANGES.items():
        if key in value:
            _number(value[key], field_name=f"sample.{key}", minimum=minimum, maximum=maximum)
    if "fuel_system_1" in value and value["fuel_system_1"] is not None:
        _text(value["fuel_system_1"], field_name="sample.fuel_system_1", maximum=128)
    if "obd_standard" in value and value["obd_standard"] is not None:
        _text(value["obd_standard"], field_name="sample.obd_standard", maximum=128)
    if "mil_on" in value and value["mil_on"] is not None and not isinstance(value["mil_on"], bool):
        raise BundleError("sample.mil_on must be a boolean")
    if "dtc_count" in value and value["dtc_count"] is not None:
        # Seven bits of PID 0x01 byte A; anything larger is not a decode of that byte.
        _integer(value["dtc_count"], field_name="sample.dtc_count", minimum=0, maximum=127)
    if "oxygen_sensors_present" in value and value["oxygen_sensors_present"] is not None:
        sensors = value["oxygen_sensors_present"]
        if (
            not isinstance(sensors, list)
            or len(sensors) > 8
            or any(
                isinstance(sensor, bool) or not isinstance(sensor, int) or not 1 <= sensor <= 8
                for sensor in sensors
            )
            or len(sensors) != len(set(sensors))
        ):
            raise BundleError("sample.oxygen_sensors_present must contain unique indices 1..8")
    quality = value["quality"]
    if not isinstance(quality, dict) or set(quality) != {"transport", "parser", "missing_pids"}:
        raise BundleError("sample.quality fields do not match v1")
    _text(quality["transport"], field_name="sample.quality.transport", maximum=128)
    _text(quality["parser"], field_name="sample.quality.parser", maximum=128)
    if not isinstance(quality["missing_pids"], list) or len(quality["missing_pids"]) > 256:
        raise BundleError("sample.quality.missing_pids must be a bounded list")
    for pid in quality["missing_pids"]:
        _integer(pid, field_name="sample.quality.missing_pids", maximum=255)
    return value, captured, sequence


def iter_samples(
    path: Path,
    *,
    drive_id: str,
    started: datetime,
    finished: datetime,
    config: AppConfig | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield validated samples without expanding the nested gzip into memory."""
    cfg = config or get_config()
    expanded = 0
    count = 0
    previous_sequence: int | None = None
    previous_at: datetime | None = None
    sample_ids: set[str] = set()
    try:
        with zipfile.ZipFile(path, "r") as archive:
            info = archive.getinfo("samples.ndjson.gz")
            with archive.open(info, "r") as compressed, gzip.GzipFile(fileobj=compressed) as stream:
                while True:
                    raw = stream.readline(MAX_SAMPLE_LINE_BYTES + 1)
                    if not raw:
                        break
                    if len(raw) > MAX_SAMPLE_LINE_BYTES:
                        raise BundleError("a sample line exceeds the 64 KiB limit")
                    expanded += len(raw)
                    if expanded > cfg.obd_max_expanded_bytes:
                        raise BundleError("samples gzip exceeds the configured decompression limit")
                    if expanded > max(1, info.file_size) * cfg.obd_max_compression_ratio:
                        raise BundleError("samples gzip exceeds the configured compression ratio")
                    if not raw.strip():
                        raise BundleError("samples.ndjson.gz contains a blank record")
                    count += 1
                    if count > cfg.obd_max_samples:
                        raise BundleError("sample count exceeds the configured limit")
                    sample, captured, sequence = _validate_sample(
                        _json_bytes(raw, name=f"sample line {count}"),
                        drive_id=drive_id,
                        started=started,
                        finished=finished,
                        previous_sequence=previous_sequence,
                        previous_at=previous_at,
                    )
                    sample_id = str(sample["sample_id"])
                    if sample_id in sample_ids:
                        raise BundleError(f"duplicate sample_id {sample_id!r}")
                    sample_ids.add(sample_id)
                    previous_sequence, previous_at = sequence, captured
                    yield sample
    except (gzip.BadGzipFile, OSError, EOFError, zipfile.BadZipFile, KeyError) as exc:
        raise BundleError(f"samples.ndjson.gz is corrupt: {type(exc).__name__}: {exc}") from None


def _validate_diagnostics(
    value: Any,
    *,
    drive_id: str,
    manifest_count: int,
    started: datetime,
    finished: datetime,
) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        raise BundleError("diagnostics.json must be a v1 object")
    if value.get("drive_id") != drive_id or not isinstance(value.get("events"), list):
        raise BundleError("diagnostics drive_id/events do not match the manifest")
    events = value["events"]
    if len(events) != manifest_count or len(events) > MAX_DIAGNOSTIC_EVENTS:
        raise BundleError("diagnostic count does not match the manifest")
    seen: set[str] = set()
    allowed_kinds = {
        "confirmed_dtcs",
        "pending_dtcs",
        "permanent_dtcs",
        "dtc_scan_complete",
        "dtc_mode_status",
        "mil_state",
        "readiness",
        "readiness_scan_complete",
        "freeze_frame",
        "freeze_frame_scan_complete",
        "calibration_id",
        "calibration_verification_numbers",
        "mode01_support",
        "mode09_count",
        "mode09_probe_status",
        "mode09_support",
        "mode09_support_scan_complete",
        "protocol_change",
        "connection_failure",
        "parser_failure",
        "pipeline_metrics",
    }
    previous_at: datetime | None = None
    dtc_scan_statuses: dict[int, str] = {}
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            raise BundleError(f"diagnostic event {index} is not an object")
        required = {"diagnostic_id", "drive_id", "timestamp_utc", "kind", "payload"}
        if set(event) != required:
            raise BundleError(f"diagnostic event {index} fields do not match v1")
        diagnostic_id = _text(
            event["diagnostic_id"], field_name="diagnostic.diagnostic_id", maximum=96
        )
        if diagnostic_id in seen:
            raise BundleError(f"duplicate diagnostic_id {diagnostic_id!r}")
        seen.add(str(diagnostic_id))
        if event["drive_id"] != drive_id:
            raise BundleError("diagnostic drive_id does not match manifest")
        observed_at = _utc(event["timestamp_utc"], field_name="diagnostic.timestamp_utc")
        if observed_at < started or observed_at > finished:
            raise BundleError("diagnostic timestamp lies outside the observed drive window")
        if previous_at is not None and observed_at < previous_at:
            raise BundleError("diagnostic events must be ordered by timestamp")
        previous_at = observed_at
        kind = event["kind"]
        payload = event["payload"]
        if kind not in allowed_kinds or not isinstance(payload, dict):
            raise BundleError("diagnostic kind/payload is not supported by v1")
        if kind in {"confirmed_dtcs", "pending_dtcs", "permanent_dtcs"}:
            if set(payload) != {"codes"} or not isinstance(payload["codes"], list):
                raise BundleError(f"diagnostic {kind} payload must contain only codes")
            if len(payload["codes"]) > 128 or any(
                not isinstance(code, str) or not DTC_RE.fullmatch(code) for code in payload["codes"]
            ):
                raise BundleError(f"diagnostic {kind} codes must be canonical OBD DTCs")
        elif kind == "dtc_scan_complete":
            if set(payload) != {"modes"} or payload.get("modes") != [0x03, 0x07, 0x0A]:
                raise BundleError("diagnostic dtc_scan_complete payload is invalid")
            if set(dtc_scan_statuses) != {0x03, 0x07, 0x0A} or any(
                status not in {"ok", "no_data"} for status in dtc_scan_statuses.values()
            ):
                raise BundleError(
                    "diagnostic dtc_scan_complete lacks three successful mode observations"
                )
            dtc_scan_statuses.clear()
        elif kind == "dtc_mode_status":
            if (
                set(payload) != {"mode", "status"}
                or payload.get("mode") not in {0x03, 0x07, 0x0A}
                or payload.get("status")
                not in {"ok", "no_data", "rejected", "transport_error", "malformed"}
            ):
                raise BundleError("diagnostic dtc_mode_status payload is invalid")
            dtc_scan_statuses[payload["mode"]] = payload["status"]
        elif kind == "mil_state":
            if set(payload) != {"on"} or not isinstance(payload["on"], bool):
                raise BundleError("diagnostic mil_state payload must contain boolean on")
        elif kind == "readiness":
            if set(payload) != {
                "supported",
                "incomplete",
                "complete",
                "confirmed_dtc_count",
                "ignition_type",
            }:
                raise BundleError("diagnostic readiness payload fields do not match v1")
            for field_name in ("supported", "incomplete"):
                monitors = payload[field_name]
                if (
                    not isinstance(monitors, list)
                    or len(monitors) > 128
                    or any(
                        not isinstance(item, str) or not item or len(item) > 128 or "\x00" in item
                        for item in monitors
                    )
                ):
                    raise BundleError(f"diagnostic readiness.{field_name} must be bounded strings")
            if not isinstance(payload["complete"], bool):
                raise BundleError("diagnostic readiness.complete must be boolean")
            if (
                isinstance(payload["confirmed_dtc_count"], bool)
                or not isinstance(payload["confirmed_dtc_count"], int)
                or not 0 <= payload["confirmed_dtc_count"] <= 127
            ):
                raise BundleError("diagnostic readiness.confirmed_dtc_count must be 0..127")
            if payload["ignition_type"] not in {"spark", "compression"}:
                raise BundleError("diagnostic readiness.ignition_type is invalid")
            supported = set(payload["supported"])
            incomplete = set(payload["incomplete"])
            if not incomplete <= supported:
                raise BundleError("diagnostic readiness.incomplete must be supported")
            if payload["complete"] != (not incomplete):
                raise BundleError("diagnostic readiness.complete is inconsistent")
        elif kind in {"readiness_scan_complete", "mode09_support_scan_complete"}:
            if set(payload) != {"status"} or payload.get("status") not in {
                "ok",
                "no_data",
                "rejected",
                "transport_error",
                "malformed",
            }:
                raise BundleError(f"diagnostic {kind} payload is invalid")
        elif kind == "mode01_support":
            supported_pids = payload.get("supported_pids")
            if (
                set(payload) != {"supported_pids"}
                or not isinstance(supported_pids, list)
                or len(supported_pids) > 64
                or any(
                    isinstance(pid, bool) or not isinstance(pid, int) or not 1 <= pid <= 64
                    for pid in supported_pids
                )
                or len(supported_pids) != len(set(supported_pids))
            ):
                raise BundleError("diagnostic mode01_support payload is invalid")
        elif kind == "mode09_support":
            supported_pids = payload.get("supported_pids")
            if (
                set(payload) != {"supported_pids"}
                or not isinstance(supported_pids, list)
                or len(supported_pids) > 32
                or any(
                    isinstance(pid, bool) or not isinstance(pid, int) or not 1 <= pid <= 32
                    for pid in supported_pids
                )
                or len(supported_pids) != len(set(supported_pids))
            ):
                raise BundleError("diagnostic mode09_support payload is invalid")
        elif kind == "mode09_count":
            if (
                set(payload) != {"pid", "count"}
                or payload.get("pid") not in {0x03, 0x05}
                or isinstance(payload.get("count"), bool)
                or not isinstance(payload.get("count"), int)
                or not 0 <= payload["count"] <= 255
            ):
                raise BundleError("diagnostic mode09_count payload is invalid")
        elif kind == "mode09_probe_status":
            if (
                set(payload) != {"pid", "status"}
                or payload.get("pid") not in {0x03, 0x04, 0x05, 0x06}
                or payload.get("status")
                not in {"ok", "no_data", "rejected", "transport_error", "malformed"}
            ):
                raise BundleError("diagnostic mode09_probe_status payload is invalid")
        elif kind == "freeze_frame":
            status = payload.get("status")
            required = {
                "no_data": {"status", "frame", "values"},
                "empty": {"status", "frame", "dtc", "values"},
                "ok": {
                    "status",
                    "frame",
                    "dtc",
                    "supported_pids",
                    "missing_pids",
                    "values",
                },
            }.get(status)
            if required is None or set(payload) != required or payload.get("frame") != 0:
                raise BundleError("diagnostic freeze_frame fields do not match v1")
            values = payload.get("values")
            if not isinstance(values, dict) or set(values) - SAMPLE_TELEMETRY_FIELDS:
                raise BundleError("diagnostic freeze_frame.values has unsupported fields")
            for key, item in values.items():
                if key in {"fuel_system_1", "obd_standard"}:
                    _text(item, field_name=f"diagnostic.freeze_frame.{key}", maximum=128)
                    continue
                if key == "oxygen_sensors_present":
                    if (
                        not isinstance(item, list)
                        or len(item) > 8
                        or any(
                            isinstance(sensor, bool)
                            or not isinstance(sensor, int)
                            or not 1 <= sensor <= 8
                            for sensor in item
                        )
                        or len(item) != len(set(item))
                    ):
                        raise BundleError(
                            "diagnostic freeze_frame.oxygen_sensors_present is invalid"
                        )
                    continue
                minimum, maximum = SAMPLE_NUMERIC_RANGES[key]
                _number(
                    item,
                    field_name=f"diagnostic.freeze_frame.{key}",
                    minimum=minimum,
                    maximum=maximum,
                    nullable=False,
                )
            if status == "no_data" and values:
                raise BundleError("diagnostic no_data freeze frame must have empty values")
            if status == "empty":
                if payload.get("dtc") is not None or values:
                    raise BundleError("diagnostic empty freeze frame must have no DTC/values")
            if status == "ok":
                if not isinstance(payload.get("dtc"), str) or not DTC_RE.fullmatch(payload["dtc"]):
                    raise BundleError("diagnostic freeze_frame.dtc must be canonical")
                for field_name in ("supported_pids", "missing_pids"):
                    pids = payload.get(field_name)
                    if (
                        not isinstance(pids, list)
                        or len(pids) > 256
                        or any(
                            not isinstance(pid, str) or not PID_HEX_RE.fullmatch(pid)
                            for pid in pids
                        )
                        or len(set(pids)) != len(pids)
                    ):
                        raise BundleError(
                            f"diagnostic freeze_frame.{field_name} must be unique hex PIDs"
                        )
        elif kind == "freeze_frame_scan_complete":
            if set(payload) != {"status"} or payload.get("status") not in {
                "ok",
                "empty",
                "no_data",
                "rejected",
                "transport_error",
                "malformed",
            }:
                raise BundleError("diagnostic freeze_frame_scan_complete payload is invalid")
        elif kind == "calibration_id":
            if set(payload) != {"value"}:
                raise BundleError("diagnostic calibration_id payload fields do not match v1")
            _text(payload["value"], field_name="diagnostic.calibration_id", maximum=256)
        elif kind == "calibration_verification_numbers":
            if set(payload) != {"values"} or not isinstance(payload["values"], list):
                raise BundleError(
                    "diagnostic calibration_verification_numbers payload must contain values"
                )
            if len(payload["values"]) > 128:
                raise BundleError("diagnostic calibration verification values are unbounded")
            for item in payload["values"]:
                _text(
                    item,
                    field_name="diagnostic.calibration_verification_number",
                    maximum=256,
                )
        elif kind == "protocol_change":
            if set(payload) != {"protocol", "protocol_number"}:
                raise BundleError("diagnostic protocol_change payload fields do not match v1")
            _text(payload["protocol"], field_name="diagnostic.protocol", maximum=256)
            if payload["protocol_number"] is not None:
                _text(
                    payload["protocol_number"],
                    field_name="diagnostic.protocol_number",
                    maximum=32,
                )
        elif kind in {"connection_failure", "parser_failure"}:
            if set(payload) != {"category", "message"}:
                raise BundleError(f"diagnostic {kind} payload fields do not match v1")
            _text(payload["category"], field_name=f"diagnostic.{kind}.category", maximum=128)
            _text(payload["message"], field_name=f"diagnostic.{kind}.message", maximum=1024)
        elif kind == "pipeline_metrics":
            _validate_pipeline_metrics(payload)
    return value


def _validate_summary(
    value: Any,
    *,
    drive_id: str,
    sample_count: int,
    started: datetime,
    finished: datetime,
    clean_end: bool,
    manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BundleError("summary.json must be an object")
    required = SUMMARY_FIELDS_V1
    hardened = HARDENED_SUMMARY_FIELDS
    expected = required | (hardened if manifest and "poll_plan_version" in manifest else set())
    if set(value) != expected:
        raise BundleError(
            f"summary fields do not match the manifest's v1 shape "
            f"(missing={sorted(expected - value.keys())}, extras={sorted(value.keys() - expected)})"
        )
    if value.get("schema_version") != SCHEMA_VERSION:
        raise BundleError("summary schema_version is unsupported")
    if value.get("drive_id") != drive_id:
        raise BundleError("summary drive_id does not match manifest")
    if _utc(value["start_time_utc"], field_name="summary.start_time_utc") != started:
        raise BundleError("summary start_time_utc does not match manifest")
    if _utc(value["finish_time_utc"], field_name="summary.finish_time_utc") != finished:
        raise BundleError("summary finish_time_utc does not match manifest")
    if value["clean_end"] is not clean_end:
        raise BundleError("summary clean_end does not match manifest")
    if hardened.issubset(value):
        assert manifest is not None
        for key in hardened:
            if value[key] != manifest[key]:
                raise BundleError(f"summary {key} does not match manifest")
        for key in ("last_sample_at_utc", "termination_noticed_at_utc", "finalised_at_utc"):
            if value[key] is not None:
                _utc(value[key], field_name=f"summary.{key}")
    if _integer(value.get("sample_count"), field_name="summary.sample_count") != sample_count:
        raise BundleError("summary sample_count does not match manifest")
    numeric_ranges = {
        "duration_s": (0, 2_678_400),
        "distance_km": (0, 300_000),
        "average_speed_kmh": (0, 400),
        "maximum_speed_kmh": (0, 400),
        "average_rpm": (0, 20000),
        "maximum_rpm": (0, 20000),
        "idle_duration_s": (0, 2_678_400),
        "estimated_fuel_used_l": (0, 750_000),
        "average_fuel_consumption_l_per_100km": (0, 10000),
        "maximum_coolant_temperature_c": (-80, 250),
        "maximum_engine_load_pct": (0, 100),
        "missing_data_duration_s": (0, 2_678_400),
        "received_sample_percentage": (0, 100),
    }
    non_nullable = {"duration_s", "missing_data_duration_s", "received_sample_percentage"}
    for key, (minimum, maximum) in numeric_ranges.items():
        _number(
            value[key],
            field_name=f"summary.{key}",
            minimum=minimum,
            maximum=maximum,
            nullable=key not in non_nullable,
        )
    expected = _integer(value["expected_sample_count"], field_name="summary.expected_sample_count")
    if expected < sample_count:
        raise BundleError("summary.expected_sample_count cannot be below sample_count")
    if not isinstance(value["dtcs_observed"], list) or any(
        not isinstance(code, str) or not DTC_RE.fullmatch(code) for code in value["dtcs_observed"]
    ):
        raise BundleError("summary.dtcs_observed must contain canonical OBD DTCs")
    if len(value["dtcs_observed"]) > 128 or len(set(value["dtcs_observed"])) != len(
        value["dtcs_observed"]
    ):
        raise BundleError("summary.dtcs_observed must be bounded and unique")
    duration = float(value["duration_s"])
    missing_duration = float(value["missing_data_duration_s"])
    if missing_duration > duration:
        raise BundleError("summary.missing_data_duration_s cannot exceed duration_s")
    return value


def validate_bundle(path: Path, *, config: AppConfig | None = None) -> ValidatedBundle:
    """Validate one immutable ZIP without extracting it or trusting member paths."""
    cfg = config or get_config()
    try:
        if path.is_symlink():
            raise BundleError("bundle must be a regular non-symlink file")
        resolved = path.resolve(strict=True)
        if not resolved.is_file():
            raise BundleError("bundle must be a regular non-symlink file")
    except OSError as exc:
        raise BundleError(f"bundle file cannot be inspected: {type(exc).__name__}: {exc}") from None
    filename_drive_id = drive_id_from_name(resolved.name)
    try:
        bundle_hash, size_bytes = file_sha256(resolved, maximum=cfg.obd_max_bundle_bytes)
    except OSError as exc:
        raise BundleError(f"bundle file cannot be read: {type(exc).__name__}: {exc}") from None

    try:
        with zipfile.ZipFile(resolved, "r") as archive:
            infos = _zip_infos(archive, cfg)
            manifest = _json_bytes(
                _read_member(archive, infos["manifest.json"], maximum=MAX_JSON_MEMBER_BYTES),
                name="manifest.json",
            )
            started, finished, summary_problem = _validate_manifest(
                manifest,
                filename_drive_id=filename_drive_id,
                archive=archive,
                infos=infos,
                config=cfg,
            )
            # Samples end at the effective drive finish. Hardened interruption diagnostics
            # can truthfully be observed a moment later, up to the producer's separately
            # retained termination/finalisation clock. Legacy bundles keep the old bound.
            diagnostic_finished = finished
            if "poll_plan_version" in manifest:
                for field_name in ("termination_noticed_at_utc", "finalised_at_utc"):
                    raw_time = manifest.get(field_name)
                    if raw_time is not None:
                        diagnostic_finished = max(
                            diagnostic_finished,
                            _utc(raw_time, field_name=f"manifest.{field_name}"),
                        )
            diagnostics = _validate_diagnostics(
                _json_bytes(
                    _read_member(archive, infos["diagnostics.json"], maximum=MAX_JSON_MEMBER_BYTES),
                    name="diagnostics.json",
                ),
                drive_id=filename_drive_id,
                manifest_count=int(manifest["diagnostic_count"]),
                started=started,
                finished=diagnostic_finished,
            )
            summary: dict[str, Any] | None = None
            if summary_problem is None:
                try:
                    summary = _validate_summary(
                        _json_bytes(
                            _read_member(
                                archive,
                                infos["summary.json"],
                                maximum=MAX_JSON_MEMBER_BYTES,
                            ),
                            name="summary.json",
                        ),
                        drive_id=filename_drive_id,
                        sample_count=int(manifest["sample_count"]),
                        started=started,
                        finished=finished,
                        clean_end=manifest["clean_end"],
                        manifest=manifest,
                    )
                except BundleError as exc:
                    summary_problem = str(exc)
    except (zipfile.BadZipFile, OSError, EOFError) as exc:
        raise BundleError(f"bundle ZIP is corrupt: {type(exc).__name__}: {exc}") from None

    count = 0
    for _sample in iter_samples(
        resolved,
        drive_id=filename_drive_id,
        started=started,
        finished=finished,
        config=cfg,
    ):
        count += 1
    if count != int(manifest["sample_count"]):
        raise BundleError("decompressed sample count does not match manifest")
    if count == 0:
        raise BundleError("a completed drive bundle must contain at least one sample")
    record_count = manifest["files"]["samples.ndjson.gz"]["record_count"]
    if record_count != count:
        raise BundleError("samples member record_count does not match its contents")
    if manifest["files"]["diagnostics.json"]["record_count"] != len(diagnostics["events"]):
        raise BundleError("diagnostics member record_count does not match its contents")
    if manifest["files"]["summary.json"]["record_count"] != 1:
        summary_problem = "summary member record_count is not one"

    warnings: list[str] = []
    summary_source = "producer"
    if summary is None or summary_problem is not None:
        # The summary contains no primary observations. Deriving it from the already
        # checksum-verified, schema-validated sample stream is safer than hiding the drive
        # or trusting corrupt producer arithmetic. The original ZIP remains byte-for-byte
        # available for forensic download.
        from app.obd.summary import calculate_summary

        summary = calculate_summary(
            manifest,
            iter_samples(
                resolved,
                drive_id=filename_drive_id,
                started=started,
                finished=finished,
                config=cfg,
            ),
            diagnostics["events"],
        )
        if "poll_plan_version" in manifest:
            summary.update(
                {
                    key: manifest[key]
                    for key in (
                        "last_sample_at_utc",
                        "termination_noticed_at_utc",
                        "finalised_at_utc",
                        "completion_status",
                        "interruption_reason",
                    )
                }
            )
        summary = _validate_summary(
            summary,
            drive_id=filename_drive_id,
            sample_count=int(manifest["sample_count"]),
            started=started,
            finished=finished,
            clean_end=manifest["clean_end"],
            manifest=manifest,
        )
        summary_source = "derived"
        warnings.append(
            "summary.json was missing or invalid; the server derived summary fields "
            "from validated raw samples"
        )
    now = datetime.now(UTC)
    if started > now + timedelta(days=1):
        warnings.append("drive timestamps are more than 24 hours ahead of the server clock")
    return ValidatedBundle(
        path=resolved,
        filename=resolved.name,
        bundle_sha256=bundle_hash,
        size_bytes=size_bytes,
        manifest=manifest,
        summary=summary,
        diagnostics_document=diagnostics,
        summary_source=summary_source,
        warnings=tuple(warnings),
    )


def _sample_row(sample: dict[str, Any], drive_db_id: int) -> dict[str, Any]:
    quality = sample["quality"]
    return {
        "drive_db_id": drive_db_id,
        "sample_id": sample["sample_id"],
        "sequence": sample["sequence"],
        "captured_at": _utc(sample["timestamp_utc"], field_name="sample.timestamp_utc"),
        "ecu_data_status": sample["ecu_data_status"],
        "engine_rpm": sample.get("engine_rpm"),
        "vehicle_speed_kmh": sample.get("vehicle_speed"),
        "coolant_temperature_c": sample.get("coolant_temperature"),
        "intake_air_temperature_c": sample.get("intake_air_temperature"),
        "engine_load_pct": sample.get("engine_load"),
        "throttle_position_pct": sample.get("throttle_position"),
        "timing_advance_deg": sample.get("timing_advance"),
        "mass_air_flow_g_s": sample.get("mass_air_flow"),
        "short_term_fuel_trim_bank_1_pct": sample.get("short_term_fuel_trim_bank_1"),
        "long_term_fuel_trim_bank_1_pct": sample.get("long_term_fuel_trim_bank_1"),
        "fuel_system_status": sample.get("fuel_system_1"),
        "oxygen_sensors_present": sample.get("oxygen_sensors_present"),
        "obd_standard": sample.get("obd_standard"),
        "distance_with_mil_km": sample.get("distance_with_mil"),
        "mil_on": sample.get("mil_on"),
        "dtc_count": sample.get("dtc_count"),
        "oxygen_sensor_1_voltage_v": sample.get("oxygen_sensor_1_voltage"),
        "oxygen_sensor_1_short_term_fuel_trim_pct": sample.get(
            "oxygen_sensor_1_short_term_fuel_trim"
        ),
        "oxygen_sensor_2_voltage_v": sample.get("oxygen_sensor_2_voltage"),
        "oxygen_sensor_2_short_term_fuel_trim_pct": sample.get(
            "oxygen_sensor_2_short_term_fuel_trim"
        ),
        "adapter_voltage_v": sample.get("adapter_voltage"),
        "estimated_fuel_rate_l_h": sample.get("estimated_fuel_rate"),
        "estimated_fuel_consumption_l_100km": sample.get("estimated_fuel_consumption"),
        "quality_json": quality,
        "raw_json": sample,
    }


def _next_sample_rows(
    samples: Iterator[dict[str, Any]], drive_db_id: int, limit: int = 500
) -> list[dict[str, Any]]:
    """Decode one bounded insert batch in a worker thread."""
    rows: list[dict[str, Any]] = []
    for _index in range(limit):
        try:
            sample = next(samples)
        except StopIteration:
            break
        rows.append(_sample_row(sample, drive_db_id))
    return rows


async def store_validated_bundle(session: AsyncSession, bundle: ValidatedBundle) -> OBDBundle:
    """Persist queue metadata, summary, diagnostics and every raw sample atomically."""
    existing = (
        await session.execute(
            select(OBDBundle).where(
                OBDBundle.drive_id == bundle.drive_id,
                OBDBundle.bundle_hash == bundle.bundle_sha256,
                OBDBundle.schema_version == bundle.schema_version,
                OBDBundle.metadata_trusted.is_(True),
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.state == OBDBundleState.QUARANTINED.value or (
            existing.state == OBDBundleState.FAILED.value
            and existing.failure_kind in {"integrity", "local_path", "quarantine_io"}
        ):
            # A fresh unit copy can repair the exact immutable identity after retained
            # server bytes were quarantined. Raw history already exists, so reconcile only
            # queue/path metadata and never insert a second drive/sample set.
            now = utcnow()
            existing.filename = bundle.filename
            existing.size_bytes = bundle.size_bytes
            existing.state = OBDBundleState.STORED.value
            existing.verified_at = now
            existing.last_error = None
            existing.failure_kind = None
            existing.updated_at = now
            await session.flush()
        drive = (
            await session.execute(select(OBDDrive).where(OBDDrive.bundle_id == existing.id))
        ).scalar_one_or_none()
        if drive is not None:
            from app.ingest.obd_reconciliation import reconcile_drive_projection

            await reconcile_drive_projection(
                session,
                drive,
                summary_source=bundle.summary_source,
            )
        return existing
    rejected = (
        await session.execute(
            select(OBDBundle).where(
                OBDBundle.filename == bundle.filename,
                OBDBundle.metadata_trusted.is_(False),
            )
        )
    ).scalar_one_or_none()
    conflicting = (
        (
            await session.execute(
                select(OBDBundle).where(
                    OBDBundle.drive_id == bundle.drive_id,
                    OBDBundle.metadata_trusted.is_(True),
                )
            )
        )
        .scalars()
        .first()
    )
    if conflicting is not None:
        raise BundleConflict(
            f"drive {bundle.drive_id} already exists with a different bundle SHA-256"
        )

    manifest = bundle.manifest
    summary = bundle.summary
    now = utcnow()
    values = {
        "drive_id": bundle.drive_id,
        "bundle_hash": bundle.bundle_sha256,
        "schema_version": bundle.schema_version,
        "filename": bundle.filename,
        "size_bytes": bundle.size_bytes,
        "vehicle_id": bundle.vehicle_id,
        "adapter_id": manifest.get("adapter_id"),
        "logger_id": manifest["logger_id"],
        "logger_version": manifest["logger_version"],
        "drive_started_at": _utc(manifest["start_time_utc"], field_name="manifest.start_time_utc"),
        "drive_finished_at": _utc(
            manifest["finish_time_utc"], field_name="manifest.finish_time_utc"
        ),
        "sample_count": manifest["sample_count"],
        "diagnostic_count": manifest["diagnostic_count"],
        "metadata_trusted": True,
        "state": OBDBundleState.STORED.value,
        "copied_at": rejected.copied_at if rejected is not None else now,
        "verified_at": now,
        "validation_warnings": list(bundle.warnings) or None,
        "last_error": None,
        "failure_kind": None,
    }
    if rejected is None:
        row = OBDBundle(**values)
        session.add(row)
    else:
        row = rejected
        for key, value in values.items():
            setattr(row, key, value)
        row.updated_at = now
    await session.flush()

    drive = OBDDrive(
        bundle_id=row.id,
        drive_id=bundle.drive_id,
        vehicle_id=bundle.vehicle_id,
        started_at=row.drive_started_at,
        finished_at=row.drive_finished_at,
        original_timezone=manifest.get("original_timezone"),
        start_reason=manifest.get("start_reason"),
        stop_reason=manifest.get("stop_reason"),
        obd_protocol=manifest.get("obd_protocol"),
        completion_status=manifest["completion_status"],
        clean_end=manifest["clean_end"],
        lifecycle_status=(
            "complete"
            if manifest["clean_end"]
            else "recovered"
            if manifest.get("stop_reason") == "device_restart"
            or manifest["completion_status"] == "recovered"
            else "interrupted"
        ),
        interruption_reason=(manifest.get("stop_reason") if not manifest["clean_end"] else None),
        finalization_observed_at=row.drive_finished_at,
        processing_status="pending",
        summary_source=bundle.summary_source,
        duration_s=summary.get("duration_s"),
        distance_km=summary.get("distance_km"),
        average_speed_kmh=summary.get("average_speed_kmh"),
        maximum_speed_kmh=summary.get("maximum_speed_kmh"),
        average_rpm=summary.get("average_rpm"),
        maximum_rpm=summary.get("maximum_rpm"),
        idle_duration_s=summary.get("idle_duration_s"),
        estimated_fuel_used_l=summary.get("estimated_fuel_used_l"),
        average_fuel_consumption_l_100km=summary.get("average_fuel_consumption_l_per_100km"),
        maximum_coolant_temperature_c=summary.get("maximum_coolant_temperature_c"),
        maximum_engine_load_pct=summary.get("maximum_engine_load_pct"),
        missing_data_duration_s=summary.get("missing_data_duration_s"),
        expected_sample_count=summary["expected_sample_count"],
        received_sample_percentage=summary.get("received_sample_percentage"),
        sample_count=summary["sample_count"],
        error_count=manifest["error_count"],
        dtcs_observed=summary.get("dtcs_observed"),
        units=manifest["units"],
        manifest_json=manifest,
        summary_json=summary,
    )
    session.add(drive)
    await session.flush()

    started = row.drive_started_at
    finished = row.drive_finished_at
    samples = iter_samples(
        bundle.path,
        drive_id=bundle.drive_id,
        started=started,
        finished=finished,
    )
    try:
        while sample_rows := await asyncio.to_thread(_next_sample_rows, samples, drive.id):
            await session.execute(insert(OBDSample), sample_rows)
    finally:
        await asyncio.to_thread(samples.close)

    diagnostic_rows: list[dict[str, Any]] = []
    for event in bundle.diagnostics_document["events"]:
        canonical = json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        diagnostic_rows.append(
            {
                "drive_db_id": drive.id,
                "event_hash": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
                "observed_at": _utc(event["timestamp_utc"], field_name="diagnostic.timestamp_utc"),
                "kind": event["kind"],
                "payload_json": event["payload"],
            }
        )
    if diagnostic_rows:
        await session.execute(insert(OBDDiagnostic), diagnostic_rows)
    await session.flush()
    from app.ingest.obd_reconciliation import reconcile_drive_projection

    await reconcile_drive_projection(
        session,
        drive,
        summary_source=bundle.summary_source,
    )
    return row


async def store_rejected_bundle(
    session: AsyncSession,
    *,
    filename: str,
    bundle_hash: str,
    size_bytes: int,
    error: str,
    quarantined: bool,
    observed_at: datetime | None = None,
) -> OBDBundle:
    """Persist an invalid copy without trusting any bytes inside its manifest.

    The safe filename supplies only the drive-id-shaped recovery key. Schema, vehicle,
    logger, and time placeholders are explicitly marked untrusted and are replaced in
    the same row if a repaired quarantine copy later passes full validation.
    """
    if not is_bundle_name(filename):
        raise BundleError("rejected bundle filename is unsafe")
    if not SHA256_RE.fullmatch(bundle_hash):
        raise BundleError("rejected bundle hash is not lowercase SHA-256")
    if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 0:
        raise BundleError("rejected bundle size is invalid")
    now = utcnow()
    observed = observed_at or now
    row = (
        await session.execute(select(OBDBundle).where(OBDBundle.filename == filename))
    ).scalar_one_or_none()
    if row is not None and row.metadata_trusted:
        return row
    if row is None:
        row = OBDBundle(
            drive_id=drive_id_from_name(filename),
            bundle_hash=bundle_hash,
            schema_version=0,
            filename=filename,
            size_bytes=size_bytes,
            vehicle_id="unknown",
            adapter_id=None,
            logger_id="unknown",
            logger_version="unknown",
            drive_started_at=observed,
            drive_finished_at=observed,
            sample_count=0,
            diagnostic_count=0,
            metadata_trusted=False,
            copied_at=now,
        )
        session.add(row)
    else:
        row.bundle_hash = bundle_hash
        row.size_bytes = size_bytes
    row.state = OBDBundleState.QUARANTINED.value if quarantined else OBDBundleState.FAILED.value
    row.verified_at = None
    row.failure_kind = "integrity" if quarantined else "quarantine_io"
    row.last_error = error[:2048]
    row.updated_at = now
    await session.flush()
    return row


def bundle_path_for(row: OBDBundle, *, config: AppConfig | None = None) -> Path:
    cfg = config or get_config()
    if not is_bundle_name(row.filename):
        raise BundleError("stored bundle filename is unsafe")
    directory = (
        cfg.obd_quarantine_dir
        if row.state == OBDBundleState.QUARANTINED.value
        else cfg.obd_verified_dir
    )
    candidate = (directory / row.filename).resolve()
    root = directory.resolve()
    if candidate.parent != root:
        raise BundleError("stored bundle path escapes the verified directory")
    return candidate


__all__ = [
    "BUNDLE_SUFFIX",
    "MEMBERS",
    "SCHEMA_VERSION",
    "UNITS_V1",
    "BundleConflict",
    "BundleError",
    "ValidatedBundle",
    "bundle_path_for",
    "drive_id_from_name",
    "file_sha256",
    "is_bundle_name",
    "iter_samples",
    "store_rejected_bundle",
    "store_validated_bundle",
    "validate_bundle",
]
