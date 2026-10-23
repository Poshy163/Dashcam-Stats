"""Exercise native-window endings through the real pre-transfer run and cleanup."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from test_ingest_quiet_required import Transition
from test_ingest_quiet_required import isolated_mirror as _isolated_mirror
from test_ingest_quiet_required import prepared_unit as _prepared_unit
from test_ingest_quiet_required import quiet_run as _quiet_run

from app.ingest import puller, radio_coordinator, radios
from app.ingest.models import RunState
from app.ingest.status import get_status

isolated_mirror = _isolated_mirror
prepared_unit = _prepared_unit
quiet_run = _quiet_run


@pytest.mark.parametrize("origin", ["controller", "coordinator"])
@pytest.mark.parametrize("proof", ["deadline_approaching", "restoring"])
@pytest.mark.parametrize("restore_ok", [True, False])
async def test_native_deadline_during_capture_is_benign_only_after_verified_restore(
    quiet_run, monkeypatch, origin, proof, restore_ok
):
    error = (
        radios.WatchdogWindowEnding(proof)
        if origin == "controller"
        else radio_coordinator.RadioWindowEnding(proof)
    )
    transition = Transition(quiet_run.events)

    async def capture():
        quiet_run.events.append("quiet")
        raise error

    async def restore(*, error=None):
        quiet_run.events.append("restore")
        # A low-level native proof must reach durable restoration as the same
        # expected cause, even if no background heartbeat has observed it yet.
        assert isinstance(error, radio_coordinator.RadioWindowEnding)
        assert error.recovery_state == proof
        return restore_ok

    transition.capture_and_quiet = capture
    transition.restore = AsyncMock(side_effect=restore)
    monkeypatch.setattr(puller.radio_coordinator, "begin", AsyncMock(return_value=transition))
    result = await asyncio.wait_for(puller.run_pull(info=quiet_run.unit.info), 5)

    assert result.state is (RunState.IDLE if restore_ok else RunState.ERROR)
    assert result.files == result.bytes == 0
    if restore_ok:
        assert result.error == puller._NATIVE_WINDOW_END_MESSAGE
    else:
        assert "radio recovery could not be verified" in result.error
        assert result.error != puller._NATIVE_WINDOW_END_MESSAGE
    assert get_status().cancel_reason == "native_sleep_window_ending"
    assert get_status().state is result.state
    assert not get_status().running
    assert not get_status().radio_quieting_hold
    assert quiet_run.events == ["durable", "quiet", "restore"]
    transition.restore.assert_awaited_once()
    quiet_run.move.assert_not_awaited()
    quiet_run.resume.assert_not_awaited()
    assert not quiet_run.unit.path.exists()


@pytest.mark.parametrize("restore_ok", [True, False])
async def test_operator_stop_during_native_preparation_restore_remains_terminal(
    quiet_run, monkeypatch, restore_ok
):
    transition = Transition(quiet_run.events)

    async def capture():
        quiet_run.events.append("quiet")
        raise radios.WatchdogWindowEnding("restoring")

    async def restore(**kwargs):
        quiet_run.events.append("restore")
        get_status().cancel()
        return restore_ok

    transition.capture_and_quiet = capture
    transition.restore = restore
    monkeypatch.setattr(puller.radio_coordinator, "begin", AsyncMock(return_value=transition))
    result = await asyncio.wait_for(puller.run_pull(info=quiet_run.unit.info), 5)
    assert result.state is RunState.CANCELLED
    assert get_status().cancel_reason is None
    assert result.files == result.bytes == 0
    quiet_run.move.assert_not_awaited()
    assert quiet_run.events == ["durable", "quiet", "restore"]


async def test_native_capture_does_not_override_prior_dead_watchdog_evidence(
    quiet_run, monkeypatch
):
    transition = Transition(quiet_run.events)
    failure = "detached on-unit radio recovery watchdog was lost"

    async def capture():
        quiet_run.events.append("quiet")
        puller._cancel_for_radio_stop(radio_coordinator.RadioTransitionError(failure))
        raise radios.WatchdogWindowEnding("deadline_approaching")

    transition.capture_and_quiet = capture
    monkeypatch.setattr(puller.radio_coordinator, "begin", AsyncMock(return_value=transition))
    result = await asyncio.wait_for(puller.run_pull(info=quiet_run.unit.info), 5)
    assert result.state is RunState.ERROR
    assert failure in result.error
    assert get_status().cancel_reason == "radio_safety_lease_lost"
    assert result.files == result.bytes == 0
    quiet_run.move.assert_not_awaited()
    assert quiet_run.events == ["durable", "quiet", "restore"]


async def test_identical_untyped_native_message_still_requires_radio_shutdown(
    quiet_run, monkeypatch
):
    transition = Transition(quiet_run.events)
    error = RuntimeError("native sleep deadline is ending the backup window")

    async def capture():
        quiet_run.events.append("quiet")
        raise error

    transition.capture_and_quiet = capture
    transition.restore = AsyncMock(return_value=True)
    monkeypatch.setattr(puller.radio_coordinator, "begin", AsyncMock(return_value=transition))
    result = await asyncio.wait_for(puller.run_pull(info=quiet_run.unit.info), 5)
    assert result.state is RunState.IDLE
    assert result.error != puller._NATIVE_WINDOW_END_MESSAGE
    assert get_status().cancel_reason is None
    assert get_status().radio_quieting_hold
    assert "Waiting for radio shutdown" in get_status().radio_quieting_hold_reason
    transition.restore.assert_awaited_once_with(error=error)
    quiet_run.move.assert_not_awaited()


@pytest.mark.parametrize("saved", [False, True])
@pytest.mark.parametrize("restore_ok", [True, False])
async def test_transfer_window_end_waits_for_restore_and_preserves_durable_arrivals(
    quiet_run, monkeypatch, saved, restore_ok
):
    transition = Transition(quiet_run.events, restore_ok=restore_ok)

    async def interrupted_move(*args, **kwargs):
        if saved:
            await quiet_run.move(*args, **kwargs)
        error = radio_coordinator.RadioWindowEnding("deadline_approaching")
        puller._cancel_for_radio_stop(error)
        raise error

    monkeypatch.setattr(puller.radio_coordinator, "begin", AsyncMock(return_value=transition))
    monkeypatch.setattr(puller, "_move", interrupted_move)
    result = await asyncio.wait_for(puller.run_pull(info=quiet_run.unit.info), 5)
    assert result.state is (
        RunState.PARTIAL if saved else RunState.IDLE if restore_ok else RunState.ERROR
    )
    assert result.files == int(saved)
    assert result.bytes == (len(quiet_run.unit.payload) if saved else 0)
    if restore_ok:
        assert result.error == puller._NATIVE_WINDOW_END_MESSAGE
    else:
        assert "radio restoration remains pending" in result.error
        assert result.error != puller._NATIVE_WINDOW_END_MESSAGE
    assert quiet_run.events[-1] == "restore"
    assert quiet_run.events.count("restore") == 1
    assert get_status().state is result.state
    assert not get_status().running
    assert get_status().backlog_files == (0 if saved else 1)
    if saved:
        assert quiet_run.unit.path.read_bytes() == quiet_run.unit.payload
    else:
        assert not quiet_run.unit.path.exists()
