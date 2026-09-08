"""Server-side OBD bundle validation, durable history and transfer safety."""

from __future__ import annotations

import gzip
import hashlib
import json
import re
import threading
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.api.routes import obd_import as obd_api
from app.db.models import OBDBundle, OBDBundleState, OBDDrive, OBDSample
from app.db.session import session_scope
from app.ingest import obd_bundle, obd_transfer
from app.ingest.models import RemoteFile, UnitInfo, UnitState
from app.ingest.obd_bundle import (
    UNITS_V1,
    BundleError,
    store_rejected_bundle,
    store_validated_bundle,
    validate_bundle,
)
from app.ingest.obd_storage import reconcile_orphan_bundles, recover_interrupted_validations
from app.ingest.transport import TransferResult

BASE = datetime(2026, 8, 29, 1, 0, tzinfo=UTC)


def _legacy_pipeline_metrics() -> dict[str, int]:
    return {
        "commands_requested": 0,
        "commands_completed": 0,
        "command_timeouts": 0,
        "notifications_received": 0,
        "notification_fragments_received": 0,
        "frames_assembled": 0,
        "checksum_failures": 0,
        "parse_failures": 0,
        "samples_created": 0,
        "samples_queued": 0,
        "samples_persisted": 0,
        "samples_dropped": 0,
        "database_write_failures": 0,
        "ble_disconnects": 0,
        "reconnect_attempts": 0,
        "radio_shutdowns": 0,
        "queue_depth": 0,
        "maximum_queue_depth": 0,
    }


def _current_pipeline_metrics() -> dict[str, int | float]:
    return {
        "commands_requested": 3,
        "commands_blocked": 1,
        "commands_sent": 3,
        "commands_completed": 2,
        "command_timeouts": 1,
        "adapter_local_commands": 2,
        "vehicle_bus_commands": 1,
        "ble_connection_attempts": 1,
        "adapter_targets_resolved": 1,
        "gatt_connections_established": 1,
        "notification_subscriptions_enabled": 1,
        "ble_connection_successes": 1,
        "ble_connection_failures": 0,
        "voltage_reads_successful": 1,
        "voltage_reads_failed": 0,
        "invalid_voltage_responses": 0,
        "notifications_received": 2,
        "notification_fragments_received": 2,
        "frames_assembled": 2,
        "checksum_failures": 0,
        "parse_failures": 0,
        "samples_created": 1,
        "samples_queued": 1,
        "samples_persisted": 1,
        "samples_dropped": 0,
        "database_write_failures": 0,
        "ble_disconnects": 1,
        "reconnect_attempts": 0,
        "radio_shutdowns": 0,
        "queue_depth": 0,
        "maximum_queue_depth": 1,
        "observation_window_ms": 1_000,
        "polling_duty_cycle_percent": 48.0,
        "connection_time_samples": 1,
        "median_connection_time_ms": 340.0,
        "maximum_connection_time_ms": 340,
        "total_connection_time_ms": 340,
        "command_response_time_samples": 2,
        "median_command_response_time_ms": 100.0,
        "maximum_command_response_time_ms": 120,
        "total_command_response_time_ms": 200,
        "voltage_response_time_samples": 1,
        "median_voltage_response_time_ms": 120.0,
        "maximum_voltage_response_time_ms": 120,
        "total_voltage_response_time_ms": 120,
        "connected_time_samples": 1,
        "median_connected_time_ms": 480.0,
        "maximum_connected_time_ms": 480,
        "total_connected_time_ms": 480,
    }


def _pipeline_diagnostic(
    drive_id: str,
    payload: dict[str, int | float],
) -> dict:
    return {
        "diagnostic_id": f"diag_{drive_id}",
        "drive_id": drive_id,
        "timestamp_utc": BASE.isoformat(),
        "kind": "pipeline_metrics",
        "payload": payload,
    }


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
    def test_hardened_last_response_may_follow_effective_interrupted_end(
        self, tmp_path, app_config
    ):
        finish = BASE + timedelta(seconds=5)
        noticed = BASE + timedelta(seconds=10)
        finalised = BASE + timedelta(seconds=11)
        lifecycle = {
            "last_sample_at_utc": finish.isoformat(),
            "termination_noticed_at_utc": noticed.isoformat(),
            "finalised_at_utc": finalised.isoformat(),
            "completion_status": "interrupted",
            "interruption_reason": "connection_lost",
        }
        checked = validate_bundle(
            make_bundle(
                tmp_path,
                "drive_response_after_sample",
                summary_patch={**lifecycle, "clean_end": False},
                manifest_patch={
                    **lifecycle,
                    "last_successful_obd_response_at_utc": (
                        BASE + timedelta(seconds=8)
                    ).isoformat(),
                    "poll_plan_version": 2,
                    "stop_reason": "connection_lost",
                    "clean_end": False,
                    "created_at_utc": (BASE + timedelta(seconds=12)).isoformat(),
                },
            ),
            config=app_config,
        )
        assert (
            checked.manifest["last_successful_obd_response_at_utc"]
            == (BASE + timedelta(seconds=8)).isoformat()
        )

    def test_missing_summary_member_is_derived_without_hiding_raw_history(
        self, tmp_path, app_config
    ):
        path = make_bundle(tmp_path, "drive_missing_summary")
        replacement = path.with_suffix(".replacement")
        with zipfile.ZipFile(path, "r") as source:
            retained = {
                name: source.read(name) for name in source.namelist() if name != "summary.json"
            }
        with zipfile.ZipFile(replacement, "w", compression=zipfile.ZIP_STORED) as target:
            for name, body in retained.items():
                target.writestr(name, body)
        replacement.replace(path)

        checked = validate_bundle(path, config=app_config)
        assert checked.summary_source == "derived"
        assert checked.summary["drive_id"] == "drive_missing_summary"
        assert checked.summary["sample_count"] == 2

    def test_partial_bundle_name_is_excluded(self, tmp_path, app_config):
        path = make_bundle(tmp_path)
        partial = path.with_name(path.name + ".partial")
        path.rename(partial)
        with pytest.raises(BundleError, match="filename"):
            validate_bundle(partial, config=app_config)

    def test_corrupt_summary_hash_falls_back_to_validated_samples(self, tmp_path, app_config):
        checked = validate_bundle(
            make_bundle(tmp_path, wrong_payload_hash=True),
            config=app_config,
        )
        assert checked.summary_source == "derived"
        assert checked.summary["sample_count"] == 2
        assert checked.warnings == (
            "summary.json was missing or invalid; the server derived summary fields "
            "from validated raw samples",
        )

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

    def test_vehicle_id_and_summary_constraints_fail_during_validation(self, tmp_path, app_config):
        with pytest.raises(BundleError, match="vehicle"):
            validate_bundle(
                make_bundle(tmp_path, "drive_vehicle", vehicle_id="Nissan Tiida"),
                config=app_config,
            )

    def test_rejects_out_of_range_numeric_values(self, tmp_path, app_config):
        sample = _sample("drive_bad_coolant", 0)
        sample["coolant_temperature"] = 251
        with pytest.raises(BundleError, match="coolant_temperature"):
            validate_bundle(
                make_bundle(tmp_path, "drive_bad_coolant", samples=[sample]),
                config=app_config,
            )
        checked = validate_bundle(
            make_bundle(
                tmp_path,
                "drive_bad_distance",
                summary_patch={"distance_km": 300_001},
            ),
            config=app_config,
        )
        assert checked.summary_source == "derived"
        assert checked.summary["distance_km"] != 300_001

    def test_diagnostic_payload_is_validated_before_storage(self, tmp_path, app_config):
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
        checked = validate_bundle(
            make_bundle(
                tmp_path,
                "drive_expected",
                summary_patch={"expected_sample_count": 1},
            ),
            config=app_config,
        )
        assert checked.summary_source == "derived"
        assert checked.summary["expected_sample_count"] >= checked.summary["sample_count"]

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

    def test_hardened_pipeline_metrics_may_follow_effective_interrupted_finish(
        self, tmp_path, app_config
    ):
        drive_id = "drive_pipeline_metrics"
        finished = BASE + timedelta(seconds=5)
        noticed = BASE + timedelta(seconds=6)
        finalised = BASE + timedelta(seconds=7)
        diagnostics = [
            {
                "diagnostic_id": "diag_pipeline_metrics",
                "drive_id": drive_id,
                "timestamp_utc": noticed.isoformat(),
                "kind": "pipeline_metrics",
                "payload": _legacy_pipeline_metrics(),
            }
        ]
        manifest_lifecycle = {
            "poll_plan_version": 2,
            "completion_status": "interrupted",
            "clean_end": False,
            "stop_reason": "connection_lost",
            "last_sample_at_utc": finished.isoformat(),
            "last_successful_obd_response_at_utc": finished.isoformat(),
            "termination_noticed_at_utc": noticed.isoformat(),
            "finalised_at_utc": finalised.isoformat(),
            "interruption_reason": "connection_lost",
            "created_at_utc": finalised.isoformat(),
        }
        summary_lifecycle = {
            "clean_end": False,
            "completion_status": "interrupted",
            "last_sample_at_utc": finished.isoformat(),
            "termination_noticed_at_utc": noticed.isoformat(),
            "finalised_at_utc": finalised.isoformat(),
            "interruption_reason": "connection_lost",
        }

        checked = validate_bundle(
            make_bundle(
                tmp_path,
                drive_id,
                diagnostics=diagnostics,
                manifest_patch=manifest_lifecycle,
                summary_patch=summary_lifecycle,
            ),
            config=app_config,
        )

        assert checked.diagnostics_document["events"][0]["kind"] == "pipeline_metrics"

    def test_current_android_pipeline_metrics_are_accepted(self, tmp_path, app_config):
        drive_id = "drive_current_metrics"
        payload = _current_pipeline_metrics()
        assert frozenset(payload) == (
            obd_bundle.PIPELINE_METRIC_FIELDS_CURRENT_REQUIRED
            | obd_bundle.PIPELINE_METRIC_FIELDS_CURRENT_OPTIONAL
        )

        checked = validate_bundle(
            make_bundle(
                tmp_path,
                drive_id,
                diagnostics=[_pipeline_diagnostic(drive_id, payload)],
            ),
            config=app_config,
        )

        assert checked.diagnostics_document["events"][0]["payload"] == payload

    def test_current_android_empty_timing_metrics_are_accepted(self, tmp_path, app_config):
        drive_id = "drive_empty_metrics"
        payload = _current_pipeline_metrics()
        for name in obd_bundle.PIPELINE_METRIC_TIMING_NAMES_CURRENT:
            payload[f"{name}_samples"] = 0
            payload.pop(f"median_{name}_ms")
            payload[f"maximum_{name}_ms"] = 0
            payload[f"total_{name}_ms"] = 0
        payload["polling_duty_cycle_percent"] = 0.0

        checked = validate_bundle(
            make_bundle(
                tmp_path,
                drive_id,
                diagnostics=[_pipeline_diagnostic(drive_id, payload)],
            ),
            config=app_config,
        )

        assert (
            "median_connected_time_ms" not in checked.diagnostics_document["events"][0]["payload"]
        )

    @pytest.mark.parametrize("field", ["polling_duty_cycle_percent", "median_connected_time_ms"])
    def test_pipeline_metrics_reject_non_finite_floats(
        self,
        tmp_path,
        app_config,
        field,
    ):
        drive_id = f"drive_nan_{field}"
        payload = _current_pipeline_metrics()
        payload[field] = float("nan")

        with pytest.raises(BundleError, match="non-finite JSON number"):
            validate_bundle(
                make_bundle(
                    tmp_path,
                    drive_id,
                    diagnostics=[_pipeline_diagnostic(drive_id, payload)],
                ),
                config=app_config,
            )

    @pytest.mark.parametrize(
        ("field", "value", "message"),
        [
            ("commands_requested", True, "must be an integer"),
            ("observation_window_ms", 0, "must be an integer"),
            ("polling_duty_cycle_percent", 101.0, "is above 100"),
            ("connection_time_samples", 257, "must be an integer"),
        ],
    )
    def test_pipeline_metrics_reject_invalid_types_and_ranges(
        self,
        tmp_path,
        app_config,
        field,
        value,
        message,
    ):
        drive_id = f"drive_bad_range_{field}"
        payload = _current_pipeline_metrics()
        payload[field] = value

        with pytest.raises(BundleError, match=message):
            validate_bundle(
                make_bundle(
                    tmp_path,
                    drive_id,
                    diagnostics=[_pipeline_diagnostic(drive_id, payload)],
                ),
                config=app_config,
            )

    @pytest.mark.parametrize("mutation", ["unknown", "missing"])
    def test_pipeline_metrics_reject_field_drift(
        self,
        tmp_path,
        app_config,
        mutation,
    ):
        drive_id = f"drive_field_{mutation}"
        payload = _current_pipeline_metrics()
        if mutation == "unknown":
            payload["future_unreviewed_metric"] = 1
        else:
            payload.pop("commands_blocked")

        with pytest.raises(BundleError, match="fields do not match a supported shape"):
            validate_bundle(
                make_bundle(
                    tmp_path,
                    drive_id,
                    diagnostics=[_pipeline_diagnostic(drive_id, payload)],
                ),
                config=app_config,
            )

    @pytest.mark.parametrize("mutation", ["missing_nonempty", "present_empty"])
    def test_pipeline_metrics_require_medians_exactly_when_samples_exist(
        self,
        tmp_path,
        app_config,
        mutation,
    ):
        drive_id = f"drive_median_{mutation}"
        payload = _current_pipeline_metrics()
        if mutation == "missing_nonempty":
            payload.pop("median_connection_time_ms")
            message = "connection_time median is missing"
        else:
            payload["connection_time_samples"] = 0
            payload["maximum_connection_time_ms"] = 0
            payload["total_connection_time_ms"] = 0
            message = "connection_time median requires timing samples"

        with pytest.raises(BundleError, match=message):
            validate_bundle(
                make_bundle(
                    tmp_path,
                    drive_id,
                    diagnostics=[_pipeline_diagnostic(drive_id, payload)],
                ),
                config=app_config,
            )

    @pytest.mark.parametrize("mutation", ["empty_nonzero", "median_over_max", "max_over_total"])
    def test_pipeline_metrics_reject_inconsistent_timings(
        self,
        tmp_path,
        app_config,
        mutation,
    ):
        drive_id = f"drive_timing_{mutation}"
        payload = _current_pipeline_metrics()
        if mutation == "empty_nonzero":
            payload["connection_time_samples"] = 0
            payload.pop("median_connection_time_ms")
            payload["maximum_connection_time_ms"] = 1
            payload["total_connection_time_ms"] = 1
            message = "empty timing is inconsistent"
        elif mutation == "median_over_max":
            payload["median_connection_time_ms"] = 341.0
            message = "connection_time timing is inconsistent"
        else:
            payload["maximum_connection_time_ms"] = 341
            message = "connection_time timing is inconsistent"

        with pytest.raises(BundleError, match=message):
            validate_bundle(
                make_bundle(
                    tmp_path,
                    drive_id,
                    diagnostics=[_pipeline_diagnostic(drive_id, payload)],
                ),
                config=app_config,
            )

    def test_pipeline_metrics_reject_inconsistent_duty_cycle(self, tmp_path, app_config):
        drive_id = "drive_bad_duty"
        payload = _current_pipeline_metrics()
        payload["polling_duty_cycle_percent"] = 47.0

        with pytest.raises(BundleError, match="polling duty cycle is inconsistent"):
            validate_bundle(
                make_bundle(
                    tmp_path,
                    drive_id,
                    diagnostics=[_pipeline_diagnostic(drive_id, payload)],
                ),
                config=app_config,
            )

    def test_pipeline_metrics_accept_clamped_duty_cycle(self, tmp_path, app_config):
        drive_id = "drive_clamped_duty"
        payload = _current_pipeline_metrics()
        # Android measures the observation window and connected sessions with different
        # monotonic clocks. A session spanning deep sleep can therefore exceed the window,
        # and the producer deliberately clamps the emitted duty cycle to 100 percent.
        payload["observation_window_ms"] = 400
        payload["polling_duty_cycle_percent"] = 100.0

        checked = validate_bundle(
            make_bundle(
                tmp_path,
                drive_id,
                diagnostics=[_pipeline_diagnostic(drive_id, payload)],
            ),
            config=app_config,
        )

        assert checked.diagnostics_document["events"][0]["payload"] == payload

    def test_pipeline_metric_contract_matches_android_source(self):
        source_path = (
            Path(__file__).parents[1]
            / "android"
            / "obd-logger"
            / "app"
            / "src"
            / "main"
            / "java"
            / "com"
            / "dashcamstats"
            / "obdlogger"
            / "PipelineMetrics.kt"
        )
        source = source_path.read_text(encoding="utf-8")
        counters_match = re.search(
            r"private val counters = linkedMapOf\((.*?)\n    \)",
            source,
            re.DOTALL,
        )
        timings_match = re.search(
            r"val timings = linkedMapOf\((.*?)\n        \)",
            source,
            re.DOTALL,
        )
        json_match = re.search(
            r"fun toJson\(\): JSONObject = JSONObject\(\)\.also \{ body ->(.*?)\n    \}",
            source,
            re.DOTALL,
        )
        assert counters_match is not None
        assert timings_match is not None
        assert json_match is not None

        counter_fields = frozenset(re.findall(r'"([a-z0-9_]+)"\s+to\s+0L', counters_match.group(1)))
        timing_names = frozenset(
            re.findall(
                r'"([a-z0-9_]+)"\s+to\s+\w+Timing\.snapshot\(\)',
                timings_match.group(1),
            )
        )
        literal_json_fields = frozenset(
            re.findall(r'body\.put\("([a-z0-9_]+)"', json_match.group(1))
        )
        timing_required_fields = frozenset(
            field_name
            for name in timing_names
            for field_name in (
                f"{name}_samples",
                f"maximum_{name}_ms",
                f"total_{name}_ms",
            )
        )
        timing_optional_fields = frozenset(f"median_{name}_ms" for name in timing_names)

        assert counter_fields == obd_bundle.PIPELINE_METRIC_COUNTER_FIELDS_CURRENT
        assert timing_names == obd_bundle.PIPELINE_METRIC_TIMING_NAMES_CURRENT
        assert literal_json_fields == {
            "queue_depth",
            "maximum_queue_depth",
            "observation_window_ms",
            "polling_duty_cycle_percent",
        }
        assert counter_fields | literal_json_fields | timing_required_fields == (
            obd_bundle.PIPELINE_METRIC_FIELDS_CURRENT_REQUIRED
        )
        assert timing_optional_fields == obd_bundle.PIPELINE_METRIC_FIELDS_CURRENT_OPTIONAL
        for template in (
            'body.put("${name}_samples", timing.sampleCount)',
            'body.put("median_${name}_ms", it)',
            'body.put("maximum_${name}_ms", timing.maximumMillis)',
            'body.put("total_${name}_ms", timing.totalMillis)',
        ):
            assert template in json_match.group(1)


class TestTransactionalHistory:
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

    async def test_restart_recovers_each_interrupted_validation_state_without_losing_history(
        self, db_session, app_config
    ):
        rows = {}
        for drive_id in (
            "recover_verified",
            "recover_quarantined",
            "recover_corrupt",
            "recover_missing",
        ):
            checked = validate_bundle(
                make_bundle(app_config.obd_verified_dir, drive_id), config=app_config
            )
            async with session_scope() as session:
                row = await store_validated_bundle(session, checked)
                row.state = OBDBundleState.VALIDATING.value
                rows[drive_id] = row.id

        quarantined_path = app_config.obd_verified_dir / "recover_quarantined.obd2.zip"
        quarantined_path.replace(app_config.obd_quarantine_dir / quarantined_path.name)
        (app_config.obd_verified_dir / "recover_corrupt.obd2.zip").write_bytes(b"changed")
        (app_config.obd_verified_dir / "recover_missing.obd2.zip").unlink()

        assert await recover_interrupted_validations(config=app_config) == 4

        async with session_scope() as session:
            recovered = {
                row.drive_id: row
                for row in (
                    await session.execute(select(OBDBundle).where(OBDBundle.id.in_(rows.values())))
                )
                .scalars()
                .all()
            }
            drive_count = int(
                (await session.execute(select(func.count(OBDDrive.id)))).scalar() or 0
            )
            sample_count = int(
                (await session.execute(select(func.count(OBDSample.id)))).scalar() or 0
            )
        assert recovered["recover_verified"].state == OBDBundleState.STORED.value
        assert recovered["recover_quarantined"].state == OBDBundleState.QUARANTINED.value
        assert recovered["recover_corrupt"].state == OBDBundleState.QUARANTINED.value
        assert recovered["recover_corrupt"].failure_kind == "integrity"
        assert recovered["recover_missing"].state == OBDBundleState.FAILED.value
        assert recovered["recover_missing"].failure_kind == "local_path"
        assert drive_count == 4
        assert sample_count == 8

    async def test_storage_rebuild_registers_orphans_and_quarantines_invalid_files(
        self, db_session, app_config
    ):
        make_bundle(app_config.obd_verified_dir, "orphan_valid")
        invalid = app_config.obd_verified_dir / "orphan_invalid.obd2.zip"
        invalid.write_bytes(b"not a zip")

        result = await reconcile_orphan_bundles(config=app_config)

        assert result == {"registered": 1, "duplicates": 0, "quarantined": 1}
        assert not invalid.exists()
        assert (app_config.obd_quarantine_dir / invalid.name).is_file()
        async with session_scope() as session:
            stored = (
                await session.execute(select(OBDBundle).where(OBDBundle.drive_id == "orphan_valid"))
            ).scalar_one()
            samples = int((await session.execute(select(func.count(OBDSample.id)))).scalar() or 0)
        assert stored.state == OBDBundleState.STORED.value
        assert samples == 2


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

    def test_cached_logger_voltage_cannot_remain_fresh_indefinitely(self, monkeypatch):
        cached = obd_transfer.OBDTransferStatus()
        cached.set_logger(
            {
                "schema_version": 4,
                "state": "parked",
                "adapter_reachable": True,
                "battery_voltage": 12.7,
                "battery_voltage_source": "dashcam_elm_atrv",
                "battery_voltage_sample_at_utc": "2026-08-30T00:58:00+00:00",
                "battery_voltage_fresh": True,
                "battery_voltage_quality": "valid",
            }
        )
        monkeypatch.setattr(
            obd_transfer,
            "utcnow",
            lambda: datetime(2026, 8, 30, 1, 0, tzinfo=UTC),
        )

        logger = cached.snapshot()["logger"]

        assert logger["battery_voltage_fresh"] is False
        assert logger["adapter_reachable"] is False
        assert logger["battery_voltage_quality"] == "stale"

    async def test_logger_status_accepts_canonical_redacted_fixture(self, monkeypatch):
        fixture = {
            "schema_version": 4,
            "logger_version": "1.0.0",
            "app_version_name": "0.2.0",
            "app_version_code": 3,
            "poll_plan_version": 2,
            "build_git_sha": "0123456789ab",
            "state": "parked",
            "ownership_enabled": True,
            "adapter_state": "connected",
            "vehicle_state": "engine_running",
            "adapter_reachable": True,
            "adapter_connected": False,
            "ecu_connected": False,
            "engine_running": False,
            "battery_voltage": 12.74,
            "battery_voltage_source": "dashcam_elm_atrv",
            "battery_voltage_sample_at_utc": "2026-08-30T00:59:59+00:00",
            "battery_voltage_fresh": True,
            "battery_voltage_raw_response": "12.74 V",
            "battery_voltage_quality": "valid",
            "ble_owner": "dashcam_voltage_only",
            "head_unit_state": "awake",
            "voltage_only_mode": True,
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
        monkeypatch.setattr(
            obd_transfer,
            "utcnow",
            lambda: datetime(2026, 8, 30, 1, 0, tzinfo=UTC),
        )
        status = await obd_transfer.read_logger_status("unit", "/safe/status.json")
        assert status is not None
        assert status["ownership_enabled"] is True
        assert status["last_drive_id"] == "drive_previous"
        assert status["pending_bundle_count"] == 3
        assert status["app_version_name"] == "0.2.0"
        assert status["app_version_code"] == 3
        assert status["poll_plan_version"] == 2
        assert status["build_git_sha"] == "0123456789ab"
        assert status["battery_voltage"] == 12.74
        assert status["battery_voltage_raw_response"] == "12.74 V"
        assert status["battery_voltage_source"] == "dashcam_elm_atrv"
        assert status["battery_voltage_fresh"] is True
        assert status["ble_owner"] == "dashcam_voltage_only"
        assert status["voltage_only_mode"] is True
        assert "private_adapter_address" not in status

    async def test_logger_status_recomputes_stale_voltage_from_sample_time(self, monkeypatch):
        fixture = {
            "schema_version": 4,
            "state": "parked",
            "adapter_reachable": True,
            "battery_voltage": 12.7,
            "battery_voltage_source": "dashcam_elm_atrv",
            "battery_voltage_sample_at_utc": "2026-08-30T00:58:00Z",
            "battery_voltage_fresh": True,
            "battery_voltage_quality": "valid",
        }
        monkeypatch.setattr(
            obd_transfer.adb,
            "shell",
            lambda *_args, **_kwargs: _async_value(json.dumps(fixture)),
        )
        monkeypatch.setattr(
            obd_transfer,
            "utcnow",
            lambda: datetime(2026, 8, 30, 1, 0, tzinfo=UTC),
        )

        status = await obd_transfer.read_logger_status("unit", "/safe/status.json")

        assert status is not None
        assert status["battery_voltage"] == 12.7
        assert status["battery_voltage_fresh"] is False
        assert status["adapter_reachable"] is False
        assert status["battery_voltage_quality"] == "stale"

    async def test_logger_status_rejects_mistyped_v4_evidence(self, monkeypatch):
        fixture = {
            "schema_version": 4,
            "state": "parked",
            "adapter_reachable": "false",
            "adapter_connected": 0,
            "ecu_connected": None,
            "engine_running": "no",
            "battery_voltage_fresh": 1,
            "voltage_only_mode": "true",
            "battery_voltage_sample_at_utc": "not-a-timestamp",
            "vehicle_state": "flying",
        }
        monkeypatch.setattr(
            obd_transfer.adb,
            "shell",
            lambda *_args, **_kwargs: _async_value(json.dumps(fixture)),
        )

        status = await obd_transfer.read_logger_status("unit", "/safe/status.json")

        assert status == {
            "schema_version": 4,
            "state": "parked",
            "battery_voltage_fresh": False,
            "adapter_reachable": False,
        }

    async def test_logger_status_rejects_non_string_state_and_nonfinite_counts(self, monkeypatch):
        fixture = {
            "schema_version": 4,
            "state": 7,
            "adapter_state": False,
            "pending_bundle_count": float("nan"),
            "sample_count": -1,
        }
        monkeypatch.setattr(
            obd_transfer.adb,
            "shell",
            lambda *_args, **_kwargs: _async_value(json.dumps(fixture)),
        )

        status = await obd_transfer.read_logger_status("unit", "/safe/status.json")

        assert status == {
            "schema_version": 4,
            "battery_voltage_fresh": False,
            "adapter_reachable": False,
        }

    async def test_logger_status_rejects_invalid_voltage_and_ownership_metadata(self, monkeypatch):
        fixture = {
            "schema_version": 4,
            "state": "parked",
            "battery_voltage": 99.0,
            "battery_voltage_source": "hostile_source",
            "battery_voltage_raw_response": "99 V",
            "battery_voltage_quality": "perfect",
            "ble_owner": "everyone",
            "head_unit_state": "definitely asleep",
            "battery_voltage_fresh": False,
        }
        monkeypatch.setattr(
            obd_transfer.adb,
            "shell",
            lambda *_args, **_kwargs: _async_value(json.dumps(fixture)),
        )

        status = await obd_transfer.read_logger_status("unit", "/safe/status.json")

        assert status == {
            "schema_version": 4,
            "state": "parked",
            "battery_voltage_fresh": False,
            "adapter_reachable": False,
        }

    async def test_logger_status_accepts_bounded_sleep_window_evidence(self, monkeypatch):
        fixture = {
            "schema_version": 5,
            "state": "parked",
            "wifi_connected": True,
            "ingestion_sleep_hold": False,
            "ingestion_sleep_hold_known": True,
            "sleep_window_policy": "managed_idle",
            "sleep_window_target_s": 300,
            "sleep_window_observed_s": 900,
            "sleep_window_verified": False,
            "sleep_window_error": "sleep countdown readback did not match the requested value",
        }
        monkeypatch.setattr(
            obd_transfer.adb,
            "shell",
            lambda *_args, **_kwargs: _async_value(json.dumps(fixture)),
        )

        status = await obd_transfer.read_logger_status("unit", "/safe/status.json")

        assert status is not None
        assert status["wifi_connected"] is True
        assert status["ingestion_sleep_hold"] is False
        assert status["ingestion_sleep_hold_known"] is True
        assert status["sleep_window_policy"] == "managed_idle"
        assert status["sleep_window_target_s"] == 300
        assert status["sleep_window_observed_s"] == 900
        assert status["sleep_window_verified"] is False
        assert (
            status["sleep_window_error"]
            == "sleep countdown readback did not match the requested value"
        )

    async def test_logger_status_rejects_invalid_sleep_window_evidence(self, monkeypatch):
        fixture = {
            "schema_version": 5,
            "state": "parked",
            "wifi_connected": "true",
            "ingestion_sleep_hold": 1,
            "ingestion_sleep_hold_known": 0,
            "sleep_window_policy": ["managed_idle"],
            "sleep_window_target_s": True,
            "sleep_window_observed_s": 3_601,
            "sleep_window_verified": "false",
            "sleep_window_error": {"message": "sleep countdown readback was unavailable"},
        }
        monkeypatch.setattr(
            obd_transfer.adb,
            "shell",
            lambda *_args, **_kwargs: _async_value(json.dumps(fixture)),
        )

        status = await obd_transfer.read_logger_status("unit", "/safe/status.json")

        assert status == {
            "schema_version": 5,
            "state": "parked",
            "battery_voltage_fresh": False,
            "adapter_reachable": False,
        }

    @pytest.mark.parametrize("value", [0, 299, 301, 899, 901, 3_601])
    async def test_logger_status_rejects_unrecognized_sleep_window_target(
        self,
        monkeypatch,
        value,
    ):
        fixture = {
            "schema_version": 5,
            "state": "parked",
            "sleep_window_target_s": value,
        }
        monkeypatch.setattr(
            obd_transfer.adb,
            "shell",
            lambda *_args, **_kwargs: _async_value(json.dumps(fixture)),
        )

        status = await obd_transfer.read_logger_status("unit", "/safe/status.json")

        assert status is not None
        assert "sleep_window_target_s" not in status

    async def test_logger_status_accepts_safe_sleep_window_startup_state(self, monkeypatch):
        fixture = {
            "schema_version": 5,
            "state": "starting",
            "wifi_connected": True,
            "ingestion_sleep_hold": False,
            "ingestion_sleep_hold_known": False,
            "sleep_window_policy": "awaiting_ingestion_state",
            "sleep_window_target_s": None,
            "sleep_window_observed_s": 900,
            "sleep_window_verified": False,
            "sleep_window_error": None,
        }
        monkeypatch.setattr(
            obd_transfer.adb,
            "shell",
            lambda *_args, **_kwargs: _async_value(json.dumps(fixture)),
        )

        status = await obd_transfer.read_logger_status("unit", "/safe/status.json")

        assert status is not None
        assert status["ingestion_sleep_hold_known"] is False
        assert status["sleep_window_policy"] == "awaiting_ingestion_state"
        assert status["sleep_window_target_s"] is None
        assert status["sleep_window_observed_s"] == 900
        assert status["sleep_window_verified"] is False
        assert status["sleep_window_error"] is None

    async def test_logger_build_identity_rejects_unbounded_or_mistyped_values(self, monkeypatch):
        fixture = {
            "schema_version": 3,
            "app_version_name": "secret value with spaces",
            "app_version_code": True,
            "poll_plan_version": -1,
            "build_git_sha": "not-a-git-revision",
            "state": "parked",
            "ownership_enabled": True,
        }
        monkeypatch.setattr(
            obd_transfer.adb,
            "shell",
            lambda *_args, **_kwargs: _async_value(json.dumps(fixture)),
        )

        status = await obd_transfer.read_logger_status("unit", "/safe/status.json")

        assert status == {
            "schema_version": 3,
            "state": "parked",
            "ownership_enabled": True,
        }

    async def test_obd_status_api_exposes_logger_build_identity(self, client, monkeypatch):
        identity = {
            "schema_version": 3,
            "app_version_name": "0.2.0",
            "app_version_code": 3,
            "poll_plan_version": 2,
            "build_git_sha": "0123456789ab",
        }

        class TransferStatus:
            @staticmethod
            def snapshot():
                return {"logger": identity}

        monkeypatch.setattr(obd_api, "get_obd_transfer_status", TransferStatus)

        response = await client.get("/api/obd/status")

        assert response.status_code == 200
        body = response.json()
        assert body["logger"] == identity
        assert body["stored_drive_count"] == 0
        assert body["last_stored_drive"] is None
        assert body["last_storage_error"] is None
        assert not {
            "home_assistant_authentication",
            "waiting_for_home_assistant",
            "current_import",
            "last_successful_home_assistant_sync",
            "worker_running",
        }.intersection(body)

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
        assert not result.complete
        assert result.missing == 1
        assert result.failed == 1
        assert deleted == []


async def _async_value(value):
    return value


class TestDriveSeriesApi:
    """The dashboard's drive list and full-resolution chart data.

    The server keeps every sample. These endpoints make that retained resolution reachable.
    """

    @pytest.mark.parametrize(
        ("stop_reason", "expected", "vehicle_data"),
        [
            ("ingestion_requested", "saved_for_backup", True),
            ("connection_lost", "shutdown_detected", True),
            ("ingestion_requested", "no_vehicle_data", False),
        ],
    )
    async def test_shutdown_projection_repairs_history_without_changing_evidence(
        self,
        db_session,
        app_config,
        client,
        stop_reason,
        expected,
        vehicle_data,
    ):
        from app.ingest.obd_reconciliation import reconcile_all_drives

        drive_id = f"drive_ending_{stop_reason}"
        samples = [_sample(drive_id, i, telemetry=False) for i in range(5)]
        if vehicle_data:
            samples[0].update(engine_rpm=737.5, vehicle_speed=0, adapter_voltage=13.8)
            samples[1].update(engine_rpm=137.5, vehicle_speed=0, adapter_voltage=12.9)
        for sample in samples[2:]:
            sample.update(adapter_voltage=12.8)
        path = make_bundle(
            app_config.obd_verified_dir,
            drive_id,
            samples=samples,
            summary_patch={"clean_end": False},
            manifest_patch={
                "stop_reason": stop_reason,
                "clean_end": False,
                "completion_status": "interrupted",
            },
        )
        original_bytes = path.read_bytes()
        checked = validate_bundle(path, config=app_config)
        async with session_scope() as session:
            await store_validated_bundle(session, checked)
            drive = (await session.execute(select(OBDDrive))).scalars().one()
            # Simulate the existing projection before upgrading the server.
            drive.lifecycle_status = "interrupted"
        assert (await reconcile_all_drives())["errors"] == 0
        response = await client.get(f"/api/obd/drives/{drive_id}/series")
        assert response.status_code == 200
        projected = response.json()["drive"]
        assert projected["lifecycle_status"] == expected
        assert projected["producer_completion_status"] == "interrupted"
        assert projected["stop_reason"] == stop_reason
        assert projected["clean_end"] is False
        assert projected["sample_count"] == 5
        assert projected["finished_at"] == (BASE + timedelta(seconds=20)).isoformat()
        second = await client.post(f"/api/obd/drives/{drive_id}/reprocess")
        assert second.json()["result"]["changed"] is False
        assert path.read_bytes() == original_bytes
        async with session_scope() as session:
            raw = (
                (await session.execute(select(OBDSample).order_by(OBDSample.sequence)))
                .scalars()
                .all()
            )
            assert [row.raw_json for row in raw] == samples

    async def test_legacy_unclean_complete_is_projected_interrupted_idempotently(
        self, db_session, app_config, client
    ):
        drive_id = "drive_legacy_interrupted"
        samples = [_sample(drive_id, 0), _sample(drive_id, 1)]
        observed = BASE + timedelta(seconds=20)
        path = make_bundle(
            app_config.obd_verified_dir,
            drive_id,
            samples=samples,
            summary_patch={
                "finish_time_utc": observed.isoformat(),
                "duration_s": 20.0,
                "clean_end": False,
            },
            manifest_patch={
                "finish_time_utc": observed.isoformat(),
                "created_at_utc": observed.isoformat(),
                "stop_reason": "connection_lost",
                "completion_status": "complete",
                "clean_end": False,
            },
        )
        checked = validate_bundle(path, config=app_config)
        async with session_scope() as session:
            await store_validated_bundle(session, checked)

        first = await client.post(f"/api/obd/drives/{drive_id}/reprocess")
        second = await client.post(f"/api/obd/drives/{drive_id}/reprocess")
        assert first.status_code == second.status_code == 200
        projected = second.json()["drive"]
        assert projected["lifecycle_status"] == "interrupted"
        assert projected["completion_status"] == "interrupted"
        assert projected["producer_completion_status"] == "complete"
        assert projected["interruption_reason"] == "connection_lost"
        assert projected["finished_at"] == (BASE + timedelta(seconds=5)).isoformat()
        assert projected["finalization_observed_at"] == observed.isoformat()
        async with session_scope() as session:
            assert (
                int(
                    (
                        await session.execute(
                            select(func.count(OBDSample.id))
                            .join(OBDDrive)
                            .where(OBDDrive.drive_id == drive_id)
                        )
                    ).scalar()
                    or 0
                )
                == 2
            )

    async def test_hardened_phase_plan_does_not_count_intentional_spacing_as_missing(
        self, db_session, app_config, client
    ):
        drive_id = "drive_phased_plan"
        samples = []
        for sequence in range(7):
            sample = _sample(drive_id, sequence, telemetry=False)
            sample.update(engine_rpm=900.0, adapter_voltage=14.0)
            if sequence % 3 == 1:
                sample["coolant_temperature"] = 80.0 + sequence
            samples.append(sample)
        finish = BASE + timedelta(seconds=30)
        lifecycle = {
            "last_sample_at_utc": finish.isoformat(),
            "termination_noticed_at_utc": finish.isoformat(),
            "finalised_at_utc": finish.isoformat(),
            "completion_status": "complete",
            "interruption_reason": None,
        }
        diagnostics = [
            {
                "diagnostic_id": "diag_phased_support",
                "drive_id": drive_id,
                "timestamp_utc": BASE.isoformat(),
                "kind": "mode01_support",
                "payload": {"supported_pids": [0x05, 0x0C]},
            }
        ]
        checked = validate_bundle(
            make_bundle(
                app_config.obd_verified_dir,
                drive_id,
                samples=samples,
                diagnostics=diagnostics,
                summary_patch=lifecycle,
                manifest_patch={
                    **lifecycle,
                    "last_successful_obd_response_at_utc": finish.isoformat(),
                    "poll_plan_version": 2,
                },
            ),
            config=app_config,
        )
        async with session_scope() as session:
            await store_validated_bundle(session, checked)

        payload = (await client.get(f"/api/obd/drives/{drive_id}/series")).json()
        quality = payload["drive"]["gap_analysis"]
        coolant = next(item for item in quality["signals"] if item["name"] == "coolant_temperature")
        assert quality["poll_plan_version"] == 2
        assert coolant["expected_observation_count"] == 2
        assert coolant["received_observation_count"] == 2
        assert coolant["missing_observation_count"] == 0
        assert coolant["coverage_percentage"] == 100.0

    async def test_wholly_missing_sequence_cycles_count_for_every_scheduled_phase(
        self, db_session, app_config, client
    ):
        drive_id = "drive_missing_cycles"
        samples = [
            {
                **_sample(drive_id, 0, telemetry=False),
                "engine_rpm": 900.0,
                "adapter_voltage": 14.0,
            },
            {
                **_sample(
                    drive_id,
                    12,
                    at=BASE + timedelta(seconds=5),
                    telemetry=False,
                ),
                "engine_rpm": 2100.0,
                "adapter_voltage": 13.8,
            },
        ]
        finish = BASE + timedelta(seconds=5)
        lifecycle = {
            "last_sample_at_utc": finish.isoformat(),
            "termination_noticed_at_utc": finish.isoformat(),
            "finalised_at_utc": finish.isoformat(),
            "completion_status": "complete",
            "interruption_reason": None,
        }
        diagnostics = [
            {
                "diagnostic_id": "diag_missing_cycle_support",
                "drive_id": drive_id,
                "timestamp_utc": BASE.isoformat(),
                "kind": "mode01_support",
                "payload": {"supported_pids": [0x05, 0x0C]},
            }
        ]
        checked = validate_bundle(
            make_bundle(
                app_config.obd_verified_dir,
                drive_id,
                samples=samples,
                diagnostics=diagnostics,
                summary_patch=lifecycle,
                manifest_patch={
                    **lifecycle,
                    "last_successful_obd_response_at_utc": finish.isoformat(),
                    "poll_plan_version": 2,
                },
            ),
            config=app_config,
        )
        async with session_scope() as session:
            await store_validated_bundle(session, checked)
            raw_before = list(
                (
                    await session.execute(
                        select(OBDSample.raw_json)
                        .join(OBDDrive)
                        .where(OBDDrive.drive_id == drive_id)
                        .order_by(OBDSample.sequence)
                    )
                ).scalars()
            )

        response = await client.get(f"/api/obd/drives/{drive_id}/series")
        assert response.status_code == 200
        drive = response.json()["drive"]
        quality = drive["gap_analysis"]
        transport = quality["transport"]
        rpm = next(item for item in quality["signals"] if item["name"] == "engine_rpm")
        coolant = next(item for item in quality["signals"] if item["name"] == "coolant_temperature")

        assert quality["expected_cycle_count"] == 13
        assert quality["expected_cycle_count_capped"] is False
        assert transport["expected_observation_count"] == 13
        assert transport["received_observation_count"] == 2
        assert transport["missing_observation_count"] == 11
        assert transport["sequence_gap_count"] == 11
        assert rpm["expected_observation_count"] == 13
        assert rpm["received_observation_count"] == 2
        assert rpm["missing_observation_count"] == 11
        # Coolant is phase one in poll-plan v2: absent cycles 1, 4, 7 and 10 are
        # scheduled misses even though no sample row exists at any of those sequences.
        assert coolant["expected_observation_count"] == 4
        assert coolant["received_observation_count"] == 0
        assert coolant["missing_observation_count"] == 4
        assert coolant["missing_run_count"] == 1
        assert coolant["longest_missing_run"] == 4
        assert drive["expected_sample_count"] == 13
        assert drive["received_sample_percentage"] == pytest.approx(200 / 13)
        assert drive["average_rpm"] == pytest.approx(1500.0)
        assert drive["distance_km"] is None
        assert drive["estimated_fuel_used_l"] is None
        assert drive["summary_source"] == "derived"

        first_reprocess = await client.post(f"/api/obd/drives/{drive_id}/reprocess")
        second_reprocess = await client.post(f"/api/obd/drives/{drive_id}/reprocess")
        assert first_reprocess.status_code == second_reprocess.status_code == 200
        assert first_reprocess.json()["result"]["changed"] is False
        assert second_reprocess.json()["result"]["changed"] is False
        assert (
            first_reprocess.json()["drive"]["summary_generated_at"]
            == second_reprocess.json()["drive"]["summary_generated_at"]
        )

        # A damaged projection is materially repaired once; immutable sample evidence is
        # untouched and the next reconciliation is again a no-op.
        async with session_scope() as session:
            row = (
                await session.execute(select(OBDDrive).where(OBDDrive.drive_id == drive_id))
            ).scalar_one()
            row.average_rpm = -1.0
            row.summary_generated_at = BASE - timedelta(days=1)
        repaired = await client.post(f"/api/obd/drives/{drive_id}/reprocess")
        stable = await client.post(f"/api/obd/drives/{drive_id}/reprocess")
        assert repaired.json()["result"]["changed"] is True
        assert repaired.json()["drive"]["average_rpm"] == pytest.approx(1500.0)
        assert stable.json()["result"]["changed"] is False
        assert (
            repaired.json()["drive"]["summary_generated_at"]
            == stable.json()["drive"]["summary_generated_at"]
        )
        async with session_scope() as session:
            raw_after = list(
                (
                    await session.execute(
                        select(OBDSample.raw_json)
                        .join(OBDDrive)
                        .where(OBDDrive.drive_id == drive_id)
                        .order_by(OBDSample.sequence)
                    )
                ).scalars()
            )
        assert raw_after == raw_before

    async def test_affected_style_timing_rebuilds_every_canonical_rollup_from_raw_samples(
        self, db_session, app_config, client
    ):
        drive_id = "drive_affected_timing"
        started = BASE
        first = BASE + timedelta(seconds=12.193799)
        last = BASE + timedelta(seconds=1612.033636)
        noticed = last + timedelta(seconds=1.070158)
        sample_span_s = (last - first).total_seconds()
        samples: list[dict] = []
        for sequence in range(245):
            captured = (
                last
                if sequence == 244
                else first + timedelta(seconds=sample_span_s * sequence / 244)
            )
            sample = _sample(drive_id, sequence, at=captured, telemetry=False)
            sample.update(
                engine_rpm=1000.0 + sequence,
                vehicle_speed=36.0,
                estimated_fuel_rate=2.0,
                adapter_voltage=14.0,
            )
            if sequence % 3 == 0:
                sample["coolant_temperature"] = 80.0
            samples.append(sample)
        diagnostics = [
            {
                "diagnostic_id": "diag_affected_support",
                "drive_id": drive_id,
                "timestamp_utc": started.isoformat(),
                "kind": "mode01_support",
                "payload": {"supported_pids": [0x05, 0x0C, 0x0D]},
            },
            {
                "diagnostic_id": "diag_affected_disconnect",
                "drive_id": drive_id,
                "timestamp_utc": noticed.isoformat(),
                "kind": "connection_failure",
                "payload": {"category": "ble_or_elm", "message": "write rejected"},
            },
        ]
        checked = validate_bundle(
            make_bundle(
                app_config.obd_verified_dir,
                drive_id,
                samples=samples,
                diagnostics=diagnostics,
                summary_patch={
                    "start_time_utc": started.isoformat(),
                    "finish_time_utc": noticed.isoformat(),
                    "duration_s": (noticed - started).total_seconds(),
                    "clean_end": False,
                },
                manifest_patch={
                    "start_time_utc": started.isoformat(),
                    "finish_time_utc": noticed.isoformat(),
                    "created_at_utc": noticed.isoformat(),
                    "stop_reason": "connection_lost",
                    "completion_status": "complete",
                    "clean_end": False,
                },
            ),
            config=app_config,
        )
        async with session_scope() as session:
            await store_validated_bundle(session, checked)

        response = await client.get(f"/api/obd/drives/{drive_id}/series")
        assert response.status_code == 200
        drive = response.json()["drive"]
        quality = drive["gap_analysis"]
        transport = quality["transport"]
        rpm = next(item for item in quality["signals"] if item["name"] == "engine_rpm")
        coolant = next(item for item in quality["signals"] if item["name"] == "coolant_temperature")
        expected = int((last - started).total_seconds() // 5) + 1

        assert expected == 323
        assert drive["lifecycle_status"] == "interrupted"
        assert drive["finished_at"] == last.isoformat()
        assert drive["first_sample_at"] == first.isoformat()
        assert drive["last_sample_at"] == last.isoformat()
        assert drive["finalization_observed_at"] == noticed.isoformat()
        assert drive["duration_s"] == pytest.approx((last - started).total_seconds())
        assert drive["sample_count"] == 245
        assert drive["expected_sample_count"] == expected
        assert drive["received_sample_percentage"] == pytest.approx(24500 / expected)
        assert transport["expected_observation_count"] == expected
        assert transport["coverage_percentage"] == pytest.approx(
            drive["received_sample_percentage"]
        )
        assert rpm["expected_observation_count"] == expected
        assert rpm["received_observation_count"] == 245
        assert coolant["expected_observation_count"] == 108
        assert coolant["received_observation_count"] == 82
        assert drive["average_speed_kmh"] == pytest.approx(36.0)
        assert drive["maximum_speed_kmh"] == pytest.approx(36.0)
        assert drive["average_rpm"] == pytest.approx(1122.0)
        assert drive["maximum_rpm"] == pytest.approx(1244.0)
        assert drive["distance_km"] == pytest.approx(36.0 * sample_span_s / 3600)
        assert drive["estimated_fuel_used_l"] == pytest.approx(2.0 * sample_span_s / 3600)
        assert drive["idle_duration_s"] == pytest.approx(0.0)
        assert drive["summary_source"] == "derived"
        assert drive["missing_data_duration_s"] == pytest.approx(transport["total_gap_duration_s"])

        async with session_scope() as session:
            stored = (
                await session.execute(select(OBDDrive).where(OBDDrive.drive_id == drive_id))
            ).scalar_one()
            assert stored.summary_json["finish_time_utc"] == last.isoformat().replace("+00:00", "Z")
            assert stored.summary_json["duration_s"] == pytest.approx(stored.duration_s)
            assert stored.summary_json["distance_km"] == pytest.approx(stored.distance_km)
            assert stored.summary_json["average_speed_kmh"] == pytest.approx(
                stored.average_speed_kmh
            )
            assert stored.summary_json["average_rpm"] == pytest.approx(stored.average_rpm)
            assert stored.summary_json["estimated_fuel_used_l"] == pytest.approx(
                stored.estimated_fuel_used_l
            )
            assert stored.summary_json["missing_data_duration_s"] == pytest.approx(
                stored.missing_data_duration_s
            )
            assert stored.summary_json["expected_sample_count"] == stored.expected_sample_count
            assert stored.summary_json["received_sample_percentage"] == pytest.approx(
                stored.received_sample_percentage
            )
            assert stored.summary_json["sample_count"] == stored.sample_count

    async def test_verified_bundle_download_checks_identity(self, db_session, app_config, client):
        path = make_bundle(app_config.obd_verified_dir, "drive_download")
        expected = path.read_bytes()
        checked = validate_bundle(path, config=app_config)
        async with session_scope() as session:
            await store_validated_bundle(session, checked)

        response = await client.get("/api/obd/drives/drive_download/bundle")
        assert response.status_code == 200
        assert response.content == expected
        assert response.headers["cache-control"] == "private, no-store"
        assert response.headers["x-content-type-options"] == "nosniff"

    async def test_missing_summary_remains_visible_in_drive_api(
        self, db_session, app_config, client
    ):
        path = make_bundle(app_config.obd_verified_dir, "drive_api_missing_summary")
        replacement = path.with_suffix(".replacement")
        with zipfile.ZipFile(path, "r") as source:
            retained = {
                name: source.read(name) for name in source.namelist() if name != "summary.json"
            }
        with zipfile.ZipFile(replacement, "w", compression=zipfile.ZIP_STORED) as target:
            for name, body in retained.items():
                target.writestr(name, body)
        replacement.replace(path)
        checked = validate_bundle(path, config=app_config)
        async with session_scope() as session:
            await store_validated_bundle(session, checked)

        response = await client.get("/api/obd/drives/drive_api_missing_summary/series")
        assert response.status_code == 200
        drive = response.json()["drive"]
        assert drive["summary_source"] == "derived"
        assert drive["sample_count"] == 2
        assert drive["validation_warnings"]

    async def test_unknown_drive_series_is_a_404(self, db_session, client):
        response = await client.get("/api/obd/drives/drive_missing/series")
        assert response.status_code == 404

    async def test_summary_totals_are_consistent_with_the_drive_list(
        self, db_session, app_config, client
    ):
        for drive_id in ("drive_summary_a", "drive_summary_b"):
            path = make_bundle(app_config.obd_verified_dir, drive_id)
            checked = validate_bundle(path, config=app_config)
            async with session_scope() as session:
                await store_validated_bundle(session, checked)

        listing = (await client.get("/api/obd/drives")).json()
        summary_response = await client.get("/api/obd/drives/summary")
        assert summary_response.status_code == 200
        summary = summary_response.json()

        assert summary["drive_count"] == listing["total"] == 2
        assert summary["total_sample_count"] == sum(
            item["sample_count"] for item in listing["items"]
        )
        assert summary["total_distance_km"] == pytest.approx(
            sum(item["distance_km"] or 0 for item in listing["items"])
        )
        assert summary["total_duration_s"] == pytest.approx(
            sum(item["duration_s"] or 0 for item in listing["items"])
        )
        assert summary["first_drive_at"] == min(item["started_at"] for item in listing["items"])
        assert summary["last_drive_at"] == max(item["finished_at"] for item in listing["items"])

    async def test_journey_and_drive_link_by_time_overlap(self, db_session, app_config, client):
        from app.db.models import Journey

        path = make_bundle(app_config.obd_verified_dir, "drive_journey_link")
        checked = validate_bundle(path, config=app_config)
        async with session_scope() as session:
            await store_validated_bundle(session, checked)
            # Overlapping the drive (BASE .. BASE+5s), plus a decoy from an hour earlier.
            overlapping = Journey(
                started_at=BASE - timedelta(seconds=30),
                ended_at=BASE + timedelta(seconds=60),
                duration_s=90.0,
                title="the school run",
            )
            decoy = Journey(
                started_at=BASE - timedelta(hours=1),
                ended_at=BASE - timedelta(minutes=50),
                duration_s=600.0,
            )
            session.add_all([overlapping, decoy])
            await session.flush()
            journey_id = overlapping.id
            decoy_id = decoy.id

        matched = await client.get(f"/api/obd/drives/for-journey/{journey_id}")
        assert matched.status_code == 200
        assert matched.json()["drive"]["drive_id"] == "drive_journey_link"
        assert matched.json()["overlap_s"] == pytest.approx(5.0)

        unmatched = await client.get(f"/api/obd/drives/for-journey/{decoy_id}")
        assert unmatched.status_code == 200
        assert unmatched.json() == {"drive": None, "overlap_s": None}

        missing = await client.get("/api/obd/drives/for-journey/999999")
        assert missing.status_code == 404

        series = (await client.get("/api/obd/drives/drive_journey_link/series")).json()
        assert series["journey"]["id"] == journey_id
        assert series["journey"]["title"] == "the school run"
        assert series["journey"]["overlap_s"] == pytest.approx(5.0)

    async def test_summary_of_an_empty_library_is_all_zeroes(self, db_session, client):
        response = await client.get("/api/obd/drives/summary")
        assert response.status_code == 200
        summary = response.json()
        assert summary["drive_count"] == 0
        assert summary["total_distance_km"] == 0.0
        assert summary["average_fuel_consumption_l_100km"] is None
        assert summary["first_drive_at"] is None


class TestPollPlanV4Bundles:
    """Logger 0.2.8 exports plan v4: PID 0x01's lamp and DTC count ride on each sample."""

    def _lifecycle(self):
        finish = BASE + timedelta(seconds=5)
        lifecycle = {
            "last_sample_at_utc": finish.isoformat(),
            "termination_noticed_at_utc": finish.isoformat(),
            "finalised_at_utc": finish.isoformat(),
            "completion_status": "complete",
            "interruption_reason": None,
        }
        manifest = {
            **lifecycle,
            "last_successful_obd_response_at_utc": finish.isoformat(),
            "poll_plan_version": 4,
        }
        return lifecycle, manifest

    def test_a_lamp_that_is_not_a_boolean_is_rejected(self, tmp_path, app_config):
        from app.ingest.obd_bundle import BundleError, validate_bundle

        lifecycle, manifest = self._lifecycle()
        samples = [{**_sample("drive_v4", 0), "mil_on": "yes"}, _sample("drive_v4", 1)]
        with pytest.raises(BundleError):
            validate_bundle(
                make_bundle(
                    tmp_path,
                    "drive_v4",
                    samples=samples,
                    summary_patch=lifecycle,
                    manifest_patch=manifest,
                ),
                config=app_config,
            )

    def test_a_dtc_count_beyond_seven_bits_is_rejected(self, tmp_path, app_config):
        from app.ingest.obd_bundle import BundleError, validate_bundle

        lifecycle, manifest = self._lifecycle()
        samples = [{**_sample("drive_v4", 0), "dtc_count": 200}, _sample("drive_v4", 1)]
        with pytest.raises(BundleError):
            validate_bundle(
                make_bundle(
                    tmp_path,
                    "drive_v4",
                    samples=samples,
                    summary_patch=lifecycle,
                    manifest_patch=manifest,
                ),
                config=app_config,
            )


class TestPollPlanV5Bundles:
    """Logger 0.2.9 exports plan v5: accepted, same sample shape as v4."""
