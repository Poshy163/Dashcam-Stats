"""A parked-only backup hands the radios back the moment the ignition comes on."""

from __future__ import annotations

import asyncio

import pytest
from test_ingest_radio_coordinator import fake_controller as fake_controller

from app.db.models import IngestRadioTransition
from app.db.session import session_scope
from app.ingest import puller, radio_coordinator
from app.ingest.models import RunResult, RunState
from app.ingest.status import IngestStatus


def _acc_sequence(monkeypatch, *states):
    """Feed the ignition probe a sequence of readings, repeating the last one."""
    readings = list(states)
    calls = []

    async def ignition_state(address, *, timeout=10.0):
        calls.append((address, timeout))
        return readings.pop(0) if len(readings) > 1 else readings[0]

    monkeypatch.setattr(radio_coordinator.adb, "ignition_state", ignition_state)
    return calls


async def _begin(cancelled, *, stop_on_ignition):
    return await radio_coordinator.begin(
        trigger="auto",
        address="unit:5555",
        logger_status=None,
        logger_status_path=None,
        watchdog_deadline_s=300,
        lease_loss_callback=cancelled.set,
        stop_on_ignition=stop_on_ignition,
    )


@pytest.fixture
def fast_ignition_poll(monkeypatch):
    monkeypatch.setattr(radio_coordinator, "IGNITION_POLL_S", 0.001)
    # Keep the lease heartbeat out of the way; these tests are about the ACC line.
    monkeypatch.setattr(radio_coordinator, "HEARTBEAT_INTERVAL_S", 3600.0)


async def test_ignition_on_stops_the_backup_in_order_and_restores(
    db_session, fake_controller, fast_ignition_poll, monkeypatch
):
    calls = _acc_sequence(monkeypatch, "off", "off", "on")
    cancelled = asyncio.Event()
    transition = await _begin(cancelled, stop_on_ignition=True)
    try:
        error = await asyncio.wait_for(transition.wait_for_lease_loss(), timeout=2)
        assert isinstance(error, radio_coordinator.RadioIgnitionOn)
        # The orderly window-ending path, not a safety failure.
        assert isinstance(error, radio_coordinator.RadioWindowEnding)
        assert error.reason_code == "ignition_on"
        assert cancelled.is_set() and transition.process_fence.held
        assert len(calls) >= 3
        assert all(address == "unit:5555" for address, _ in calls)
        assert all(timeout == radio_coordinator.IGNITION_PROBE_TIMEOUT_S for _, timeout in calls)
        assert await transition.restore()
        async with session_scope() as session:
            row = await session.get(IngestRadioTransition, transition.id)
            assert row.phase == "complete" and not row.active and not row.recovery_required
    finally:
        await transition.close()


@pytest.mark.parametrize("reading", ["off", "unknown"])
async def test_an_off_or_unreadable_ignition_never_stops_the_backup(
    db_session, fake_controller, fast_ignition_poll, monkeypatch, reading
):
    calls = _acc_sequence(monkeypatch, reading)
    cancelled = asyncio.Event()
    transition = await _begin(cancelled, stop_on_ignition=True)
    try:
        while len(calls) < 5:
            await asyncio.sleep(0.005)
        assert not transition.lease_lost and not cancelled.is_set()
    finally:
        await transition.close()


async def test_the_watch_is_off_when_backups_may_run_with_the_ignition_on(
    db_session, fake_controller, fast_ignition_poll, monkeypatch
):
    calls = _acc_sequence(monkeypatch, "on")
    cancelled = asyncio.Event()
    transition = await _begin(cancelled, stop_on_ignition=False)
    try:
        await asyncio.sleep(0.05)
        assert calls == []
        assert not transition.lease_lost and not cancelled.is_set()
    finally:
        await transition.close()


async def test_closing_the_transition_stops_the_ignition_watch(
    db_session, fake_controller, fast_ignition_poll, monkeypatch
):
    calls = _acc_sequence(monkeypatch, "off")
    transition = await _begin(asyncio.Event(), stop_on_ignition=True)
    while not calls:
        await asyncio.sleep(0.005)
    await transition.close()
    seen = len(calls)
    await asyncio.sleep(0.05)
    assert len(calls) == seen


@pytest.fixture
def running_status(monkeypatch):
    status = IngestStatus()
    assert status.try_begin()
    status.set_backlog(5, 500)
    monkeypatch.setattr(puller, "get_status", lambda: status)
    return status


@pytest.mark.parametrize("files,bytes_", [(0, 0), (2, 200)])
def test_ignition_stop_is_reported_as_an_orderly_end_with_its_own_reason(
    running_status, files, bytes_
):
    puller._cancel_for_radio_stop(radio_coordinator.RadioIgnitionOn())
    assert running_status.cancel_event.is_set()
    assert running_status.cancel_reason == "native_sleep_window_ending"
    assert running_status.cancel_detail == puller._IGNITION_ON_MESSAGE
    result = puller._classify_cancellation(
        RunResult(state=RunState.CANCELLED, files=files, bytes=bytes_, error="backup cancelled")
    )
    assert result.state is (RunState.PARTIAL if files else RunState.IDLE)
    assert (result.files, result.bytes) == (files, bytes_)
    assert result.error == puller._IGNITION_ON_MESSAGE
    assert running_status.backlog_files == 5


def test_an_ignition_stop_during_preparation_does_not_claim_copying(running_status):
    puller._cancel_for_radio_stop(radio_coordinator.RadioIgnitionOn())
    result = puller._classify_cancellation(puller._radio_shutdown_hold(puller._IGNITION_ON_MESSAGE))
    assert result.state is RunState.IDLE
    assert result.files == result.bytes == 0
    assert result.error == puller._IGNITION_ON_MESSAGE


def test_operator_stop_still_outranks_an_ignition_stop(running_status):
    puller._cancel_for_radio_stop(radio_coordinator.RadioIgnitionOn())
    running_status.cancel()
    result = puller._classify_cancellation(
        RunResult(state=RunState.PARTIAL, files=2, bytes=200, error=puller._IGNITION_ON_MESSAGE)
    )
    assert result.state is RunState.CANCELLED
    assert running_status.cancel_reason is None


def test_a_window_end_still_reports_the_sleep_message(running_status):
    puller._cancel_for_radio_stop(radio_coordinator.RadioWindowEnding())
    result = puller._classify_cancellation(RunResult(state=RunState.CANCELLED))
    assert result.error == puller._NATIVE_WINDOW_END_MESSAGE


def test_the_ignition_stop_cannot_be_built_with_a_sleep_state():
    error = radio_coordinator.RadioIgnitionOn()
    assert error.recovery_state == "ignition_on"
    assert "ignition" in str(error)
    with pytest.raises(ValueError):
        radio_coordinator.RadioWindowEnding("ignition_on")
