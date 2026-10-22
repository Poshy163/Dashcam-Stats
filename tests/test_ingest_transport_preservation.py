"""Discovery must not sever the fresh evidence that authorizes a maintenance backup."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from app.core.process_lock import try_acquire
from app.ingest import adb, elevate, maintenance, puller
from app.ingest.models import UnitState

ADDRESS = "unit:5555"
BOOT = "01234567-1234-1234-1234-012345678901"
NORMAL = '<hierarchy><node package="com.android.launcher" /></hierarchy>'


@pytest.fixture(autouse=True)
def isolated_transports(monkeypatch):
    monkeypatch.setattr(adb, "_transport_controls", {})


async def test_healthy_discovery_never_disconnects_existing_session(monkeypatch):
    state = AsyncMock(return_value=UnitState.DEVICE)
    probe = AsyncMock(return_value=True)
    reconnect = AsyncMock(side_effect=AssertionError("healthy transport was disconnected"))
    source = AsyncMock(return_value="/card/Video")
    monkeypatch.setattr(adb, "state", state)
    monkeypatch.setattr(adb, "probe_shell", probe)
    monkeypatch.setattr(adb, "reconnect", reconnect)
    monkeypatch.setattr(adb, "resolve_source", source)

    info = await adb.describe(ADDRESS)

    assert info.online and info.source == "/card/Video"
    state.assert_awaited_once_with(ADDRESS)
    probe.assert_awaited_once_with(ADDRESS)
    reconnect.assert_not_awaited()


@pytest.mark.parametrize("initial", [UnitState.DEVICE, UnitState.OFFLINE])
async def test_stale_transport_can_reconnect_before_any_maintenance(initial, monkeypatch):
    state = AsyncMock(side_effect=[initial, UnitState.DEVICE])
    probe = AsyncMock(side_effect=[False, True] if initial is UnitState.DEVICE else [True])
    reconnect = AsyncMock()
    monkeypatch.setattr(adb, "state", state)
    monkeypatch.setattr(adb, "probe_shell", probe)
    monkeypatch.setattr(adb, "reconnect", reconnect)
    monkeypatch.setattr(adb, "resolve_source", AsyncMock(return_value="/card/Video"))

    assert (await adb.describe(ADDRESS)).online
    reconnect.assert_awaited_once_with(ADDRESS)


async def test_reconnect_with_stale_device_state_does_not_claim_online(monkeypatch):
    monkeypatch.setattr(adb, "state", AsyncMock(return_value=UnitState.DEVICE))
    monkeypatch.setattr(adb, "probe_shell", AsyncMock(return_value=False))
    reconnect = AsyncMock()
    source = AsyncMock()
    monkeypatch.setattr(adb, "reconnect", reconnect)
    monkeypatch.setattr(adb, "resolve_source", source)
    assert not (await adb.describe(ADDRESS)).online
    reconnect.assert_awaited_once()
    source.assert_not_awaited()


async def test_manual_and_polling_discovery_reuse_maintenance_channel(monkeypatch):
    owner = await adb.preserve_transport("unit")
    raw = AsyncMock(side_effect=AssertionError("discovery tore down maintenance"))
    monkeypatch.setattr(adb, "_adb", raw)
    monkeypatch.setattr(adb, "state", AsyncMock(return_value=UnitState.DEVICE))
    monkeypatch.setattr(adb, "probe_shell", AsyncMock(return_value=True))
    monkeypatch.setattr(adb, "resolve_source", AsyncMock(return_value="/card/Video"))
    monkeypatch.setattr(
        puller, "_get", lambda key, default="": "unit" if key == "unit_adb_address" else default
    )
    try:
        # Both the manual run and poller use this central discovery path.
        assert (await puller.probe_unit()).online
        assert (await puller.probe_unit()).online
        assert adb.transport_preserved(ADDRESS)
        with pytest.raises(adb.TransportPreserved):
            await adb.reconnect(ADDRESS)
        with pytest.raises(adb.TransportPreserved):
            await adb.root(ADDRESS)
        raw.assert_not_awaited()
    finally:
        owner.release()


@pytest.mark.parametrize("initial", [UnitState.DEVICE, UnitState.OFFLINE, "error"])
async def test_failed_read_during_hold_never_disconnects_or_claims_online(initial, monkeypatch):
    owner = await adb.preserve_transport(ADDRESS)
    state = (
        AsyncMock(side_effect=adb.AdbError("unavailable"))
        if initial == "error"
        else AsyncMock(return_value=initial)
    )
    raw = AsyncMock(side_effect=AssertionError("protected channel was disconnected"))
    monkeypatch.setattr(adb, "state", state)
    monkeypatch.setattr(adb, "probe_shell", AsyncMock(return_value=False))
    monkeypatch.setattr(adb, "_adb", raw)
    try:
        assert not (await adb.describe(ADDRESS)).online
        assert adb.transport_preserved(ADDRESS)
        raw.assert_not_awaited()
    finally:
        owner.release()


async def test_hold_acquired_after_discovery_check_still_prevents_disconnect(monkeypatch):
    original = adb.reconnect
    owners = []

    async def raced_reconnect(address):
        owners.append(await adb.preserve_transport(address))
        await original(address)

    raw = AsyncMock(side_effect=AssertionError("reconnect won the ownership race"))
    monkeypatch.setattr(adb, "_adb", raw)
    monkeypatch.setattr(adb, "state", AsyncMock(return_value=UnitState.DEVICE))
    monkeypatch.setattr(adb, "probe_shell", AsyncMock(return_value=False))
    monkeypatch.setattr(adb, "reconnect", raced_reconnect)
    try:
        assert not (await adb.describe(ADDRESS)).online
        raw.assert_not_awaited()
    finally:
        for owner in owners:
            owner.release()


async def test_preservation_waits_for_in_flight_reconnect_before_power_proofs(monkeypatch):
    disconnected, finish = asyncio.Event(), asyncio.Event()
    calls = []

    async def raw(*args, **kwargs):
        calls.append(args)
        if args[0] == "disconnect":
            disconnected.set()
            await finish.wait()
        return adb.AdbResult(0, "", "")

    monkeypatch.setattr(adb, "_adb", raw)
    reconnect = asyncio.create_task(adb.reconnect(ADDRESS))
    await disconnected.wait()
    acquiring = asyncio.create_task(adb.preserve_transport("unit"))
    await asyncio.sleep(0)
    assert not acquiring.done()
    finish.set()
    await reconnect
    owner = await acquiring
    try:
        with pytest.raises(adb.TransportPreserved):
            await adb.reconnect(ADDRESS)
        assert calls == [("disconnect", ADDRESS), ("connect", ADDRESS)]
    finally:
        owner.release()


@pytest.mark.parametrize("cancel", [False, True])
async def test_timed_out_or_cancelled_guard_acquisition_leaks_no_ownership(cancel):
    control = adb._transport_control(ADDRESS)
    await control.lock.acquire()
    task = asyncio.create_task(adb.preserve_transport(ADDRESS, timeout=0.01 if not cancel else 3))
    if cancel:
        await asyncio.sleep(0)
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else adb.AdbError):
        await task
    control.lock.release()
    assert not adb.transport_preserved(ADDRESS)
    owner = await adb.preserve_transport(ADDRESS)
    owner.release()
    owner.release()
    assert not adb.transport_preserved(ADDRESS)


async def test_nested_ownership_and_address_normalization(monkeypatch):
    first = await adb.preserve_transport("unit")
    second = await adb.preserve_transport(ADDRESS)
    first.release()
    assert adb.transport_preserved(ADDRESS)
    second.release()
    second.release()
    assert not adb.transport_preserved("unit")
    raw = AsyncMock(return_value=adb.AdbResult(0, "", ""))
    monkeypatch.setattr(adb, "_adb", raw)
    await adb.reconnect(ADDRESS)
    assert raw.await_count == 2


@pytest.mark.parametrize("uid", ["0", "2000", None])
async def test_elevation_only_reads_existing_uid_during_hold(uid, monkeypatch):
    owner = await adb.preserve_transport(ADDRESS)
    monkeypatch.setattr(elevate, "_wanted", lambda: True)
    monkeypatch.setattr(elevate, "_impossible", set())
    monkeypatch.setattr(elevate, "_cooldown", {})
    monkeypatch.setattr(elevate, "_probe_uid", AsyncMock(return_value=uid))
    root = AsyncMock(side_effect=AssertionError("elevation restarted a maintenance channel"))
    wait = AsyncMock(side_effect=AssertionError("elevation waited for an unrequested restart"))
    monkeypatch.setattr(adb, "root", root)
    monkeypatch.setattr(elevate, "_wait_for_daemon", wait)
    try:
        assert await elevate.ensure_root(ADDRESS) is (uid == "0")
        root.assert_not_awaited()
        wait.assert_not_awaited()
        assert not elevate._channel_lost
    finally:
        owner.release()


async def test_elevation_race_does_not_restart_or_enter_reconnect_wait(monkeypatch):
    original = adb.root
    owners = []

    async def raced_root(address):
        owners.append(await adb.preserve_transport(address))
        return await original(address)

    monkeypatch.setattr(elevate, "_wanted", lambda: True)
    monkeypatch.setattr(elevate, "_impossible", set())
    monkeypatch.setattr(elevate, "_cooldown", {})
    monkeypatch.setattr(elevate, "_probe_uid", AsyncMock(return_value="0"))
    monkeypatch.setattr(adb, "root", raced_root)
    raw = AsyncMock(side_effect=AssertionError("daemon restart escaped the guard"))
    monkeypatch.setattr(adb, "_adb", raw)
    try:
        assert await elevate.ensure_root(ADDRESS)
        raw.assert_not_awaited()
    finally:
        for owner in owners:
            owner.release()


@pytest.mark.parametrize("stage", ["_attest", "_power", "_screen"])
async def test_every_early_preparation_refusal_releases_transport(
    stage, monkeypatch, tmp_path, app_config
):
    controller = maintenance.MaintenanceController(tmp_path / "cancelled.json")
    monkeypatch.setattr(maintenance, "_attest", AsyncMock())
    monkeypatch.setattr(
        maintenance, "_power", AsyncMock(return_value=maintenance.PowerProof(BOOT, 1000))
    )
    monkeypatch.setattr(maintenance, "_screen", AsyncMock(return_value=NORMAL))

    async def refuse(*args):
        assert adb.transport_preserved(ADDRESS)
        raise maintenance.MaintenanceRefused("fresh proof refused")

    monkeypatch.setattr(maintenance, stage, refuse)
    with pytest.raises(maintenance.MaintenanceRefused):
        await controller.start(ADDRESS, 300)
    assert not adb.transport_preserved(ADDRESS)
    assert controller._fence is None


async def test_invalid_duration_releases_transport_before_any_device_proof(
    monkeypatch, tmp_path, app_config
):
    controller = maintenance.MaintenanceController(tmp_path / "cancelled.json")
    attest = AsyncMock(side_effect=AssertionError("invalid duration reached the device"))
    monkeypatch.setattr(maintenance, "_attest", attest)
    with pytest.raises(maintenance.MaintenanceRefused, match="duration"):
        await controller.start(ADDRESS, 59)
    assert not adb.transport_preserved(ADDRESS)
    assert controller._fence is None
    attest.assert_not_awaited()


@pytest.mark.parametrize("cancel", [False, True])
async def test_failed_or_cancelled_guard_acquisition_releases_process_fence(
    cancel, monkeypatch, tmp_path, app_config
):
    controller = maintenance.MaintenanceController(tmp_path / "cancelled.json")
    entered = asyncio.Event()

    async def acquire(*args, **kwargs):
        entered.set()
        if cancel:
            await asyncio.Event().wait()
        raise adb.AdbError("discovery busy")

    monkeypatch.setattr(adb, "preserve_transport", acquire)
    task = asyncio.create_task(controller.start(ADDRESS, 300))
    await entered.wait()
    if cancel:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else adb.AdbError):
        await task
    assert controller._fence is None
    assert not adb.transport_preserved(ADDRESS)
    fence = try_acquire(app_config.data_dir / ".ingest-maintenance.lock")
    assert fence is not None
    fence.release()


async def test_pending_disk_cleanup_keeps_process_fence_but_frees_transport(
    monkeypatch, tmp_path, app_config
):
    controller = maintenance.MaintenanceController(tmp_path / "cancelled.json")
    fence_path = app_config.data_dir / ".ingest-maintenance.lock"
    controller._fence = try_acquire(fence_path)
    assert controller._fence is not None
    controller._transport_preservation = await adb.preserve_transport(ADDRESS)
    controller._state = "active"
    pending = asyncio.create_task(asyncio.Event().wait())
    controller._persistence_task = pending
    monkeypatch.setattr(puller, "stop_for_maintenance", AsyncMock(return_value=True))
    monkeypatch.setattr(maintenance, "_RECORD_IO_TIMEOUT_S", 0.01)
    try:
        await controller._end_hold("inactive", "stopped")
        assert controller._state == "restoring"
        assert not adb.transport_preserved(ADDRESS)
        assert controller._fence is not None
        assert try_acquire(fence_path) is None
        assert not pending.done()
    finally:
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        controller._fence.release()
        controller._fence = None


async def test_active_guard_is_released_before_restoration_reconnect(
    monkeypatch, tmp_path, app_config
):
    path = tmp_path / "cancelled.json"
    record = maintenance.CancellationRecord(BOOT, BOOT, 900_000, native_verified=True)
    path.write_text(json.dumps(record.document()), encoding="utf-8")
    controller = maintenance.MaintenanceController(path)
    monkeypatch.setattr(maintenance, "_attest", AsyncMock())
    monkeypatch.setattr(
        maintenance, "_power", AsyncMock(return_value=maintenance.PowerProof(BOOT, 1000))
    )
    monkeypatch.setattr(maintenance, "_screen", AsyncMock(return_value=NORMAL))
    monkeypatch.setattr(controller, "_publish", AsyncMock())
    monkeypatch.setattr(adb, "shell", AsyncMock(return_value=""))
    raw = AsyncMock(return_value=adb.AdbResult(0, "", ""))
    monkeypatch.setattr(adb, "_adb", raw)

    async def restore():
        assert controller._state == "restoring"
        assert not controller.snapshot()["active"]
        assert not adb.transport_preserved(ADDRESS)
        await adb.reconnect(ADDRESS)
        return True

    monkeypatch.setattr(puller, "stop_for_maintenance", restore)
    try:
        assert (await controller.start(ADDRESS, 300))["active"]
        assert adb.transport_preserved(ADDRESS)
        assert not (await controller.stop())["active"]
        assert raw.await_count == 2
        assert controller._fence is None
        assert path.read_text() == json.dumps(record.document())
    finally:
        if controller._task is not None and not controller._task.done():
            await controller.stop()


async def test_empty_snapshot_is_unavailable_not_countdown_absence(monkeypatch):
    monkeypatch.setattr(maintenance, "_ensure_ui_helper", AsyncMock())
    monkeypatch.setattr(adb, "shell", AsyncMock(return_value=""))
    with pytest.raises(maintenance.MaintenanceTransient) as exc:
        await maintenance._screen(ADDRESS)
    assert exc.value.code == "screen_empty"
    assert exc.value.stage == "screen"


async def test_empty_power_is_unavailable_but_partial_power_stays_refused(monkeypatch):
    monkeypatch.setattr(adb, "shell", AsyncMock(side_effect=["", "partial"]))
    with pytest.raises(maintenance.MaintenanceTransient) as exc:
        await maintenance._power(ADDRESS)
    assert exc.value.code == "power_empty"
    with pytest.raises(maintenance.MaintenanceRefused) as exc:
        await maintenance._power(ADDRESS)
    assert not isinstance(exc.value, maintenance.MaintenanceTransient)


async def test_nonempty_malformed_snapshot_is_still_a_semantic_refusal(monkeypatch):
    monkeypatch.setattr(maintenance, "_ensure_ui_helper", AsyncMock())
    monkeypatch.setattr(adb, "shell", AsyncMock(return_value="<hierarchy>"))
    with pytest.raises(maintenance.MaintenanceRefused) as exc:
        maintenance.countdown_target(await maintenance._screen(ADDRESS))
    assert not isinstance(exc.value, maintenance.MaintenanceTransient)
