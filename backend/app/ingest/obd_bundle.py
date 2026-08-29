"""Strict validation and transactional storage for dashcam OBD export bundles.

The server is the primary high-resolution history.  Home Assistant receives the latest
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
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
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
PAYLOAD_MEMBERS = frozenset({"samples.ndjson.gz", "diagnostics.json", "summary.json"})
SAFE_DRIVE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
SAFE_VEHICLE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
SAFE_SAMPLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,95}$")
DTC_RE = re.compile(r"^[PCBU][0-9A-F]{4}$")
PID_HEX_RE = re.compile(r"^[0-9A-F]{2}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_JSON_MEMBER_BYTES = 2 * 1024 * 1024
MAX_SAMPLE_LINE_BYTES = 64 * 1024
MAX_DIAGNOSTIC_EVENTS = 4096
MAX_DRIVE_SPAN = timedelta(days=31)

# Byte-for-byte shared with the logger and the Home Assistant endpoint.  Keeping this in
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
    {*UNITS_V1, "fuel_system_1", "oxygen_sensors_present", "obd_standard"}
)
# Quality remains server-side and is stripped from the HA body.  The names below are the
# v1 logger contract; accepting only bounded JSON values prevents a future logger bug from
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
    latest_sample: dict[str, Any]
    latest_values: dict[str, dict[str, Any]]
    statistics: list[dict[str, Any]]
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

    def ha_payload(self) -> dict[str, Any]:
        """The bounded v1 body.  Raw samples and filesystem metadata never leave here."""
        return {
            "schema_version": self.schema_version,
            "drive_id": self.drive_id,
            "bundle_sha256": self.bundle_sha256,
            "vehicle_id": self.vehicle_id,
            "units": dict(self.manifest["units"]),
            "latest_sample": {
                key: value
                for key, value in self.latest_sample.items()
                if key in SAMPLE_IDENTITY_FIELDS or key in SAMPLE_TELEMETRY_FIELDS
            },
            "latest_values": self.latest_values,
            "summary": self.summary,
            "statistics": self.statistics,
            "diagnostics": diagnostics_for_ha(self.diagnostics_document),
        }


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
    if set(names) != MEMBERS:
        missing = sorted(MEMBERS - set(names))
        unexpected = sorted(set(names) - MEMBERS)
        raise BundleError(
            f"ZIP members do not match v1 (missing={missing}, unexpected={unexpected})"
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
) -> tuple[datetime, datetime]:
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
    if set(manifest) != manifest_fields:
        raise BundleError(
            f"manifest fields do not match v1 (missing={sorted(manifest_fields - manifest.keys())}, "
            f"extras={sorted(manifest.keys() - manifest_fields)})"
        )
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
    _utc(manifest["created_at_utc"], field_name="manifest.created_at_utc")
    if manifest["completion_status"] not in {"complete", "recovered"}:
        raise BundleError("completion_status must be complete or recovered")
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

    files = manifest["files"]
    if not isinstance(files, dict) or set(files) != PAYLOAD_MEMBERS:
        raise BundleError("manifest.files must map the three payload members exactly")
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
        with archive.open(infos[name], "r") as source:
            actual_hash, actual_size = _sha256_stream(source)
        if actual_size != expected_size or actual_size != infos[name].file_size:
            raise BundleError(f"{name} size does not match its manifest")
        if actual_hash != expected_hash:
            raise BundleError(f"{name} SHA-256 does not match its manifest")
    return started, finished


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


@dataclass(slots=True)
class _Hour:
    count: int = 0
    distance_km: float = 0.0
    engine_runtime_s: float = 0.0
    estimated_fuel_used_l: float = 0.0
    idle_duration_s: float = 0.0
    speed_sum: float = 0.0
    speed_count: int = 0
    max_speed: float | None = None
    rpm_sum: float = 0.0
    rpm_count: int = 0
    max_rpm: float | None = None
    max_coolant: float | None = None
    # Segment duration, original sample gap, speed, rpm, fuel rate. The original gap
    # controls whether interpolation is trustworthy; the segment duration ensures a
    # 12:59:58 -> 13:00:03 interval is charged 2s/3s to the correct UTC hours.
    intervals: list[tuple[float, float, float | None, float | None, float | None]] = field(
        default_factory=list
    )


def _hour_start(value: datetime) -> datetime:
    return value.replace(minute=0, second=0, microsecond=0)


def _present(sample: dict[str, Any], key: str) -> float | None:
    value = sample.get(key)
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


class _StatisticsBuilder:
    """Incremental rollup: retains O(hours + gaps), never the raw drive."""

    def __init__(self) -> None:
        self.hours: dict[datetime, _Hour] = defaultdict(_Hour)
        self.previous: dict[str, Any] | None = None
        self.previous_at: datetime | None = None
        self.first_at: datetime | None = None
        self.transitions: list[tuple[datetime, datetime]] = []
        self.ordinary_gaps: list[float] = []

    def add(self, sample: dict[str, Any]) -> None:
        captured = _utc(sample["timestamp_utc"], field_name="sample.timestamp_utc")
        hour = self.hours[_hour_start(captured)]
        hour.count += 1
        speed = _present(sample, "vehicle_speed")
        rpm = _present(sample, "engine_rpm")
        coolant = _present(sample, "coolant_temperature")
        if speed is not None:
            hour.speed_sum += speed
            hour.speed_count += 1
            hour.max_speed = speed if hour.max_speed is None else max(hour.max_speed, speed)
        if rpm is not None:
            hour.rpm_sum += rpm
            hour.rpm_count += 1
            hour.max_rpm = rpm if hour.max_rpm is None else max(hour.max_rpm, rpm)
        if coolant is not None:
            hour.max_coolant = (
                coolant if hour.max_coolant is None else max(hour.max_coolant, coolant)
            )

        if self.previous is None or self.previous_at is None:
            self.first_at = captured
            self.previous = sample
            self.previous_at = captured
            return

        previous = self.previous
        previous_at = self.previous_at
        gap = max(0.0, (captured - previous_at).total_seconds())
        self.transitions.append((previous_at, captured))
        previous_speed = _present(previous, "vehicle_speed")
        previous_rpm = _present(previous, "engine_rpm")
        previous_fuel_rate = _present(previous, "estimated_fuel_rate")
        cursor = previous_at
        while cursor < captured:
            segment_end = min(captured, _hour_start(cursor) + timedelta(hours=1))
            segment = (segment_end - cursor).total_seconds()
            self.hours[_hour_start(cursor)].intervals.append(
                (segment, gap, previous_speed, previous_rpm, previous_fuel_rate)
            )
            cursor = segment_end
        # Equal timestamps still count as two received samples/expected observations,
        # but have no duration to integrate.
        if 0 < gap <= 60:
            self.ordinary_gaps.append(gap)
        self.previous = sample
        self.previous_at = captured

    def finish(self) -> list[dict[str, Any]]:
        if not self.hours:
            return []
        positive = sorted(self.ordinary_gaps)
        expected_interval = positive[len(positive) // 2] if positive else 5.0
        expected_interval = min(60.0, max(1.0, expected_interval))
        maximum_integrated_gap = 3 * expected_interval
        expected_by_hour: dict[datetime, int] = defaultdict(int)
        if self.first_at is not None:
            expected_by_hour[_hour_start(self.first_at)] += 1
        # Count the evenly spaced expected observations arithmetically per crossed hour.
        # Iterating one datetime per observation made a sparse 30-day/1-second-cadence
        # drive perform 2.6 million operations. The stream is chronological, so all
        # transitions together cross at most the drive's <=744 hour boundaries.
        for previous_at, captured in self.transitions:
            gap = max(0.0, (captured - previous_at).total_seconds())
            steps = max(1, round(gap / expected_interval))
            if gap == 0:
                expected_by_hour[_hour_start(captured)] += 1
                continue
            delta = captured - previous_at
            total_us = delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds

            def points_before(
                offset_us: int,
                *,
                total_us: int = total_us,
                steps: int = steps,
            ) -> int:
                """Expected points strictly before an offset from ``previous_at``."""
                if offset_us <= 0:
                    return 0
                if offset_us > total_us:
                    return steps
                # ceil(offset * steps / total) - 1 implements the strict upper bound;
                # an endpoint exactly on 13:00 therefore belongs to the 13:00 hour.
                return min(
                    steps,
                    (offset_us * steps + total_us - 1) // total_us - 1,
                )

            cursor = _hour_start(previous_at)
            final_hour = _hour_start(captured)
            while cursor <= final_hour:
                lower = cursor - previous_at
                upper = cursor + timedelta(hours=1) - previous_at
                lower_us = (
                    lower.days * 86_400_000_000 + lower.seconds * 1_000_000 + lower.microseconds
                )
                upper_us = (
                    upper.days * 86_400_000_000 + upper.seconds * 1_000_000 + upper.microseconds
                )
                count = points_before(upper_us) - points_before(lower_us)
                if count:
                    expected_by_hour[cursor] += count
                cursor += timedelta(hours=1)
        rows: list[dict[str, Any]] = []
        for start, hour in sorted(self.hours.items()):
            expected = max(hour.count, expected_by_hour.get(start, 0))
            row: dict[str, Any] = {
                "start_time_utc": start.isoformat(),
                "sample_count": hour.count,
                # HA merges drives which overlap the same UTC hour. Averages must be
                # weighted only by samples where that metric was actually present, not
                # by every transport sample in the hour.
                "speed_sample_count": hour.speed_count,
                "rpm_sample_count": hour.rpm_count,
                "expected_sample_count": expected,
                "missing_data_percentage": 100.0 * max(0, expected - hour.count) / expected,
            }
            distance_evidence = runtime_evidence = fuel_evidence = idle_evidence = False
            for segment, original_gap, speed, rpm, fuel_rate in hour.intervals:
                if segment <= 0 or original_gap > maximum_integrated_gap:
                    continue
                if speed is not None:
                    hour.distance_km += speed * segment / 3600.0
                    distance_evidence = True
                if rpm is not None:
                    runtime_evidence = True
                    if rpm > 300:
                        hour.engine_runtime_s += segment
                        if speed is not None:
                            idle_evidence = True
                            if speed < 1.0:
                                hour.idle_duration_s += segment
                if fuel_rate is not None:
                    hour.estimated_fuel_used_l += fuel_rate * segment / 3600.0
                    fuel_evidence = True
            if distance_evidence:
                row["distance_km"] = hour.distance_km
            if runtime_evidence:
                row["engine_runtime_s"] = hour.engine_runtime_s
            if fuel_evidence:
                row["estimated_fuel_used_l"] = hour.estimated_fuel_used_l
            if idle_evidence:
                row["idle_duration_s"] = hour.idle_duration_s
            if hour.speed_count:
                row["average_speed_kmh"] = hour.speed_sum / hour.speed_count
                row["maximum_speed_kmh"] = hour.max_speed
            if hour.rpm_count:
                row["average_rpm"] = hour.rpm_sum / hour.rpm_count
                row["maximum_rpm"] = hour.max_rpm
            if hour.max_coolant is not None:
                row["maximum_coolant_temperature_c"] = hour.max_coolant
            rows.append(row)
        if len(rows) > 744:
            raise BundleError("drive produces more than the 744 allowed hourly statistics rows")
        return rows


def aggregate_statistics(samples) -> list[dict[str, Any]]:
    """Derive bounded hourly rows under original UTC hours without retaining samples."""
    builder = _StatisticsBuilder()
    for sample in samples:
        builder.add(sample)
    return builder.finish()


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
            raise BundleError("diagnostic timestamp lies outside the manifest drive window")
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
    return value


def _validate_summary(
    value: Any,
    *,
    drive_id: str,
    sample_count: int,
    started: datetime,
    finished: datetime,
    clean_end: bool,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BundleError("summary.json must be an object")
    required = {
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
    if set(value) != required:
        raise BundleError(
            f"summary fields do not match v1 (missing={sorted(required - value.keys())}, "
            f"extras={sorted(value.keys() - required)})"
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
            started, finished = _validate_manifest(
                manifest,
                filename_drive_id=filename_drive_id,
                archive=archive,
                infos=infos,
                config=cfg,
            )
            diagnostics = _validate_diagnostics(
                _json_bytes(
                    _read_member(archive, infos["diagnostics.json"], maximum=MAX_JSON_MEMBER_BYTES),
                    name="diagnostics.json",
                ),
                drive_id=filename_drive_id,
                manifest_count=int(manifest["diagnostic_count"]),
                started=started,
                finished=finished,
            )
            summary = _validate_summary(
                _json_bytes(
                    _read_member(archive, infos["summary.json"], maximum=MAX_JSON_MEMBER_BYTES),
                    name="summary.json",
                ),
                drive_id=filename_drive_id,
                sample_count=int(manifest["sample_count"]),
                started=started,
                finished=finished,
                clean_end=manifest["clean_end"],
            )
    except (zipfile.BadZipFile, OSError, EOFError) as exc:
        raise BundleError(f"bundle ZIP is corrupt: {type(exc).__name__}: {exc}") from None

    count = 0
    latest: dict[str, Any] | None = None
    latest_values: dict[str, dict[str, Any]] = {}
    statistics = _StatisticsBuilder()
    for sample in iter_samples(
        resolved,
        drive_id=filename_drive_id,
        started=started,
        finished=finished,
        config=cfg,
    ):
        count += 1
        latest = sample
        for key in SAMPLE_TELEMETRY_FIELDS:
            if key in sample and sample[key] is not None:
                latest_values[key] = {
                    "value": sample[key],
                    "timestamp_utc": sample["timestamp_utc"],
                }
        statistics.add(sample)
    if count != int(manifest["sample_count"]):
        raise BundleError("decompressed sample count does not match manifest")
    if latest is None:
        raise BundleError("a completed drive bundle must contain at least one sample")
    record_count = manifest["files"]["samples.ndjson.gz"]["record_count"]
    if record_count != count:
        raise BundleError("samples member record_count does not match its contents")
    if manifest["files"]["diagnostics.json"]["record_count"] != len(diagnostics["events"]):
        raise BundleError("diagnostics member record_count does not match its contents")
    if manifest["files"]["summary.json"]["record_count"] != 1:
        raise BundleError("summary member record_count must be one")

    warnings: list[str] = []
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
        latest_sample=latest,
        latest_values=latest_values,
        statistics=statistics.finish(),
        warnings=tuple(warnings),
    )


def diagnostics_for_ha(document: dict[str, Any]) -> dict[str, Any]:
    """Reduce sparse events to the endpoint's strict, identifier-free metadata object."""
    result: dict[str, Any] = {
        "event_count": len(document.get("events", [])),
        "parser_failure_count": 0,
        "connection_failure_count": 0,
    }
    for event in document.get("events", []):
        kind = event.get("kind")
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        observed = event.get("timestamp_utc")
        result["last_event_timestamp_utc"] = observed
        if kind in {"confirmed_dtcs", "pending_dtcs", "permanent_dtcs"}:
            codes = payload.get("codes", [])
            if isinstance(codes, list):
                result[kind] = [str(code)[:16] for code in codes if isinstance(code, str)][:128]
                result[f"{kind}_timestamp_utc"] = observed
        elif kind == "dtc_scan_complete":
            result["dtc_scan_timestamp_utc"] = observed
        elif kind == "dtc_mode_status":
            mode_name = {0x03: "confirmed", 0x07: "pending", 0x0A: "permanent"}.get(
                payload.get("mode")
            )
            if mode_name is not None:
                result.setdefault("dtc_mode_status", {})[mode_name] = payload.get("status")
                result["dtc_mode_status_timestamp_utc"] = observed
                if payload.get("status") in {"ok", "no_data"}:
                    value_field = f"{mode_name}_dtcs"
                    if value_field in result:
                        result[f"{value_field}_timestamp_utc"] = observed
        elif kind == "mil_state":
            result["check_engine_light"] = payload.get("on")
            result["check_engine_light_timestamp_utc"] = observed
        elif kind == "readiness":
            result["readiness_supported"] = payload.get("supported", [])
            result["readiness_incomplete"] = payload.get("incomplete", [])
            result["readiness_complete"] = payload.get("complete")
            result["readiness_timestamp_utc"] = observed
            result["confirmed_dtc_count"] = payload.get("confirmed_dtc_count")
            result["confirmed_dtc_count_timestamp_utc"] = observed
            result["ignition_type"] = payload.get("ignition_type")
            result["ignition_type_timestamp_utc"] = observed
        elif kind == "readiness_scan_complete" and payload.get("status") == "ok":
            # The full value events are deduplicated on the logger. A later compact
            # successful observation proves that the unchanged values were seen again.
            for timestamp_field in (
                "check_engine_light_timestamp_utc",
                "readiness_timestamp_utc",
                "confirmed_dtc_count_timestamp_utc",
                "ignition_type_timestamp_utc",
            ):
                if timestamp_field in result:
                    result[timestamp_field] = observed
        elif kind == "mode01_support":
            result["supported_pids"] = list(payload.get("supported_pids", []))
            result["supported_pids_timestamp_utc"] = observed
        elif kind == "calibration_id":
            result["calibration_id"] = payload.get("value")
            result["calibration_id_timestamp_utc"] = observed
            result.setdefault("mode09_pid_status", {})["04"] = "ok"
            result["mode09_pid_status_timestamp_utc"] = observed
        elif kind == "calibration_verification_numbers":
            result["calibration_verification_numbers"] = list(payload.get("values", []))
            result["calibration_verification_numbers_timestamp_utc"] = event.get("timestamp_utc")
            result.setdefault("mode09_pid_status", {})["06"] = "ok"
            result["mode09_pid_status_timestamp_utc"] = observed
        elif kind == "mode09_support":
            supported_pids = list(payload.get("supported_pids", []))
            result["mode09_supported_pids"] = supported_pids
            result["mode09_supported_pids_timestamp_utc"] = observed
            result["mode09_pid_status"] = {f"{pid:02X}": "supported" for pid in supported_pids}
            result["mode09_pid_status_timestamp_utc"] = observed
        elif kind == "mode09_support_scan_complete" and payload.get("status") == "ok":
            if "mode09_supported_pids" in result:
                result["mode09_supported_pids_timestamp_utc"] = observed
            result["mode09_scan_status"] = "ok"
            result["mode09_scan_status_timestamp_utc"] = observed
        elif kind == "mode09_support_scan_complete":
            result["mode09_scan_status"] = payload.get("status")
            result["mode09_scan_status_timestamp_utc"] = observed
        elif kind == "mode09_count":
            pid = payload.get("pid")
            field_name = {
                0x03: "calibration_id_message_count",
                0x05: "calibration_verification_number_message_count",
            }.get(pid)
            if field_name is not None:
                result[field_name] = payload.get("count")
                result[f"{field_name}_timestamp_utc"] = observed
                result.setdefault("mode09_pid_status", {})[f"{pid:02X}"] = "ok"
                result["mode09_pid_status_timestamp_utc"] = observed
        elif kind == "mode09_probe_status":
            pid = payload.get("pid")
            if isinstance(pid, int):
                result.setdefault("mode09_pid_status", {})[f"{pid:02X}"] = payload.get("status")
                result["mode09_pid_status_timestamp_utc"] = observed
                if payload.get("status") == "ok":
                    value_field = {
                        0x03: "calibration_id_message_count",
                        0x04: "calibration_id",
                        0x05: "calibration_verification_number_message_count",
                        0x06: "calibration_verification_numbers",
                    }.get(pid)
                    if value_field is not None and value_field in result:
                        result[f"{value_field}_timestamp_utc"] = observed
        elif kind == "freeze_frame":
            result["freeze_frame"] = dict(payload)
            result["freeze_frame_timestamp_utc"] = event.get("timestamp_utc")
        elif kind == "freeze_frame_scan_complete":
            result["freeze_frame_scan_status"] = payload.get("status")
            result["freeze_frame_scan_timestamp_utc"] = observed
        elif kind == "protocol_change":
            result["protocol"] = payload.get("protocol")
            result["protocol_timestamp_utc"] = observed
            result["protocol_number"] = payload.get("protocol_number")
            result["protocol_number_timestamp_utc"] = observed
        elif kind == "parser_failure":
            result["parser_failure_count"] += 1
        elif kind == "connection_failure":
            result["connection_failure_count"] += 1
    return result


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
            existing.state = OBDBundleState.READY_TO_IMPORT.value
            existing.verified_at = now
            existing.next_attempt_at = now
            existing.import_started_at = None
            existing.last_error = None
            existing.failure_kind = None
            existing.last_http_status = None
            existing.updated_at = now
            await session.flush()
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
        "state": OBDBundleState.READY_TO_IMPORT.value,
        "copied_at": rejected.copied_at if rejected is not None else now,
        "verified_at": now,
        "next_attempt_at": now,
        "validation_warnings": list(bundle.warnings) or None,
        "last_error": None,
        "failure_kind": None,
        "last_http_status": None,
        "import_started_at": None,
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
    row.next_attempt_at = None
    row.verified_at = None
    row.import_started_at = None
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
    "aggregate_statistics",
    "bundle_path_for",
    "diagnostics_for_ha",
    "drive_id_from_name",
    "file_sha256",
    "is_bundle_name",
    "iter_samples",
    "store_rejected_bundle",
    "store_validated_bundle",
    "validate_bundle",
]
