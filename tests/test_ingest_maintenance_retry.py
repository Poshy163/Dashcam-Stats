"""A new operator request can reuse proven cancellation, never an unknown timer."""

from __future__ import annotations

import asyncio
import json
import threading
from unittest.mock import AsyncMock

import pytest

from app.core.process_lock import try_acquire
from app.ingest import adb, maintenance, puller

BOOT = "01234567-1234-1234-1234-012345678901"
OTHER = "01234567-1234-1234-1234-012345678902"
NORMAL = '<hierarchy><node package="com.android.launcher" /></hierarchy>'
COUNTDOWN = (
    '<hierarchy><node package="com.android.systemui" '
    'resource-id="com.android.systemui:id/tv_countdown" '
    'class="android.widget.TextView" enabled="true" long-clickable="true" '
    'text="300 seconds until sleep" bounds="[600,300][1000,500]" /></hierarchy>'
)


@pytest.fixture
def retry(monkeypatch, tmp_path, app_config):
    path = tmp_path / "cancelled.json"
    record = maintenance.CancellationRecord(BOOT, OTHER, 900_000, native_verified=True)
    path.write_text(json.dumps(record.document()), encoding="utf-8")
    controller = maintenance.MaintenanceController(path)
    monkeypatch.setattr(maintenance, "_controller", controller)
    monkeypatch.setattr(maintenance, "_attest", AsyncMock())
    monkeypatch.setattr(
        maintenance, "_power", AsyncMock(return_value=maintenance.PowerProof(BOOT, 1000))
    )
    monkeypatch.setattr(maintenance, "_screen", AsyncMock(return_value=NORMAL))
    monkeypatch.setattr(controller, "_publish", AsyncMock())
    monkeypatch.setattr(adb, "shell", AsyncMock(return_value=""))
    monkeypatch.setattr(puller, "stop_for_maintenance", AsyncMock(return_value=True))
    return controller


async def test_restart_retry_publishes_new_bounded_lease_without_gesture_or_record_change(retry):
    original = retry._record_path().read_bytes()
    result = await retry.start("unit", 900)
    first_token = retry._token
    assert result["active"] and result["verified"] and result["countdown_cancelled"]
    assert result["requires_ignition_cycle"]
    assert 838 < result["remaining_s"] <= 840
    assert retry._device_end == 1900
    assert retry._record_path().read_bytes() == original
    adb.shell.assert_not_awaited()
    stopped = await retry.stop()
    assert not stopped["active"] and stopped["requires_ignition_cycle"]
    assert retry._record_path().read_bytes() == original
    await retry.start("unit", 300)
    assert retry._token != first_token and retry._device_end == 1300
    assert retry._record_path().read_bytes() == original
    await retry.stop()


@pytest.mark.parametrize("kind", ["v1", "v2", "provisional", "unverified", "corrupt", "missing"])
async def test_old_unknown_and_provisional_records_cannot_authorize_retry(retry, kind):
    document = json.loads(retry._record_path().read_text())
    if kind in {"v1", "v2"}:
        document["schema_version"] = 1 if kind == "v1" else 2
        document.pop("native_verified")
        if kind == "v1":
            document.pop("cycle_after_elapsed_ms")
    elif kind == "provisional":
        document.update(cycle_after_elapsed_ms=None, native_verified=False)
    elif kind == "unverified":
        document["native_verified"] = False
    if kind == "missing":
        retry._record_path().unlink()
    else:
        retry._record_path().write_text("{bad" if kind == "corrupt" else json.dumps(document))
    with pytest.raises(maintenance.MaintenanceRefused, match="no verified same-boot"):
        await retry.start("unit", 900)
    retry._publish.assert_not_awaited()
    adb.shell.assert_not_awaited()
    assert retry.remaining(BOOT) is None
    assert retry._fence is None


async def test_legacy_anchor_never_creates_native_verification(retry):
    document = json.loads(retry._record_path().read_text())
    document.update(schema_version=1)
    document.pop("native_verified")
    document.pop("cycle_after_elapsed_ms")
    retry._record_path().write_text(json.dumps(document))
    retry.load_once()
    assert retry._record.legacy
    assert retry._record_cycle_boundary(retry._record, 900_000)
    assert not retry._record.native_verified
    assert json.loads(retry._record_path().read_text())["native_verified"] is False
    with pytest.raises(maintenance.MaintenanceRefused, match="no verified same-boot"):
        await retry.start("unit", 900)


@pytest.mark.parametrize(
    "proofs",
    [
        [(OTHER, 1000)],
        [(BOOT, 1000), (OTHER, 1001)],
        [(BOOT, 1000), (BOOT, 1001), (OTHER, 1002)],
        [(BOOT, 1000), (BOOT, 999)],
        [(BOOT, 1000), (BOOT, 1001), (BOOT, 1000)],
        [(BOOT, 899)],
    ],
)
async def test_boot_changes_and_reversed_or_preverification_uptime_refuse(
    retry, monkeypatch, proofs
):
    monkeypatch.setattr(
        maintenance, "_power", AsyncMock(side_effect=[maintenance.PowerProof(*p) for p in proofs])
    )
    original = retry._record_path().read_bytes()
    with pytest.raises(maintenance.MaintenanceRefused):
        await retry.start("unit", 900)
    retry._publish.assert_not_awaited()
    assert retry._record_path().read_bytes() == original
    assert retry.remaining(BOOT) is None


async def test_new_popup_during_retry_never_sends_a_second_cancellation_gesture(retry, monkeypatch):
    monkeypatch.setattr(maintenance, "_screen", AsyncMock(side_effect=[NORMAL, COUNTDOWN]))
    with pytest.raises(maintenance.MaintenanceRefused, match="new native countdown"):
        await retry.start("unit", 900)
    retry._publish.assert_not_awaited()
    assert all("input" not in call.args[1] for call in adb.shell.await_args_list)
    puller.stop_for_maintenance.assert_awaited_once()


@pytest.mark.parametrize("corrupt", [False, True])
async def test_exact_durable_generation_is_checked_again_after_native_proof(
    retry, monkeypatch, corrupt
):
    count = 0

    async def screen(_address):
        nonlocal count
        count += 1
        if count == 2:
            document = json.loads(retry._record_path().read_text())
            document["generation"] = BOOT
            retry._record_path().write_text("{bad" if corrupt else json.dumps(document))
        return NORMAL

    monkeypatch.setattr(maintenance, "_screen", screen)
    with pytest.raises(maintenance.MaintenanceRefused, match="record changed"):
        await retry.start("unit", 900)
    retry._publish.assert_not_awaited()
    assert retry.remaining(BOOT) is None
    assert retry.snapshot()["requires_ignition_cycle"]


async def test_publish_failure_preserves_verified_record_and_restores(retry, monkeypatch):
    original = retry._record_path().read_bytes()
    monkeypatch.setattr(retry, "_publish", AsyncMock(side_effect=adb.AdbError("private text")))
    with pytest.raises(adb.AdbError):
        await retry.start("unit", 900)
    assert retry._record_path().read_bytes() == original
    assert retry.remaining(BOOT) is None
    assert "private" not in str(retry.snapshot())
    puller.stop_for_maintenance.assert_awaited_once()


async def test_competing_process_and_pending_record_worker_refuse_retry(retry, app_config):
    fence = try_acquire(maintenance.get_config().data_dir / ".ingest-maintenance.lock")
    assert fence is not None
    try:
        with pytest.raises(maintenance.MaintenanceRefused, match="Another server process"):
            await retry.start("unit", 900)
    finally:
        fence.release()
    retry._persistence_task = asyncio.create_task(asyncio.Event().wait())
    try:
        with pytest.raises(maintenance.MaintenanceRefused, match="operation is still pending"):
            await retry.start("unit", 900)
    finally:
        retry._persistence_task.cancel()
        await asyncio.gather(retry._persistence_task, return_exceptions=True)
    retry._publish.assert_not_awaited()


async def test_timed_out_retry_record_read_retains_fence_until_worker_exits(retry, monkeypatch):
    monkeypatch.setattr(maintenance, "_RECORD_IO_TIMEOUT_S", 0.02)
    entered, release = threading.Event(), threading.Event()
    original = retry._verified_record_matches
    original_bytes = retry._record_path().read_bytes()

    def blocked_read(record):
        entered.set()
        if not release.wait(timeout=5):
            raise OSError("test read remained blocked")
        return original(record)

    monkeypatch.setattr(retry, "_verified_record_matches", blocked_read)
    try:
        with pytest.raises(TimeoutError):
            await retry.start("unit", 900)
        assert entered.is_set() and not retry._persistence_task.done()
        assert retry._fence is not None and retry._fence.held
        assert retry.snapshot()["state"] == "restoring"
        assert try_acquire(maintenance.get_config().data_dir / ".ingest-maintenance.lock") is None
        retry._publish.assert_not_awaited()
    finally:
        release.set()
        if retry._persistence_task is not None:
            await asyncio.wait_for(retry._persistence_task, timeout=3)
        await retry.stop()
    assert retry._fence is None
    assert retry._record_path().read_bytes() == original_bytes


@pytest.mark.parametrize("value", [None, 1, "true", [], {}])
def test_v3_verification_requires_boolean(retry, value):
    document = json.loads(retry._record_path().read_text())
    document["native_verified"] = value
    retry._record_path().write_text(json.dumps(document))
    with pytest.raises(ValueError):
        retry._read_record(retry._record_path())


def test_v3_verified_record_requires_postgesture_boundary(retry):
    document = json.loads(retry._record_path().read_text())
    document["cycle_after_elapsed_ms"] = None
    retry._record_path().write_text(json.dumps(document))
    with pytest.raises(ValueError):
        retry._read_record(retry._record_path())
