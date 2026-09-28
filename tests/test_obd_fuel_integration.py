"""Fuel estimates must span scheduled sparse MAF rows without bridging missing data."""

from datetime import UTC, datetime, timedelta

import pytest

from app.db.models import OBDSample
from app.ingest.obd_reconciliation import _Rollup
from app.obd.summary import calculate_summary


def sample(seconds, rate=None, *, sequence=None, drive=1, status="live", quality=None):
    return OBDSample(
        drive_db_id=drive,
        captured_at=datetime(2026, 9, 28, tzinfo=UTC) + timedelta(seconds=seconds),
        sequence=int(seconds / 5) if sequence is None else sequence,
        ecu_data_status=status,
        estimated_fuel_rate_l_h=rate,
        quality_json=quality or {"transport": "ok", "parser": "ok", "missing_pids": []},
    )


def integrate(*rows):
    result = _Rollup()
    for row in rows:
        result.observe(row)
    return result


def test_scheduled_sparse_maf_covers_all_observed_intervals():
    rows = [sample(t, 6.0 if t % 15 == 0 else None) for t in range(0, 31, 5)]
    result = integrate(*rows)
    # Old arithmetic covered only 0..5 and 15..20: 0.016667 L, one third of this.
    assert result.estimated_fuel_used_l == pytest.approx(6 * 30 / 3600)
    assert result.fuel_evidence


def test_new_rate_only_changes_intervals_after_its_observation():
    result = integrate(
        sample(0, 6), sample(5), sample(10), sample(15, 12), sample(20), sample(25), sample(30)
    )
    assert result.estimated_fuel_used_l == pytest.approx((6 * 15 + 12 * 15) / 3600)


def test_expiry_clips_final_interval_and_never_extrapolates_after_last_row():
    result = integrate(sample(0, 6), *(sample(t) for t in range(5, 46, 5)))
    assert result.estimated_fuel_used_l == pytest.approx(6 * 22.5 / 3600)
    assert not integrate(sample(0, 6)).fuel_evidence


@pytest.mark.parametrize(
    "breaking_row",
    [
        sample(15, quality={"missing_pids": [0x10]}),
        sample(15, quality={"transport": "failed_after_partial"}),
        sample(15, status="unavailable"),
        sample(15, float("nan")),
        sample(15, float("inf")),
        sample(15, -1),
        sample(15, sequence=4),
        sample(15, drive=2),
        sample(20, sequence=3),
        sample(10, sequence=3),
        sample(5, sequence=3),
    ],
)
def test_invalid_or_discontinuous_row_clears_the_rate(breaking_row):
    result = integrate(sample(0, 6), sample(5), sample(10), breaking_row, sample(25))
    assert result.estimated_fuel_used_l == pytest.approx(6 * 10 / 3600)


def test_failed_maf_blocks_a_contradictory_present_estimate():
    result = integrate(sample(0, 6), sample(5, 6, quality={"missing_pids": [0x10]}), sample(10))
    assert not result.fuel_evidence


def test_unrelated_pid_failures_do_not_discard_a_valid_fuel_estimate():
    result = integrate(
        sample(0, 6), sample(5, quality={"parser": "partial", "missing_pids": [0x15]})
    )
    assert result.estimated_fuel_used_l == pytest.approx(6 * 5 / 3600)


def test_fresh_rate_after_a_gap_restarts_without_backfilling_the_gap():
    result = integrate(sample(0, 6), sample(10, 12, sequence=1), sample(15, sequence=2))
    assert result.estimated_fuel_used_l == pytest.approx(12 * 5 / 3600)


def test_zero_rate_is_evidence_and_drive_instances_do_not_share_state():
    result = integrate(sample(0, 0), sample(5))
    assert result.fuel_evidence
    assert result.estimated_fuel_used_l == 0
    assert not integrate(sample(0), sample(5)).fuel_evidence


@pytest.mark.parametrize("failed_maf", [False, True])
def test_python_export_summary_matches_canonical_sparse_fuel(failed_maf):
    rows = [
        sample(
            t,
            6.0 if t % 15 == 0 and not (t == 15 and failed_maf) else None,
            quality={"missing_pids": [0x10]} if t == 15 and failed_maf else None,
        )
        for t in range(0, 31, 5)
    ]
    exported = [
        {
            "drive_id": "test",
            "sequence": row.sequence,
            "timestamp_utc": row.captured_at.isoformat(),
            "ecu_data_status": row.ecu_data_status,
            "quality": row.quality_json,
            "estimated_fuel_rate": row.estimated_fuel_rate_l_h,
        }
        for row in rows
    ]
    summary = calculate_summary(
        {
            "drive_id": "test",
            "start_time_utc": exported[0]["timestamp_utc"],
            "finish_time_utc": exported[-1]["timestamp_utc"],
            "clean_end": True,
        },
        exported,
        [],
    )
    assert summary["estimated_fuel_used_l"] == pytest.approx(integrate(*rows).estimated_fuel_used_l)
