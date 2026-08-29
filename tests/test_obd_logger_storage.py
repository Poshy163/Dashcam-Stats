from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.obd.schema import DiagnosticEvent, DriveStart, Sample
from app.obd.storage import ObdStore


def drive_start(drive_id: str, started: datetime) -> DriveStart:
    return DriveStart(
        drive_id=drive_id,
        vehicle_id="nissan_tiida",
        adapter_id="ble-0123456789ab",
        logger_id="dashcam-1",
        logger_version="0.1.0",
        started_at=started,
        start_reason="checksum_valid_0100",
        original_timezone="Australia/Adelaide",
        obd_protocol="AUTO, ISO 9141-2",
    )


def test_obd_logger_sqlite_wal_sample_transaction_and_dedup(tmp_path) -> None:
    started = datetime(2026, 8, 29, 1, 0, tzinfo=UTC)
    with ObdStore(tmp_path / "obd.db") as store:
        store.start_drive(drive_start("drive-1", started))
        sample = Sample(
            sample_id="drive-1-0",
            drive_id="drive-1",
            timestamp=started,
            sequence=0,
            values={"engine_rpm": 850.0, "vehicle_speed": 0.0},
        )
        assert store.add_sample(sample) is True
        assert store.add_sample(sample) is False
        assert store.drive("drive-1")["sample_count"] == 1
        assert store.quick_check() is True
        assert store._connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_obd_logger_invalid_sample_rolls_back_whole_write(tmp_path) -> None:
    started = datetime(2026, 8, 29, 1, 0, tzinfo=UTC)
    with ObdStore(tmp_path / "obd.db") as store:
        store.start_drive(drive_start("drive-1", started))
        with pytest.raises(ValueError, match="unknown sample"):
            store.add_sample(
                Sample(
                    sample_id="drive-1-0",
                    drive_id="drive-1",
                    timestamp=started,
                    sequence=0,
                    values={"made_up_pid": 1.0},
                )
            )
        assert store.drive("drive-1")["sample_count"] == 0
        assert store.samples("drive-1") == []


def test_obd_logger_restart_closes_at_last_sample_not_reboot_time(tmp_path) -> None:
    started = datetime(2026, 8, 29, 1, 0, tzinfo=UTC)
    path = tmp_path / "obd.db"
    with ObdStore(path) as store:
        store.start_drive(drive_start("drive-1", started))
        store.add_sample(
            Sample(
                sample_id="drive-1-0",
                drive_id="drive-1",
                timestamp=started + timedelta(seconds=10),
                sequence=0,
                values={"engine_rpm": 900.0},
            )
        )
    with ObdStore(path) as reopened:
        recovered = reopened.recover_interrupted_drives(stopped_at=started + timedelta(hours=8))
        assert recovered == ["drive-1"]
        row = reopened.drive("drive-1")
        assert row["finish_time_utc"] == "2026-08-29T01:00:10Z"
        assert row["stop_reason"] == "device_restart"
        assert row["clean_end"] == 0


def test_obd_logger_diagnostics_are_sparse_and_deduplicated(tmp_path) -> None:
    started = datetime(2026, 8, 29, 1, 0, tzinfo=UTC)
    with ObdStore(tmp_path / "obd.db") as store:
        store.start_drive(drive_start("drive-1", started))
        first = DiagnosticEvent(
            "diag-1",
            "drive-1",
            started,
            "pending_dtcs",
            {"codes": ["P0420"]},
        )
        duplicate = DiagnosticEvent(
            "diag-2",
            "drive-1",
            started + timedelta(minutes=2),
            "pending_dtcs",
            {"codes": ["P0420"]},
        )
        assert store.add_diagnostic(first) is True
        assert store.add_diagnostic(duplicate) is False
        assert len(store.diagnostics("drive-1")) == 1


def test_obd_logger_restart_export_queue_retries_and_quarantines_empty_drives(tmp_path) -> None:
    started = datetime(2026, 8, 29, 1, 0, tzinfo=UTC)
    with ObdStore(tmp_path / "obd.db") as store:
        store.start_drive(drive_start("crash-after-finish", started))
        store.add_sample(
            Sample(
                sample_id="crash-after-finish-0",
                drive_id="crash-after-finish",
                timestamp=started,
                sequence=0,
                values={"engine_rpm": 850.0},
            )
        )
        store.finish_drive(
            "crash-after-finish",
            finished_at=started + timedelta(seconds=5),
            stop_reason="engine_stopped",
            clean_end=True,
        )
        # A prior export failure leaves waiting_for_backup unchanged, so startup retries it.
        assert store.completed_drive_ids() == ["crash-after-finish"]

        store.start_drive(drive_start("empty-restart", started + timedelta(minutes=1)))
        store.recover_interrupted_drives(stopped_at=started + timedelta(hours=1))
        assert store.quarantine_zero_sample_drives() == ["empty-restart"]
        assert store.drive("empty-restart")["export_status"] == "not_exportable_zero_samples"
        assert store.completed_drive_ids() == ["crash-after-finish"]
