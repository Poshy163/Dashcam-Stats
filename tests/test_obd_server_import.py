"""Server-side OBD bundle validation, durable history and HA retry queue."""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import threading
import time
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.api.routes import obd_import as obd_api
from app.db.models import OBDBundle, OBDBundleState, OBDDrive, OBDSample
from app.db.session import session_scope
from app.ingest import ha_import_queue as queue
from app.ingest import obd_bundle, obd_transfer
from app.ingest.ha_import_queue import (
    PermanentImportError,
    TemporaryImportError,
    post_bundle,
    recover_interrupted_imports,
    redact,
    retry_delay,
)
from app.ingest.models import RemoteFile, UnitInfo, UnitState
from app.ingest.obd_bundle import (
    UNITS_V1,
    BundleError,
    aggregate_statistics,
    file_sha256,
    store_rejected_bundle,
    store_validated_bundle,
    validate_bundle,
)
from app.ingest.transport import TransferResult

BASE = datetime(2026, 8, 29, 1, 0, tzinfo=UTC)


def _receipt_body(drive_id: str, bundle_sha256: str) -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "drive_id": drive_id,
            "bundle_sha256": bundle_sha256,
        },
        ensure_ascii=True,
        separators=(",", ":"),
    )


def _sample(
    drive_id: str,
    sequence: int,
    *,
    sample_id: str | None = None,
    at: datetime | None = None,
    telemetry: bool = True,
) -> dict:
    value: dict = {
        "sample_id": sample_id or f"sample_{drive_id}_{sequence}",
        "drive_id": drive_id,
        "timestamp_utc": (at or BASE + timedelta(seconds=sequence * 5)).isoformat(),
        "sequence": sequence,
        "ecu_data_status": "live",
        "quality": {"transport": "ok", "parser": "ok", "missing_pids": []},
    }
    if telemetry:
        value.update(
            engine_rpm=900 + sequence * 100,
            vehicle_speed=20 + sequence,
            coolant_temperature=80 + sequence,
            estimated_fuel_rate=2.0,
            adapter_voltage=14.1,
        )
    return value


def make_bundle(
    directory: Path,
    drive_id: str = "drive_01",
    *,
    samples: list[dict] | None = None,
    diagnostics: list[dict] | None = None,
    vehicle_id: str = "tiida_c11",
    summary_patch: dict | None = None,
    manifest_patch: dict | None = None,
    corrupt_sample_gzip: bool = False,
    wrong_payload_hash: bool = False,
) -> Path:
    samples = samples or [_sample(drive_id, 0), _sample(drive_id, 1)]
    diagnostics = diagnostics or []
    start = datetime.fromisoformat(samples[0]["timestamp_utc"])
    finish = datetime.fromisoformat(samples[-1]["timestamp_utc"])
    ndjson = (
        b"\n".join(
            json.dumps(item, separators=(",", ":"), ensure_ascii=False).encode() for item in samples
        )
        + b"\n"
    )
    payloads: dict[str, bytes] = {
        "samples.ndjson.gz": b"not-a-gzip" if corrupt_sample_gzip else gzip.compress(ndjson),
        "diagnostics.json": json.dumps(
            {"schema_version": 1, "drive_id": drive_id, "events": diagnostics},
            separators=(",", ":"),
        ).encode(),
        "summary.json": b"",
    }
    summary = {
        "schema_version": 1,
        "drive_id": drive_id,
        "start_time_utc": start.isoformat(),
        "finish_time_utc": finish.isoformat(),
        "duration_s": max(0.0, (finish - start).total_seconds()),
        "distance_km": 0.03,
        "average_speed_kmh": 20.5,
        "maximum_speed_kmh": 21.0,
        "average_rpm": 950.0,
        "maximum_rpm": 1000.0,
        "idle_duration_s": 0.0,
        "estimated_fuel_used_l": 0.003,
        "average_fuel_consumption_l_per_100km": 10.0,
        "maximum_coolant_temperature_c": 81.0,
        "maximum_engine_load_pct": None,
        "dtcs_observed": [],
        "sample_count": len(samples),
        "missing_data_duration_s": 0.0,
        "expected_sample_count": len(samples),
        "received_sample_percentage": 100.0,
        "clean_end": True,
    }
    summary.update(summary_patch or {})
    payloads["summary.json"] = json.dumps(summary, separators=(",", ":")).encode()
    files = {
        name: {
            "size_bytes": len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
            "record_count": (
                len(samples)
                if name == "samples.ndjson.gz"
                else len(diagnostics)
                if name == "diagnostics.json"
                else 1
            ),
        }
        for name, body in payloads.items()
    }
    if wrong_payload_hash:
        files["summary.json"]["sha256"] = "0" * 64
    manifest = {
        "schema_version": 1,
        "bundle_format": "dashcam-obd",
        "drive_id": drive_id,
        "vehicle_id": vehicle_id,
        "adapter_id": None,
        "logger_id": "dashcam_head_unit",
        "logger_version": "1.0.0",
        "start_time_utc": start.isoformat(),
        "finish_time_utc": finish.isoformat(),
        "original_timezone": "Australia/Adelaide",
        "start_reason": "ecu_online",
        "stop_reason": "ecu_offline",
        "obd_protocol": "ISO 15765-4 CAN",
        "completion_status": "complete",
        "clean_end": True,
        "sample_count": len(samples),
        "diagnostic_count": len(diagnostics),
        "error_count": 0,
        "created_at_utc": finish.isoformat(),
        "included_filenames": [
            "manifest.json",
            "samples.ndjson.gz",
            "diagnostics.json",
            "summary.json",
        ],
        "units": UNITS_V1,
        "files": files,
    }
    manifest.update(manifest_patch or {})
    path = directory / f"{drive_id}.obd2.zip"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, separators=(",", ":")))
        for name, body in payloads.items():
            archive.writestr(name, body)
    return path


class TestBundleValidation:
    def test_valid_bundle_preserves_latest_and_original_hour(self, tmp_path, app_config):
        checked = validate_bundle(make_bundle(tmp_path), config=app_config)
        assert checked.latest_sample["sequence"] == 1
        assert checked.statistics[0]["start_time_utc"] == "2026-08-29T01:00:00+00:00"
        assert checked.statistics[0]["sample_count"] == 2
        assert checked.statistics[0]["speed_sample_count"] == 2
        assert checked.statistics[0]["rpm_sample_count"] == 2
        assert checked.ha_payload()["units"] == UNITS_V1
        assert "quality" not in checked.ha_payload()["latest_sample"]

    def test_partial_bundle_name_is_excluded(self, tmp_path, app_config):
        path = make_bundle(tmp_path)
        partial = path.with_name(path.name + ".partial")
        path.rename(partial)
        with pytest.raises(BundleError, match="filename"):
            validate_bundle(partial, config=app_config)

    def test_manifest_hash_mismatch_is_quarantinable(self, tmp_path, app_config):
        with pytest.raises(BundleError, match="SHA-256"):
            validate_bundle(make_bundle(tmp_path, wrong_payload_hash=True), config=app_config)

    def test_nested_corrupt_gzip_is_rejected(self, tmp_path, app_config):
        with pytest.raises(BundleError, match="gzip"):
            validate_bundle(make_bundle(tmp_path, corrupt_sample_gzip=True), config=app_config)

    def test_out_of_order_samples_are_rejected(self, tmp_path, app_config):
        samples = [
            _sample("drive_order", 2, at=BASE),
            _sample("drive_order", 1, at=BASE + timedelta(seconds=5)),
        ]
        with pytest.raises(BundleError, match="increasing"):
            validate_bundle(
                make_bundle(tmp_path, "drive_order", samples=samples), config=app_config
            )

    def test_vehicle_id_and_summary_constraints_fail_before_ha(self, tmp_path, app_config):
        with pytest.raises(BundleError, match="vehicle"):
            validate_bundle(
                make_bundle(tmp_path, "drive_vehicle", vehicle_id="Nissan Tiida"),
                config=app_config,
            )

    def test_rejects_numeric_values_ha_would_reject(self, tmp_path, app_config):
        sample = _sample("drive_bad_coolant", 0)
        sample["coolant_temperature"] = 251
        with pytest.raises(BundleError, match="coolant_temperature"):
            validate_bundle(
                make_bundle(tmp_path, "drive_bad_coolant", samples=[sample]),
                config=app_config,
            )
        with pytest.raises(BundleError, match="distance_km"):
            validate_bundle(
                make_bundle(
                    tmp_path,
                    "drive_bad_distance",
                    summary_patch={"distance_km": 300_001},
                ),
                config=app_config,
            )

    def test_diagnostic_payload_is_validated_before_ha(self, tmp_path, app_config):
        invalid = {
            "diagnostic_id": "diag_1",
            "drive_id": "drive_dtc",
            "timestamp_utc": BASE.isoformat(),
            "kind": "confirmed_dtcs",
            "payload": {"codes": ["not-a-dtc"]},
        }
        with pytest.raises(BundleError, match="canonical OBD DTCs"):
            validate_bundle(
                make_bundle(tmp_path, "drive_dtc", diagnostics=[invalid]),
                config=app_config,
            )

    def test_manifest_extras_and_out_of_order_diagnostics_are_rejected(self, tmp_path, app_config):
        with pytest.raises(BundleError, match="manifest fields"):
            validate_bundle(
                make_bundle(
                    tmp_path,
                    "drive_manifest_extra",
                    manifest_patch={"future_unversioned_field": True},
                ),
                config=app_config,
            )
        diagnostics = [
            {
                "diagnostic_id": "diag_later",
                "drive_id": "drive_diag_order",
                "timestamp_utc": (BASE + timedelta(seconds=5)).isoformat(),
                "kind": "parser_failure",
                "payload": {"category": "response", "message": "bad frame"},
            },
            {
                "diagnostic_id": "diag_earlier",
                "drive_id": "drive_diag_order",
                "timestamp_utc": BASE.isoformat(),
                "kind": "parser_failure",
                "payload": {"category": "response", "message": "bad frame"},
            },
        ]
        with pytest.raises(BundleError, match="ordered by timestamp"):
            validate_bundle(
                make_bundle(tmp_path, "drive_diag_order", diagnostics=diagnostics),
                config=app_config,
            )
        with pytest.raises(BundleError, match="expected_sample_count"):
            validate_bundle(
                make_bundle(
                    tmp_path,
                    "drive_expected",
                    summary_patch={"expected_sample_count": 1},
                ),
                config=app_config,
            )

    def test_missing_values_do_not_become_zero_statistics(self):
        samples = [
            _sample("drive_missing", 0, telemetry=False),
            _sample("drive_missing", 1, telemetry=False),
        ]
        row = aggregate_statistics(samples)[0]
        assert "distance_km" not in row
        assert "engine_runtime_s" not in row
        assert "estimated_fuel_used_l" not in row
        assert "idle_duration_s" not in row
        assert row["speed_sample_count"] == 0
        assert row["rpm_sample_count"] == 0

    def test_sparse_metric_counts_are_preserved_for_ha_weighting(self):
        samples = [
            _sample("drive_sparse_counts", 0),
            _sample("drive_sparse_counts", 1, telemetry=False),
        ]
        row = aggregate_statistics(samples)[0]
        assert row["sample_count"] == 2
        assert row["speed_sample_count"] == 1
        assert row["rpm_sample_count"] == 1
        assert row["average_speed_kmh"] == 20
        assert row["average_rpm"] == 900

    def test_sparse_final_cycle_retains_each_latest_value_timestamp(self, tmp_path, app_config):
        samples = [
            _sample("drive_sparse_latest", 0, at=BASE),
            _sample(
                "drive_sparse_latest",
                1,
                at=BASE + timedelta(seconds=5),
                telemetry=False,
            ),
        ]
        checked = validate_bundle(
            make_bundle(tmp_path, "drive_sparse_latest", samples=samples),
            config=app_config,
        )

        payload = checked.ha_payload()

        assert "coolant_temperature" not in payload["latest_sample"]
        assert payload["latest_values"]["coolant_temperature"] == {
            "value": 80.0,
            "timestamp_utc": BASE.isoformat(),
        }
        assert payload["latest_values"]["engine_rpm"]["timestamp_utc"] == BASE.isoformat()

    def test_long_gap_is_missing_not_integrated_distance(self):
        samples = [
            _sample("drive_gap", 0, at=BASE),
            _sample("drive_gap", 1, at=BASE + timedelta(seconds=5)),
            _sample("drive_gap", 2, at=BASE + timedelta(minutes=5)),
        ]
        row = aggregate_statistics(samples)[0]
        # Only the first five-second interval contributes; the five-minute hole does not.
        assert row["distance_km"] == pytest.approx(20 * 5 / 3600)
        assert row["missing_data_percentage"] > 0

    def test_cross_hour_interval_splits_additive_and_missing_accounting(self):
        samples = [
            _sample(
                "drive_hour_boundary",
                0,
                at=BASE - timedelta(seconds=2),
            ),
            _sample(
                "drive_hour_boundary",
                1,
                at=BASE + timedelta(seconds=3),
            ),
        ]
        samples[0].update(vehicle_speed=36.0, engine_rpm=900.0, estimated_fuel_rate=3.6)
        rows = aggregate_statistics(samples)

        assert [row["start_time_utc"] for row in rows] == [
            "2026-08-29T00:00:00+00:00",
            "2026-08-29T01:00:00+00:00",
        ]
        assert rows[0]["distance_km"] == pytest.approx(36 * 2 / 3600)
        assert rows[1]["distance_km"] == pytest.approx(36 * 3 / 3600)
        assert rows[0]["engine_runtime_s"] == 2
        assert rows[1]["engine_runtime_s"] == 3
        assert rows[0]["estimated_fuel_used_l"] == pytest.approx(3.6 * 2 / 3600)
        assert rows[1]["estimated_fuel_used_l"] == pytest.approx(3.6 * 3 / 3600)
        assert [row["sample_count"] for row in rows] == [1, 1]
        assert [row["expected_sample_count"] for row in rows] == [1, 1]
        assert [row["missing_data_percentage"] for row in rows] == [0, 0]

    @pytest.mark.parametrize(
        ("local_start", "local_finish"),
        [
            # The two 02:30 observations are the repeated fall-back hour.
            (
                datetime(2026, 4, 5, 2, 30, fold=0),
                datetime(2026, 4, 5, 2, 30, fold=1),
            ),
            # Adelaide skips from 01:59:59 to 03:00 during spring-forward.
            (datetime(2026, 10, 4, 1, 30), datetime(2026, 10, 4, 3, 30)),
        ],
    )
    def test_adelaide_dst_transitions_remain_distinct_utc_hours(self, local_start, local_finish):
        adelaide = ZoneInfo("Australia/Adelaide")
        start = local_start.replace(tzinfo=adelaide).astimezone(UTC)
        finish = local_finish.replace(tzinfo=adelaide).astimezone(UTC)
        assert finish > start
        samples = [
            _sample("drive_dst", 0, at=start),
            _sample("drive_dst", 1, at=finish),
        ]

        rows = aggregate_statistics(samples)

        assert [row["start_time_utc"] for row in rows] == [
            start.replace(minute=0, second=0, microsecond=0).isoformat(),
            finish.replace(minute=0, second=0, microsecond=0).isoformat(),
        ]

    def test_sparse_month_expected_counts_are_binned_by_hour_not_observation(self):
        samples = [
            _sample("drive_sparse_month", 0, at=BASE),
            _sample("drive_sparse_month", 1, at=BASE + timedelta(seconds=1)),
            _sample("drive_sparse_month", 2, at=BASE + timedelta(days=30)),
        ]
        started = time.perf_counter()

        rows = aggregate_statistics(samples)

        assert time.perf_counter() - started < 1.0
        assert len(rows) == 721
        assert sum(row["expected_sample_count"] for row in rows) == 2_592_001
        assert rows[-1]["sample_count"] == 1

    def test_freeze_frame_and_cvn_retain_original_observation_times(self, tmp_path, app_config):
        diagnostics = [
            {
                "diagnostic_id": "diag_cvn",
                "drive_id": "drive_diag_continuity",
                "timestamp_utc": (BASE + timedelta(seconds=1)).isoformat(),
                "kind": "calibration_verification_numbers",
                "payload": {"values": ["A1B2C3D4", "01020304"]},
            },
            {
                "diagnostic_id": "diag_freeze",
                "drive_id": "drive_diag_continuity",
                "timestamp_utc": (BASE + timedelta(seconds=2)).isoformat(),
                "kind": "freeze_frame",
                "payload": {
                    "status": "ok",
                    "frame": 0,
                    "dtc": "P0420",
                    "supported_pids": ["01", "04", "05"],
                    "missing_pids": ["04"],
                    "values": {"engine_rpm": 1250.0, "coolant_temperature": 87.0},
                },
            },
        ]
        checked = validate_bundle(
            make_bundle(
                tmp_path,
                "drive_diag_continuity",
                diagnostics=diagnostics,
            ),
            config=app_config,
        )
        payload = checked.ha_payload()["diagnostics"]

        assert payload["calibration_verification_numbers"] == [
            "A1B2C3D4",
            "01020304",
        ]
        assert (
            payload["calibration_verification_numbers_timestamp_utc"]
            == (BASE + timedelta(seconds=1)).isoformat()
        )
        assert payload["freeze_frame"]["values"]["engine_rpm"] == 1250.0
        assert payload["freeze_frame_timestamp_utc"] == (BASE + timedelta(seconds=2)).isoformat()

    def test_all_diagnostic_continuity_values_retain_event_times(self, tmp_path, app_config):
        diagnostics = [
            {
                "diagnostic_id": "diag_dtcs",
                "drive_id": "drive_diag_times",
                "timestamp_utc": (BASE + timedelta(seconds=0)).isoformat(),
                "kind": "confirmed_dtcs",
                "payload": {"codes": ["P0420"]},
            },
            {
                "diagnostic_id": "diag_pending",
                "drive_id": "drive_diag_times",
                "timestamp_utc": BASE.isoformat(),
                "kind": "pending_dtcs",
                "payload": {"codes": []},
            },
            {
                "diagnostic_id": "diag_permanent",
                "drive_id": "drive_diag_times",
                "timestamp_utc": BASE.isoformat(),
                "kind": "permanent_dtcs",
                "payload": {"codes": []},
            },
            *[
                {
                    "diagnostic_id": f"diag_mode_status_{mode}",
                    "drive_id": "drive_diag_times",
                    "timestamp_utc": BASE.isoformat(),
                    "kind": "dtc_mode_status",
                    "payload": {"mode": mode, "status": "ok"},
                }
                for mode in (3, 7, 10)
            ],
            {
                "diagnostic_id": "diag_scan_complete",
                "drive_id": "drive_diag_times",
                "timestamp_utc": (BASE + timedelta(seconds=1)).isoformat(),
                "kind": "dtc_scan_complete",
                "payload": {"modes": [3, 7, 10]},
            },
            {
                "diagnostic_id": "diag_mil",
                "drive_id": "drive_diag_times",
                "timestamp_utc": (BASE + timedelta(seconds=1)).isoformat(),
                "kind": "mil_state",
                "payload": {"on": True},
            },
            {
                "diagnostic_id": "diag_ready",
                "drive_id": "drive_diag_times",
                "timestamp_utc": (BASE + timedelta(seconds=2)).isoformat(),
                "kind": "readiness",
                "payload": {
                    "supported": ["catalyst"],
                    "incomplete": ["catalyst"],
                    "complete": False,
                    "confirmed_dtc_count": 1,
                    "ignition_type": "spark",
                },
            },
            {
                "diagnostic_id": "diag_mode01",
                "drive_id": "drive_diag_times",
                "timestamp_utc": (BASE + timedelta(seconds=3)).isoformat(),
                "kind": "mode01_support",
                "payload": {"supported_pids": [1, 12, 19, 28, 33]},
            },
            {
                "diagnostic_id": "diag_mode09",
                "drive_id": "drive_diag_times",
                "timestamp_utc": (BASE + timedelta(seconds=3)).isoformat(),
                "kind": "mode09_support",
                "payload": {"supported_pids": [3, 5]},
            },
            {
                "diagnostic_id": "diag_mode09_count03",
                "drive_id": "drive_diag_times",
                "timestamp_utc": (BASE + timedelta(seconds=3)).isoformat(),
                "kind": "mode09_count",
                "payload": {"pid": 3, "count": 2},
            },
            {
                "diagnostic_id": "diag_mode09_count05",
                "drive_id": "drive_diag_times",
                "timestamp_utc": (BASE + timedelta(seconds=3)).isoformat(),
                "kind": "mode09_count",
                "payload": {"pid": 5, "count": 1},
            },
            {
                "diagnostic_id": "diag_calibration",
                "drive_id": "drive_diag_times",
                "timestamp_utc": (BASE + timedelta(seconds=4)).isoformat(),
                "kind": "calibration_id",
                "payload": {"value": "CAL-123"},
            },
            {
                "diagnostic_id": "diag_protocol",
                "drive_id": "drive_diag_times",
                "timestamp_utc": (BASE + timedelta(seconds=5)).isoformat(),
                "kind": "protocol_change",
                "payload": {"protocol": "ISO 15765-4 CAN", "protocol_number": "6"},
            },
        ]
        payload = validate_bundle(
            make_bundle(tmp_path, "drive_diag_times", diagnostics=diagnostics),
            config=app_config,
        ).ha_payload()["diagnostics"]

        assert payload["confirmed_dtcs_timestamp_utc"] == BASE.isoformat()
        assert (
            payload["check_engine_light_timestamp_utc"] == (BASE + timedelta(seconds=1)).isoformat()
        )
        assert payload["readiness_timestamp_utc"] == (BASE + timedelta(seconds=2)).isoformat()
        assert payload["readiness_complete"] is False
        assert payload["confirmed_dtc_count"] == 1
        assert payload["ignition_type"] == "spark"
        assert payload["supported_pids"] == [1, 12, 19, 28, 33]
        assert (
            payload["confirmed_dtc_count_timestamp_utc"]
            == (BASE + timedelta(seconds=2)).isoformat()
        )
        assert payload["dtc_scan_timestamp_utc"] == (BASE + timedelta(seconds=1)).isoformat()
        assert payload["mode09_supported_pids"] == [3, 5]
        assert payload["mode09_pid_status"] == {
            "03": "ok",
            "04": "ok",
            "05": "ok",
        }
        assert payload["calibration_id_message_count"] == 2
        assert payload["calibration_verification_number_message_count"] == 1
        assert (
            payload["mode09_supported_pids_timestamp_utc"]
            == (BASE + timedelta(seconds=3)).isoformat()
        )
        assert (
            payload["mode09_pid_status_timestamp_utc"] == (BASE + timedelta(seconds=4)).isoformat()
        )
        assert payload["calibration_id_timestamp_utc"] == (BASE + timedelta(seconds=4)).isoformat()
        assert payload["protocol_timestamp_utc"] == (BASE + timedelta(seconds=5)).isoformat()
        assert payload["protocol_number"] == "6"
        assert payload["protocol_number_timestamp_utc"] == (BASE + timedelta(seconds=5)).isoformat()
        assert payload["last_event_timestamp_utc"] == (BASE + timedelta(seconds=5)).isoformat()

    def test_partial_dtc_observations_never_claim_a_complete_scan(self, tmp_path, app_config):
        confirmed = {
            "diagnostic_id": "diag_confirmed_only",
            "drive_id": "drive_partial_scan",
            "timestamp_utc": BASE.isoformat(),
            "kind": "confirmed_dtcs",
            "payload": {"codes": []},
        }
        checked = validate_bundle(
            make_bundle(
                tmp_path,
                "drive_partial_scan",
                diagnostics=[confirmed],
            ),
            config=app_config,
        )
        assert "dtc_scan_timestamp_utc" not in checked.ha_payload()["diagnostics"]

        false_completion = {
            **confirmed,
            "diagnostic_id": "diag_false_complete",
            "timestamp_utc": (BASE + timedelta(seconds=1)).isoformat(),
            "kind": "dtc_scan_complete",
            "payload": {"modes": [3, 7, 10]},
        }
        with pytest.raises(BundleError, match="lacks three successful"):
            validate_bundle(
                make_bundle(
                    tmp_path,
                    "drive_partial_scan",
                    diagnostics=[confirmed, false_completion],
                ),
                config=app_config,
            )

    def test_later_failed_dtc_modes_cannot_reuse_an_older_successful_scan(
        self, tmp_path, app_config
    ):
        drive_id = "drive_dtc_scan_windows"
        events = [
            *[
                {
                    "diagnostic_id": f"diag_value_{mode}",
                    "drive_id": drive_id,
                    "timestamp_utc": BASE.isoformat(),
                    "kind": kind,
                    "payload": {"codes": []},
                }
                for mode, kind in (
                    (3, "confirmed_dtcs"),
                    (7, "pending_dtcs"),
                    (10, "permanent_dtcs"),
                )
            ],
            *[
                {
                    "diagnostic_id": f"diag_good_status_{mode}",
                    "drive_id": drive_id,
                    "timestamp_utc": BASE.isoformat(),
                    "kind": "dtc_mode_status",
                    "payload": {"mode": mode, "status": "ok"},
                }
                for mode in (3, 7, 10)
            ],
            {
                "diagnostic_id": "diag_good_complete",
                "drive_id": drive_id,
                "timestamp_utc": (BASE + timedelta(seconds=1)).isoformat(),
                "kind": "dtc_scan_complete",
                "payload": {"modes": [3, 7, 10]},
            },
            *[
                {
                    "diagnostic_id": f"diag_failed_status_{mode}",
                    "drive_id": drive_id,
                    "timestamp_utc": (BASE + timedelta(seconds=2)).isoformat(),
                    "kind": "dtc_mode_status",
                    "payload": {"mode": mode, "status": "transport_error"},
                }
                for mode in (3, 7, 10)
            ],
            {
                "diagnostic_id": "diag_forged_complete",
                "drive_id": drive_id,
                "timestamp_utc": (BASE + timedelta(seconds=3)).isoformat(),
                "kind": "dtc_scan_complete",
                "payload": {"modes": [3, 7, 10]},
            },
        ]

        with pytest.raises(BundleError, match="lacks three successful"):
            validate_bundle(make_bundle(tmp_path, drive_id, diagnostics=events), config=app_config)

    def test_repeated_unchanged_scan_evidence_advances_scan_timestamps(self, tmp_path, app_config):
        drive_id = "drive_repeated_scans"
        events = [
            {
                "diagnostic_id": f"diag_{kind}",
                "drive_id": drive_id,
                "timestamp_utc": BASE.isoformat(),
                "kind": kind,
                "payload": {"codes": []},
            }
            for kind in ("confirmed_dtcs", "pending_dtcs", "permanent_dtcs")
        ]
        events.extend(
            {
                "diagnostic_id": f"diag_mode_status_first_{mode}",
                "drive_id": drive_id,
                "timestamp_utc": BASE.isoformat(),
                "kind": "dtc_mode_status",
                "payload": {"mode": mode, "status": "ok"},
            }
            for mode in (3, 7, 10)
        )
        events.extend(
            [
                {
                    "diagnostic_id": "diag_scan_first",
                    "drive_id": drive_id,
                    "timestamp_utc": (BASE + timedelta(seconds=1)).isoformat(),
                    "kind": "dtc_scan_complete",
                    "payload": {"modes": [3, 7, 10]},
                },
                {
                    "diagnostic_id": "diag_freeze_value",
                    "drive_id": drive_id,
                    "timestamp_utc": (BASE + timedelta(seconds=1)).isoformat(),
                    "kind": "freeze_frame",
                    "payload": {
                        "status": "ok",
                        "frame": 0,
                        "dtc": "P0420",
                        "supported_pids": ["0C"],
                        "missing_pids": [],
                        "values": {"engine_rpm": 1200.0},
                    },
                },
                {
                    "diagnostic_id": "diag_freeze_scan_first",
                    "drive_id": drive_id,
                    "timestamp_utc": (BASE + timedelta(seconds=1)).isoformat(),
                    "kind": "freeze_frame_scan_complete",
                    "payload": {"status": "ok"},
                },
                {
                    "diagnostic_id": "diag_mil_value",
                    "drive_id": drive_id,
                    "timestamp_utc": (BASE + timedelta(seconds=1)).isoformat(),
                    "kind": "mil_state",
                    "payload": {"on": False},
                },
                {
                    "diagnostic_id": "diag_readiness_value",
                    "drive_id": drive_id,
                    "timestamp_utc": (BASE + timedelta(seconds=1)).isoformat(),
                    "kind": "readiness",
                    "payload": {
                        "supported": ["catalyst"],
                        "incomplete": [],
                        "complete": True,
                        "confirmed_dtc_count": 0,
                        "ignition_type": "spark",
                    },
                },
                {
                    "diagnostic_id": "diag_readiness_scan_first",
                    "drive_id": drive_id,
                    "timestamp_utc": (BASE + timedelta(seconds=1)).isoformat(),
                    "kind": "readiness_scan_complete",
                    "payload": {"status": "ok"},
                },
                {
                    "diagnostic_id": "diag_mode09_support_value",
                    "drive_id": drive_id,
                    "timestamp_utc": (BASE + timedelta(seconds=1)).isoformat(),
                    "kind": "mode09_support",
                    "payload": {"supported_pids": [3, 4, 5, 6]},
                },
                {
                    "diagnostic_id": "diag_mode09_support_first",
                    "drive_id": drive_id,
                    "timestamp_utc": (BASE + timedelta(seconds=1)).isoformat(),
                    "kind": "mode09_support_scan_complete",
                    "payload": {"status": "ok"},
                },
                {
                    "diagnostic_id": "diag_calid_value",
                    "drive_id": drive_id,
                    "timestamp_utc": (BASE + timedelta(seconds=1)).isoformat(),
                    "kind": "calibration_id",
                    "payload": {"value": "CAL-UNCHANGED"},
                },
                {
                    "diagnostic_id": "diag_cvn_value",
                    "drive_id": drive_id,
                    "timestamp_utc": (BASE + timedelta(seconds=1)).isoformat(),
                    "kind": "calibration_verification_numbers",
                    "payload": {"values": ["A1B2C3D4"]},
                },
                {
                    "diagnostic_id": "diag_calid_count_value",
                    "drive_id": drive_id,
                    "timestamp_utc": (BASE + timedelta(seconds=1)).isoformat(),
                    "kind": "mode09_count",
                    "payload": {"pid": 3, "count": 1},
                },
                {
                    "diagnostic_id": "diag_cvn_count_value",
                    "drive_id": drive_id,
                    "timestamp_utc": (BASE + timedelta(seconds=1)).isoformat(),
                    "kind": "mode09_count",
                    "payload": {"pid": 5, "count": 1},
                },
                {
                    "diagnostic_id": "diag_mode_status_second_3",
                    "drive_id": drive_id,
                    "timestamp_utc": (BASE + timedelta(seconds=2)).isoformat(),
                    "kind": "dtc_mode_status",
                    "payload": {"mode": 3, "status": "ok"},
                },
                {
                    "diagnostic_id": "diag_mode_status_second_7",
                    "drive_id": drive_id,
                    "timestamp_utc": (BASE + timedelta(seconds=2)).isoformat(),
                    "kind": "dtc_mode_status",
                    "payload": {"mode": 7, "status": "no_data"},
                },
                {
                    "diagnostic_id": "diag_mode_status_second_10",
                    "drive_id": drive_id,
                    "timestamp_utc": (BASE + timedelta(seconds=2)).isoformat(),
                    "kind": "dtc_mode_status",
                    "payload": {"mode": 10, "status": "ok"},
                },
                {
                    "diagnostic_id": "diag_scan_second",
                    "drive_id": drive_id,
                    "timestamp_utc": (BASE + timedelta(seconds=2)).isoformat(),
                    "kind": "dtc_scan_complete",
                    "payload": {"modes": [3, 7, 10]},
                },
                {
                    "diagnostic_id": "diag_freeze_scan_second",
                    "drive_id": drive_id,
                    "timestamp_utc": (BASE + timedelta(seconds=2)).isoformat(),
                    "kind": "freeze_frame_scan_complete",
                    "payload": {"status": "no_data"},
                },
                {
                    "diagnostic_id": "diag_readiness_scan_second",
                    "drive_id": drive_id,
                    "timestamp_utc": (BASE + timedelta(seconds=2)).isoformat(),
                    "kind": "readiness_scan_complete",
                    "payload": {"status": "ok"},
                },
                {
                    "diagnostic_id": "diag_mode09_support_second",
                    "drive_id": drive_id,
                    "timestamp_utc": (BASE + timedelta(seconds=2)).isoformat(),
                    "kind": "mode09_support_scan_complete",
                    "payload": {"status": "ok"},
                },
                *[
                    {
                        "diagnostic_id": f"diag_mode09_probe_second_{pid}",
                        "drive_id": drive_id,
                        "timestamp_utc": (BASE + timedelta(seconds=2)).isoformat(),
                        "kind": "mode09_probe_status",
                        "payload": {"pid": pid, "status": "ok"},
                    }
                    for pid in (3, 4, 5, 6)
                ],
            ]
        )
        payload = validate_bundle(
            make_bundle(tmp_path, drive_id, diagnostics=events), config=app_config
        ).ha_payload()["diagnostics"]

        assert payload["freeze_frame_timestamp_utc"] == (BASE + timedelta(seconds=1)).isoformat()
        assert payload["freeze_frame"]["status"] == "ok"
        assert payload["freeze_frame"]["values"]["engine_rpm"] == 1200.0
        assert payload["dtc_scan_timestamp_utc"] == (BASE + timedelta(seconds=2)).isoformat()
        assert (
            payload["freeze_frame_scan_timestamp_utc"] == (BASE + timedelta(seconds=2)).isoformat()
        )
        assert payload["freeze_frame_scan_status"] == "no_data"
        assert payload["mode09_scan_status"] == "ok"
        assert (
            payload["mode09_scan_status_timestamp_utc"] == (BASE + timedelta(seconds=2)).isoformat()
        )
        for timestamp_field in (
            "confirmed_dtcs_timestamp_utc",
            "pending_dtcs_timestamp_utc",
            "permanent_dtcs_timestamp_utc",
            "check_engine_light_timestamp_utc",
            "readiness_timestamp_utc",
            "confirmed_dtc_count_timestamp_utc",
            "ignition_type_timestamp_utc",
            "mode09_supported_pids_timestamp_utc",
            "calibration_id_timestamp_utc",
            "calibration_verification_numbers_timestamp_utc",
            "calibration_id_message_count_timestamp_utc",
            "calibration_verification_number_message_count_timestamp_utc",
        ):
            assert payload[timestamp_field] == (BASE + timedelta(seconds=2)).isoformat()

    @pytest.mark.parametrize(
        "readiness",
        [
            {
                "supported": [],
                "incomplete": ["catalyst"],
                "complete": False,
                "confirmed_dtc_count": 0,
                "ignition_type": "spark",
            },
            {
                "supported": ["catalyst"],
                "incomplete": ["catalyst"],
                "complete": True,
                "confirmed_dtc_count": 0,
                "ignition_type": "spark",
            },
        ],
    )
    def test_readiness_consistency_is_enforced(self, tmp_path, app_config, readiness):
        diagnostic = {
            "diagnostic_id": "diag_ready_bad",
            "drive_id": "drive_ready_bad",
            "timestamp_utc": BASE.isoformat(),
            "kind": "readiness",
            "payload": readiness,
        }
        with pytest.raises(BundleError, match="readiness"):
            validate_bundle(
                make_bundle(
                    tmp_path,
                    "drive_ready_bad",
                    diagnostics=[diagnostic],
                ),
                config=app_config,
            )

    def test_freeze_frame_unknown_fields_are_rejected(self, tmp_path, app_config):
        diagnostic = {
            "diagnostic_id": "diag_freeze_bad",
            "drive_id": "drive_freeze_bad",
            "timestamp_utc": BASE.isoformat(),
            "kind": "freeze_frame",
            "payload": {
                "status": "no_data",
                "frame": 0,
                "values": {},
                "private_transport_dump": "must not cross the trust boundary",
            },
        }
        with pytest.raises(BundleError, match="freeze_frame fields"):
            validate_bundle(
                make_bundle(
                    tmp_path,
                    "drive_freeze_bad",
                    diagnostics=[diagnostic],
                ),
                config=app_config,
            )


class TestTransactionalHistory:
    async def test_stores_high_resolution_rows_exactly_once(self, db_session, app_config):
        samples = [_sample("drive_01", 0), _sample("drive_01", 1)]
        samples[-1].update(
            oxygen_sensors_present=[1, 2],
            obd_standard="JOBD",
            distance_with_mil=42.0,
        )
        path = make_bundle(
            app_config.obd_verified_dir,
            samples=samples,
            manifest_patch={"error_count": 3},
        )
        checked = validate_bundle(path, config=app_config)
        async with session_scope() as session:
            first = await store_validated_bundle(session, checked)
        async with session_scope() as session:
            second = await store_validated_bundle(session, checked)
        assert first.id == second.id
        async with session_scope() as session:
            assert (await session.execute(select(func.count(OBDDrive.id)))).scalar() == 1
            assert (await session.execute(select(func.count(OBDSample.id)))).scalar() == 2
            stored_drive = (await session.execute(select(OBDDrive))).scalar_one()
            assert stored_drive.error_count == 3
            stored_sample = (
                await session.execute(select(OBDSample).where(OBDSample.sequence == 1))
            ).scalar_one()
            assert stored_sample.oxygen_sensors_present == [1, 2]
            assert stored_sample.obd_standard == "JOBD"
            assert stored_sample.distance_with_mil_km == 42.0
            assert checked.latest_values["oxygen_sensors_present"]["value"] == [1, 2]

    async def test_manifest_text_boundaries_match_database_columns(self, db_session, app_config):
        checked = validate_bundle(
            make_bundle(
                app_config.obd_verified_dir,
                "drive_text_widths",
                manifest_patch={
                    "start_reason": "s" * 128,
                    "stop_reason": "t" * 128,
                    "obd_protocol": "p" * 256,
                },
            ),
            config=app_config,
        )
        async with session_scope() as session:
            await store_validated_bundle(session, checked)
        async with session_scope() as session:
            drive = (
                await session.execute(
                    select(OBDDrive).where(OBDDrive.drive_id == "drive_text_widths")
                )
            ).scalar_one()
            assert len(drive.start_reason or "") == 128
            assert len(drive.stop_reason or "") == 128
            assert len(drive.obd_protocol or "") == 256
        assert OBDDrive.__table__.c.start_reason.type.length == 128
        assert OBDDrive.__table__.c.stop_reason.type.length == 128
        assert OBDDrive.__table__.c.obd_protocol.type.length == 256

    async def test_sample_decode_batches_run_off_the_event_loop(
        self, db_session, app_config, monkeypatch
    ):
        checked = validate_bundle(
            make_bundle(app_config.obd_verified_dir, "drive_threaded_decode"),
            config=app_config,
        )
        event_loop_thread = threading.get_ident()
        worker_threads: list[int] = []
        original = obd_bundle._next_sample_rows

        def observe(*args, **kwargs):
            worker_threads.append(threading.get_ident())
            return original(*args, **kwargs)

        monkeypatch.setattr(obd_bundle, "_next_sample_rows", observe)
        async with session_scope() as session:
            await store_validated_bundle(session, checked)

        assert worker_threads
        assert all(thread_id != event_loop_thread for thread_id in worker_threads)

    async def test_sample_conflict_rolls_back_the_whole_second_drive(self, db_session, app_config):
        first_path = make_bundle(
            app_config.obd_verified_dir,
            "drive_first",
            samples=[_sample("drive_first", 0, sample_id="global_sample")],
        )
        first = validate_bundle(first_path, config=app_config)
        async with session_scope() as session:
            await store_validated_bundle(session, first)

        second_path = make_bundle(
            app_config.obd_verified_dir,
            "drive_second",
            samples=[_sample("drive_second", 0, sample_id="global_sample")],
        )
        second = validate_bundle(second_path, config=app_config)
        with pytest.raises(IntegrityError):
            async with session_scope() as session:
                await store_validated_bundle(session, second)
        async with session_scope() as session:
            drives = int((await session.execute(select(func.count(OBDDrive.id)))).scalar() or 0)
        assert drives == 1


class _FakeClient:
    def __init__(self, response: httpx.Response | Exception, capture: dict):
        self.response = response
        self.capture = capture

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def stream(self, method, url, *, content, headers):
        self.capture.update(method=method, url=url, body=content, headers=headers)
        return _FakeResponseContext(self.response)


class _FakeResponseContext:
    def __init__(self, response: httpx.Response | Exception):
        self.response = response

    async def __aenter__(self):
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    async def __aexit__(self, *_args):
        return None


def _response(code: int, body: dict | None = None, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(
        code,
        json=body or {"status": "error", "errors": ["no"]},
        headers=headers,
        request=httpx.Request("POST", "http://ha/api/obd2_ble/import"),
    )


@pytest.fixture
def ha_config(app_config, tmp_path, monkeypatch):
    token_file = tmp_path / "ha-token"
    token_file.write_text("secret-token-value", encoding="utf-8")
    app_config.ha_url = "http://192.168.1.103:8123"
    app_config.ha_token_file = token_file
    app_config.ha_obd_import_path = "/api/obd2_ble/import"
    return app_config


class TestHAClient:
    def test_documented_local_secret_directory_is_gitignored(self):
        assert "secrets/" in Path(".gitignore").read_text(encoding="utf-8").splitlines()

    @pytest.mark.parametrize("host", ["example\u3002com", "b\u00fccher.example"])
    def test_cleartext_unicode_public_hostname_is_rejected(self, ha_config, host):
        ha_config.ha_url = f"http://{host}:8123"
        with pytest.raises(queue.HAConfigurationError, match="trusted LAN"):
            queue.import_url(ha_config)

    async def test_exact_bounded_gzip_body_and_idempotency_key(
        self, tmp_path, ha_config, monkeypatch
    ):
        checked = validate_bundle(make_bundle(tmp_path), config=ha_config)
        capture: dict = {}
        fake = _FakeClient(_response(200, {"status": "ok", "drive_id": checked.drive_id}), capture)
        client_options: dict = {}

        def client_factory(**kwargs):
            client_options.update(kwargs)
            return fake

        monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:8080")
        monkeypatch.setattr(queue.httpx, "AsyncClient", client_factory)
        result = await post_bundle(checked, config=ha_config)
        body = json.loads(gzip.decompress(capture["body"]))
        assert result["status"] == "ok"
        assert set(body) == {
            "schema_version",
            "drive_id",
            "bundle_sha256",
            "vehicle_id",
            "units",
            "latest_sample",
            "latest_values",
            "summary",
            "statistics",
            "diagnostics",
        }
        assert capture["headers"]["Authorization"] == "Bearer secret-token-value"
        assert checked.bundle_sha256 in capture["headers"]["Idempotency-Key"]
        assert client_options["trust_env"] is False

    @pytest.mark.parametrize("drive_id", [None, "different_drive"])
    async def test_success_requires_matching_drive_id(
        self, drive_id, tmp_path, ha_config, monkeypatch
    ):
        checked = validate_bundle(make_bundle(tmp_path), config=ha_config)
        body = {"status": "ok"}
        if drive_id is not None:
            body["drive_id"] = drive_id
        monkeypatch.setattr(
            queue.httpx,
            "AsyncClient",
            lambda **_kwargs: _FakeClient(_response(200, body), {}),
        )
        with pytest.raises(PermanentImportError, match="drive_id"):
            await post_bundle(checked, config=ha_config)

    async def test_success_body_is_allowlisted_and_redacted(self, tmp_path, ha_config, monkeypatch):
        checked = validate_bundle(make_bundle(tmp_path), config=ha_config)
        response = _response(
            200,
            {
                "status": "ok",
                "drive_id": checked.drive_id,
                "warnings": ["Bearer secret-token-value was rejected upstream"],
                "accepted_samples": 1,
            },
        )
        monkeypatch.setattr(
            queue.httpx,
            "AsyncClient",
            lambda **_kwargs: _FakeClient(response, {}),
        )
        result = await post_bundle(checked, config=ha_config)
        assert "secret-token-value" not in json.dumps(result)
        assert set(result) == {"status", "drive_id", "warnings", "accepted_samples"}

        unknown = _response(
            200,
            {
                "status": "ok",
                "drive_id": checked.drive_id,
                "reflected_request": "Bearer secret-token-value",
            },
        )
        monkeypatch.setattr(
            queue.httpx,
            "AsyncClient",
            lambda **_kwargs: _FakeClient(unknown, {}),
        )
        with pytest.raises(PermanentImportError, match="unsupported fields"):
            await post_bundle(checked, config=ha_config)

    async def test_decoded_compressed_response_is_bounded(self, tmp_path, ha_config, monkeypatch):
        checked = validate_bundle(make_bundle(tmp_path), config=ha_config)
        response = httpx.Response(
            200,
            headers={"Content-Encoding": "gzip"},
            content=gzip.compress(b"x" * (queue.MAX_RESPONSE_BYTES + 2)),
            request=httpx.Request("POST", "http://ha/api/obd2_ble/import"),
        )
        monkeypatch.setattr(
            queue.httpx,
            "AsyncClient",
            lambda **_kwargs: _FakeClient(response, {}),
        )

        with pytest.raises(PermanentImportError, match="exceeds 256 KiB"):
            await post_bundle(checked, config=ha_config)

    async def test_streaming_response_stops_after_limit_sentinel(
        self, tmp_path, ha_config, monkeypatch
    ):
        class HostileStream(httpx.AsyncByteStream):
            yielded = 0

            async def __aiter__(self):
                for _ in range(100):
                    self.yielded += 1
                    yield b"x" * (64 * 1024)

        checked = validate_bundle(make_bundle(tmp_path), config=ha_config)
        stream = HostileStream()
        response = httpx.Response(
            200,
            stream=stream,
            request=httpx.Request("POST", "http://ha/api/obd2_ble/import"),
        )
        monkeypatch.setattr(
            queue.httpx,
            "AsyncClient",
            lambda **_kwargs: _FakeClient(response, {}),
        )

        with pytest.raises(PermanentImportError, match="exceeds 256 KiB"):
            await post_bundle(checked, config=ha_config)
        assert stream.yielded == 5

    @pytest.mark.parametrize("code", [401, 403, 400, 413, 415, 422])
    async def test_permanent_http_failures(self, code, tmp_path, ha_config, monkeypatch):
        checked = validate_bundle(make_bundle(tmp_path), config=ha_config)
        monkeypatch.setattr(
            queue.httpx,
            "AsyncClient",
            lambda **_kwargs: _FakeClient(_response(code), {}),
        )
        with pytest.raises(PermanentImportError) as caught:
            await post_bundle(checked, config=ha_config)
        if code in {401, 403}:
            assert caught.value.kind == "authentication"

    @pytest.mark.parametrize("code", [404, 408, 425, 429, 500, 503])
    async def test_temporary_http_failures(self, code, tmp_path, ha_config, monkeypatch):
        checked = validate_bundle(make_bundle(tmp_path), config=ha_config)
        response = _response(code, headers={"Retry-After": "120"})
        monkeypatch.setattr(
            queue.httpx,
            "AsyncClient",
            lambda **_kwargs: _FakeClient(response, {}),
        )
        with pytest.raises(TemporaryImportError) as caught:
            await post_bundle(checked, config=ha_config)
        if code in {425, 429, 503}:
            assert caught.value.retry_after == 120

    @pytest.mark.parametrize(
        "error_type",
        [httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout],
    )
    async def test_transport_failures_are_temporary(
        self, error_type, tmp_path, ha_config, monkeypatch
    ):
        checked = validate_bundle(make_bundle(tmp_path), config=ha_config)
        request = httpx.Request("POST", "http://ha/api/obd2_ble/import")
        error = error_type("Home Assistant unavailable", request=request)
        monkeypatch.setattr(
            queue.httpx,
            "AsyncClient",
            lambda **_kwargs: _FakeClient(error, {}),
        )

        with pytest.raises(TemporaryImportError, match=error_type.__name__):
            await post_bundle(checked, config=ha_config)

    async def test_startup_404_retries_to_success(self, tmp_path, ha_config, monkeypatch):
        checked = validate_bundle(make_bundle(tmp_path), config=ha_config)
        responses = iter(
            [
                _response(404),
                _response(200, {"status": "ok", "drive_id": checked.drive_id}),
            ]
        )
        monkeypatch.setattr(
            queue.httpx,
            "AsyncClient",
            lambda **_kwargs: _FakeClient(next(responses), {}),
        )

        with pytest.raises(TemporaryImportError) as caught:
            await post_bundle(checked, config=ha_config)
        assert caught.value.status == 404
        assert (await post_bundle(checked, config=ha_config))["status"] == "ok"

    def test_token_redaction_and_bounded_backoff(self, ha_config):
        assert "secret-token-value" not in redact(
            "Authorization: Bearer secret-token-value", token="secret-token-value"
        )
        assert retry_delay(50, config=ha_config) == ha_config.obd_retry_max_s


class TestRecoveryAndAPI:
    async def _stored(self, app_config, drive_id="drive_queue") -> OBDBundle:
        path = make_bundle(app_config.obd_verified_dir, drive_id)
        checked = validate_bundle(path, config=app_config)
        async with session_scope() as session:
            return await store_validated_bundle(session, checked)

    async def test_restart_requeues_importing(self, db_session, app_config):
        row = await self._stored(app_config)
        async with session_scope() as session:
            current = await session.get(OBDBundle, row.id)
            current.state = OBDBundleState.IMPORTING.value
        assert await recover_interrupted_imports() == 1
        async with session_scope() as session:
            current = await session.get(OBDBundle, row.id)
            assert current.state == OBDBundleState.RETRY_WAIT.value
            assert current.next_attempt_at is not None

    async def test_restart_recovers_import_crash_after_quarantine_move(
        self, db_session, app_config
    ):
        row = await self._stored(app_config, "drive_import_quarantine_crash")
        verified = app_config.obd_verified_dir / row.filename
        quarantined = app_config.obd_quarantine_dir / row.filename
        verified.replace(quarantined)
        quarantined.write_bytes(b"corrupt bytes retained before the database mark")
        async with session_scope() as session:
            current = await session.get(OBDBundle, row.id)
            current.state = OBDBundleState.IMPORTING.value
            current.import_started_at = BASE

        assert await recover_interrupted_imports(config=app_config) == 1

        async with session_scope() as session:
            current = await session.get(OBDBundle, row.id)
            assert current.state == OBDBundleState.QUARANTINED.value
            assert current.failure_kind == "integrity"
            assert current.next_attempt_at is None
            assert current.import_started_at is None
            assert "observed sha256=" in (current.last_error or "")
        assert quarantined.read_bytes() == b"corrupt bytes retained before the database mark"

    async def test_restart_retains_oversized_quarantine_only_archive(self, db_session, app_config):
        row = await self._stored(app_config, "drive_import_oversized_quarantine")
        verified = app_config.obd_verified_dir / row.filename
        quarantined = app_config.obd_quarantine_dir / row.filename
        verified.replace(quarantined)
        quarantined.write_bytes(b"x" * 1024)
        app_config.obd_max_bundle_bytes = 128
        async with session_scope() as session:
            current = await session.get(OBDBundle, row.id)
            current.state = OBDBundleState.IMPORTING.value

        assert await recover_interrupted_imports(config=app_config) == 1

        async with session_scope() as session:
            current = await session.get(OBDBundle, row.id)
            assert current.state == OBDBundleState.QUARANTINED.value
            assert current.failure_kind == "integrity"
            assert "unreadable or oversized" in (current.last_error or "")
        assert quarantined.stat().st_size == 1024

    async def test_restart_recovers_interrupted_manual_validation(self, db_session, app_config):
        row = await self._stored(app_config, "drive_validation_restart")
        async with session_scope() as session:
            current = await session.get(OBDBundle, row.id)
            current.state = OBDBundleState.VALIDATING.value

        assert await queue.recover_interrupted_validations(config=app_config) == 1

        async with session_scope() as session:
            recovered = await session.get(OBDBundle, row.id)
            assert recovered.state == OBDBundleState.READY_TO_IMPORT.value
            assert recovered.failure_kind == "interrupted"

    async def test_manual_rebuild_does_not_reset_active_import(self, db_session, app_config):
        row = await self._stored(app_config, "drive_active_rebuild")
        async with session_scope() as session:
            current = await session.get(OBDBundle, row.id)
            current.state = OBDBundleState.IMPORTING.value

        await queue.rebuild_queue(config=app_config)

        async with session_scope() as session:
            active = await session.get(OBDBundle, row.id)
            assert active.state == OBDBundleState.IMPORTING.value

    async def test_temporary_failure_is_durably_retried_and_acknowledged(
        self, db_session, app_config, monkeypatch
    ):
        row = await self._stored(app_config, "drive_retry_success")
        async with session_scope() as session:
            current = await session.get(OBDBundle, row.id)
            current.state = OBDBundleState.IMPORTING.value
            current.attempts = 1
        monkeypatch.setattr(
            queue,
            "post_bundle",
            AsyncMock(side_effect=TemporaryImportError("HA starting", status=503)),
        )

        await queue.import_one(row.id)

        async with session_scope() as session:
            waiting = await session.get(OBDBundle, row.id)
            assert waiting.state == OBDBundleState.RETRY_WAIT.value
            assert waiting.last_http_status == 503
            waiting.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
        claimed = await queue._claim_next()
        assert claimed == row.id
        monkeypatch.setattr(
            queue,
            "post_bundle",
            AsyncMock(
                return_value={
                    "status": "ok",
                    "drive_id": "drive_retry_success",
                    "accepted_samples": 1,
                }
            ),
        )

        await queue.import_one(row.id)

        async with session_scope() as session:
            imported = await session.get(OBDBundle, row.id)
            assert imported.state == OBDBundleState.IMPORTED.value
            assert imported.ha_result == {
                "status": "ok",
                "drive_id": "drive_retry_success",
                "accepted_samples": 1,
            }

    async def test_worker_survives_one_iteration_exception(
        self, db_session, app_config, monkeypatch
    ):
        worker = queue.HAImportWorker()
        calls = 0

        async def no_rebuild():
            return {}

        async def claim():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("one transient filesystem error")
            worker._stop.set()
            return None

        app_config.obd_import_poll_s = 0.01
        monkeypatch.setattr(queue, "rebuild_queue", no_rebuild)
        monkeypatch.setattr(queue, "_claim_next", claim)
        monkeypatch.setattr(queue, "get_config", lambda: app_config)
        await asyncio.wait_for(worker._run(), timeout=1)
        assert calls == 2

    async def test_manual_retry_and_status_never_expose_token(
        self, db_session, app_config, client, monkeypatch, tmp_path
    ):
        row = await self._stored(app_config)
        async with session_scope() as session:
            current = await session.get(OBDBundle, row.id)
            current.state = OBDBundleState.FAILED.value
            current.last_error = "HTTP 401"
        monkeypatch.setenv("HA_URL", "http://192.168.1.103:8123")
        response = await client.post(f"/api/obd/bundles/{row.id}/retry")
        assert response.status_code == 200
        status_response = await client.get("/api/obd/status")
        assert status_response.status_code == 200
        assert "secret" not in status_response.text.lower()

    async def test_manual_retry_rejects_in_flight_import(self, db_session, app_config, client):
        row = await self._stored(app_config, "drive_importing")
        async with session_scope() as session:
            current = await session.get(OBDBundle, row.id)
            current.state = OBDBundleState.IMPORTING.value
        response = await client.post(f"/api/obd/bundles/{row.id}/retry")
        assert response.status_code == 409
        async with session_scope() as session:
            current = await session.get(OBDBundle, row.id)
            assert current.state == OBDBundleState.IMPORTING.value

    async def test_manual_validate_rejects_in_flight_import(self, db_session, app_config, client):
        row = await self._stored(app_config, "drive_validate_importing")
        async with session_scope() as session:
            current = await session.get(OBDBundle, row.id)
            current.state = OBDBundleState.IMPORTING.value

        response = await client.post(f"/api/obd/bundles/{row.id}/validate")

        assert response.status_code == 409
        async with session_scope() as session:
            current = await session.get(OBDBundle, row.id)
            assert current.state == OBDBundleState.IMPORTING.value
            assert current.verified_at is not None

    async def test_manual_validation_claim_blocks_worker_race(
        self, db_session, app_config, client, monkeypatch
    ):
        row = await self._stored(app_config, "drive_validation_claim")
        entered = threading.Event()
        release = threading.Event()
        original = obd_api.validate_bundle

        def blocked_validate(*args, **kwargs):
            entered.set()
            assert release.wait(2)
            return original(*args, **kwargs)

        monkeypatch.setattr(obd_api, "validate_bundle", blocked_validate)
        request = asyncio.create_task(client.post(f"/api/obd/bundles/{row.id}/validate"))
        assert await asyncio.to_thread(entered.wait, 2)
        try:
            assert await queue._claim_next() is None
        finally:
            release.set()

        response = await request

        assert response.status_code == 200
        assert response.json()["valid"] is True
        async with session_scope() as session:
            current = await session.get(OBDBundle, row.id)
            assert current.state == OBDBundleState.READY_TO_IMPORT.value

    async def test_queue_rebuild_registers_orphan_bundle(self, db_session, app_config):
        make_bundle(app_config.obd_verified_dir, "drive_orphan")
        result = await queue.rebuild_queue(config=app_config)
        assert result["registered"] == 1
        async with session_scope() as session:
            stored = (
                await session.execute(select(OBDBundle).where(OBDBundle.drive_id == "drive_orphan"))
            ).scalar_one()
        assert stored.state == OBDBundleState.READY_TO_IMPORT.value

    async def test_rebuild_stale_quarantine_cleanup_failure_does_not_starve_later_orphan(
        self, db_session, app_config, monkeypatch
    ):
        repair = await self._stored(app_config, "drive_rebuild_cleanup")
        stale = app_config.obd_quarantine_dir / repair.filename
        stale.write_bytes(b"old quarantined bytes")
        async with session_scope() as session:
            row = await session.get(OBDBundle, repair.id)
            row.state = OBDBundleState.QUARANTINED.value
            row.failure_kind = "integrity"
        make_bundle(app_config.obd_verified_dir, "drive_rebuild_later")

        original_unlink = Path.unlink

        def guarded_unlink(path: Path, *args, **kwargs):
            if path == stale:
                raise OSError("simulated stale cleanup failure")
            return original_unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", guarded_unlink)

        result = await queue.rebuild_queue(config=app_config)

        assert result["registered"] == 1
        assert stale.exists()
        async with session_scope() as session:
            repaired = await session.get(OBDBundle, repair.id)
            later = (
                await session.execute(
                    select(OBDBundle).where(OBDBundle.drive_id == "drive_rebuild_later")
                )
            ).scalar_one()
            assert repaired.state == OBDBundleState.READY_TO_IMPORT.value
            assert later.state == OBDBundleState.READY_TO_IMPORT.value

    async def test_queue_rebuild_keeps_corrupt_row_and_bytes_in_quarantine(
        self, db_session, app_config
    ):
        row = await self._stored(app_config, "drive_rebuild_corrupt")
        path = app_config.obd_verified_dir / row.filename
        with path.open("ab") as handle:
            handle.write(b"changed")
        result = await queue.rebuild_queue(config=app_config)
        assert result["quarantined"] == 1
        assert not path.exists()
        assert (app_config.obd_quarantine_dir / row.filename).exists()
        async with session_scope() as session:
            current = await session.get(OBDBundle, row.id)
            assert current.state == OBDBundleState.QUARANTINED.value

    async def test_invalid_orphan_has_durable_manual_recovery_row(
        self, db_session, app_config, client
    ):
        name = "drive_rejected_orphan.obd2.zip"
        body = b"not a zip"
        (app_config.obd_verified_dir / name).write_bytes(body)

        result = await queue.rebuild_queue(config=app_config)

        assert result["quarantined"] == 1
        quarantine_path = app_config.obd_quarantine_dir / name
        assert quarantine_path.read_bytes() == body
        async with session_scope() as session:
            row = (
                await session.execute(select(OBDBundle).where(OBDBundle.filename == name))
            ).scalar_one()
            row_id = row.id
            assert row.metadata_trusted is False
            assert row.bundle_hash == hashlib.sha256(body).hexdigest()
            assert row.state == OBDBundleState.QUARANTINED.value
            assert row.last_error

        # A restart/rebuild does not make the rejection disappear from API counts.
        await queue.rebuild_queue(config=app_config)
        status_response = await client.get("/api/obd/status")
        list_response = await client.get("/api/obd/bundles")
        assert status_response.json()["failed_count"] >= 1
        listed = next(item for item in list_response.json()["items"] if item["id"] == row_id)
        assert listed["metadata_trusted"] is False

        invalid = await client.post(f"/api/obd/bundles/{row_id}/validate")
        assert invalid.status_code == 200
        assert invalid.json()["valid"] is False
        assert quarantine_path.exists()

        make_bundle(app_config.obd_quarantine_dir, "drive_rejected_orphan")
        repaired = await client.post(f"/api/obd/bundles/{row_id}/validate")
        assert repaired.status_code == 200
        assert repaired.json()["valid"] is True
        assert repaired.json()["bundle"]["metadata_trusted"] is True
        assert not quarantine_path.exists()
        assert (app_config.obd_verified_dir / name).exists()
        async with session_scope() as session:
            promoted = await session.get(OBDBundle, row_id)
            assert promoted.metadata_trusted is True
            assert promoted.state == OBDBundleState.READY_TO_IMPORT.value
            assert (
                await session.execute(
                    select(func.count(OBDDrive.id)).where(OBDDrive.bundle_id == row_id)
                )
            ).scalar_one() == 1

    async def test_newly_supported_same_bytes_promote_without_recopy(
        self, db_session, app_config, client
    ):
        path = make_bundle(app_config.obd_quarantine_dir, "drive_newly_supported")
        bundle_hash, size_bytes = file_sha256(path)
        async with session_scope() as session:
            row = await store_rejected_bundle(
                session,
                filename=path.name,
                bundle_hash=bundle_hash,
                size_bytes=size_bytes,
                error="unsupported before server upgrade",
                quarantined=True,
            )
            row_id = row.id

        response = await client.post(f"/api/obd/bundles/{row_id}/validate")

        assert response.status_code == 200
        assert response.json()["valid"] is True
        assert response.json()["bundle"]["bundle_sha256"] == bundle_hash
        async with session_scope() as session:
            promoted = await session.get(OBDBundle, row_id)
            assert promoted.metadata_trusted is True
            assert promoted.bundle_hash == bundle_hash
            assert (
                await session.execute(
                    select(func.count(OBDSample.id))
                    .join(OBDDrive)
                    .where(OBDDrive.bundle_id == row_id)
                )
            ).scalar_one() == 2


class TestTransferIsolation:
    def test_status_reconciles_only_successful_remote_removals(self):
        drained = obd_transfer.OBDTransferStatus()
        drained.set_inventory(1)
        drained.set_logger({"pending_bundle_count": 1, "state": "parked"})
        drained.finish(obd_transfer.OBDTransferResult(copied=1, removed_from_unit=1))
        assert drained.snapshot()["waiting_on_unit"] == 0
        assert drained.snapshot()["logger"]["pending_bundle_count"] == 0

        delete_failed = obd_transfer.OBDTransferStatus()
        delete_failed.set_inventory(1)
        delete_failed.set_logger({"pending_bundle_count": 1, "state": "parked"})
        delete_failed.finish(obd_transfer.OBDTransferResult(copied=1, removed_from_unit=0))
        assert delete_failed.snapshot()["waiting_on_unit"] == 1
        assert delete_failed.snapshot()["logger"]["pending_bundle_count"] == 1

    async def test_logger_status_accepts_canonical_redacted_fixture(self, monkeypatch):
        fixture = {
            "schema_version": 1,
            "logger_version": "1.0.0",
            "state": "ecu_online",
            "ownership_enabled": True,
            "adapter_state": "connected",
            "vehicle_state": "engine_running",
            "current_drive_id": "drive_current",
            "last_drive_id": "drive_previous",
            "last_drive_finished_at_utc": "2026-08-29T00:00:00+00:00",
            "pending_bundle_count": 3,
            "sample_count": 42,
            "last_error": None,
            "last_error_at_utc": None,
            "updated_at_utc": "2026-08-29T01:00:00+00:00",
            "private_adapter_address": "AA:BB:CC:DD:EE:FF",
        }
        monkeypatch.setattr(
            obd_transfer.adb,
            "shell",
            lambda *_args, **_kwargs: _async_value(json.dumps(fixture)),
        )
        status = await obd_transfer.read_logger_status("unit", "/safe/status.json")
        assert status is not None
        assert status["ownership_enabled"] is True
        assert status["last_drive_id"] == "drive_previous"
        assert status["pending_bundle_count"] == 3
        assert "private_adapter_address" not in status

    async def test_remote_inventory_is_oldest_first_and_ignores_partial(self, monkeypatch):
        monkeypatch.setattr(
            obd_transfer.adb,
            "shell",
            lambda *_args, **_kwargs: _async_value(
                "10|0002-new.obd2.zip|10\n10|0001-old.obd2.zip|20\n10|half.obd2.zip.partial|1"
            ),
        )
        rows = await obd_transfer.inventory_remote_bundles("unit", "/safe/ready")
        assert [item.name for item in rows] == [
            "0001-old.obd2.zip",
            "0002-new.obd2.zip",
        ]

    async def test_remote_delete_rechecks_exact_hash_and_unlinks_in_one_shell_command(
        self, monkeypatch
    ):
        calls: list[str] = []

        async def shell(_address, command, **_kwargs):
            calls.append(command)
            return "OBD_DELETED"

        monkeypatch.setattr(obd_transfer.adb, "shell", shell)
        digest = "a" * 64

        await obd_transfer._delete_remote_if_hash(
            "unit", "/safe/ready", filename="drive_atomic.obd2.zip", bundle_sha256=digest
        )

        assert len(calls) == 1
        assert f"[ \"$1\" = '{digest}' ]" in calls[0]
        assert (
            "mv '/safe/ready/drive_atomic.obd2.zip' '/safe/ready/.drive_atomic.obd2.zip."
            in calls[0]
        )
        assert calls[0].index("mv '") < calls[0].index("sha256sum '") < calls[0].index("rm -f '")
        assert "[ ! -e '/safe/ready/drive_atomic.obd2.zip' ]" in calls[0]

        monkeypatch.setattr(
            obd_transfer.adb,
            "shell",
            lambda *_args, **_kwargs: _async_value(""),
        )
        with pytest.raises(obd_transfer.adb.AdbError, match="changed"):
            await obd_transfer._delete_remote_if_hash(
                "unit",
                "/safe/ready",
                filename="drive_atomic.obd2.zip",
                bundle_sha256=digest,
            )

    async def test_receipt_success_requires_a_separate_exact_final_readback(self, monkeypatch):
        calls: list[str] = []
        digest = "b" * 64
        body = _receipt_body("drive_readback", digest)

        async def shell(_address, command, **_kwargs):
            calls.append(command)
            return "" if len(calls) == 1 else body

        monkeypatch.setattr(obd_transfer.adb, "shell", shell)

        await obd_transfer.write_verification_receipt(
            "unit",
            "/safe/receipts",
            drive_id="drive_readback",
            bundle_sha256=digest,
        )

        assert len(calls) == 2
        assert "drive_readback.verified.json.partial" in calls[0]
        assert "mv -f" in calls[0]
        assert ".partial" not in calls[1]
        assert "cat '/safe/receipts/drive_readback.verified.json'" in calls[1]

    @pytest.mark.parametrize("failure", ["disconnect", "partial_only"])
    async def test_receipt_readback_failure_never_returns_success(self, monkeypatch, failure):
        calls: list[str] = []

        async def shell(_address, command, **_kwargs):
            calls.append(command)
            if len(calls) == 1:
                return ""
            assert ".partial" not in command
            if failure == "disconnect":
                raise obd_transfer.adb.AdbError("device offline during receipt readback")
            # A producer left only the partial path; the authoritative final-path command
            # cannot produce the expected body.
            return ""

        monkeypatch.setattr(obd_transfer.adb, "shell", shell)

        with pytest.raises(obd_transfer.adb.AdbError):
            await obd_transfer.write_verification_receipt(
                "unit",
                "/safe/receipts",
                drive_id="drive_readback_failure",
                bundle_sha256="c" * 64,
            )
        assert len(calls) == 2

    async def test_same_size_corrupt_local_copy_is_not_trusted(self, db_session, app_config):
        path = make_bundle(app_config.obd_verified_dir, "drive_local_hash")
        checked = validate_bundle(path, config=app_config)
        async with session_scope() as session:
            await store_validated_bundle(session, checked)
        body = bytearray(path.read_bytes())
        body[-1] ^= 0x01
        path.write_bytes(body)
        item = RemoteFile(name=path.name, size=len(body), mtime=1, directory="/safe/ready")
        assert await obd_transfer._already_verified(item, app_config) is None

    async def test_duplicate_replacement_after_receipt_is_retained(
        self, db_session, app_config, monkeypatch
    ):
        path = make_bundle(app_config.obd_verified_dir, "drive_duplicate_toctou")
        checked = validate_bundle(path, config=app_config)
        async with session_scope() as session:
            row = await store_validated_bundle(session, checked)

        monkeypatch.setattr(
            obd_transfer, "read_logger_status", lambda *_args, **_kwargs: _async_value(None)
        )
        monkeypatch.setattr(
            obd_transfer,
            "_remote_bundle_sha256",
            lambda *_args, **_kwargs: _async_value(checked.bundle_sha256),
        )
        monkeypatch.setattr(
            obd_transfer,
            "write_verification_receipt",
            lambda *_args, **_kwargs: _async_value(None),
        )

        async def changed(*_args, **_kwargs):
            raise obd_transfer.adb.AdbError("remote OBD bundle changed before verified deletion")

        monkeypatch.setattr(obd_transfer, "_delete_remote_if_hash", changed)
        monkeypatch.setattr(
            obd_transfer.transport,
            "receive",
            lambda *_args, **_kwargs: pytest.fail("duplicate replacement was recopied"),
        )
        item = RemoteFile(path.name, checked.size_bytes, 1, "/safe/ready")
        info = UnitInfo("192.168.1.2:5555", UnitState.DEVICE, "/card")

        result = await obd_transfer.sync_remote_bundles(info, remote=[item], config=app_config)

        assert result.duplicates == result.removed_from_unit == 0
        assert result.failed == 1
        assert "changed before verified deletion" in (result.error or "")
        async with session_scope() as session:
            current = await session.get(OBDBundle, row.id)
            assert current.remote_deleted_at is None

    async def test_fresh_replacement_after_receipt_is_retained(
        self, db_session, app_config, monkeypatch, tmp_path
    ):
        source = make_bundle(tmp_path, "drive_fresh_toctou")
        body = source.read_bytes()

        async def no_status(*_args, **_kwargs):
            return None

        def receive(_host, _port, staging, **_kwargs):
            (staging / source.name).write_bytes(body)
            return TransferResult(
                files=[source.name], bytes_received=len(body), complete=True, seconds=0.01
            )

        async def changed(*_args, **_kwargs):
            raise obd_transfer.adb.AdbError("remote OBD bundle changed before verified deletion")

        for name in ("read_logger_status",):
            monkeypatch.setattr(obd_transfer, name, no_status)
        for name in ("clear_listener", "launch_listener", "stop_listener"):
            monkeypatch.setattr(obd_transfer.adb, name, no_status)
        monkeypatch.setattr(obd_transfer.transport, "receive", receive)
        monkeypatch.setattr(
            obd_transfer,
            "write_verification_receipt",
            lambda *_args, **_kwargs: _async_value(None),
        )
        monkeypatch.setattr(obd_transfer, "_delete_remote_if_hash", changed)
        item = RemoteFile(source.name, len(body), 1, "/safe/ready")
        info = UnitInfo("192.168.1.2:5555", UnitState.DEVICE, "/card")

        result = await obd_transfer.sync_remote_bundles(info, remote=[item], config=app_config)

        assert result.copied == 1
        assert result.removed_from_unit == 0
        assert result.failed == 1
        assert "changed before verified deletion" in (result.error or "")
        assert (app_config.obd_verified_dir / source.name).read_bytes() == body

    async def test_fresh_registration_survives_stale_quarantine_cleanup_failure(
        self, db_session, app_config, monkeypatch, tmp_path
    ):
        source = make_bundle(tmp_path, "drive_cleanup_failure")
        body = source.read_bytes()

        async def no_status(*_args, **_kwargs):
            return None

        def receive(_host, _port, staging, **_kwargs):
            (staging / source.name).write_bytes(body)
            return TransferResult(
                files=[source.name], bytes_received=len(body), complete=True, seconds=0.01
            )

        for name in ("clear_listener", "launch_listener", "stop_listener"):
            monkeypatch.setattr(obd_transfer.adb, name, no_status)
        monkeypatch.setattr(obd_transfer, "read_logger_status", no_status)
        monkeypatch.setattr(obd_transfer.transport, "receive", receive)
        monkeypatch.setattr(
            obd_transfer,
            "_clear_stale_quarantine",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("cleanup blocked")),
        )
        monkeypatch.setattr(
            obd_transfer,
            "write_verification_receipt",
            lambda *_args, **_kwargs: _async_value(None),
        )
        monkeypatch.setattr(
            obd_transfer,
            "_delete_remote_if_hash",
            lambda *_args, **_kwargs: _async_value(None),
        )
        item = RemoteFile(source.name, len(body), 1, "/safe/ready")
        info = UnitInfo("192.168.1.2:5555", UnitState.DEVICE, "/card")

        result = await obd_transfer.sync_remote_bundles(info, remote=[item], config=app_config)

        assert result.copied == result.removed_from_unit == 1
        assert (app_config.obd_verified_dir / source.name).read_bytes() == body
        async with session_scope() as session:
            row = (
                await session.execute(select(OBDBundle).where(OBDBundle.filename == source.name))
            ).scalar_one()
            assert row.state == OBDBundleState.READY_TO_IMPORT.value

    async def test_cross_drive_sample_id_conflict_quarantines_one_and_continues(
        self, db_session, app_config, monkeypatch, tmp_path
    ):
        shared_sample_id = "globally_shared_sample"
        baseline_path = make_bundle(
            app_config.obd_verified_dir,
            "drive_sample_baseline",
            samples=[_sample("drive_sample_baseline", 0, sample_id=shared_sample_id)],
        )
        async with session_scope() as session:
            await store_validated_bundle(session, validate_bundle(baseline_path, config=app_config))

        poison_path = make_bundle(
            tmp_path,
            "drive_sample_poison",
            samples=[_sample("drive_sample_poison", 0, sample_id=shared_sample_id)],
        )
        following_path = make_bundle(
            tmp_path,
            "drive_sample_following",
            samples=[_sample("drive_sample_following", 0)],
        )
        bodies = {
            poison_path.name: poison_path.read_bytes(),
            following_path.name: following_path.read_bytes(),
        }
        following_receipt = _receipt_body(
            "drive_sample_following",
            hashlib.sha256(bodies[following_path.name]).hexdigest(),
        )
        deleted: list[str] = []

        async def no_status(*_args, **_kwargs):
            return None

        async def receipt_shell(_address, command, **_kwargs):
            return "" if ".partial" in command else following_receipt

        async def delete(_address, _source, names):
            deleted.extend(names)
            return len(names)

        def receive(_host, _port, staging, **_kwargs):
            for name, body in bodies.items():
                (staging / name).write_bytes(body)
            return TransferResult(
                files=list(bodies),
                bytes_received=sum(map(len, bodies.values())),
                complete=True,
                seconds=0.01,
            )

        monkeypatch.setattr(obd_transfer, "read_logger_status", no_status)
        monkeypatch.setattr(obd_transfer.adb, "clear_listener", no_status)
        monkeypatch.setattr(obd_transfer.adb, "launch_listener", no_status)
        monkeypatch.setattr(obd_transfer.adb, "stop_listener", no_status)
        monkeypatch.setattr(obd_transfer.adb, "shell", receipt_shell)
        monkeypatch.setattr(
            obd_transfer,
            "_delete_remote_if_hash",
            lambda address, source, *, filename, bundle_sha256: delete(address, source, [filename]),
        )
        monkeypatch.setattr(obd_transfer.transport, "receive", receive)
        items = [
            RemoteFile(name=name, size=len(body), mtime=index, directory="/safe/ready")
            for index, (name, body) in enumerate(bodies.items())
        ]
        info = UnitInfo(address="192.168.1.2:5555", state=UnitState.DEVICE, source="/card")

        result = await obd_transfer.sync_remote_bundles(info, remote=items, config=app_config)

        assert result.failed == 1
        assert result.copied == result.removed_from_unit == 1
        assert deleted == [following_path.name]
        assert not (app_config.obd_verified_dir / poison_path.name).exists()
        assert (app_config.obd_quarantine_dir / poison_path.name).is_file()
        assert (app_config.obd_verified_dir / following_path.name).is_file()
        async with session_scope() as session:
            poison = (
                await session.execute(
                    select(OBDBundle).where(OBDBundle.filename == poison_path.name)
                )
            ).scalar_one()
            following = (
                await session.execute(
                    select(OBDBundle).where(OBDBundle.filename == following_path.name)
                )
            ).scalar_one()
            assert poison.metadata_trusted is False
            assert poison.state == OBDBundleState.QUARANTINED.value
            assert following.metadata_trusted is True
            assert following.state == OBDBundleState.READY_TO_IMPORT.value

    async def test_fresh_unit_copy_repairs_quarantined_exact_identity(
        self, db_session, app_config, monkeypatch
    ):
        path = make_bundle(app_config.obd_verified_dir, "drive_exact_repair")
        good_bytes = path.read_bytes()
        checked = validate_bundle(path, config=app_config)
        async with session_scope() as session:
            row = await store_validated_bundle(session, checked)
            row_id = row.id
        quarantine = app_config.obd_quarantine_dir / path.name
        path.replace(quarantine)
        quarantine.write_bytes(b"corrupt retained bytes")
        async with session_scope() as session:
            row = await session.get(OBDBundle, row_id)
            row.state = OBDBundleState.QUARANTINED.value
            row.failure_kind = "integrity"

        deleted: list[str] = []
        events: list[str] = []
        expected_receipt = _receipt_body("drive_exact_repair", checked.bundle_sha256)

        async def no_status(*_args, **_kwargs):
            return None

        async def delete(_address, _source, names):
            events.append("delete")
            deleted.extend(names)
            return len(names)

        async def receipt_shell(_address, command, **_kwargs):
            if ".partial" not in command:
                assert "drive_exact_repair.verified.json" in command
                return expected_receipt
            events.append("receipt")
            assert "drive_exact_repair.verified.json.partial" in command
            assert expected_receipt in command
            assert "mv -f" in command
            assert "[ ! -L '/storage/Tfcard/Android/data/" in command
            assert "$(cat '/storage/Tfcard/Android/data/" in command
            assert "[ -f '/storage/Tfcard/Android/data/" in command
            return ""

        def receive(_host, _port, staging, **_kwargs):
            (staging / checked.filename).write_bytes(good_bytes)
            return TransferResult(
                files=[checked.filename],
                bytes_received=len(good_bytes),
                complete=True,
                seconds=0.01,
            )

        monkeypatch.setattr(obd_transfer, "read_logger_status", no_status)
        monkeypatch.setattr(obd_transfer.adb, "clear_listener", no_status)
        monkeypatch.setattr(obd_transfer.adb, "launch_listener", no_status)
        monkeypatch.setattr(obd_transfer.adb, "stop_listener", no_status)
        monkeypatch.setattr(obd_transfer.adb, "shell", receipt_shell)
        monkeypatch.setattr(
            obd_transfer,
            "_delete_remote_if_hash",
            lambda address, source, *, filename, bundle_sha256: delete(address, source, [filename]),
        )
        monkeypatch.setattr(obd_transfer.transport, "receive", receive)
        item = RemoteFile(
            name=checked.filename,
            size=len(good_bytes),
            mtime=1,
            directory="/safe/ready",
        )
        info = UnitInfo(address="192.168.1.2:5555", state=UnitState.DEVICE, source="/card")

        result = await obd_transfer.sync_remote_bundles(info, remote=[item], config=app_config)

        assert result.copied == result.removed_from_unit == 1
        assert events == ["receipt", "delete"]
        assert deleted == [checked.filename]
        assert (app_config.obd_verified_dir / checked.filename).read_bytes() == good_bytes
        assert not quarantine.exists()
        async with session_scope() as session:
            repaired = await session.get(OBDBundle, row_id)
            assert repaired.state == OBDBundleState.READY_TO_IMPORT.value
            assert repaired.failure_kind is None
            assert (
                await session.execute(
                    select(func.count(OBDSample.id))
                    .join(OBDDrive)
                    .where(OBDDrive.bundle_id == row_id)
                )
            ).scalar_one() == 2

    async def test_crash_after_verified_move_reconciles_before_remote_delete(
        self, db_session, app_config, monkeypatch
    ):
        path = make_bundle(app_config.obd_verified_dir, "drive_crash_repair")
        good_bytes = path.read_bytes()
        checked = validate_bundle(path, config=app_config)
        async with session_scope() as session:
            row = await store_validated_bundle(session, checked)
            row_id = row.id
        quarantine = app_config.obd_quarantine_dir / path.name
        path.replace(quarantine)
        quarantine.write_bytes(b"corrupt retained bytes")
        # Simulate a crash after the fresh unit copy reached verified/ but before _register.
        path.write_bytes(good_bytes)
        async with session_scope() as session:
            row = await session.get(OBDBundle, row_id)
            row.state = OBDBundleState.QUARANTINED.value
            row.failure_kind = "integrity"

        deleted: list[str] = []
        events: list[str] = []
        expected_receipt = _receipt_body("drive_crash_repair", checked.bundle_sha256)

        async def no_status(*_args, **_kwargs):
            return None

        async def delete(_address, _source, names):
            events.append("delete")
            deleted.extend(names)
            return len(names)

        async def receipt_shell(_address, command, **_kwargs):
            if ".partial" not in command:
                assert "drive_crash_repair.verified.json" in command
                return expected_receipt
            events.append("receipt")
            assert "drive_crash_repair.verified.json.partial" in command
            return ""

        monkeypatch.setattr(obd_transfer, "read_logger_status", no_status)
        monkeypatch.setattr(
            obd_transfer,
            "_remote_bundle_sha256",
            lambda *_args, **_kwargs: _async_value(checked.bundle_sha256),
        )
        monkeypatch.setattr(obd_transfer.adb, "shell", receipt_shell)
        monkeypatch.setattr(
            obd_transfer,
            "_delete_remote_if_hash",
            lambda address, source, *, filename, bundle_sha256: delete(address, source, [filename]),
        )
        monkeypatch.setattr(
            obd_transfer.transport,
            "receive",
            lambda *_args, **_kwargs: pytest.fail("verified crash recovery recopied bytes"),
        )
        item = RemoteFile(
            name=checked.filename,
            size=len(good_bytes),
            mtime=1,
            directory="/safe/ready",
        )
        info = UnitInfo(address="192.168.1.2:5555", state=UnitState.DEVICE, source="/card")

        result = await obd_transfer.sync_remote_bundles(info, remote=[item], config=app_config)

        assert result.duplicates == result.removed_from_unit == 1
        assert events == ["receipt", "delete"]
        assert deleted == [checked.filename]
        assert not quarantine.exists()
        async with session_scope() as session:
            repaired = await session.get(OBDBundle, row_id)
            assert repaired.state == OBDBundleState.READY_TO_IMPORT.value
            assert repaired.failure_kind is None

    async def test_failed_ha_delivery_still_publishes_missing_durable_receipt(
        self, db_session, app_config, monkeypatch
    ):
        path = make_bundle(app_config.obd_verified_dir, "drive_failed_before_receipt")
        checked = validate_bundle(path, config=app_config)
        async with session_scope() as session:
            row = await store_validated_bundle(session, checked)
            row_id = row.id
            # Simulate: registration committed, the process crashed before receipt, then
            # the independent HA worker reached a terminal authentication failure.
            row.state = OBDBundleState.FAILED.value
            row.failure_kind = "authentication"
            row.last_error = "Home Assistant rejected its token"

        events: list[str] = []
        expected_receipt = _receipt_body("drive_failed_before_receipt", checked.bundle_sha256)

        async def no_status(*_args, **_kwargs):
            return None

        async def receipt_shell(_address, command, **_kwargs):
            if ".partial" not in command:
                assert "drive_failed_before_receipt.verified.json" in command
                return expected_receipt
            events.append("receipt")
            assert "drive_failed_before_receipt.verified.json.partial" in command
            assert checked.bundle_sha256 in command
            return ""

        async def delete(_address, _source, names):
            events.append("delete")
            assert names == [checked.filename]
            return 1

        monkeypatch.setattr(obd_transfer, "read_logger_status", no_status)
        monkeypatch.setattr(
            obd_transfer,
            "_remote_bundle_sha256",
            lambda *_args, **_kwargs: _async_value(checked.bundle_sha256),
        )
        monkeypatch.setattr(obd_transfer.adb, "shell", receipt_shell)
        monkeypatch.setattr(
            obd_transfer,
            "_delete_remote_if_hash",
            lambda address, source, *, filename, bundle_sha256: delete(address, source, [filename]),
        )
        monkeypatch.setattr(
            obd_transfer.transport,
            "receive",
            lambda *_args, **_kwargs: pytest.fail("durable failed row recopied bundle bytes"),
        )
        item = RemoteFile(
            name=checked.filename,
            size=checked.size_bytes,
            mtime=1,
            directory="/safe/ready",
        )
        info = UnitInfo(address="192.168.1.2:5555", state=UnitState.DEVICE, source="/card")

        result = await obd_transfer.sync_remote_bundles(info, remote=[item], config=app_config)

        assert result.duplicates == result.removed_from_unit == 1
        assert events == ["receipt", "delete"]
        async with session_scope() as session:
            failed = await session.get(OBDBundle, row_id)
            assert failed.state == OBDBundleState.FAILED.value
            assert failed.failure_kind == "authentication"
            assert failed.remote_deleted_at is not None

    async def test_same_name_size_changed_remote_is_copied_without_overwriting_verified(
        self, db_session, app_config, monkeypatch
    ):
        path = make_bundle(app_config.obd_verified_dir, "drive_remote_hash_mismatch")
        durable_bytes = path.read_bytes()
        checked = validate_bundle(path, config=app_config)
        async with session_scope() as session:
            row = await store_validated_bundle(session, checked)
            row_id = row.id

        changed_bytes = bytearray(durable_bytes)
        changed_bytes[-1] ^= 0x01
        changed_body = bytes(changed_bytes)
        changed_hash = hashlib.sha256(changed_body).hexdigest()

        async def no_status(*_args, **_kwargs):
            return None

        async def remote_hash_shell(_address, command, **_kwargs):
            remote_path = f"{app_config.obd_remote_ready_dir}/{checked.filename}"
            assert f"sha256sum '{remote_path}'" in command
            assert f"[ ! -L '{remote_path}' ]" in command
            return f"{changed_hash}  {remote_path}\n"

        def receive(_host, _port, staging, **_kwargs):
            (staging / checked.filename).write_bytes(changed_body)
            return TransferResult(
                files=[checked.filename],
                bytes_received=len(changed_body),
                complete=True,
                seconds=0.01,
            )

        monkeypatch.setattr(obd_transfer, "read_logger_status", no_status)
        monkeypatch.setattr(obd_transfer.adb, "clear_listener", no_status)
        monkeypatch.setattr(obd_transfer.adb, "launch_listener", no_status)
        monkeypatch.setattr(obd_transfer.adb, "stop_listener", no_status)
        monkeypatch.setattr(obd_transfer.adb, "shell", remote_hash_shell)
        monkeypatch.setattr(obd_transfer.transport, "receive", receive)
        monkeypatch.setattr(
            obd_transfer,
            "write_verification_receipt",
            lambda *_args, **_kwargs: pytest.fail("mismatched bytes received a receipt"),
        )
        monkeypatch.setattr(
            obd_transfer.adb,
            "delete",
            lambda *_args, **_kwargs: pytest.fail("mismatched bytes were deleted"),
        )
        item = RemoteFile(
            name=checked.filename,
            size=len(changed_body),
            mtime=1,
            directory="/safe/ready",
        )
        info = UnitInfo(address="192.168.1.2:5555", state=UnitState.DEVICE, source="/card")

        result = await obd_transfer.sync_remote_bundles(info, remote=[item], config=app_config)

        assert result.failed == 1
        assert result.copied == result.removed_from_unit == 0
        assert path.read_bytes() == durable_bytes
        assert (app_config.obd_quarantine_dir / checked.filename).read_bytes() == changed_body
        async with session_scope() as session:
            durable = await session.get(OBDBundle, row_id)
            assert durable.bundle_hash == checked.bundle_sha256
            assert durable.metadata_trusted is True
            assert durable.state == OBDBundleState.READY_TO_IMPORT.value
            assert durable.remote_deleted_at is None

    @pytest.mark.parametrize(
        "failure_message",
        [
            "receipts root is a symlink",
            "receipt target is a directory",
            "verification receipt final readback content mismatch",
        ],
    )
    async def test_receipt_failure_retains_verified_duplicate_on_unit(
        self, db_session, app_config, monkeypatch, failure_message
    ):
        path = make_bundle(app_config.obd_verified_dir, "drive_receipt_retry")
        checked = validate_bundle(path, config=app_config)
        async with session_scope() as session:
            await store_validated_bundle(session, checked)
        deleted: list[str] = []
        receipt_calls = 0

        async def no_status(*_args, **_kwargs):
            return None

        async def receipt_failure(*_args, **_kwargs):
            nonlocal receipt_calls
            receipt_calls += 1
            if (
                failure_message == "verification receipt final readback content mismatch"
                and receipt_calls == 1
            ):
                # The mutating write/rename round trip completed; only the independent
                # final-path readback reveals that the receipt is not authoritative.
                return ""
            if failure_message == "verification receipt final readback content mismatch":
                return "partial-only content"
            raise obd_transfer.adb.AdbError(failure_message)

        async def delete(_address, _source, *, filename, bundle_sha256):
            deleted.append(filename)

        monkeypatch.setattr(obd_transfer, "read_logger_status", no_status)
        monkeypatch.setattr(
            obd_transfer,
            "_remote_bundle_sha256",
            lambda *_args, **_kwargs: _async_value(checked.bundle_sha256),
        )
        monkeypatch.setattr(obd_transfer.adb, "shell", receipt_failure)
        monkeypatch.setattr(obd_transfer, "_delete_remote_if_hash", delete)
        monkeypatch.setattr(
            obd_transfer.transport,
            "receive",
            lambda *_args, **_kwargs: pytest.fail("receipt retry recopied verified bytes"),
        )
        item = RemoteFile(
            name=checked.filename,
            size=checked.size_bytes,
            mtime=1,
            directory="/safe/ready",
        )
        info = UnitInfo(address="192.168.1.2:5555", state=UnitState.DEVICE, source="/card")

        result = await obd_transfer.sync_remote_bundles(info, remote=[item], config=app_config)

        assert result.failed == 1
        assert result.duplicates == result.removed_from_unit == 0
        assert failure_message in (result.error or "")
        assert deleted == []
        assert receipt_calls == (
            2 if failure_message == "verification receipt final readback content mismatch" else 1
        )
        assert obd_transfer.get_obd_transfer_status().snapshot()["waiting_on_unit"] >= 1

    async def test_receipt_failure_retains_freshly_registered_bundle_on_unit(
        self, db_session, app_config, monkeypatch, tmp_path
    ):
        source = make_bundle(tmp_path, "drive_fresh_receipt_retry")
        body = source.read_bytes()
        name = source.name
        deleted: list[str] = []

        async def no_status(*_args, **_kwargs):
            return None

        async def receipt_failure(*_args, **_kwargs):
            raise obd_transfer.adb.AdbError("receipt readback mismatch")

        async def delete(_address, _source, names):
            deleted.extend(names)
            return len(names)

        def receive(_host, _port, staging, **_kwargs):
            (staging / name).write_bytes(body)
            return TransferResult(
                files=[name], bytes_received=len(body), complete=True, seconds=0.01
            )

        monkeypatch.setattr(obd_transfer, "read_logger_status", no_status)
        monkeypatch.setattr(obd_transfer.adb, "clear_listener", no_status)
        monkeypatch.setattr(obd_transfer.adb, "launch_listener", no_status)
        monkeypatch.setattr(obd_transfer.adb, "stop_listener", no_status)
        monkeypatch.setattr(obd_transfer.adb, "shell", receipt_failure)
        monkeypatch.setattr(obd_transfer.adb, "delete", delete)
        monkeypatch.setattr(obd_transfer.transport, "receive", receive)
        item = RemoteFile(name=name, size=len(body), mtime=1, directory="/safe/ready")
        info = UnitInfo(address="192.168.1.2:5555", state=UnitState.DEVICE, source="/card")

        result = await obd_transfer.sync_remote_bundles(info, remote=[item], config=app_config)

        assert result.copied == 1
        assert result.failed == 1
        assert result.removed_from_unit == 0
        assert deleted == []
        assert (app_config.obd_verified_dir / name).read_bytes() == body
        async with session_scope() as session:
            row = (
                await session.execute(select(OBDBundle).where(OBDBundle.filename == name))
            ).scalar_one()
            assert row.state == OBDBundleState.READY_TO_IMPORT.value
            assert row.remote_deleted_at is None

    async def test_receipt_rejects_unsafe_remote_directory(self, monkeypatch):
        monkeypatch.setattr(
            obd_transfer.adb,
            "shell",
            lambda *_args, **_kwargs: pytest.fail("unsafe receipt reached adb shell"),
        )
        with pytest.raises(BundleError, match="safe absolute Android path"):
            await obd_transfer.write_verification_receipt(
                "unit",
                "/safe/receipts'; rm -rf /",
                drive_id="safe_drive",
                bundle_sha256="a" * 64,
            )

    async def test_corrupt_copy_is_not_deleted_from_the_unit(
        self, db_session, app_config, monkeypatch
    ):
        name = "drive_bad.obd2.zip"
        body = b"not a zip"
        item = RemoteFile(name=name, size=len(body), mtime=1, directory="/safe/ready")
        deleted: list[str] = []

        async def receive_status(*_args, **_kwargs):
            return None

        async def launch(*_args, **_kwargs):
            return None

        async def delete(_address, _source, names):
            deleted.extend(names)
            return len(names)

        def receive(_host, _port, staging, **_kwargs):
            (staging / name).write_bytes(body)
            return TransferResult(
                files=[name], bytes_received=len(body), complete=True, seconds=0.01
            )

        monkeypatch.setattr(obd_transfer, "read_logger_status", receive_status)
        monkeypatch.setattr(obd_transfer.adb, "clear_listener", receive_status)
        monkeypatch.setattr(obd_transfer.adb, "launch_listener", launch)
        monkeypatch.setattr(obd_transfer.adb, "stop_listener", receive_status)
        monkeypatch.setattr(obd_transfer.adb, "delete", delete)
        monkeypatch.setattr(obd_transfer.transport, "receive", receive)
        app_config.obd_remote_ready_dir = "/safe/ready"
        info = UnitInfo(address="192.168.1.2:5555", state=UnitState.DEVICE, source="/card")
        first = await obd_transfer.sync_remote_bundles(info, remote=[item], config=app_config)
        second = await obd_transfer.sync_remote_bundles(info, remote=[item], config=app_config)
        assert first.failed == second.failed == 1
        assert deleted == []
        assert [path.name for path in app_config.obd_quarantine_dir.iterdir()] == [name]
        async with session_scope() as session:
            rejected = (
                await session.execute(select(OBDBundle).where(OBDBundle.filename == name))
            ).scalar_one()
            assert rejected.metadata_trusted is False
            assert rejected.state == OBDBundleState.QUARANTINED.value

    async def test_more_than_one_window_of_known_rejections_cannot_starve_later_valid_bundle(
        self, db_session, app_config, monkeypatch, tmp_path
    ):
        remote_hashes: dict[str, str] = {}
        rejected_items: list[RemoteFile] = []
        async with session_scope() as session:
            for index in range(obd_transfer.MAX_BUNDLES_PER_WINDOW + 1):
                name = f"reject_{index:03d}.obd2.zip"
                body = f"permanently invalid {index}".encode()
                digest = hashlib.sha256(body).hexdigest()
                (app_config.obd_quarantine_dir / name).write_bytes(body)
                await store_rejected_bundle(
                    session,
                    filename=name,
                    bundle_hash=digest,
                    size_bytes=len(body),
                    error="known invalid archive",
                    quarantined=True,
                )
                remote_hashes[name] = digest
                rejected_items.append(RemoteFile(name, len(body), index, "/safe/ready"))

        valid = make_bundle(tmp_path, "zz_later_valid")
        valid_body = valid.read_bytes()

        async def no_status(*_args, **_kwargs):
            return None

        async def remote_hash(_address, _source, item):
            return remote_hashes[item.name]

        def receive(_host, _port, staging, **kwargs):
            assert kwargs["expected"] == {valid.name: len(valid_body)}
            (staging / valid.name).write_bytes(valid_body)
            return TransferResult(
                files=[valid.name],
                bytes_received=len(valid_body),
                complete=True,
                seconds=0.01,
            )

        monkeypatch.setattr(obd_transfer, "read_logger_status", no_status)
        monkeypatch.setattr(obd_transfer, "_remote_bundle_sha256", remote_hash)
        for name in ("clear_listener", "launch_listener", "stop_listener"):
            monkeypatch.setattr(obd_transfer.adb, name, no_status)
        monkeypatch.setattr(obd_transfer.transport, "receive", receive)
        monkeypatch.setattr(
            obd_transfer,
            "write_verification_receipt",
            lambda *_args, **_kwargs: _async_value(None),
        )
        monkeypatch.setattr(
            obd_transfer,
            "_delete_remote_if_hash",
            lambda *_args, **_kwargs: _async_value(None),
        )
        info = UnitInfo("192.168.1.2:5555", UnitState.DEVICE, "/card")
        valid_item = RemoteFile(valid.name, len(valid_body), len(rejected_items), "/safe/ready")

        result = await obd_transfer.sync_remote_bundles(
            info, remote=[*rejected_items, valid_item], config=app_config
        )

        assert result.copied == result.removed_from_unit == 1
        assert result.failed == 0
        assert (app_config.obd_verified_dir / valid.name).is_file()

    async def test_obd_transfer_passes_exact_inventory_and_bounds_to_tar_receiver(
        self, db_session, app_config, monkeypatch
    ):
        name = "drive_hostile_tar.obd2.zip"
        item = RemoteFile(name=name, size=1234, mtime=1, directory="/safe/ready")
        deleted: list[str] = []

        async def no_status(*_args, **_kwargs):
            return None

        async def delete(_address, _source, names):
            deleted.extend(names)
            return len(names)

        def receive(_host, _port, _staging, **kwargs):
            assert kwargs["expected"] == {name: 1234}
            assert kwargs["max_member_bytes"] == app_config.obd_max_bundle_bytes
            assert kwargs["max_total_bytes"] == app_config.obd_max_bundle_bytes
            return TransferResult(
                complete=False,
                error="the archive contains an unrequested member: attacker.bin",
            )

        monkeypatch.setattr(obd_transfer, "read_logger_status", no_status)
        monkeypatch.setattr(obd_transfer.adb, "clear_listener", no_status)
        monkeypatch.setattr(obd_transfer.adb, "launch_listener", no_status)
        monkeypatch.setattr(obd_transfer.adb, "stop_listener", no_status)
        monkeypatch.setattr(obd_transfer.adb, "delete", delete)
        monkeypatch.setattr(obd_transfer.transport, "receive", receive)
        app_config.obd_remote_ready_dir = "/safe/ready"
        info = UnitInfo(address="192.168.1.2:5555", state=UnitState.DEVICE, source="/card")

        result = await obd_transfer.sync_remote_bundles(info, remote=[item], config=app_config)

        assert "unrequested member" in (result.error or "")
        assert result.copied == result.removed_from_unit == 0
        assert deleted == []


async def _async_value(value):
    return value


class TestDriveSeriesApi:
    """The dashboard's drive list and full-resolution chart data.

    HA keeps hourly statistics forever and nothing finer; the server keeps every sample.
    These endpoints are what makes that retained resolution actually reachable.
    """

    async def test_drive_list_and_series_return_every_sample(self, db_session, app_config, client):
        samples = [_sample("drive_series_api", sequence) for sequence in range(5)]
        diagnostics = [
            {
                "diagnostic_id": "diag_series_confirmed",
                "drive_id": "drive_series_api",
                "timestamp_utc": BASE.isoformat(),
                "kind": "confirmed_dtcs",
                "payload": {"codes": ["P0420"]},
            }
        ]
        path = make_bundle(
            app_config.obd_verified_dir,
            "drive_series_api",
            samples=samples,
            diagnostics=diagnostics,
        )
        checked = validate_bundle(path, config=app_config)
        async with session_scope() as session:
            await store_validated_bundle(session, checked)

        listing = await client.get("/api/obd/drives")
        assert listing.status_code == 200
        body = listing.json()
        assert body["total"] == 1
        item = body["items"][0]
        assert item["drive_id"] == "drive_series_api"
        assert item["vehicle_id"] == "tiida_c11"
        assert item["sample_count"] == 5
        assert item["import_state"] == OBDBundleState.READY_TO_IMPORT.value

        series = await client.get("/api/obd/drives/drive_series_api/series")
        assert series.status_code == 200
        payload = series.json()
        assert payload["drive"]["drive_id"] == "drive_series_api"
        assert [item["sequence"] for item in payload["samples"]] == [0, 1, 2, 3, 4]
        assert [item["engine_rpm"] for item in payload["samples"]] == [900, 1000, 1100, 1200, 1300]
        assert [item["vehicle_speed_kmh"] for item in payload["samples"]] == [20, 21, 22, 23, 24]
        assert payload["samples"][0]["t"] == BASE.isoformat()
        assert payload["samples"][1]["t"] == (BASE + timedelta(seconds=5)).isoformat()
        assert payload["samples"][0]["adapter_voltage_v"] == 14.1
        assert payload["diagnostics"] == [
            {
                "observed_at": BASE.isoformat(),
                "kind": "confirmed_dtcs",
                "payload": {"codes": ["P0420"]},
            }
        ]

    async def test_unknown_drive_series_is_a_404(self, db_session, client):
        response = await client.get("/api/obd/drives/drive_missing/series")
        assert response.status_code == 404
