"""Old-copy cleanup must not reopen the normal transfer or radio preparation path."""

from __future__ import annotations

import asyncio
import hashlib
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.ingest import adb, duplicate_cleanup, puller, transport
from app.ingest.models import RemoteFile, RunState, UnitInfo, UnitState
from app.ingest.obd_events import EventSyncResult
from app.ingest.obd_transfer import get_obd_transfer_status
from app.ingest.status import get_status, reset_status_for_tests


@pytest.fixture
def duplicate_unit(monkeypatch, tmp_path):
    reset_status_for_tests()
    get_obd_transfer_status().set_logger(None)
    monkeypatch.setattr(puller, "_event_mirror_task", None)
    status = get_status()
    budget = {"remaining": 300.0}
    monkeypatch.setattr(status, "sleep_countdown_remaining_s", lambda: budget["remaining"])
    settings = SimpleNamespace(footage_dir=AsyncMock(return_value=tmp_path))
    monkeypatch.setattr(puller, "get_settings_service", lambda: settings)
    monkeypatch.setattr(
        puller,
        "get_config",
        lambda: SimpleNamespace(
            obd_remote_ready_dir="/ready",
            obd_remote_status_file="/status",
            obd_remote_events_file="/events",
        ),
    )
    options = {
        "enabled": True,
        "only_when_parked": False,
        "include_locked": False,
        "quiet_radios": True,
        "show_on_unit": True,
        "delete_after_verify": True,
        "sweep_passes": 0,
        "rescue_partials": False,
        "skip_parked_footage": False,
    }
    monkeypatch.setattr(puller, "_get", lambda key, default=None: options.get(key, default))
    monkeypatch.setattr(puller, "_recover_before_run", AsyncMock(return_value=True))
    removed = AsyncMock(return_value=set())
    safe = AsyncMock(return_value=(True, None))
    clock = AsyncMock(return_value=int(time.time()))
    monkeypatch.setattr(puller, "_deliberately_removed", removed)
    monkeypatch.setattr(puller, "_footage_is_safe_to_write", safe)
    monkeypatch.setattr(adb, "unit_clock", clock)
    monkeypatch.setattr(adb, "ignition_state", AsyncMock(return_value="off"))
    monkeypatch.setattr(
        adb, "shell", AsyncMock(side_effect=AssertionError("unexpected device I/O"))
    )
    monkeypatch.setattr(puller, "inventory_remote_bundles", AsyncMock(return_value=[]))
    monkeypatch.setattr(puller, "read_logger_status", AsyncMock(return_value=None))
    monkeypatch.setattr(puller, "sync_remote_events", AsyncMock(return_value=EventSyncResult()))
    monkeypatch.setattr(puller, "_drop_still_growing", AsyncMock())
    monkeypatch.setattr(puller, "_fit_plan_to_space", AsyncMock(return_value=None))
    monkeypatch.setattr(puller, "_record_run_completion", AsyncMock())
    monkeypatch.setattr(puller, "_sleep_window_may_close", AsyncMock(return_value=False))
    monkeypatch.setattr(puller, "close_sleep_window", AsyncMock())
    forbidden = {}
    for owner, name in (
        (puller.elevate, "ensure_root"),
        (puller.radio_coordinator, "begin"),
        (puller, "_move"),
        (puller, "_show_backup_page_during_transfer"),
        (puller, "report_event"),
    ):
        mocked = AsyncMock(side_effect=AssertionError(f"duplicate cleanup reached {name}"))
        monkeypatch.setattr(owner, name, mocked)
        forbidden[name] = mocked
    band_gate = AsyncMock(return_value=True)
    monkeypatch.setattr(puller.band, "gate", band_gate)
    monkeypatch.setattr(puller.elevate, "channel_lost", lambda: False)
    pending = AsyncMock(return_value=None)
    monkeypatch.setattr(puller.radio_coordinator, "pending_recovery_address", pending)
    name = "20261026192900_camera_0.ts"
    body = b"already durable complete footage" * 50
    local = tmp_path / name
    local.write_bytes(body)
    remote = RemoteFile(name, len(body), 1, "/card/DCIM/Video")
    inventory = AsyncMock(return_value=[remote])
    monkeypatch.setattr(adb, "inventory_all", inventory)

    async def verify(address, source, item, expected_sha256, **kwargs):
        assert status.running, "cleanup must retain the original run's ownership"
        assert (tmp_path / item.name).read_bytes() == body
        assert expected_sha256 == hashlib.sha256(body).hexdigest()
        return adb.VerifiedRemoteFile(
            address=address,
            source=source,
            name=item.name,
            size=item.size,
            mtime=item.mtime,
            identity="fixture-pinned-identity",
            sha256=expected_sha256,
        )

    verify_remote = AsyncMock(side_effect=verify)
    delete_remote = AsyncMock(return_value=True)
    monkeypatch.setattr(adb, "verify_remote_file", verify_remote)
    monkeypatch.setattr(adb, "delete_verified_remote_file", delete_remote)
    yield SimpleNamespace(
        options=options,
        status=status,
        budget=budget,
        path=local,
        body=body,
        remote=remote,
        inventory=inventory,
        removed=removed,
        safe=safe,
        clock=clock,
        pending=pending,
        verify=verify_remote,
        delete=delete_remote,
        forbidden=forbidden,
        band_gate=band_gate,
        info=UnitInfo("unit:5555", UnitState.DEVICE, "/card/DCIM/Video"),
        root=tmp_path,
    )
    get_obd_transfer_status().set_logger(None)


async def test_duplicate_only_run_verifies_full_copy_without_starting_a_transfer(duplicate_unit):
    unit = duplicate_unit
    result = await puller.run_pull(info=unit.info)

    assert result.state is RunState.IDLE
    assert result.files == result.bytes == 0
    assert unit.path.read_bytes() == unit.body
    unit.verify.assert_awaited_once()
    unit.delete.assert_awaited_once()
    unit.band_gate.assert_awaited_once_with(unit.info.address, allow_adjustments=False)
    for call in unit.forbidden.values():
        call.assert_not_called()
    assert not unit.status.running
    assert unit.status.snapshot()["files_done"] == 0
    assert unit.status.snapshot()["bytes_done"] == 0


@pytest.mark.parametrize(
    "refusal",
    ["disabled", "unknown_clock", "unknown_budget", "short_budget", "unsafe", "recovery", "band"],
)
async def test_duplicate_cleanup_refusals_never_hash_or_unlink(duplicate_unit, refusal):
    unit = duplicate_unit
    if refusal == "disabled":
        unit.options["delete_after_verify"] = False
    elif refusal == "unknown_clock":
        unit.clock.return_value = None
    elif refusal == "unknown_budget":
        unit.budget["remaining"] = None
    elif refusal == "short_budget":
        unit.budget["remaining"] = 15.0
    elif refusal == "unsafe":
        unit.safe.return_value = (False, "unsafe footage volume")
    elif refusal == "recovery":
        unit.pending.return_value = unit.info.address
    else:
        unit.band_gate.return_value = False

    result = await puller.run_pull(info=unit.info)

    assert result.state is RunState.IDLE
    assert result.files == result.bytes == 0
    unit.verify.assert_not_awaited()
    unit.delete.assert_not_awaited()
    assert unit.path.read_bytes() == unit.body
    if refusal in {"disabled", "unknown_clock"}:
        unit.band_gate.assert_not_awaited()


async def test_operator_cancellation_does_not_start_duplicate_hashing(duplicate_unit):
    unit = duplicate_unit

    async def inventory(*args):
        unit.status.cancel()
        return [unit.remote]

    unit.inventory.side_effect = inventory
    result = await puller.run_pull(info=unit.info)

    assert result.state is RunState.CANCELLED
    unit.verify.assert_not_awaited()
    unit.delete.assert_not_awaited()
    assert unit.path.read_bytes() == unit.body


@pytest.mark.parametrize("selection", ["camera_filter", "removed_tombstone"])
async def test_nonselected_duplicate_candidates_are_not_reclaimed(duplicate_unit, selection):
    unit = duplicate_unit
    if selection == "camera_filter":
        unit.options["camera_filter"] = "camera_1"
    else:
        unit.removed.return_value = {unit.remote.name}

    result = await puller.run_pull(info=unit.info)

    assert result.state is RunState.IDLE
    unit.verify.assert_not_awaited()
    unit.delete.assert_not_awaited()
    assert unit.path.exists()


@pytest.mark.parametrize("hold", ["band", "adb_restart", "logger_owner"])
async def test_a_postponed_normal_transfer_cannot_fall_through_to_duplicate_cleanup(
    duplicate_unit, monkeypatch, hold
):
    unit = duplicate_unit
    unit.inventory.return_value = [
        unit.remote,
        RemoteFile("20261027112500_camera_0.ts", 100, 1, unit.info.source),
    ]
    unit.band_gate.return_value = hold != "band"
    if hold != "band":
        monkeypatch.setattr(puller.elevate, "ensure_root", AsyncMock(return_value=False))
    if hold == "adb_restart":
        monkeypatch.setattr(puller.elevate, "channel_lost", lambda: True)
    elif hold == "logger_owner":
        monkeypatch.setattr(puller.radios, "new_quieting_allowed", lambda: True)
        monkeypatch.setattr(
            puller, "read_logger_status", AsyncMock(return_value={"ownership_enabled": True})
        )

    result = await puller.run_pull(info=unit.info)

    assert result.state is RunState.IDLE
    assert result.files == result.bytes == 0
    unit.verify.assert_not_awaited()
    unit.delete.assert_not_awaited()
    unit.forbidden["_move"].assert_not_awaited()
    assert unit.path.read_bytes() == unit.body


async def test_duplicate_only_cleanup_waits_for_staging_thread_ownership(
    duplicate_unit, monkeypatch
):
    unit = duplicate_unit
    entered = threading.Event()
    release = threading.Event()

    def clean(staging):
        entered.set()
        assert release.wait(3)

    monkeypatch.setattr(puller, "_clean", clean)
    task = asyncio.create_task(puller.run_pull(info=unit.info))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        await asyncio.sleep(0.03)
        assert unit.status.running
        assert not task.done()
        unit.verify.assert_not_awaited()
    finally:
        release.set()
        result = await asyncio.wait_for(task, timeout=3)
    assert result.state is RunState.IDLE
    unit.verify.assert_awaited_once()


async def normal_backup(unit, monkeypatch, *, cleanup_error: bool):
    events = []
    restore_entered = asyncio.Event()
    release_restore = asyncio.Event()
    name = "20261027112500_camera_0.ts"
    payload = b"newly copied footage" * 30
    remote = RemoteFile(name, len(payload), 1, unit.info.source)
    unit.inventory.return_value = [unit.remote, remote]
    monkeypatch.setattr(puller.band, "gate", AsyncMock(return_value=True))
    monkeypatch.setattr(puller.elevate, "ensure_root", AsyncMock(return_value=False))
    monkeypatch.setattr(puller.radios, "new_quieting_allowed", lambda: True)
    monkeypatch.setattr(puller.radios, "QUIET_AFTER_ONLINE_S", 0)
    monkeypatch.setattr(puller, "report_event", AsyncMock())
    # This run tests radio restoration, so leave courtesy display work out of its path.
    unit.options["show_on_unit"] = False
    monkeypatch.setattr(puller, "_reclaim", AsyncMock(return_value=1))
    monkeypatch.setattr(puller, "_clean", lambda staging: events.append("staging-finished"))
    from app.workers import scheduler

    monkeypatch.setattr(scheduler, "request_ingest_scan", lambda: events.append("scan-requested"))

    class Transition:
        def raise_if_lease_lost(self):
            pass

        async def mark_obd_transfer_complete(self):
            pass

        async def capture_and_quiet(self):
            events.append("quiet")

        async def restore(self, **kwargs):
            events.append("restore-started")
            restore_entered.set()
            await release_restore.wait()
            events.append("restored")
            return True

    monkeypatch.setattr(puller.radio_coordinator, "begin", AsyncMock(return_value=Transition()))

    async def move(info, files, *, staging, on_chunk_completed, **kwargs):
        assert [item.name for item in files] == [name]
        events.append("transfer")
        staging.mkdir(exist_ok=True)
        (staging / name).write_bytes(payload)
        await on_chunk_completed(files)
        return transport.TransferResult(
            files=[name], bytes_received=len(payload), seconds=0.1, complete=True
        )

    monkeypatch.setattr(puller, "_move", move)

    async def cleanup(info, footage, candidates, **kwargs):
        events.append("duplicate-cleanup")
        assert unit.status.running
        assert [item.name for item in candidates] == [unit.remote.name]
        assert (unit.root / name).read_bytes() == payload
        assert events.index("staging-finished") < events.index("transfer")
        assert events.index("restored") < events.index("duplicate-cleanup")
        if cleanup_error:
            raise OSError("optional cleanup unavailable")
        return duplicate_cleanup.DuplicateCleanupResult(
            deleted_files=1, deleted_bytes=unit.remote.size
        )

    cleanup_call = AsyncMock(side_effect=cleanup)
    monkeypatch.setattr(duplicate_cleanup, "cleanup_verified_duplicates", cleanup_call)
    task = asyncio.create_task(puller.run_pull(info=unit.info))
    try:
        await asyncio.wait_for(restore_entered.wait(), timeout=2)
        cleanup_call.assert_not_awaited()
        assert unit.status.running
    finally:
        release_restore.set()
        result = await asyncio.wait_for(task, timeout=3)
    cleanup_call.assert_awaited_once()
    assert result.state is RunState.OK
    assert result.error is None
    assert result.files == 1
    assert result.bytes == len(payload)
    assert unit.path.read_bytes() == unit.body
    assert (unit.root / name).read_bytes() == payload
    assert not unit.status.running


async def test_normal_backup_cleans_duplicates_after_restoration_without_changing_copy_totals(
    duplicate_unit, monkeypatch
):
    await normal_backup(duplicate_unit, monkeypatch, cleanup_error=False)


async def test_optional_cleanup_fault_cannot_fail_durable_backup(duplicate_unit, monkeypatch):
    await normal_backup(duplicate_unit, monkeypatch, cleanup_error=True)
