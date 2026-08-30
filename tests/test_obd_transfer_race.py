"""Cross-process and uniqueness-race safety for the OBD bundle transport."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.process_lock import try_acquire
from app.db.models import OBDBundle, OBDBundleState
from app.db.session import session_scope
from app.ingest import obd_transfer
from app.ingest.models import RemoteFile, UnitInfo, UnitState
from app.ingest.obd_bundle import ValidatedBundle
from app.ingest.transport import TransferResult


def _unit() -> UnitInfo:
    return UnitInfo("192.0.2.10:5555", UnitState.DEVICE, "/card")


async def _none(*_args, **_kwargs):
    return None


async def test_contended_sync_does_not_touch_another_runs_staging(app_config, monkeypatch):
    orphan = app_config.obd_staging_dir / ".transfer-live-owner.partial"
    orphan.mkdir()
    (orphan / "still-copying").write_bytes(b"live")
    lock = try_acquire(obd_transfer._sync_lock_path(app_config))
    assert lock is not None
    try:
        monkeypatch.setattr(
            obd_transfer,
            "read_logger_status",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("a lock loser contacted the unit")
            ),
        )

        result = await obd_transfer.sync_remote_bundles(_unit(), remote=[], config=app_config)

        assert not result.complete
        assert result.failed == 1
        assert result.error == "another OBD transfer is already active"
        assert (orphan / "still-copying").read_bytes() == b"live"
    finally:
        lock.release()

    monkeypatch.setattr(obd_transfer, "read_logger_status", _none)
    result = await obd_transfer.sync_remote_bundles(_unit(), remote=[], config=app_config)
    assert result.complete
    assert not orphan.exists(), "the next lock owner may clean a dead owner's staging"


async def test_final_cleanup_removes_only_this_runs_transfer_directory(app_config, monkeypatch):
    item = RemoteFile("drive_cleanup_scope.obd2.zip", 5, 1, "/safe/ready")
    this_run = None
    foreign = app_config.obd_staging_dir / ".transfer-new-owner.partial"

    async def missing(*_args, **_kwargs):
        return None

    def receive(_host, _port, staging, **_kwargs):
        nonlocal this_run
        this_run = staging
        foreign.mkdir()
        (foreign / "still-copying").write_bytes(b"live")
        return TransferResult(complete=False, error="unit left before the first file")

    monkeypatch.setattr(obd_transfer, "read_logger_status", _none)
    monkeypatch.setattr(obd_transfer, "_already_verified", missing)
    monkeypatch.setattr(obd_transfer, "_already_rejected", missing)
    monkeypatch.setattr(obd_transfer, "ingest_setting", lambda _key, default: default)
    monkeypatch.setattr(obd_transfer.adb, "clear_listener", _none)
    monkeypatch.setattr(obd_transfer.adb, "launch_listener", _none)
    monkeypatch.setattr(obd_transfer.adb, "stop_listener", _none)
    monkeypatch.setattr(obd_transfer.transport, "receive", receive)

    result = await obd_transfer.sync_remote_bundles(_unit(), remote=[item], config=app_config)

    assert this_run is not None and not this_run.exists()
    assert (foreign / "still-copying").read_bytes() == b"live"
    assert not result.complete
    assert result.missing == result.failed == 1


async def test_integrity_loser_preserves_matching_trusted_winners_file(
    db_session, app_config, monkeypatch
):
    drive_id = "drive_concurrent_winner"
    filename = f"{drive_id}.obd2.zip"
    body = b"exact immutable winner bytes"
    digest = hashlib.sha256(body).hexdigest()
    canonical = app_config.obd_verified_dir / filename
    canonical.write_bytes(body)
    observed = datetime(2026, 8, 30, 6, 0, tzinfo=UTC)

    async with session_scope() as session:
        row = OBDBundle(
            drive_id=drive_id,
            bundle_hash=digest,
            schema_version=1,
            filename=filename,
            size_bytes=len(body),
            vehicle_id="tiida_c11",
            adapter_id=None,
            logger_id="dashcam_head_unit",
            logger_version="2.0.0",
            drive_started_at=observed,
            drive_finished_at=observed,
            sample_count=1,
            diagnostic_count=0,
            metadata_trusted=True,
            state=OBDBundleState.READY_TO_IMPORT.value,
            copied_at=observed,
            verified_at=observed,
            next_attempt_at=observed,
        )
        session.add(row)
        await session.flush()
        row_id = row.id

    validated = ValidatedBundle(
        path=canonical,
        filename=filename,
        bundle_sha256=digest,
        size_bytes=len(body),
        manifest={
            "drive_id": drive_id,
            "schema_version": 1,
            "vehicle_id": "tiida_c11",
        },
        summary={},
        diagnostics_document={},
        latest_sample={},
        latest_values={},
        statistics=[],
    )
    deleted: list[str] = []

    async def missing(*_args, **_kwargs):
        # Simulate the winner committing after this run's initial duplicate lookup.
        return None

    async def uniqueness_loser(_validated):
        raise IntegrityError("INSERT", {}, RuntimeError("winner committed first"))

    async def delete(_address, _source, *, filename, bundle_sha256):
        assert bundle_sha256 == digest
        deleted.append(filename)

    def receive(_host, _port, staging, **_kwargs):
        (staging / filename).write_bytes(body)
        return TransferResult(
            files=[filename],
            bytes_received=len(body),
            complete=True,
            seconds=0.01,
        )

    monkeypatch.setattr(obd_transfer, "read_logger_status", _none)
    monkeypatch.setattr(obd_transfer, "_already_verified", missing)
    monkeypatch.setattr(obd_transfer, "_already_rejected", missing)
    monkeypatch.setattr(obd_transfer, "ingest_setting", lambda _key, default: default)
    monkeypatch.setattr(obd_transfer.adb, "clear_listener", _none)
    monkeypatch.setattr(obd_transfer.adb, "launch_listener", _none)
    monkeypatch.setattr(obd_transfer.adb, "stop_listener", _none)
    monkeypatch.setattr(obd_transfer.transport, "receive", receive)
    monkeypatch.setattr(
        obd_transfer,
        "validate_bundle",
        lambda _path, **_kwargs: validated,
    )
    monkeypatch.setattr(obd_transfer, "_register", uniqueness_loser)
    monkeypatch.setattr(obd_transfer, "write_verification_receipt", _none)
    monkeypatch.setattr(obd_transfer, "_delete_remote_if_hash", delete)
    monkeypatch.setattr(
        obd_transfer,
        "get_import_worker",
        lambda: SimpleNamespace(wake=lambda: None),
    )
    item = RemoteFile(filename, len(body), 1, "/safe/ready")

    result = await obd_transfer.sync_remote_bundles(_unit(), remote=[item], config=app_config)

    assert result.complete
    assert result.failed == result.missing == 0
    assert result.copied == result.removed_from_unit == 1
    assert deleted == [filename]
    assert canonical.read_bytes() == body
    assert not (app_config.obd_quarantine_dir / filename).exists()
    async with session_scope() as session:
        winner = await session.scalar(select(OBDBundle).where(OBDBundle.id == row_id))
        assert winner is not None
        assert winner.metadata_trusted
        assert winner.bundle_hash == digest
        assert winner.remote_deleted_at is not None


async def test_verified_bundle_match_requires_exact_canonical_bytes(
    db_session, app_config, monkeypatch
):
    filename = "drive_verified_gate.obd2.zip"
    body = b"trusted immutable export"
    digest = hashlib.sha256(body).hexdigest()
    observed = datetime(2026, 8, 30, 6, 30, tzinfo=UTC)
    canonical = app_config.obd_verified_dir / filename
    canonical.write_bytes(body)

    async with session_scope() as session:
        session.add(
            OBDBundle(
                drive_id="drive_verified_gate",
                bundle_hash=digest,
                schema_version=1,
                filename=filename,
                size_bytes=len(body),
                vehicle_id="tiida_c11",
                logger_id="dashcam_head_unit",
                logger_version="2.0.0",
                drive_started_at=observed,
                drive_finished_at=observed,
                sample_count=1,
                diagnostic_count=0,
                metadata_trusted=True,
                state=OBDBundleState.READY_TO_IMPORT.value,
                copied_at=observed,
                verified_at=observed,
                next_attempt_at=observed,
            )
        )
        await session.flush()

    monkeypatch.setattr(obd_transfer, "get_config", lambda: app_config)
    assert await obd_transfer.verified_bundle_matches(filename, digest)

    canonical.write_bytes(b"x" * len(body))
    assert not await obd_transfer.verified_bundle_matches(filename, digest)

    canonical.unlink()
    assert not await obd_transfer.verified_bundle_matches(filename, digest)
