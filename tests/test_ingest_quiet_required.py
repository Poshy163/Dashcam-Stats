"""Requested radio shutdown is a prerequisite for footage, including retries."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from test_ingest_preparation_lifecycle import isolated_mirror as _isolated_mirror
from test_ingest_preparation_lifecycle import prepared_unit as _prepared_unit

from app.ingest import adb, puller, radios, transport
from app.ingest.models import RemoteFile, RunState, UnitInfo, UnitState
from app.ingest.obd_transfer import get_obd_transfer_status
from app.ingest.status import get_status, reset_status_for_tests

isolated_mirror = _isolated_mirror
prepared_unit = _prepared_unit
BOOT = "09255025-8fa6-4dd4-bede-607e8a2603bc"


class Transition:
    def __init__(self, events, *, fail=None, restore_ok=True):
        self.events = events
        self.fail = fail
        self.restore_ok = restore_ok

    def raise_if_lease_lost(self):
        return None

    async def prepare_logger(self):
        self.events.append("prepare")
        if self.fail == "prepare":
            raise RuntimeError("logger acknowledgement missing")
        return SimpleNamespace(bundle_filename=None, bundle_sha256=None)

    async def mark_obd_transfer_complete(self):
        self.events.append("durable")
        if self.fail == "durable":
            raise RuntimeError("OBD durability checkpoint failed")

    async def capture_and_quiet(self):
        self.events.append("quiet")
        if self.fail == "quiet":
            raise RuntimeError("hotspot shutdown could not be verified")

    async def verify_footage_quiet(self):
        self.events.append("verify")
        if self.fail == "verify":
            raise puller.radio_coordinator.RadioTransitionError("quiet proof lost")

    async def restore(self, **_kwargs):
        self.events.append("restore")
        return self.restore_ok


@pytest.fixture
def quiet_run(prepared_unit, monkeypatch):
    prepared_unit.options["quiet_radios"] = True
    get_obd_transfer_status().set_logger(None)
    monkeypatch.setattr(radios, "QUIET_AFTER_ONLINE_S", 0)
    monkeypatch.setattr(radios, "new_quieting_allowed", lambda: True)
    events = []
    real_move = puller._move

    async def move(*args, **kwargs):
        events.append("copy")
        return await real_move(*args, **kwargs)

    moved = AsyncMock(side_effect=move)
    resume = AsyncMock(side_effect=AssertionError("a held run must not resume footage"))
    monkeypatch.setattr(puller, "_move", moved)
    monkeypatch.setattr(puller, "_can_resume_stream", resume)
    return SimpleNamespace(unit=prepared_unit, events=events, move=moved, resume=resume)


@pytest.mark.parametrize("stage", ["claim", "prepare", "durable", "quiet"])
@pytest.mark.parametrize("restore_ok", [False, True])
async def test_failed_required_preparation_never_copies_or_resumes(
    quiet_run, monkeypatch, stage, restore_ok
):
    transition = Transition(quiet_run.events, fail=stage, restore_ok=restore_ok)
    begin = AsyncMock(return_value=transition)
    if stage == "claim":
        begin.side_effect = puller.radio_coordinator.RadioTransitionError("claim unavailable")
    if stage == "prepare":
        monkeypatch.setattr(
            puller,
            "read_logger_status",
            AsyncMock(
                return_value={
                    "schema_version": 2,
                    "state": "ecu_online",
                    "ownership_enabled": True,
                    "capabilities": [puller.obd_control.CAPABILITY],
                }
            ),
        )
    monkeypatch.setattr(puller.radio_coordinator, "begin", begin)
    result = await asyncio.wait_for(puller.run_pull(info=quiet_run.unit.info), 1)
    assert result.state is RunState.IDLE
    assert result.files == result.bytes == 0
    quiet_run.move.assert_not_awaited()
    quiet_run.resume.assert_not_awaited()
    assert not quiet_run.unit.path.exists()
    assert not get_status().running
    if stage != "claim":
        assert quiet_run.events[-1] == "restore"
        assert quiet_run.events.count("restore") == 1
    if restore_ok or stage == "claim":
        assert "Waiting for radio shutdown" in get_status().radio_quieting_hold_reason
        assert get_status().radio_quieting_hold


async def test_next_attempt_retries_shutdown_and_restores_after_success(quiet_run, monkeypatch):
    failed = Transition(quiet_run.events, fail="quiet")
    succeeding = Transition(quiet_run.events)
    begin = AsyncMock(side_effect=[failed, succeeding])
    monkeypatch.setattr(puller.radio_coordinator, "begin", begin)
    first = await puller.run_pull(info=quiet_run.unit.info)
    assert first.state is RunState.IDLE
    quiet_run.move.assert_not_awaited()
    assert quiet_run.events == ["durable", "quiet", "restore"]
    quiet_run.events.clear()
    second = await puller.run_pull(info=quiet_run.unit.info)
    assert second.state is RunState.OK
    assert quiet_run.unit.path.read_bytes() == quiet_run.unit.payload
    assert quiet_run.events.index("quiet") < quiet_run.events.index("copy")
    assert quiet_run.events[-1] == "restore"
    assert begin.await_count == 2
    assert not get_status().radio_quieting_hold


async def test_failed_restoration_blocks_next_attempt_until_recovery(quiet_run, monkeypatch):
    transition = Transition(quiet_run.events, fail="quiet", restore_ok=False)
    begin = AsyncMock(return_value=transition)
    monkeypatch.setattr(puller.radio_coordinator, "begin", begin)
    monkeypatch.setattr(puller, "_recover_before_run", AsyncMock(side_effect=[True, False]))
    assert (await puller.run_pull(info=quiet_run.unit.info)).state is RunState.IDLE
    assert (await puller.run_pull(info=quiet_run.unit.info)).state is RunState.IDLE
    begin.assert_awaited_once()
    quiet_run.move.assert_not_awaited()


@pytest.mark.parametrize("window", [None, 59, 60])
async def test_unknown_or_at_most_one_minute_never_starts_transfer(
    prepared_unit, monkeypatch, window
):
    prepared_unit.options["quiet_radios"] = True
    status = get_status()
    if window is not None:
        status.observe_unit_runtime(
            adb.RuntimeObservation(
                BOOT,
                100,
                "off",
                window,
                20,
                adb.SleepDeadlineEvidence(1, BOOT, 20, 100_000, False, 100_000, 100_000, window),
            )
        )
    begin = AsyncMock(side_effect=AssertionError("unsafe admission"))
    move = AsyncMock(side_effect=AssertionError("unsafe footage transfer"))
    monkeypatch.setattr(puller.radio_coordinator, "begin", begin)
    monkeypatch.setattr(puller, "_move", move)
    result = await asyncio.wait_for(puller.run_pull(info=prepared_unit.info), 1)
    assert result.state is RunState.IDLE
    assert "Waiting" in get_status().radio_quieting_hold_reason
    begin.assert_not_awaited()
    move.assert_not_awaited()
    assert not prepared_unit.path.exists()


async def test_explicit_opt_out_preserves_copy_with_unknown_deadline(prepared_unit, monkeypatch):
    prepared_unit.options["quiet_radios"] = False
    begin = AsyncMock(side_effect=AssertionError("opt-out must not claim radios"))
    monkeypatch.setattr(puller.radio_coordinator, "begin", begin)
    assert get_status().sleep_countdown_remaining_s() is None
    result = await puller.run_pull(info=prepared_unit.info)
    assert result.state is RunState.OK
    assert prepared_unit.path.read_bytes() == prepared_unit.payload
    begin.assert_not_awaited()


@pytest.fixture
def move_case(monkeypatch, tmp_path):
    reset_status_for_tests()
    monkeypatch.setattr(puller, "_get", lambda key, default=None: key == "quiet_radios")
    launch = AsyncMock(return_value=object())
    receive = Mock()
    monkeypatch.setattr(adb, "clear_listener", AsyncMock())
    monkeypatch.setattr(adb, "launch_listener", launch)
    monkeypatch.setattr(adb, "stop_listener", AsyncMock(return_value=True))
    monkeypatch.setattr(transport, "receive", receive)
    return SimpleNamespace(
        info=UnitInfo("unit:5555", UnitState.DEVICE, "/card"),
        files=[RemoteFile("a.ts", 10, 0), RemoteFile("b.ts", 10, 0)],
        staging=tmp_path,
        launch=launch,
        receive=receive,
    )


async def test_direct_move_cannot_bypass_requested_quieting(move_case):
    with pytest.raises(puller.radio_coordinator.RadioTransitionError, match="not been verified"):
        await puller._move(
            move_case.info,
            move_case.files,
            staging=move_case.staging,
            host="server",
            port=1234,
            timeout_s=10,
        )
    move_case.launch.assert_not_awaited()
    move_case.receive.assert_not_called()


async def test_quiet_proof_loss_prevents_next_chunk(move_case):
    lease = SimpleNamespace(
        raise_if_lease_lost=lambda: None,
        verify_footage_quiet=AsyncMock(
            side_effect=[None, puller.radio_coordinator.RadioTransitionError("quiet proof lost")]
        ),
    )
    move_case.receive.return_value = transport.TransferResult(
        files=["a.ts"], bytes_received=10, complete=True
    )
    with pytest.raises(puller.radio_coordinator.RadioTransitionError, match="quiet proof lost"):
        await puller._move(
            move_case.info,
            move_case.files,
            staging=move_case.staging,
            host="server",
            port=1234,
            timeout_s=10,
            lease=lease,
            chunk_size=1,
        )
    assert lease.verify_footage_quiet.await_count == 2
    move_case.launch.assert_awaited_once()
    move_case.receive.assert_called_once()


async def test_quiet_proof_loss_prevents_interrupted_stream_retry(move_case, monkeypatch):
    lease = SimpleNamespace(
        raise_if_lease_lost=lambda: None,
        verify_footage_quiet=AsyncMock(
            side_effect=[None, puller.radio_coordinator.RadioTransitionError("quiet proof lost")]
        ),
    )
    move_case.receive.return_value = transport.TransferResult(
        error="timed out", complete=False, retryable=True
    )
    monkeypatch.setattr(puller, "_can_resume_stream", AsyncMock(return_value=True))
    with pytest.raises(puller.radio_coordinator.RadioTransitionError, match="quiet proof lost"):
        await puller._move(
            move_case.info,
            move_case.files,
            staging=move_case.staging,
            host="server",
            port=1234,
            timeout_s=10,
            lease=lease,
        )
    assert lease.verify_footage_quiet.await_count == 2
    move_case.launch.assert_awaited_once()
    move_case.receive.assert_called_once()
