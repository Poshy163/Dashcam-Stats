from __future__ import annotations

import gzip
import json
import zipfile
from datetime import UTC, datetime, timedelta

import pytest
from test_obd_logger_storage import drive_start

from app.ingest.obd_bundle import validate_bundle as validate_server_bundle
from app.obd.bundle import (
    BundleExporter,
    BundleValidationError,
    inspect_bundle,
    publish_status,
    ready_bundles,
)
from app.obd.schema import BUNDLE_MEMBERS, SAMPLE_UNITS, DiagnosticEvent, Sample
from app.obd.storage import ObdStore
from app.obd.summary import calculate_summary


def completed_drive(store: ObdStore, drive_id: str, started: datetime) -> None:
    store.start_drive(drive_start(drive_id, started))
    values = (
        {
            "engine_rpm": 800.0,
            "vehicle_speed": 0.0,
            "estimated_fuel_rate": 0.8,
            "oxygen_sensors_present": [1, 2],
            "obd_standard": "JOBD",
            "distance_with_mil": 42.0,
        },
        {
            "engine_rpm": 1500.0,
            "vehicle_speed": 36.0,
            "coolant_temperature": 90.0,
            "engine_load": 55.0,
            "estimated_fuel_rate": 4.0,
        },
        {"engine_rpm": 900.0, "vehicle_speed": 0.0, "estimated_fuel_rate": 1.0},
    )
    for sequence, sample_values in enumerate(values):
        store.add_sample(
            Sample(
                sample_id=f"{drive_id}-{sequence}",
                drive_id=drive_id,
                timestamp=started + timedelta(seconds=sequence * 5),
                sequence=sequence,
                values=sample_values,
            )
        )
    store.add_diagnostic(
        DiagnosticEvent(
            diagnostic_id=f"{drive_id}-diag",
            drive_id=drive_id,
            timestamp=started,
            kind="pending_dtcs",
            payload={"codes": ["P0420"]},
        )
    )
    store.finish_drive(
        drive_id,
        finished_at=started + timedelta(seconds=10),
        stop_reason="engine_stopped",
        clean_end=True,
    )


def test_obd_logger_atomic_bundle_matches_canonical_v1_contract(tmp_path) -> None:
    started = datetime(2026, 8, 29, 1, 0, tzinfo=UTC)
    ready = tmp_path / "ready"
    with ObdStore(tmp_path / "obd.db") as store:
        completed_drive(store, "drive-1", started)
        store.increment_error("drive-1")
        exported = BundleExporter(store, ready).export(
            "drive-1", created_at=started + timedelta(minutes=1)
        )
        assert exported.path.name == "drive-1.obd2.zip"
        assert exported.sha256 == store.drive("drive-1")["bundle_sha256"]
        assert not (ready / "drive-1.obd2.zip.partial").exists()
        manifest = inspect_bundle(exported.path)
        server_view = validate_server_bundle(exported.path)
        assert server_view.drive_id == "drive-1"
        assert server_view.bundle_sha256 == exported.sha256
        assert manifest["schema_version"] == 1
        assert manifest["bundle_format"] == "dashcam-obd"
        assert manifest["error_count"] == 1
        assert manifest["completion_status"] == "complete"
        assert manifest["units"] == SAMPLE_UNITS
        assert manifest["units"]["distance_with_mil"] == "km"
        assert manifest["included_filenames"] == list(BUNDLE_MEMBERS)
        assert set(manifest["files"]) == set(BUNDLE_MEMBERS[1:])
        with zipfile.ZipFile(exported.path) as archive:
            assert archive.namelist() == list(BUNDLE_MEMBERS)
            assert all(item.compress_type == zipfile.ZIP_STORED for item in archive.infolist())
            with gzip.GzipFile(fileobj=archive.open("samples.ndjson.gz")) as samples:
                rows = [json.loads(line) for line in samples]
            assert [row["sample_id"] for row in rows] == [
                "drive-1-0",
                "drive-1-1",
                "drive-1-2",
            ]
            assert rows[0]["quality"] == {
                "missing_pids": [],
                "parser": "ok",
                "transport": "ok",
            }
            assert rows[0]["oxygen_sensors_present"] == [1, 2]
            assert rows[0]["obd_standard"] == "JOBD"
            assert rows[0]["distance_with_mil"] == 42.0
            summary = json.loads(archive.read("summary.json"))
            assert summary["distance_km"] == pytest.approx(0.05)
            assert summary["dtcs_observed"] == ["P0420"]


def test_obd_logger_export_is_immutable_and_partial_files_are_excluded(tmp_path) -> None:
    started = datetime(2026, 8, 29, 1, 0, tzinfo=UTC)
    ready = tmp_path / "ready"
    with ObdStore(tmp_path / "obd.db") as store:
        completed_drive(store, "drive-b", started + timedelta(hours=1))
        completed_drive(store, "drive-a", started)
        exporter = BundleExporter(store, ready)
        later = exporter.export("drive-b")
        earlier = exporter.export("drive-a")
        (ready / "drive-0.obd2.zip.partial").write_bytes(b"incomplete")
        assert ready_bundles(ready) == [earlier.path, later.path]
        before = earlier.path.read_bytes()
        repeated = exporter.export("drive-a")
        assert repeated.sha256 == earlier.sha256
        assert earlier.path.read_bytes() == before


def test_obd_logger_bundle_rejects_hash_mismatch_and_corrupt_gzip(tmp_path) -> None:
    started = datetime(2026, 8, 29, 1, 0, tzinfo=UTC)
    with ObdStore(tmp_path / "obd.db") as store:
        completed_drive(store, "drive-1", started)
        path = BundleExporter(store, tmp_path / "ready").export("drive-1").path

    members: dict[str, bytes]
    with zipfile.ZipFile(path) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    members["samples.ndjson.gz"] = b"not gzip"
    broken = tmp_path / "broken.obd2.zip"
    with zipfile.ZipFile(broken, "w", compression=zipfile.ZIP_STORED) as archive:
        for name in BUNDLE_MEMBERS:
            archive.writestr(name, members[name])
    with pytest.raises(BundleValidationError, match=r"size mismatch|hash mismatch"):
        inspect_bundle(broken)

    manifest = json.loads(members["manifest.json"])
    import hashlib

    manifest["files"]["samples.ndjson.gz"] = {
        "size_bytes": len(members["samples.ndjson.gz"]),
        "sha256": hashlib.sha256(members["samples.ndjson.gz"]).hexdigest(),
        "record_count": 3,
    }
    members["manifest.json"] = json.dumps(manifest, separators=(",", ":")).encode()
    corrupt = tmp_path / "corrupt-gzip.obd2.zip"
    with zipfile.ZipFile(corrupt, "w", compression=zipfile.ZIP_STORED) as archive:
        for name in BUNDLE_MEMBERS:
            archive.writestr(name, members[name])
    with pytest.raises(BundleValidationError, match=r"gzip|corrupt"):
        inspect_bundle(corrupt)


def test_obd_logger_status_is_atomic_and_rejects_private_fields(tmp_path) -> None:
    path = publish_status(
        tmp_path,
        {
            "state": "parked",
            "ownership_enabled": True,
            "last_drive_id": "drive-1",
            "last_drive_finished_at_utc": "2026-08-29T01:00:10Z",
            "pending_bundle_count": 1,
            "last_error": None,
            "last_error_at_utc": None,
        },
    )
    assert json.loads(path.read_text()) == {
        "state": "parked",
        "ownership_enabled": True,
        "last_drive_id": "drive-1",
        "last_drive_finished_at_utc": "2026-08-29T01:00:10Z",
        "pending_bundle_count": 1,
        "last_error": None,
        "last_error_at_utc": None,
    }
    assert not (tmp_path / "status.json.partial").exists()
    with pytest.raises(ValueError, match="private"):
        publish_status(tmp_path, {"state": "parked", "adapter_mac": "private"})


def test_obd_logger_summary_preserves_missing_values_and_counts_gaps() -> None:
    drive = {
        "drive_id": "drive-1",
        "start_time_utc": "2026-08-29T01:00:00Z",
        "finish_time_utc": "2026-08-29T01:00:30Z",
        "clean_end": 0,
    }
    samples = [
        {
            "sample_id": "drive-1-0",
            "drive_id": "drive-1",
            "timestamp_utc": "2026-08-29T01:00:00Z",
            "sequence": 0,
        },
        {
            "sample_id": "drive-1-1",
            "drive_id": "drive-1",
            "timestamp_utc": "2026-08-29T01:00:30Z",
            "sequence": 1,
        },
    ]
    summary = calculate_summary(drive, samples, [], expected_interval_s=5)
    assert summary["distance_km"] is None
    assert summary["average_speed_kmh"] is None
    assert summary["idle_duration_s"] is None
    assert summary["estimated_fuel_used_l"] is None
    assert summary["missing_data_duration_s"] == 25
    assert summary["expected_sample_count"] == 7
    assert summary["received_sample_percentage"] == pytest.approx(28.5714, rel=1e-4)


def test_obd_logger_bundle_refuses_zero_sample_drive(tmp_path) -> None:
    started = datetime(2026, 8, 29, 1, 0, tzinfo=UTC)
    with ObdStore(tmp_path / "obd.db") as store:
        store.start_drive(drive_start("empty-drive", started))
        store.finish_drive(
            "empty-drive",
            finished_at=started,
            stop_reason="device_restart",
            clean_end=False,
        )
        with pytest.raises(ValueError, match="zero-sample"):
            BundleExporter(store, tmp_path / "ready").export("empty-drive")


def test_obd_logger_recovered_drive_preserves_completion_status(tmp_path) -> None:
    started = datetime(2026, 8, 29, 1, 0, tzinfo=UTC)
    with ObdStore(tmp_path / "obd.db") as store:
        store.start_drive(drive_start("recovered-drive", started))
        store.add_sample(
            Sample(
                sample_id="recovered-drive-0",
                drive_id="recovered-drive",
                timestamp=started + timedelta(seconds=5),
                sequence=0,
                values={"engine_rpm": 850.0},
            )
        )
        assert store.recover_interrupted_drives(stopped_at=started + timedelta(minutes=5)) == [
            "recovered-drive"
        ]
        exported = BundleExporter(store, tmp_path / "ready").export("recovered-drive")
        manifest = inspect_bundle(exported.path)
        assert manifest["finish_time_utc"] == "2026-08-29T01:00:05Z"
        assert manifest["stop_reason"] == "device_restart"
        assert manifest["completion_status"] == "recovered"
        assert manifest["clean_end"] is False
        with zipfile.ZipFile(exported.path) as archive:
            summary = json.loads(archive.read("summary.json"))
        assert summary["clean_end"] is False


def test_obd_logger_exporter_uses_exact_server_drive_id_contract(tmp_path) -> None:
    started = datetime(2026, 8, 29, 1, 0, tzinfo=UTC)
    with ObdStore(tmp_path / "obd.db") as store:
        store.start_drive(drive_start("drive.with.dot", started))
        store.finish_drive(
            "drive.with.dot",
            finished_at=started,
            stop_reason="test",
            clean_end=True,
        )
        with pytest.raises(ValueError, match="filename"):
            BundleExporter(store, tmp_path / "ready").export("drive.with.dot")
        too_long = "d" * 65
        store.start_drive(drive_start(too_long, started + timedelta(minutes=1)))
        store.finish_drive(
            too_long,
            finished_at=started + timedelta(minutes=1),
            stop_reason="test",
            clean_end=True,
        )
        with pytest.raises(ValueError, match="filename"):
            BundleExporter(store, tmp_path / "ready").export(too_long)
