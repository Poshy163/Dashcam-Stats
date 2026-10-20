"""A stop acknowledgement remains visible until the single-flight run finishes."""

from app.ingest.models import Phase, RunResult, RunState
from app.ingest.status import IngestStatus


def test_cancellation_snapshot_follows_run_ownership_through_cleanup():
    status = IngestStatus()
    assert not status.cancel()
    assert status.snapshot()["cancellation_requested"] is False

    assert status.try_begin()
    status.set_phase(Phase.TRANSFERRING)
    assert status.snapshot()["cancellation_requested"] is False
    assert status.cancel()
    assert status.cancel()  # Other clients can acknowledge the same stop safely.
    snapshot = status.snapshot()
    assert snapshot["cancellation_requested"] is True
    assert snapshot["state"] == "running"
    assert snapshot["phase"] == "transferring"
    assert not status.try_begin()  # Acknowledged does not mean cleanup has finished.

    status.finish(RunResult(state=RunState.CANCELLED))
    snapshot = status.snapshot()
    assert snapshot["cancellation_requested"] is False
    assert snapshot["state"] == "cancelled"
    assert not status.cancel()
    assert status.try_begin()
    assert status.snapshot()["cancellation_requested"] is False
