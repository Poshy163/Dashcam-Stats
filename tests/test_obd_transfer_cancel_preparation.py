"""Stop during OBD preparation must not start a new transfer or delete source bundles."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.ingest import obd_transfer
from app.ingest.models import RemoteFile, UnitInfo, UnitState
from app.ingest.status import IngestStatus


@pytest.mark.parametrize("stage", ["clean", "logger", "inventory", "local_hash", "clear_listener"])
async def test_cancel_during_preparation_never_launches_listener(app_config, monkeypatch, stage):
    status = IngestStatus()
    status.try_begin()
    item = RemoteFile("drive_cancel_prep.obd2.zip", 5, 1, "/safe/ready")

    async def stop(*_args, **_kwargs):
        status.cancel()
        return [item] if stage == "inventory" else None

    monkeypatch.setattr(obd_transfer, "read_logger_status", AsyncMock(return_value=None))
    monkeypatch.setattr(obd_transfer, "inventory_remote_bundles", AsyncMock(return_value=[item]))
    monkeypatch.setattr(obd_transfer, "_already_verified", AsyncMock(return_value=None))
    monkeypatch.setattr(obd_transfer, "_already_rejected", AsyncMock(return_value=None))
    monkeypatch.setattr(obd_transfer.adb, "clear_listener", AsyncMock())
    launch = AsyncMock(side_effect=AssertionError("Stop must prevent a new listener"))
    monkeypatch.setattr(obd_transfer.adb, "launch_listener", launch)
    if stage == "clean":
        monkeypatch.setattr(obd_transfer, "_clean_staging", lambda *_args: status.cancel())
    else:
        target, name = {
            "logger": (obd_transfer, "read_logger_status"),
            "inventory": (obd_transfer, "inventory_remote_bundles"),
            "local_hash": (obd_transfer, "_already_verified"),
            "clear_listener": (obd_transfer.adb, "clear_listener"),
        }[stage]
        monkeypatch.setattr(target, name, stop)
    result = await obd_transfer.sync_remote_bundles(
        UnitInfo("unit:5555", UnitState.DEVICE, "/card"),
        ingest_status=status,
        remote=None,
        config=app_config,
    )
    assert not result.complete and "cancelled" in result.error
    launch.assert_not_called()
    assert not list(app_config.obd_staging_dir.glob(".transfer-*.partial"))


@pytest.mark.parametrize("stage", ["remote_hash", "receipt"])
async def test_cancel_before_ack_or_delete_preserves_original(app_config, monkeypatch, stage):
    status = IngestStatus()
    status.try_begin()
    item = RemoteFile("drive_cancel_prep.obd2.zip", 5, 1, "/safe/ready")
    row = SimpleNamespace(bundle_hash="a" * 64, drive_id="drive_cancel_prep")
    monkeypatch.setattr(obd_transfer, "read_logger_status", AsyncMock(return_value=None))
    monkeypatch.setattr(obd_transfer, "_already_verified", AsyncMock(return_value=row))
    monkeypatch.setattr(obd_transfer, "_receipt_eligible", lambda _row: True)

    async def remote_hash(*_args):
        if stage == "remote_hash":
            status.cancel()
        return row.bundle_hash

    async def receipt(*_args, **_kwargs):
        status.cancel()

    acknowledge = AsyncMock(side_effect=receipt)
    delete = AsyncMock(side_effect=AssertionError("cancelled cleanup must preserve source"))
    monkeypatch.setattr(obd_transfer, "_remote_bundle_sha256", remote_hash)
    monkeypatch.setattr(obd_transfer, "write_verification_receipt", acknowledge)
    monkeypatch.setattr(obd_transfer, "_delete_remote_if_hash", delete)
    result = await obd_transfer.sync_remote_bundles(
        UnitInfo("unit:5555", UnitState.DEVICE, "/card"),
        ingest_status=status,
        remote=[item],
        config=app_config,
    )
    assert not result.complete and "cancelled" in result.error
    delete.assert_not_called()
    assert acknowledge.await_count == (1 if stage == "receipt" else 0)
