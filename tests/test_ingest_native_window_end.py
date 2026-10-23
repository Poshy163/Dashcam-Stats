"""An owned native deadline stops copying without disguising independent failures."""

import pytest

from app.ingest import puller, radio_coordinator
from app.ingest.models import RunResult, RunState
from app.ingest.status import IngestStatus


@pytest.fixture
def running_status(monkeypatch):
    status = IngestStatus()
    assert status.try_begin()
    status.set_backlog(5, 500)
    monkeypatch.setattr(puller, "get_status", lambda: status)
    return status


@pytest.mark.parametrize("proof", ["deadline_approaching", "restoring", "unit_report"])
@pytest.mark.parametrize("files,bytes_", [(0, 0), (2, 200)])
def test_native_window_end_preserves_committed_counts_and_remaining_inventory(
    running_status, proof, files, bytes_
):
    puller._cancel_for_radio_stop(radio_coordinator.RadioWindowEnding(proof))
    assert running_status.cancel_event.is_set()
    assert running_status.cancel_reason == "native_sleep_window_ending"
    result = puller._classify_cancellation(
        RunResult(state=RunState.CANCELLED, files=files, bytes=bytes_, error="backup cancelled")
    )
    assert result.state is (RunState.PARTIAL if files else RunState.IDLE)
    assert (result.files, result.bytes) == (files, bytes_)
    assert result.error == puller._NATIVE_WINDOW_END_MESSAGE
    assert running_status.backlog_files == 5
    assert running_status.backlog_bytes == 500


def test_textual_sleep_claim_from_unproven_watchdog_is_still_a_safety_failure(running_status):
    puller._cancel_for_radio_stop(
        radio_coordinator.RadioTransitionError("native sleep deadline is ending the backup window")
    )
    assert running_status.cancel_reason == "radio_safety_lease_lost"
    result = puller._classify_cancellation(RunResult(state=RunState.CANCELLED))
    assert result.state is RunState.ERROR


@pytest.mark.parametrize("files,bytes_", [(0, 0), (2, 200)])
@pytest.mark.parametrize(
    "failure",
    ["radio restoration remains pending and will be retried", "checkpoint database is locked"],
)
def test_a_window_end_does_not_hide_restoration_or_database_failure(
    running_status, files, bytes_, failure
):
    puller._cancel_for_radio_stop(radio_coordinator.RadioWindowEnding())
    result = puller._classify_cancellation(
        RunResult(state=RunState.ERROR, files=files, bytes=bytes_, error=failure)
    )
    assert result.state is (RunState.PARTIAL if files else RunState.ERROR)
    assert result.error == failure


@pytest.mark.parametrize("stop_first", [True, False])
def test_operator_stop_always_outranks_expected_native_deadline(running_status, stop_first):
    if stop_first:
        running_status.cancel()
    puller._cancel_for_radio_stop(radio_coordinator.RadioWindowEnding())
    running_status.cancel()
    puller._cancel_for_radio_stop(radio_coordinator.RadioWindowEnding("restoring"))
    result = puller._classify_cancellation(
        RunResult(
            state=RunState.PARTIAL, files=2, bytes=200, error=puller._NATIVE_WINDOW_END_MESSAGE
        )
    )
    assert result.state is RunState.CANCELLED
    assert running_status.cancel_reason is None
    assert (result.files, result.bytes) == (2, 200)


def test_preparation_window_end_does_not_claim_copying_started(running_status):
    puller._cancel_for_radio_stop(radio_coordinator.RadioWindowEnding())
    result = puller._classify_cancellation(
        puller._radio_shutdown_hold(puller._NATIVE_WINDOW_END_MESSAGE)
    )
    assert result.state is RunState.IDLE
    assert result.files == result.bytes == 0
    assert result.error == puller._NATIVE_WINDOW_END_MESSAGE
