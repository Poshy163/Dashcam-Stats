"""Re-created export clocks may change bytes, but must never replace stored telemetry."""

from __future__ import annotations

import gzip
import hashlib
import json
import shutil
import zipfile
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from test_obd_server_import import BASE, make_bundle

from app.db.models import OBDBundle, OBDDrive, OBDSample
from app.db.session import session_scope
from app.ingest import obd_transfer
from app.ingest.models import RemoteFile, UnitInfo, UnitState
from app.ingest.obd_bundle import validate_bundle
from app.ingest.transport import TransferResult


def _rewrite(case, *, metadata=None, member=None, mutate=None, container_only=False):
    with zipfile.ZipFile(case.canonical) as archive:
        bodies = {name: archive.read(name) for name in archive.namelist()}
    manifest = json.loads(bodies["manifest.json"])
    if not container_only:
        manifest["created_at_utc"] = "2026-10-21T05:19:03.979345Z"
    manifest.update(metadata or {})
    if member is not None:
        bodies[member] = mutate(bodies[member])
        manifest["files"][member].update(
            size_bytes=len(bodies[member]), sha256=hashlib.sha256(bodies[member]).hexdigest()
        )
    bodies["manifest.json"] = json.dumps(manifest, separators=(",", ":")).encode()
    with zipfile.ZipFile(case.incoming, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.comment = b"new export container"
        for name, body in bodies.items():
            info = zipfile.ZipInfo(name, date_time=(2026, 10, 21, 5, 19, 4))
            archive.writestr(info, body)
    case.new = validate_bundle(case.incoming, config=case.config)
    assert case.new.summary_source == "producer"
    return case.new


@pytest.fixture
async def reexport_case(db_session, app_config, tmp_path, monkeypatch):
    drive_id = "drive_reexport"
    diagnostic = {
        "diagnostic_id": "diag_reexport",
        "drive_id": drive_id,
        "timestamp_utc": BASE.isoformat(),
        "kind": "mode01_support",
        "payload": {"supported_pids": [1]},
    }
    canonical = make_bundle(app_config.obd_verified_dir, drive_id, diagnostics=[diagnostic])
    original = validate_bundle(canonical, config=app_config)
    row = await obd_transfer._register(original)
    incoming_dir = tmp_path / "incoming"
    incoming_dir.mkdir()
    case = SimpleNamespace(
        config=app_config,
        canonical=canonical,
        original=original,
        original_bytes=canonical.read_bytes(),
        incoming=incoming_dir / canonical.name,
        row_id=row.id,
        receipts=[],
        deleted=[],
    )
    _rewrite(case)

    async def nothing(*_args, **_kwargs):
        return None

    def receive(_host, _port, staging, **_kwargs):
        shutil.copyfile(case.incoming, staging / canonical.name)
        return TransferResult(files=[canonical.name], complete=True, seconds=0.01)

    async def remote_hash(*_args):
        return case.new.bundle_sha256

    async def receipt(_address, _directory, *, drive_id, bundle_sha256):
        assert drive_id == case.new.drive_id
        assert bundle_sha256 == case.new.bundle_sha256 != case.original.bundle_sha256
        preserved = obd_transfer._safe_reexport_path(
            app_config, canonical.name, bundle_sha256, create=False
        )
        assert preserved.read_bytes() == case.incoming.read_bytes()
        assert canonical.read_bytes() == case.original_bytes
        case.receipts.append(bundle_sha256)

    async def delete(_address, _directory, *, filename, bundle_sha256):
        assert case.receipts[-1] == bundle_sha256
        assert filename == canonical.name
        case.deleted.append(bundle_sha256)

    monkeypatch.setattr(obd_transfer, "read_logger_status", nothing)
    monkeypatch.setattr(obd_transfer, "_remote_bundle_sha256", remote_hash)
    monkeypatch.setattr(obd_transfer, "ingest_setting", lambda _key, default: default)
    monkeypatch.setattr(obd_transfer.adb, "clear_listener", nothing)
    monkeypatch.setattr(obd_transfer.adb, "launch_listener", nothing)
    monkeypatch.setattr(obd_transfer.adb, "stop_listener", nothing)
    monkeypatch.setattr(obd_transfer.transport, "receive", receive)
    monkeypatch.setattr(obd_transfer, "write_verification_receipt", receipt)
    monkeypatch.setattr(obd_transfer, "_delete_remote_if_hash", delete)
    monkeypatch.setattr(obd_transfer, "get_config", lambda: app_config)
    return case


async def _sync(case):
    return await obd_transfer.sync_remote_bundles(
        UnitInfo("192.0.2.10:5555", UnitState.DEVICE, "/card"),
        remote=[RemoteFile(case.incoming.name, case.new.size_bytes, 1, "/safe/ready")],
        config=case.config,
    )


async def _assert_original_history(case):
    assert case.canonical.read_bytes() == case.original_bytes
    async with session_scope() as session:
        row = await session.get(OBDBundle, case.row_id)
        assert row.bundle_hash == case.original.bundle_sha256
        assert row.metadata_trusted
        drive = await session.scalar(select(OBDDrive))
        assert drive.manifest_json == case.original.manifest
        assert await session.scalar(select(func.count()).select_from(OBDBundle)) == 1
        assert await session.scalar(select(func.count()).select_from(OBDDrive)) == 1
        assert await session.scalar(select(func.count()).select_from(OBDSample)) == 2


@pytest.mark.parametrize("container_only", [False, True])
async def test_equivalent_reexport_preserves_both_exact_archives_and_history(
    reexport_case, container_only
):
    case = reexport_case
    _rewrite(case, container_only=container_only)
    result = await _sync(case)
    assert result.complete and result.failed == result.missing == 0
    assert result.copied == result.removed_from_unit == 1
    assert case.receipts == case.deleted == [case.new.bundle_sha256]
    await _assert_original_history(case)
    assert await obd_transfer.verified_bundle_matches(case.canonical.name, case.new.bundle_sha256)
    assert await obd_transfer.verified_bundle_matches(
        case.canonical.name, case.original.bundle_sha256
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("vehicle_id", "another_vehicle"),
        ("adapter_id", "another_adapter"),
        ("logger_id", "another_logger"),
        ("logger_version", "0.3.9"),
        ("original_timezone", "UTC"),
    ],
)
async def test_reexport_rejects_changed_source_metadata(reexport_case, field, value):
    case = reexport_case
    _rewrite(case, metadata={field: value})
    result = await _sync(case)
    assert not result.complete and result.failed == result.missing == 1
    assert not case.receipts and not case.deleted
    await _assert_original_history(case)


def _changed_samples(raw):
    rows = [json.loads(line) for line in gzip.decompress(raw).splitlines()]
    rows[0]["engine_rpm"] += 1
    return gzip.compress(b"\n".join(json.dumps(row).encode() for row in rows) + b"\n", mtime=0)


def _changed_diagnostics(raw):
    document = json.loads(raw)
    document["events"][0]["payload"]["supported_pids"] = [2]
    return json.dumps(document).encode()


def _changed_summary(raw):
    document = json.loads(raw)
    document["maximum_speed_kmh"] += 1
    return json.dumps(document).encode()


@pytest.mark.parametrize(
    ("member", "mutate"),
    [
        ("samples.ndjson.gz", _changed_samples),
        ("diagnostics.json", _changed_diagnostics),
        ("summary.json", _changed_summary),
    ],
)
async def test_reexport_rejects_fully_valid_but_changed_payload(reexport_case, member, mutate):
    case = reexport_case
    _rewrite(case, member=member, mutate=mutate)
    result = await _sync(case)
    assert not result.complete and result.failed == result.missing == 1
    assert not case.receipts and not case.deleted
    await _assert_original_history(case)


@pytest.mark.parametrize("failure", ["publish", "fsync"])
async def test_reexport_durability_failure_never_receipts_or_removes_remote(
    reexport_case, monkeypatch, failure
):
    case = reexport_case

    def fail(*_args, **_kwargs):
        raise OSError("injected durability failure")

    if failure == "publish":
        monkeypatch.setattr(obd_transfer.os, "link", fail)
    else:
        monkeypatch.setattr(obd_transfer, "_fsync_directory", fail)
    result = await _sync(case)
    assert not result.complete and result.failed == result.missing == 1
    assert not case.receipts and not case.deleted
    assert not await obd_transfer.verified_bundle_matches(
        case.canonical.name, case.new.bundle_sha256
    )
    await _assert_original_history(case)


async def test_receipt_failure_keeps_variant_and_retry_is_idempotent(reexport_case, monkeypatch):
    case = reexport_case
    success_receipt = obd_transfer.write_verification_receipt

    async def fail_receipt(*_args, **_kwargs):
        raise obd_transfer.adb.AdbError("receipt readback failed")

    monkeypatch.setattr(obd_transfer, "write_verification_receipt", fail_receipt)
    result = await _sync(case)
    assert result.complete and result.failed == 1 and result.removed_from_unit == 0
    assert not case.receipts and not case.deleted
    preserved = obd_transfer._safe_reexport_path(
        case.config, case.canonical.name, case.new.bundle_sha256, create=False
    )
    assert preserved.read_bytes() == case.incoming.read_bytes()
    monkeypatch.setattr(obd_transfer, "write_verification_receipt", success_receipt)
    result = await _sync(case)
    assert result.complete and result.failed == 0 and result.removed_from_unit == 1
    assert case.receipts == case.deleted == [case.new.bundle_sha256]
    await _assert_original_history(case)


async def test_corrupt_preserved_variant_is_never_overwritten_or_acknowledged(reexport_case):
    case = reexport_case
    preserved = obd_transfer._safe_reexport_path(
        case.config, case.canonical.name, case.new.bundle_sha256, create=True
    )
    preserved.write_bytes(b"preserve this damaged evidence")
    result = await _sync(case)
    assert not result.complete and result.failed == result.missing == 1
    assert not case.receipts and not case.deleted
    assert preserved.read_bytes() == b"preserve this damaged evidence"
    assert not await obd_transfer.verified_bundle_matches(
        case.canonical.name, case.new.bundle_sha256
    )
    await _assert_original_history(case)


async def test_ack_revalidates_original_canonical_after_preserving_variant(reexport_case):
    case = reexport_case
    assert (await _sync(case)).failed == 0
    case.canonical.write_bytes(b"damaged original canonical")
    assert not await obd_transfer.verified_bundle_matches(
        case.canonical.name, case.new.bundle_sha256
    )
