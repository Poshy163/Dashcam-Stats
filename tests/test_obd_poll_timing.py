from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest

from app.ingest.obd_bundle import BundleError, _validate_diagnostics, _validate_poll_timing


def timing(count=0, median=None, p95=None, maximum=0, total=0):
    return {
        "count": count,
        "retained": min(count, 256),
        "median_ms": median,
        "p95_ms": p95,
        "max_ms": maximum,
        "total_ms": total,
    }


@pytest.fixture
def payload():
    return {
        "schema_version": 1,
        "window_index": 0,
        "window_start_elapsed_ms": 0,
        "window_duration_ms": 10000,
        "flush_reason": "drive_end",
        "target_cycle_ms": 5000,
        "cycles_started": 2,
        "cycles_completed": 2,
        "overrun_count": 0,
        "cycle_work_ms": timing(2, 4150, 4200, 4200, 8300),
        "cycle_start_interval_ms": timing(1, 5000, 5000, 5000, 5000),
        "pids": [
            {
                "pid": 13,
                "attempts": 2,
                "successes": 2,
                "missing": 0,
                "malformed": 0,
                "timeouts": 0,
                "transport_errors": 0,
                "cancelled": 0,
                "cooldown_skips": 0,
                "request_ms": timing(2, 600, 650, 650, 1200),
                "value_age_at_row_ms": timing(2, 2900, 3000, 3000, 5800),
            }
        ],
    }


def validate_event(payload):
    started = datetime(2026, 9, 28, tzinfo=UTC)
    event = {
        "diagnostic_id": "drive-1-poll-0",
        "drive_id": "drive-1",
        "timestamp_utc": (started + timedelta(seconds=10)).isoformat(),
        "kind": "poll_timing",
        "payload": payload,
    }
    value = {"schema_version": 1, "drive_id": "drive-1", "events": [event]}
    return _validate_diagnostics(
        value,
        drive_id="drive-1",
        manifest_count=1,
        started=started,
        finished=started + timedelta(seconds=10),
    )


def test_poll_timing_passes_production_diagnostic_import_without_mutation(payload):
    original = deepcopy(payload)
    assert validate_event(payload)["events"][0]["payload"] == original
    assert payload == original


@pytest.mark.parametrize(
    "outcome", ["missing", "malformed", "timeouts", "transport_errors", "cancelled"]
)
def test_failed_requests_are_measured_without_inventing_value_ages(payload, outcome):
    row = payload["pids"][0]
    row["successes"] = 0
    row[outcome] = 2
    row["value_age_at_row_ms"] = timing()
    payload["flush_reason"] = "partial_failure"
    payload["cycles_completed"] = 1
    validate_event(payload)


def test_cooldown_skips_do_not_become_requests(payload):
    row = payload["pids"][0]
    row.update(
        attempts=0, successes=0, cooldown_skips=2, request_ms=timing(), value_age_at_row_ms=timing()
    )
    validate_event(payload)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda p: p.update(raw_response="41 0D 3C"),
        lambda p: p.update(schema_version=True),
        lambda p: p.update(flush_reason=[]),
        lambda p: p.update(window_duration_ms=-1),
        lambda p: p.update(cycles_completed=3),
        lambda p: p.update(pids=p["pids"] * 2),
        lambda p: p.update(pids=p["pids"] * 19),
        lambda p: p["pids"][0].update(value=60),
        lambda p: p["pids"][0].update(attempts=3),
        lambda p: p["pids"][0].update(successes=0, missing=2),
        lambda p: p["pids"][0]["request_ms"].update(retained=1),
        lambda p: p["pids"][0]["request_ms"].update(median_ms=float("nan")),
        lambda p: p["pids"][0]["request_ms"].update(p95_ms=500),
        lambda p: p["pids"][0]["request_ms"].update(total_ms=1),
        lambda p: p["pids"][0].update(request_ms=timing()),
        lambda p: p["pids"][0].update(value_age_at_row_ms=timing(0, 1, 1, 1, 1)),
    ],
)
def test_poll_timing_rejects_unbounded_private_or_inconsistent_data(payload, mutation):
    mutation(payload)
    with pytest.raises(BundleError):
        validate_event(payload)


def test_empty_final_window_is_valid(payload):
    payload.update(
        cycles_started=0,
        cycles_completed=0,
        overrun_count=0,
        cycle_work_ms=timing(),
        cycle_start_interval_ms=timing(),
        pids=[],
    )
    _validate_poll_timing(payload)
