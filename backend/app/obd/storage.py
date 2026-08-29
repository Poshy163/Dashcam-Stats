"""Small transactional SQLite store intended for the Android logger."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from .schema import DiagnosticEvent, DriveStart, Sample, parse_utc, utc_text

_SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS drives (
    drive_id TEXT PRIMARY KEY,
    vehicle_id TEXT NOT NULL,
    adapter_id TEXT,
    logger_id TEXT NOT NULL,
    logger_version TEXT NOT NULL,
    start_time_utc TEXT NOT NULL,
    finish_time_utc TEXT,
    original_timezone TEXT,
    start_reason TEXT NOT NULL,
    stop_reason TEXT,
    obd_protocol TEXT,
    status TEXT NOT NULL CHECK(status IN ('recording','complete')),
    export_status TEXT NOT NULL DEFAULT 'waiting_for_backup'
        CHECK(export_status IN ('waiting_for_backup','exported','not_exportable_zero_samples')),
    bundle_sha256 TEXT,
    sample_count INTEGER NOT NULL DEFAULT 0 CHECK(sample_count >= 0),
    error_count INTEGER NOT NULL DEFAULT 0 CHECK(error_count >= 0),
    clean_end INTEGER NOT NULL DEFAULT 0 CHECK(clean_end IN (0,1))
);
CREATE TABLE IF NOT EXISTS samples (
    sample_id TEXT PRIMARY KEY,
    drive_id TEXT NOT NULL REFERENCES drives(drive_id) ON DELETE CASCADE,
    timestamp_utc TEXT NOT NULL,
    sequence INTEGER NOT NULL CHECK(sequence >= 0),
    ecu_data_status TEXT NOT NULL CHECK(ecu_data_status IN ('live','last_known')),
    engine_rpm REAL,
    vehicle_speed REAL,
    coolant_temperature REAL,
    intake_air_temperature REAL,
    engine_load REAL,
    throttle_position REAL,
    timing_advance REAL,
    mass_air_flow REAL,
    short_term_fuel_trim_bank_1 REAL,
    long_term_fuel_trim_bank_1 REAL,
    fuel_system_1 TEXT,
    oxygen_sensor_1_voltage REAL,
    oxygen_sensor_1_short_term_fuel_trim REAL,
    oxygen_sensor_2_voltage REAL,
    oxygen_sensor_2_short_term_fuel_trim REAL,
    oxygen_sensors_present TEXT,
    obd_standard TEXT,
    distance_with_mil REAL,
    adapter_voltage REAL,
    estimated_fuel_rate REAL,
    estimated_fuel_consumption REAL,
    quality_json TEXT NOT NULL,
    UNIQUE(drive_id, sequence)
);
CREATE INDEX IF NOT EXISTS ix_obd_samples_drive_time
    ON samples(drive_id, timestamp_utc, sequence);
CREATE TABLE IF NOT EXISTS diagnostics (
    diagnostic_id TEXT PRIMARY KEY,
    drive_id TEXT NOT NULL REFERENCES drives(drive_id) ON DELETE CASCADE,
    timestamp_utc TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    UNIQUE(drive_id, kind, payload_sha256)
);
CREATE INDEX IF NOT EXISTS ix_obd_diagnostics_drive_time
    ON diagnostics(drive_id, timestamp_utc);
"""


class ObdStore:
    """One WAL database; each public write is a complete transaction."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path, timeout=30, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=NORMAL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.execute("PRAGMA busy_timeout=30000")
        self._connection.executescript(_SCHEMA)
        with self.transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO metadata(key,value) VALUES('schema_version','1')"
            )

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> ObdStore:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield self._connection
        except Exception:
            self._connection.rollback()
            raise
        else:
            self._connection.commit()

    def quick_check(self) -> bool:
        return self._connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"

    def checkpoint(self) -> None:
        self._connection.execute("PRAGMA wal_checkpoint(PASSIVE)")

    def start_drive(self, drive: DriveStart) -> None:
        if not drive.drive_id or not drive.vehicle_id or not drive.logger_id:
            raise ValueError("drive, vehicle and logger IDs are required")
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO drives(
                    drive_id,vehicle_id,adapter_id,logger_id,logger_version,
                    start_time_utc,original_timezone,start_reason,obd_protocol,status
                ) VALUES(?,?,?,?,?,?,?,?,?,'recording')
                """,
                (
                    drive.drive_id,
                    drive.vehicle_id,
                    drive.adapter_id,
                    drive.logger_id,
                    drive.logger_version,
                    utc_text(drive.started_at),
                    drive.original_timezone,
                    drive.start_reason,
                    drive.obd_protocol,
                ),
            )

    def open_drives(self) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT * FROM drives WHERE status='recording' ORDER BY start_time_utc,drive_id"
        ).fetchall()
        return [dict(row) for row in rows]

    def recover_interrupted_drives(self, *, stopped_at: datetime) -> list[str]:
        """Close boot-left drives without inventing a clean shutdown."""
        utc_text(stopped_at)  # Validate a caller-provided recovery timestamp; it is fallback only.
        with self.transaction() as connection:
            rows = connection.execute(
                "SELECT drive_id FROM drives WHERE status='recording' ORDER BY start_time_utc"
            ).fetchall()
            connection.execute(
                """
                UPDATE drives SET finish_time_utc=COALESCE(
                    (SELECT MAX(timestamp_utc) FROM samples WHERE samples.drive_id=drives.drive_id),
                    start_time_utc
                ),stop_reason='device_restart',status='complete',clean_end=0
                WHERE status='recording'
                """,
            )
        return [str(row[0]) for row in rows]

    def add_sample(self, sample: Sample) -> bool:
        body = sample.as_export()
        quality = json.dumps(body.pop("quality"), separators=(",", ":"), sort_keys=True)
        values = {
            key: (
                json.dumps(value, separators=(",", ":"))
                if key == "oxygen_sensors_present"
                else value
            )
            for key, value in sample.values.items()
            if value is not None
        }
        columns = [
            "sample_id",
            "drive_id",
            "timestamp_utc",
            "sequence",
            "ecu_data_status",
            *values,
            "quality_json",
        ]
        parameters: list[Any] = [
            sample.sample_id,
            sample.drive_id,
            utc_text(sample.timestamp),
            sample.sequence,
            sample.ecu_data_status,
            *values.values(),
            quality,
        ]
        with self.transaction() as connection:
            cursor = connection.execute(
                f"INSERT OR IGNORE INTO samples({','.join(columns)}) "
                f"VALUES({','.join('?' for _ in columns)})",
                parameters,
            )
            if not cursor.rowcount:
                return False
            connection.execute(
                "UPDATE drives SET sample_count=sample_count+1 WHERE drive_id=? AND status='recording'",
                (sample.drive_id,),
            )
        return True

    def add_diagnostic(self, event: DiagnosticEvent) -> bool:
        payload = json.dumps(event.payload, separators=(",", ":"), sort_keys=True)
        digest = hashlib.sha256(payload.encode()).hexdigest()
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO diagnostics(
                    diagnostic_id,drive_id,timestamp_utc,kind,payload_json,payload_sha256
                ) VALUES(?,?,?,?,?,?)
                """,
                (
                    event.diagnostic_id,
                    event.drive_id,
                    utc_text(event.timestamp),
                    event.kind,
                    payload,
                    digest,
                ),
            )
        return bool(cursor.rowcount)

    def increment_error(self, drive_id: str) -> None:
        with self.transaction() as connection:
            connection.execute(
                "UPDATE drives SET error_count=error_count+1 WHERE drive_id=?", (drive_id,)
            )

    def finish_drive(
        self, drive_id: str, *, finished_at: datetime, stop_reason: str, clean_end: bool
    ) -> None:
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE drives SET finish_time_utc=?,stop_reason=?,status='complete',clean_end=?
                WHERE drive_id=? AND status='recording'
                """,
                (utc_text(finished_at), stop_reason, int(clean_end), drive_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"drive is not recording: {drive_id}")

    def drive(self, drive_id: str) -> dict[str, Any]:
        row = self._connection.execute(
            "SELECT * FROM drives WHERE drive_id=?", (drive_id,)
        ).fetchone()
        if row is None:
            raise KeyError(drive_id)
        return dict(row)

    def samples(self, drive_id: str) -> list[dict[str, Any]]:
        return list(self.iter_samples(drive_id))

    def iter_samples(self, drive_id: str) -> Iterator[dict[str, Any]]:
        """Yield samples in export order without retaining a whole drive in memory."""
        rows = self._connection.execute(
            "SELECT * FROM samples WHERE drive_id=? ORDER BY sequence", (drive_id,)
        )
        for row in rows:
            item = dict(row)
            item["quality"] = json.loads(item.pop("quality_json"))
            if item.get("oxygen_sensors_present") is not None:
                item["oxygen_sensors_present"] = json.loads(item["oxygen_sensors_present"])
            yield item

    def diagnostics(self, drive_id: str) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT * FROM diagnostics WHERE drive_id=? ORDER BY timestamp_utc,diagnostic_id",
            (drive_id,),
        ).fetchall()
        return [
            {
                "diagnostic_id": row["diagnostic_id"],
                "drive_id": row["drive_id"],
                "timestamp_utc": row["timestamp_utc"],
                "kind": row["kind"],
                "payload": json.loads(row["payload_json"]),
            }
            for row in rows
        ]

    def completed_drive_ids(self) -> list[str]:
        """Return completed, non-empty drives whose atomic export is still pending."""
        rows = self._connection.execute(
            """
            SELECT drive_id FROM drives
            WHERE status='complete' AND export_status='waiting_for_backup' AND sample_count>0
            ORDER BY start_time_utc,drive_id
            """
        ).fetchall()
        return [str(row[0]) for row in rows]

    def quarantine_zero_sample_drives(self) -> list[str]:
        """Retain empty crash remnants locally while making their terminal reason explicit."""
        with self.transaction() as connection:
            rows = connection.execute(
                """
                SELECT drive_id FROM drives
                WHERE status='complete' AND export_status='waiting_for_backup' AND sample_count=0
                ORDER BY start_time_utc,drive_id
                """
            ).fetchall()
            connection.execute(
                """
                UPDATE drives SET export_status='not_exportable_zero_samples'
                WHERE status='complete' AND export_status='waiting_for_backup' AND sample_count=0
                """
            )
        return [str(row[0]) for row in rows]

    def mark_exported(self, drive_id: str, bundle_sha256: str) -> None:
        with self.transaction() as connection:
            connection.execute(
                "UPDATE drives SET export_status='exported',bundle_sha256=? WHERE drive_id=?",
                (bundle_sha256, drive_id),
            )

    @staticmethod
    def sample_for_export(row: dict[str, Any]) -> dict[str, Any]:
        excluded = {"quality_json"}
        result = {
            key: value for key, value in row.items() if key not in excluded and value is not None
        }
        result["timestamp_utc"] = utc_text(parse_utc(str(row["timestamp_utc"])))
        return result
