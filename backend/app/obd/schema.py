"""Version-one OBD drive schema shared by storage, export and import."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

SCHEMA_VERSION = 1
BUNDLE_FORMAT = "dashcam-obd"
BUNDLE_SUFFIX = ".obd2.zip"
BUNDLE_MEMBERS = (
    "manifest.json",
    "samples.ndjson.gz",
    "diagnostics.json",
    "summary.json",
)

SAMPLE_UNITS: dict[str, str] = {
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

NUMERIC_SAMPLE_FIELDS = tuple(SAMPLE_UNITS)
TEXT_SAMPLE_FIELDS = ("fuel_system_1", "obd_standard")
LIST_SAMPLE_FIELDS = ("oxygen_sensors_present",)
_SAFE_SAMPLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,95}$")


def utc_text(value: datetime) -> str:
    """Serialize an aware timestamp in canonical UTC form."""
    if value.tzinfo is None:
        raise ValueError("OBD timestamps must include a timezone")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_utc(value: str) -> datetime:
    """Parse a timestamp and reject naive or non-UTC values."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("OBD timestamps must include a timezone")
    if parsed.utcoffset() is None:
        raise ValueError("OBD timestamp has no UTC offset")
    return parsed.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class DriveStart:
    drive_id: str
    vehicle_id: str
    logger_id: str
    logger_version: str
    started_at: datetime
    start_reason: str
    original_timezone: str | None = None
    adapter_id: str | None = None
    obd_protocol: str | None = None


@dataclass(frozen=True, slots=True)
class Sample:
    sample_id: str
    drive_id: str
    timestamp: datetime
    sequence: int
    ecu_data_status: str = "live"
    values: dict[str, float | str | list[int] | None] = field(default_factory=dict)
    transport_quality: str = "ok"
    parser_quality: str = "ok"
    missing_pids: tuple[int, ...] = ()

    def as_export(self) -> dict[str, Any]:
        if not _SAFE_SAMPLE_ID.fullmatch(self.sample_id):
            raise ValueError("sample ID must use the canonical safe form")
        if self.sequence < 0:
            raise ValueError("sample sequence must be non-negative")
        if self.ecu_data_status not in {"live", "last_known"}:
            raise ValueError("invalid ECU data status")
        unknown = (
            set(self.values)
            - set(NUMERIC_SAMPLE_FIELDS)
            - set(TEXT_SAMPLE_FIELDS)
            - set(LIST_SAMPLE_FIELDS)
        )
        if unknown:
            raise ValueError(f"unknown sample fields: {sorted(unknown)}")
        for key in TEXT_SAMPLE_FIELDS:
            value = self.values.get(key)
            if value is not None and (
                not isinstance(value, str) or not 1 <= len(value) <= 128 or "\x00" in value
            ):
                raise ValueError(f"{key} must be a bounded string or null")
        sensors = self.values.get("oxygen_sensors_present")
        if sensors is not None and (
            not isinstance(sensors, list)
            or len(sensors) > 8
            or any(
                isinstance(sensor, bool) or not isinstance(sensor, int) or not 1 <= sensor <= 8
                for sensor in sensors
            )
            or len(sensors) != len(set(sensors))
        ):
            raise ValueError("oxygen_sensors_present must contain unique integer indices 1..8")
        body: dict[str, Any] = {
            "sample_id": self.sample_id,
            "drive_id": self.drive_id,
            "timestamp_utc": utc_text(self.timestamp),
            "sequence": self.sequence,
            "ecu_data_status": self.ecu_data_status,
            "quality": {
                "transport": self.transport_quality,
                "parser": self.parser_quality,
                "missing_pids": list(self.missing_pids),
            },
        }
        body.update({key: value for key, value in self.values.items() if value is not None})
        return body


@dataclass(frozen=True, slots=True)
class DiagnosticEvent:
    diagnostic_id: str
    drive_id: str
    timestamp: datetime
    kind: str
    payload: dict[str, Any]

    def as_export(self) -> dict[str, Any]:
        return {
            "diagnostic_id": self.diagnostic_id,
            "drive_id": self.drive_id,
            "timestamp_utc": utc_text(self.timestamp),
            "kind": self.kind,
            "payload": self.payload,
        }
