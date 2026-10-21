"""Database diagnostic dumps must not expose radio lease/report credentials."""

from types import SimpleNamespace

import pytest

from app.api.routes import ingest
from app.ingest.obd_storage import redact


@pytest.mark.parametrize("marker", ["[SQL:", "[parameters:", "(Background on this error at:"])
def test_database_details_are_removed_without_losing_the_failure(marker):
    error = f"OperationalError: database is locked\n{marker} lease-secret report-secret"
    assert redact(error) == "OperationalError: database is locked"


def test_normal_errors_still_preserve_useful_context_and_hide_bearers():
    assert redact("camera offline") == "camera offline"
    assert "secret-token-1234" not in redact("Bearer secret-token-1234")


async def test_live_ingest_status_redacts_old_cached_error_values(monkeypatch):
    error = "database is locked [SQL: INSERT INTO leases] [parameters: secret-value]"
    snapshot = {"state": "error", "last_error": error, "radio_quieting_hold_reason": error}
    monkeypatch.setattr(
        ingest, "get_status", lambda: SimpleNamespace(snapshot=lambda: snapshot.copy())
    )
    result = await ingest.ingest_status()
    assert result["last_error"] == result["radio_quieting_hold_reason"] == "database is locked"
    assert snapshot["last_error"] == error


async def test_history_redacts_previously_persisted_database_parameters():
    row = SimpleNamespace(
        id=1,
        started_at=None,
        finished_at=None,
        trigger="auto",
        state="error",
        files_transferred=0,
        bytes_transferred=0,
        throughput_mbs_avg=0,
        error="database is locked [SQL: INSERT INTO leases] [parameters: secret-value]",
    )

    class Session:
        async def execute(self, _query):
            return SimpleNamespace(
                scalar=lambda: 1, scalars=lambda: SimpleNamespace(all=lambda: [row])
            )

    page = SimpleNamespace(page=1, page_size=10, offset=0, pages=lambda _total: 1)
    result = await ingest.ingest_history(Session(), page)
    assert result["items"][0]["error"] == "database is locked"
    assert "secret-value" in row.error
