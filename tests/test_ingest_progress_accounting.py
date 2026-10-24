"""One run retains all received work across footage replans and OBD handoffs."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from test_ingest_preparation_lifecycle import isolated_mirror as isolated_mirror
from test_ingest_preparation_lifecycle import prepared_unit as prepared_unit
from test_ingest_quiet_required import Transition
from test_obd_server_import import make_bundle
from test_quiet_analysis_scheduling import native_observation

from app.ingest import obd_transfer, parked_filter, puller, radios, transport
from app.ingest.models import DeltaPlan, RemoteFile, RunResult, RunState, UnitInfo, UnitState
from app.ingest.status import IngestStatus, get_status


def _files(prefix: str, count: int, size: int) -> list[RemoteFile]:
    quotient, remainder = divmod(size, count)
    return [RemoteFile(f"{prefix}_{i}", quotient + (i < remainder), 0) for i in range(count)]


def _receive(status: IngestStatus, files: list[RemoteFile]) -> None:
    for item in files:
        status.add_bytes(item.size)
        status.file_done(item.name)


def test_analysis_replan_retains_the_seven_obd_files_and_615145_bytes_from_run_619():
    status = IngestStatus()
    assert status.try_begin()
    footage = _files("video.ts", 80, 4_505_082_352)
    pending = _files("pending.ts", 12, 700_000_000)
    bundles = _files("drive.obd2.zip", 7, 615_145)
    status.plan(DeltaPlan(files=[*footage, *pending], backlog_files=92))
    status.extend_plan(DeltaPlan(files=bundles), scope="obd")
    _receive(status, bundles)
    _receive(status, footage)

    # Only delivered footage remains in wanted at the analysis batch boundary.
    delivered = DeltaPlan(
        files=footage,
        backlog_files=92,
        backlog_bytes=5_205_082_352,
        policy_deferred_files=2,
        policy_deferred_bytes=120_000_000,
        policy_deferred_until_utc="2026-10-24T05:50:00+00:00",
    )
    for _ in range(3):
        status.plan(delivered)
        snapshot = status.snapshot()
        assert snapshot["files_done"] == snapshot["files_total"] == 87
        assert snapshot["bytes_done"] == snapshot["bytes_total"] == 4_505_697_497
        assert snapshot["backlog_files"] == 92
        assert snapshot["backlog_bytes"] == 5_205_082_352
        assert snapshot["policy_deferred_files"] == 2
        assert snapshot["policy_deferred_bytes"] == 120_000_000


def test_space_replan_preserves_obd_and_completed_work_but_removes_unstarted_footage():
    status = IngestStatus()
    assert status.try_begin()
    footage = _files("video.ts", 3, 300)
    bundles = _files("drive.obd2.zip", 2, 70)
    status.plan(DeltaPlan(files=footage, backlog_files=3, backlog_bytes=300, active_skipped=2))
    status.extend_plan(DeltaPlan(files=bundles), scope="obd")
    assert status.active_skipped == 2, "OBD inventory is not a new footage inventory"
    _receive(status, bundles)
    _receive(status, footage[:1])
    status.plan(DeltaPlan(files=footage[1:2], backlog_files=3, backlog_bytes=300, active_skipped=2))
    assert (status.files_done, status.files_total) == (3, 4)
    assert (status.bytes_done, status.bytes_total) == (170, 270)
    assert (status.backlog_files, status.backlog_bytes) == (3, 300)


def test_sweeps_retries_and_repeated_handoffs_do_not_double_count_or_lose_retry_bytes():
    status = IngestStatus()
    assert status.try_begin()
    first, second, third = _files("video.ts", 3, 300)
    bundle = RemoteFile("drive.obd2.zip", 70, 0)
    status.plan(DeltaPlan(files=[first]))
    status.extend_plan(DeltaPlan(files=[bundle]), scope="obd")
    _receive(status, [bundle, first])
    status.extend_plan(DeltaPlan(files=[first, second]))
    status.extend_plan(DeltaPlan(files=[second]))
    status.add_bytes(25)
    status.account_for_retry(25)
    _receive(status, [second])
    status.extend_plan(DeltaPlan(files=[third]))
    for _ in range(3):
        status.plan(DeltaPlan(files=[first, second, third]))
        # A repeated OBD inventory and completion callback are the same work.
        status.extend_plan(DeltaPlan(files=[bundle]), scope="obd")
        status.file_done(bundle.name)
        assert (status.files_done, status.files_total) == (3, 4)
        assert (status.bytes_done, status.bytes_total) == (295, 395)
    _receive(status, [third])
    status.plan(DeltaPlan(files=[]))
    assert (status.files_done, status.files_total) == (4, 4)
    assert status.bytes_done == status.bytes_total == 395


def test_obd_before_first_video_plan_and_late_obd_both_survive_replacement():
    status = IngestStatus()
    assert status.try_begin()
    first, second = _files("drive.obd2.zip", 2, 70)
    status.extend_plan(DeltaPlan(files=[first]), scope="obd")
    _receive(status, [first])
    assert not status.backlog_known
    status.plan(DeltaPlan(files=[], backlog_files=0))
    status.extend_plan(DeltaPlan(files=[first, second]), scope="obd")
    _receive(status, [second])
    status.plan(DeltaPlan(files=[]))
    assert (status.files_done, status.files_total) == (2, 2)
    assert status.bytes_done == status.bytes_total == 70
    assert status.backlog_known and status.backlog_files == status.backlog_bytes == 0


def test_new_run_drops_all_prior_scopes_completion_and_retry_accounting():
    status = IngestStatus()
    assert status.try_begin()
    bundle = RemoteFile("drive.obd2.zip", 70, 0)
    status.extend_plan(DeltaPlan(files=[bundle]), scope="obd")
    _receive(status, [bundle])
    status.account_for_retry(20)
    status.finish(RunResult(state=RunState.OK, files=1, bytes=70))
    assert status.try_begin()
    status.plan(DeltaPlan(files=[RemoteFile("video.ts", 100, 0)]))
    assert (status.files_done, status.files_total) == (0, 1)
    assert (status.bytes_done, status.bytes_total) == (0, 100)


async def test_real_obd_transfer_scope_survives_replan_after_validation_and_durable_store(
    app_config, db_session, monkeypatch, tmp_path
):
    from sqlalchemy import func, select

    from app.db.models import OBDBundle

    status = IngestStatus()
    assert status.try_begin()
    footage = RemoteFile("video.ts", 100, 0)
    status.plan(DeltaPlan(files=[footage], backlog_files=1, backlog_bytes=100))
    paths = [make_bundle(tmp_path, f"drive_progress_{i}") for i in range(7)]
    bodies = {path.name: path.read_bytes() for path in paths}
    remote = [RemoteFile(name, len(body), 0) for name, body in bodies.items()]
    monkeypatch.setattr(obd_transfer, "read_logger_status", AsyncMock(return_value=None))
    monkeypatch.setattr(obd_transfer.adb, "clear_listener", AsyncMock())
    monkeypatch.setattr(obd_transfer.adb, "launch_listener", AsyncMock())
    monkeypatch.setattr(obd_transfer.adb, "stop_listener", AsyncMock())
    monkeypatch.setattr(obd_transfer, "write_verification_receipt", AsyncMock())
    monkeypatch.setattr(obd_transfer, "_delete_remote_if_hash", AsyncMock())

    def receive(_host, _port, staging, *, on_file_started, on_file_done, on_bytes, **_kwargs):
        for name, body in bodies.items():
            on_file_started(name)
            (staging / name).write_bytes(body)
            on_bytes(len(body))
            on_file_done(name)
        return transport.TransferResult(
            files=list(bodies), bytes_received=sum(map(len, bodies.values())), complete=True
        )

    monkeypatch.setattr(obd_transfer.transport, "receive", receive)
    result = await obd_transfer.sync_remote_bundles(
        UnitInfo("unit:5555", UnitState.DEVICE, "/card"),
        ingest_status=status,
        remote=remote,
        config=app_config,
    )
    assert result.complete and result.failed == 0 and result.copied == 7
    assert await db_session.scalar(select(func.count(OBDBundle.id))) == 7
    _receive(status, [footage])
    status.plan(DeltaPlan(files=[footage], backlog_files=0, backlog_bytes=0))
    assert status.files_done == status.files_total == 8
    assert status.bytes_done == status.bytes_total == 100 + result.bytes
    assert status.backlog_files == status.backlog_bytes == 0


@pytest.mark.parametrize("handoff", ["chunk", "sweep"])
async def test_actual_analysis_batch_handoff_keeps_obd_progress_and_durable_result(
    prepared_unit, monkeypatch, handoff
):
    unit = prepared_unit
    unit.options.update(skip_parked_footage=True, quiet_radios=True, sweep_passes=2)
    original = unit.inventory.return_value[0]
    later = RemoteFile("20260812120100_camera_0.ts", original.size, 0)
    pair = [RemoteFile(f"20261023000200_camera_{camera}.ts", 3, 0) for camera in (0, 1)]
    until = datetime.now(UTC) + timedelta(seconds=120)
    events = []
    transition = Transition(events)
    unit.inventory.return_value = [original, later]
    bundles = _files("drive.obd2.zip", 7, 615_145)
    monkeypatch.setattr(puller, "inventory_remote_bundles", AsyncMock(return_value=bundles))
    monkeypatch.setattr(puller.radio_coordinator, "begin", AsyncMock(return_value=transition))
    monkeypatch.setattr(radios, "new_quieting_allowed", lambda: True)
    monkeypatch.setattr(radios, "QUIET_AFTER_ONLINE_S", 0)
    monkeypatch.setattr(
        puller, "read_logger_status", AsyncMock(return_value={"ownership_enabled": False})
    )
    monkeypatch.setattr(puller.movement, "inspect_before_plan", AsyncMock())
    monkeypatch.setattr(puller, "QUIET_ANALYSIS_BATCH_CHECK_S", 0)
    state = get_status()
    state.observe_unit_runtime(native_observation())

    async def sync(_info, *, ingest_status, **_kwargs):
        ingest_status.extend_plan(DeltaPlan(files=bundles), scope="obd")
        _receive(ingest_status, bundles)
        return obd_transfer.OBDTransferResult(copied=7, bytes=615_145, complete=True)

    monkeypatch.setattr(puller, "sync_remote_bundles", sync)

    async def policy(*_args, **_kwargs):
        return parked_filter.FilterResult(
            [original, later],
            deferred=[(item, until) for item in pair] if "copy" in events else [],
            analysis_ready="copy" in events,
        )

    monkeypatch.setattr(puller, "_filter_parked_inventory", policy)

    async def move(_info, files, *, staging, on_chunk_completed, yield_after_chunk, **_kwargs):
        events.append("copy")
        delivered = [original] if handoff == "chunk" else files
        staging.mkdir(exist_ok=True)
        for item in delivered:
            (staging / item.name).write_bytes(unit.payload)
        _receive(state, delivered)
        await on_chunk_completed(delivered)
        if handoff == "chunk":
            assert await yield_after_chunk()
        return transport.TransferResult(
            files=[item.name for item in delivered],
            bytes_received=sum(item.size for item in delivered),
            complete=True,
        )

    monkeypatch.setattr(puller, "_move", move)
    result = await asyncio.wait_for(puller.run_pull(info=unit.info), timeout=2)
    expected_videos = 1 if handoff == "chunk" else 2
    assert result.state is RunState.OK and result.files == expected_videos + 7
    assert result.bytes == expected_videos * original.size + 615_145
    snapshot = state.snapshot()
    assert snapshot["files_done"] == snapshot["files_total"] == result.files
    assert snapshot["bytes_done"] == snapshot["bytes_total"] == result.bytes
    assert snapshot["policy_deferred_files"] == 2
    assert snapshot["backlog_files"] == 2 + (handoff == "chunk")
    assert events[-1] == "restore"
    assert not (unit.path.parent / pair[0].name).exists()
