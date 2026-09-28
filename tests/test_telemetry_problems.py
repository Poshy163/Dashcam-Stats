"""Selected-field warnings and historical recount must agree without erasing evidence."""

from __future__ import annotations

import importlib.util
import json
from copy import deepcopy
from datetime import datetime
from pathlib import Path

import pytest
import sqlalchemy as sa

from app.osd.engine import TelemetryExtractor
from app.osd.parser import OsdReading
from app.osd.problems import PUNCTUATION_RECOVERY, effective_problems, stored_problems
from app.pipeline.telemetry_quality import quality_rollup

STAMP = datetime(2026, 9, 28, 10, 18, 58)


def reading(**changes):
    values = {
        "captured_at": STAMP,
        "lat": -34.0,
        "lon": 138.0,
        "has_fix": True,
        "speed_kmh": 20.0,
        "confidence": 0.98,
        "time_status": "valid",
        "gps_status": "valid",
        "problems": [],
    }
    values.update(changes)
    return OsdReading(**values)


def combine(*readings):
    return TelemetryExtractor._combine_candidates(
        20, [(20 + i / 2, value) for i, value in enumerate(readings)], min_confidence=0.6
    )


def quality(**changes):
    values = {
        "ocr_status": "valid",
        "time_status": "valid",
        "time_source": "overlay",
        "gps_status": "valid",
        "gps_source": "direct",
        "problems": [],
    }
    values.update(changes)
    return values


def test_successful_sibling_clock_does_not_report_failed_candidate_clock():
    sample = combine(
        reading(),
        reading(
            captured_at=None,
            time_status="parse_failed",
            confidence=0.9,
            problems=["timestamp unreadable"],
        ),
    )
    assert sample.captured_at == STAMP
    assert sample.ocr_status == sample.time_status == sample.gps_status == "valid"
    assert sample.problems == []
    assert sample.candidate_count == 2


def test_different_selected_readings_can_have_each_others_field_failures():
    sample = combine(
        reading(
            lat=None,
            lon=None,
            has_fix=False,
            gps_status="parse_failed",
            problems=["GPS fields unreadable"],
        ),
        reading(
            captured_at=None,
            time_status="parse_failed",
            confidence=0.9,
            problems=["timestamp unreadable"],
        ),
    )
    assert sample.captured_at == STAMP and sample.has_fix and sample.speed_kmh == 20
    assert sample.problems == []


def test_unresolved_field_and_unknown_warnings_stay():
    sample = combine(reading(speed_kmh=None, problems=["speed could not be parsed", "new warning"]))
    assert sample.ocr_status == "partial"
    assert sample.problems == ["speed could not be parsed", "new warning"]


def test_repair_provenance_belongs_to_selected_gps_reading():
    repaired = reading(confidence=0.9, problems=[PUNCTUATION_RECOVERY])
    assert combine(reading(), repaired).problems == []
    assert combine(repaired).problems == [PUNCTUATION_RECOVERY]


def test_no_fix_conflict_and_low_confidence_remain_problems():
    no_fix = reading(has_fix=False, lat=None, lon=None, gps_status="no_fix")
    sample = combine(reading(), no_fix)
    assert sample.gps_status == "no_fix" and not sample.has_fix
    assert any("position discarded" in problem for problem in sample.problems)
    assert "OCR confidence low" in combine(reading(confidence=0.2)).problems


@pytest.mark.parametrize(
    "problem",
    [
        "timestamp unreadable",
        "implausible year 9999",
        "invalid date/time: minute must be in 0..59",
        "GPS fields unreadable",
        "coordinates could not be parsed",
        "coordinate precision inconsistent (lon '138.6', lat '-34.0'); digits were lost or gained in the read",
        "coordinate sign ambiguous; refused rather than risk a wrong hemisphere",
        "coordinate out of range (lat=340.0, lon=138.0)",
        "coordinate is not finite (lat=nan, lon=138.0)",
        "speed could not be parsed",
        "implausible speed 999.0 km/h",
        "no recognisable overlay content",
    ],
)
def test_known_resolved_parser_failures_require_success_evidence(problem):
    assert effective_problems([problem]) == [problem]
    assert effective_problems([problem], time_valid=True, gps_valid=True, speed_valid=True) == []


@pytest.mark.parametrize(
    "problem",
    [
        PUNCTUATION_RECOVERY,
        "latitude sign recovered from recording consensus",
        "GPS interpolated across 3s OCR/parse gap",
        "coordinate failed final database validation",
        "overlay clock differed from canonical timeline by 3600.000s",
        "timestamp moved backwards; clock rejected",
        "1000 m in 1 s implies 3600 km/h",
        "new or unrecognised warning",
        "timestamp unreadable but suspicious new suffix",
    ],
)
def test_repairs_validation_and_unknown_diagnostics_are_never_blanket_cleared(problem):
    assert effective_problems([problem], time_valid=True, gps_valid=True, speed_valid=True) == [
        problem
    ]


@pytest.mark.parametrize(
    "changes",
    [
        {"time_source": "timeline"},
        {"time_status": "rejected"},
        {"time_status": None},
    ],
)
def test_historical_clock_recovery_is_not_successful_overlay_evidence(changes):
    q = quality(problems=["timestamp unreadable"], **changes)
    assert stored_problems(q, has_fix=True, speed_kmh=20) == q["problems"]


@pytest.mark.parametrize(
    "source,status",
    [
        ("paired_camera", "recovered"),
        ("interpolated", "interpolated"),
        ("context_repaired", "recovered"),
        ("none", "rejected"),
        (None, "valid"),
    ],
)
def test_historical_gps_recovery_cannot_clear_original_failure(source, status):
    q = quality(gps_source=source, gps_status=status, problems=["GPS fields unreadable"])
    assert stored_problems(q, has_fix=True, speed_kmh=20) == q["problems"]


def test_historical_speed_copied_from_partner_is_not_direct_success():
    q = quality(
        gps_source="paired_camera",
        gps_status="recovered",
        ocr_status="partial",
        problems=["speed could not be parsed"],
    )
    assert stored_problems(q, has_fix=True, speed_kmh=20) == q["problems"]
    # A rejected paired position clears its GPS source but leaves any copied speed.
    q.update(gps_source="none", gps_status="rejected")
    assert stored_problems(q, has_fix=False, speed_kmh=20) == q["problems"]
    for speed in (None, float("nan"), float("inf"), 999, "20", True):
        assert stored_problems(quality(problems=q["problems"]), has_fix=True, speed_kmh=speed)


def test_rollup_preserves_raw_provenance_and_actual_failed_ocr():
    rows = [
        {
            "t_offset_s": 0,
            "has_fix": True,
            "speed_kmh": 20,
            "quality_json": quality(problems=["timestamp unreadable"]),
        },
        {
            "t_offset_s": 1,
            "has_fix": True,
            "speed_kmh": 20,
            "quality_json": quality(ocr_status="failed", problems=[]),
        },
        {
            "t_offset_s": 2,
            "has_fix": False,
            "quality_json": quality(
                gps_status="rejected", gps_source="none", problems=["GPS fields unreadable"]
            ),
        },
    ]
    before = deepcopy(rows)
    assert quality_rollup(rows) == (1, 1.0, 2, 0, 0, 1)
    assert rows == before


def load_migration():
    path = (
        Path(__file__).parents[1]
        / "backend/migrations/versions/0024_reconcile_candidate_problems.py"
    )
    spec = importlib.util.spec_from_file_location("recount_0024", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_is_bounded_idempotent_conservative_and_preserves_all_point_data():
    migration = load_migration()
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE recordings (id INTEGER PRIMARY KEY, telemetry_problem_count INTEGER, telemetry_point_count INTEGER)"
        )
        conn.exec_driver_sql(
            "CREATE TABLE telemetry_points (recording_id INTEGER, quality_json TEXT, has_fix INTEGER, speed_kmh FLOAT, lat FLOAT, raw_text TEXT)"
        )
        # Cross the 128-recording boundary so a shrinking result set cannot skip IDs.
        for ident in range(1, 132):
            conn.exec_driver_sql("INSERT INTO recordings VALUES (?,1,1)", (ident,))
            q = quality(problems=["timestamp unreadable"])
            if ident == 129:
                q["problems"].append("real unresolved failure")
            raw = json.dumps(q)
            if ident == 130:
                raw = "malformed json"
            if ident == 131:
                raw = json.dumps({"problems": "bad scalar"})
            conn.exec_driver_sql(
                "INSERT INTO telemetry_points VALUES (?,?,1,20,-34,'keep raw')", (ident, raw)
            )
        conn.exec_driver_sql("INSERT INTO recordings VALUES (132,1,2)")
        conn.exec_driver_sql("INSERT INTO recordings VALUES (133,1,0)")
        # Missing provenance is not evidence that a formerly faulty row was healthy.
        for ident, incomplete in enumerate(
            (
                {},
                {"ocr_status": "valid"},
                {"problems": []},
                {"problems": [], "ocr_status": "unrecognised"},
            ),
            start=134,
        ):
            conn.exec_driver_sql("INSERT INTO recordings VALUES (?,1,1)", (ident,))
            conn.exec_driver_sql(
                "INSERT INTO telemetry_points VALUES (?,?,1,20,-34,'keep raw')",
                (ident, json.dumps(incomplete)),
            )
        before = conn.exec_driver_sql("SELECT * FROM telemetry_points").all()
        preview = migration.recount(conn, apply=False)
        assert preview == {"examined": 137, "changed": 128, "skipped": 8, "problems_removed": 128}
        assert (
            conn.exec_driver_sql("SELECT SUM(telemetry_problem_count) FROM recordings").scalar()
            == 137
        )
        assert migration.recount(conn) == preview
        assert conn.exec_driver_sql("SELECT * FROM telemetry_points").all() == before
        assert conn.exec_driver_sql(
            "SELECT id FROM recordings WHERE telemetry_problem_count > 0 ORDER BY id"
        ).scalars().all() == [129, 130, 131, 132, 133, 134, 135, 136, 137]
        assert migration.recount(conn)["changed"] == 0
