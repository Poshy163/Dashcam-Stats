"""The analysis preparation phase reports an absolute, bounded waiting reason."""

import pytest

from app.ingest.models import Phase, RunResult, RunState
from app.ingest.status import IngestStatus


def test_analysis_wait_is_visible_only_until_its_phase_or_run_ends():
    status = IngestStatus()
    assert status.try_begin()
    status.set_phase(Phase.ANALYSING)
    status.set_analysis_wait("2026-10-23T08:30:00Z", "awaiting_parked_clip")
    snapshot = status.snapshot()
    assert snapshot["phase"] == "analysing"
    assert snapshot["analysis_wait_reason"] == "awaiting_parked_clip"
    assert snapshot["analysis_wait_until_utc"] == "2026-10-23T08:30:00+00:00"
    status.set_phase(Phase.PREPARING)
    assert status.snapshot()["analysis_wait_until_utc"] is None
    assert status.snapshot()["analysis_wait_reason"] is None
    status.set_phase(Phase.ANALYSING)
    status.set_analysis_wait("2026-10-23T08:30:00+00:00", "pending_analysis")
    status.finish(RunResult(state=RunState.CANCELLED))
    assert status.snapshot()["analysis_wait_until_utc"] is None
    assert status.snapshot()["analysis_wait_reason"] is None
    assert status.try_begin()
    assert status.snapshot()["analysis_wait_until_utc"] is None


@pytest.mark.parametrize(
    "until,reason",
    [
        ("2026-10-23T08:30:00Z", "raw_device_reply"),
        ("2026-10-23T08:30:00", "pending_analysis"),
        (None, "pending_analysis"),
        ("2026-10-23T08:30:00Z", None),
        ("invalid", "pending_analysis"),
    ],
)
def test_invalid_analysis_wait_cannot_replace_the_last_valid_reason(until, reason):
    status = IngestStatus()
    status.set_analysis_wait("2026-10-23T08:30:00Z", "pending_analysis")
    with pytest.raises(ValueError):
        status.set_analysis_wait(until, reason)
    assert status.snapshot()["analysis_wait_reason"] == "pending_analysis"
    status.set_analysis_wait(None, None)
    assert status.snapshot()["analysis_wait_until_utc"] is None
    assert status.snapshot()["analysis_wait_reason"] is None
