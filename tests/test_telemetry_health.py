"""Telemetry-health units, coverage and classification must explain the same recordings."""

from __future__ import annotations

from app.db.models import Recording, RecordingState, StageState
from app.db.session import session_scope
from app.pipeline.revisions import CURRENT_REVISIONS


def recording(name: str, **changes) -> Recording:
    values = {
        "rel_path": f"{name}.ts",
        "filename": f"{name}.ts",
        "size_bytes": 1,
        "state": RecordingState.COMPLETED,
        "telemetry_state": StageState.DONE,
        "telemetry_revision": CURRENT_REVISIONS["telemetry"],
        "telemetry_point_count": 4,
        "gps_point_count": 4,
    }
    return Recording(**(values | changes))


async def test_status_reasons_and_coverage_use_explicit_units_and_current_results(client):
    cases = [
        recording("healthy"),
        recording("warning", telemetry_problem_count=2, gps_recovered_count=2),
        recording(
            "gap",
            gps_point_count=3,
            gps_gap_count=1,
            gps_ocr_gap_count=1,
            telemetry_problem_count=1,
        ),
        recording("no-fix", gps_point_count=0, gps_gap_count=1, gps_no_fix_count=4),
        recording("outdated", telemetry_revision="telemetry-v4"),
        recording("legacy", telemetry_revision=None),
        recording(
            "pending",
            telemetry_state=StageState.PENDING,
            telemetry_revision="telemetry-v4",
            telemetry_problem_count=1,
        ),
        recording("empty", telemetry_point_count=0, gps_point_count=0),
        # Journey validation changes surviving/rejected counts after the original gap
        # rollup. A zero original gap/problem count must not keep this recording healthy.
        recording("later-rejection", gps_point_count=3, gps_rejected_count=1),
        recording("missing-counters", gps_point_count=0),
        recording("hidden", ignored=True, telemetry_problem_count=100),
        recording("missing-file", file_missing=True, telemetry_problem_count=100),
    ]
    async with session_scope() as session:
        session.add_all(cases)

    response = await client.get("/api/telemetry/quality")
    assert response.status_code == 200
    data = response.json()
    assert {
        name: data[name] for name in ("recordings", "healthy", "degraded", "no_fix", "pending")
    } == {
        "recordings": 10,
        "healthy": 1,
        "degraded": 7,
        "no_fix": 1,
        "pending": 1,
    }
    assert data["outdated_recordings"] == 2
    assert data["empty_recordings"] == 1
    assert data["total_gaps"] == 2  # Runs, not missing samples or recordings.
    assert data["paired_recoveries"] == 2  # Points, not recordings.
    assert data["gps_coverage"] == {
        "recordings": 6,
        "total_points": 24,
        "accepted_points": 14,
        "no_fix_points": 4,
        "ocr_unreadable_points": 1,
        "rejected_points": 1,
        "full_coverage_recordings": 2,
        "gap_recordings": 4,
        "warning_recordings": 2,
        "warning_only_recordings": 1,
        "problem_samples": 3,
    }
    issues = {item["filename"]: item for item in data["issues"]}
    expected = {
        "warning.ts": ("degraded", ["telemetry_warnings"]),
        "gap.ts": ("degraded", ["gps_unreadable", "telemetry_warnings"]),
        "no-fix.ts": ("no_fix", ["gps_no_fix"]),
        "outdated.ts": ("degraded", ["analysis_outdated"]),
        "legacy.ts": ("degraded", ["analysis_outdated"]),
        "pending.ts": ("pending", ["analysis_pending"]),
        "empty.ts": ("degraded", ["no_samples"]),
        "later-rejection.ts": ("degraded", ["gps_rejected"]),
        "missing-counters.ts": ("degraded", ["gps_coverage_incomplete"]),
    }
    assert set(issues) == set(expected)
    for name, (status, reasons) in expected.items():
        assert (issues[name]["status"], issues[name]["reasons"]) == (status, reasons)
    assert issues["warning.ts"]["fixes"] == issues["warning.ts"]["points"] == 4
    assert issues["warning.ts"]["problems"] == 2
    assert data["issue_total"] == 9
    assert data["issue_limit"] == 250


async def test_issue_table_is_bounded_but_totals_include_every_visible_recording(client):
    async with session_scope() as session:
        session.add_all(
            recording(f"warning-{index}", telemetry_problem_count=1) for index in range(260)
        )
        session.add(recording("long-gap", gps_point_count=0, gps_gap_count=1, gps_longest_gap_s=90))
        session.add(recording("healthy"))

    data = (await client.get("/api/telemetry/quality")).json()
    assert data["recordings"] == 262
    assert data["degraded"] == data["issue_total"] == 261
    assert data["healthy"] == 1
    assert len(data["issues"]) == data["issue_limit"] == 250
    assert data["issues"][0]["filename"] == "long-gap.ts"
    assert data["gps_coverage"]["warning_only_recordings"] == 260
    assert data["gps_coverage"]["recordings"] == 262
    assert all(item["reasons"] for item in data["issues"])


async def test_empty_library_has_no_coverage_denominator_or_health_claim(client):
    data = (await client.get("/api/telemetry/quality")).json()
    assert data["recordings"] == data["healthy"] == data["issue_total"] == 0
    assert data["gps_coverage"]["recordings"] == data["gps_coverage"]["total_points"] == 0
    assert data["issues"] == []
