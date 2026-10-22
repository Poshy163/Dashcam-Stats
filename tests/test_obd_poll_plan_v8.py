"""The opt-in plan retains receipt times and leaves historical plans unchanged."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from test_obd_poll_timing import timing
from test_obd_server_import import make_bundle

from app.api.routes.obd_import import drive_series
from app.db.models import OBDDrive, OBDSample
from app.db.session import get_session_factory
from app.ingest.obd_bundle import (
    BundleError,
    _validate_sample,
    store_validated_bundle,
    validate_bundle,
)
from app.ingest.obd_reconciliation import (
    _Rollup,
    lifecycle_status,
    reconcile_all_drives,
    reconcile_drive_projection,
    specs_for_poll_plan,
)
from app.obd.summary import calculate_summary

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures/obd_poll_plan_v8_calculations.json").read_text()
)


def parsed(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def exported_summary(case):
    return calculate_summary(
        {**case, "drive_id": "timing-fixture", "clean_end": True}, case["samples"], []
    )


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=lambda case: case["name"])
def test_receipt_time_calculation_fixture_matches_streaming_projection(case):
    summary = exported_summary(case)
    rollup = _Rollup(
        nominal_cycle_s=3 if case["poll_plan_version"] == 8 else 5,
        maf_cadence_s=3 if case["poll_plan_version"] == 8 else 15,
        require_timing=case["poll_plan_version"] == 8,
    )
    for sample in case["samples"]:
        rollup.observe(
            OBDSample(
                drive_db_id=1,
                sequence=sample["sequence"],
                captured_at=parsed(sample["timestamp_utc"]),
                ecu_data_status=sample["ecu_data_status"],
                vehicle_speed_kmh=sample.get("vehicle_speed"),
                engine_rpm=sample.get("engine_rpm"),
                estimated_fuel_rate_l_h=sample.get("estimated_fuel_rate"),
                quality_json=sample["quality"],
            )
        )
    actual = {
        "distance_km": rollup.distance_km if rollup.distance_intervals else None,
        "estimated_fuel_used_l": rollup.estimated_fuel_used_l if rollup.fuel_evidence else None,
        "idle_duration_s": rollup.idle_duration_s if rollup.idle_evidence else None,
    }
    for name, expected in case["expected"].items():
        assert (
            summary[name] == pytest.approx(expected)
            if expected is not None
            else summary[name] is None
        )
        assert (
            actual[name] == pytest.approx(expected)
            if expected is not None
            else actual[name] is None
        )


def test_v8_specs_are_separate_from_all_historical_contracts():
    plan, specs = specs_for_poll_plan(8)
    assert plan == 8
    by_name = {spec.name: spec for spec in specs}
    for name in (
        "engine_rpm",
        "vehicle_speed",
        "mass_air_flow",
        "estimated_fuel_rate",
        "estimated_fuel_consumption",
    ):
        assert by_name[name].cadence_s == 3
    assert by_name["engine_load"].cadence_s == 15
    assert by_name["adapter_voltage"].cadence_s == 6
    assert by_name["coolant_temperature"].cadence_s == 60
    for old in range(2, 8):
        old_specs = {spec.name: spec for spec in specs_for_poll_plan(old)[1]}
        assert old_specs["vehicle_speed"].cadence_s == 5
        assert old_specs["mass_air_flow"].cadence_s == (5 if old == 2 else 15)


def validate_sample(sample, plan=8):
    return _validate_sample(
        sample,
        drive_id="timing-fixture",
        started=parsed("2026-10-22T00:00:00Z"),
        finished=parsed("2026-10-22T00:01:00Z"),
        previous_sequence=None,
        previous_at=None,
        poll_plan_version=plan,
    )


def valid_sample():
    sample = deepcopy(FIXTURE["cases"][0]["samples"][0])
    sample["sample_id"] = "timing-fixture-0"
    return sample


@pytest.mark.parametrize("plan", [2, 3, 4, 5, 6, 7, 8])
def test_valid_bounded_ages_are_preserved_without_mutation(plan):
    sample = valid_sample()
    original = deepcopy(sample)
    assert validate_sample(sample, plan)[0] == original
    assert sample == original


@pytest.mark.parametrize(
    "ages",
    [
        None,
        [],
        True,
        {"0D": True},
        {"0D": -1},
        {"0D": 60_001},
        {"0d": 0},
        {"D": 0},
        {"0D": 0.0},
        {"FF": 0},
        {"13": 0},
        {"04": 0},
        {"0D": 2001},
        {},
    ],
)
def test_invalid_or_incomplete_age_metadata_is_rejected(ages):
    sample = valid_sample()
    sample["quality"]["pid_response_age_ms"] = ages
    with pytest.raises(BundleError):
        validate_sample(sample)


def test_age_is_not_a_claim_for_a_failed_or_carried_pid():
    sample = valid_sample()
    sample["quality"]["missing_pids"] = [0x0D]
    with pytest.raises(BundleError, match="successful dynamic"):
        validate_sample(sample)
    sample = valid_sample()
    sample["ecu_data_status"] = "last_known"
    with pytest.raises(BundleError, match="successful dynamic"):
        validate_sample(sample)


def test_v8_requires_map_while_legacy_quality_remains_valid():
    sample = valid_sample()
    del sample["quality"]["pid_response_age_ms"]
    with pytest.raises(BundleError, match="requires"):
        validate_sample(sample)
    for plan in range(2, 8):
        validate_sample(sample, plan)


def trial_bundle(tmp_path, *, target=3000, map_missing=False, fallback=False, case=None):
    case = deepcopy(case or FIXTURE["cases"][0])
    samples = case["samples"]
    for sample in samples:
        sample["sample_id"] = f"timing-fixture-{sample['sequence']}"
        if map_missing:
            sample["quality"].pop("pid_response_age_ms")
    finish = samples[-1]["timestamp_utc"]
    event = {
        "diagnostic_id": "timing-fixture-poll-0",
        "drive_id": "timing-fixture",
        "timestamp_utc": finish,
        "kind": "poll_timing",
        "payload": {
            "schema_version": 1,
            "window_index": 0,
            "window_start_elapsed_ms": 0,
            "window_duration_ms": 11000,
            "flush_reason": "drive_end",
            "target_cycle_ms": target,
            "cycles_started": 0,
            "cycles_completed": 0,
            "overrun_count": 0,
            "cycle_work_ms": timing(),
            "cycle_start_interval_ms": timing(),
            "pids": [],
        },
    }
    return make_bundle(
        tmp_path,
        drive_id="timing-fixture",
        samples=samples,
        diagnostics=[event],
        summary_patch={"schema_version": 99},  # Exercise defensive summary derivation too.
        manifest_patch={
            "poll_plan_version": 8,
            "start_time_utc": case["start_time_utc"],
            "last_sample_at_utc": finish,
            "last_successful_obd_response_at_utc": finish,
            "termination_noticed_at_utc": finish,
            "finalised_at_utc": finish,
            "stop_reason": "poll_trial_fallback" if fallback else "engine_stopped",
            "completion_status": "interrupted" if fallback else "complete",
            "clean_end": not fallback,
            "interruption_reason": "poll_trial_fallback" if fallback else None,
        },
    )


@pytest.mark.parametrize("target", [3000, 5000])
def test_bundle_timing_target_must_match_immutable_plan(tmp_path, target):
    path = trial_bundle(tmp_path, target=target)
    if target == 5000:
        with pytest.raises(BundleError):
            validate_bundle(path)
    else:
        checked = validate_bundle(path)
        assert checked.summary["expected_sample_count"] == 4
        assert checked.summary["estimated_fuel_used_l"] == pytest.approx(0.015)


def test_bundle_rejects_absent_v8_map_on_every_validation_pass(tmp_path):
    with pytest.raises(BundleError, match="requires"):
        validate_bundle(trial_bundle(tmp_path, map_missing=True))


async def test_import_and_startup_reconciliation_preserve_raw_times_and_trial_lifecycle(
    db_session, tmp_path
):
    checked = validate_bundle(trial_bundle(tmp_path, fallback=True))
    await store_validated_bundle(db_session, checked)
    drive = (await db_session.scalars(select(OBDDrive))).one()
    assert drive.lifecycle_status == "interrupted"
    assert drive.interruption_reason == "poll_trial_fallback"
    assert drive.distance_km == pytest.approx(0.093)
    assert drive.estimated_fuel_used_l == pytest.approx(0.015)
    assert drive.gap_analysis_json["nominal_cycle_s"] == 3
    signals = {item["name"]: item for item in drive.gap_analysis_json["signals"]}
    assert signals["vehicle_speed"]["expected_cadence_s"] == 3
    assert signals["vehicle_speed"]["median_cadence_s"] == pytest.approx(2.8)
    assert signals["vehicle_speed"]["missing_run_count"] is None
    assert signals["vehicle_speed"]["longest_missing_run"] is None
    original_rows = (await db_session.execute(select(OBDSample.__table__))).all()
    assert not (await reconcile_drive_projection(db_session, drive))["changed"]
    assert (await db_session.execute(select(OBDSample.__table__))).all() == original_rows
    # The ordinary series API exposes the exact metadata used by computations.
    series = await drive_series(
        drive.drive_id,
        db_session,
        signals=None,
        max_points=2000,
        diagnostic_page=1,
        diagnostic_page_size=100,
    )
    assert series["samples"][0]["quality"]["pid_response_age_ms"]["0D"] == 1000
    assert (
        next(item for item in series["signal_metadata"] if item["name"] == "mass_air_flow")[
            "expected_cadence_s"
        ]
        == 3
    )
    await db_session.commit()
    assert await reconcile_all_drives(get_session_factory()) == {
        "drives": 1,
        "ready": 1,
        "errors": 0,
    }
    await db_session.refresh(drive)
    assert drive.estimated_fuel_used_l == pytest.approx(0.015)
    assert drive.gap_analysis_json["nominal_cycle_s"] == 3


def test_trial_fallback_never_becomes_inferred_shutdown():
    assert (
        lifecycle_status(
            clean_end=False,
            stop_reason="poll_trial_fallback",
            producer="interrupted",
            shutdown_detected=True,
        )
        == "interrupted"
    )


async def test_overdue_optional_observations_count_without_modulo_phase_claims(
    db_session, tmp_path
):
    case = deepcopy(FIXTURE["cases"][0])
    case["samples"] = []
    for sequence in range(7):
        sample = deepcopy(FIXTURE["cases"][0]["samples"][0])
        sample["sequence"] = sequence
        sample["timestamp_utc"] = (
            parsed(case["start_time_utc"]) + timedelta(seconds=2 + sequence * 3)
        ).isoformat()
        if sequence in {1, 6}:
            sample["engine_load"] = 20
            sample["quality"]["pid_response_age_ms"]["04"] = 500
        case["samples"].append(sample)
    checked = validate_bundle(trial_bundle(tmp_path, case=case))
    await store_validated_bundle(db_session, checked)
    drive = (await db_session.scalars(select(OBDDrive))).one()
    load = next(
        item for item in drive.gap_analysis_json["signals"] if item["name"] == "engine_load"
    )
    assert load["expected_cadence_s"] == 15
    assert load["observation_count"] == 2
    assert load["expected_observation_count"] == 2
    assert load["received_observation_count"] == 2
    assert load["coverage_percentage"] == 100
    assert load["median_cadence_s"] == 15
    assert load["missing_run_count"] is None
    assert load["longest_missing_run"] is None


def test_source_gap_and_failed_transport_never_create_motion_intervals():
    for mutation in ("large_source_gap", "transport_failure", "missing_timing"):
        case = deepcopy(FIXTURE["cases"][0])
        case["samples"] = case["samples"][:2]
        if mutation == "large_source_gap":
            case["samples"][0]["quality"]["pid_response_age_ms"]["0D"] = 2000
            case["samples"][1]["quality"]["pid_response_age_ms"]["0D"] = 0
        elif mutation == "transport_failure":
            case["samples"][1]["quality"]["transport"] = "failed_after_partial"
        else:
            for sample in case["samples"]:
                sample["quality"].pop("pid_response_age_ms")
        assert exported_summary(case)["distance_km"] is None


def test_fuel_estimates_cannot_orphan_their_maf_or_speed_sources():
    sample = valid_sample()
    del sample["mass_air_flow"]
    del sample["quality"]["pid_response_age_ms"]["10"]
    with pytest.raises(BundleError, match="timed MAF"):
        validate_sample(sample)
    sample = valid_sample()
    sample["estimated_fuel_consumption"] = 2
    del sample["vehicle_speed"]
    del sample["quality"]["pid_response_age_ms"]["0D"]
    with pytest.raises(BundleError, match="timed speed"):
        validate_sample(sample)


@pytest.mark.parametrize("ages", ["absent", None, []])
def test_missing_required_timing_cannot_hold_a_prior_fuel_rate(ages):
    case = deepcopy(FIXTURE["cases"][0])
    case["samples"] = case["samples"][:2]
    current = case["samples"][1]
    del current["estimated_fuel_rate"]
    del current["mass_air_flow"]
    if ages == "absent":
        del current["quality"]["pid_response_age_ms"]
    else:
        current["quality"]["pid_response_age_ms"] = ages
    assert exported_summary(case)["estimated_fuel_used_l"] is None


async def test_source_window_keeps_reply_saved_after_the_requested_end(db_session, tmp_path):
    await store_validated_bundle(db_session, validate_bundle(trial_bundle(tmp_path)))
    series = await drive_series(
        "timing-fixture",
        db_session,
        start=parsed("2026-10-22T00:00:09.500Z"),
        end=parsed("2026-10-22T00:00:10.500Z"),
        signals="vehicle_speed",
        max_points=64,
        diagnostic_page=1,
        diagnostic_page_size=100,
    )
    assert series["sampling"] == {
        "total_sample_count": 1,
        "returned_sample_count": 1,
        "downsampled": False,
        "method": "full-resolution",
    }
    sample = series["samples"][0]
    assert parsed(sample["t"]) == parsed("2026-10-22T00:00:11Z")
    assert parsed(sample["t"]) - timedelta(
        milliseconds=sample["quality"]["pid_response_age_ms"]["0D"]
    ) == parsed("2026-10-22T00:00:10Z")
    assert sample["vehicle_speed_kmh"] == 0
    assert series["diagnostic_total"] == 0


async def test_window_downsampling_extrema_use_source_window_not_other_reply_times(
    db_session, tmp_path
):
    case = deepcopy(FIXTURE["cases"][0])
    case["samples"] = []
    for sequence in range(100):
        sample = deepcopy(FIXTURE["cases"][0]["samples"][0])
        sample["sequence"] = sequence
        sample["timestamp_utc"] = (
            parsed(case["start_time_utc"]) + timedelta(seconds=2 + sequence * 3)
        ).isoformat()
        sample["vehicle_speed"] = 100 if sequence == 79 else 255 if sequence == 80 else 50
        if sequence >= 80:
            sample["quality"]["pid_response_age_ms"].update({"0D": 0, "10": 60_000})
        case["samples"].append(sample)
    await store_validated_bundle(db_session, validate_bundle(trial_bundle(tmp_path, case=case)))
    before = (await db_session.execute(select(OBDSample.__table__))).all()
    series = await drive_series(
        "timing-fixture",
        db_session,
        start=parsed("2026-10-22T00:01:00Z"),
        end=parsed("2026-10-22T00:04:00Z"),
        signals="vehicle_speed,mass_air_flow",
        max_points=64,
        diagnostic_page=1,
        diagnostic_page_size=100,
    )
    assert series["sampling"]["downsampled"]
    assert series["sampling"]["total_sample_count"] == 80
    assert len(series["samples"]) <= 64
    assert any(
        sample["sequence"] == 79 and sample["vehicle_speed_kmh"] == 100
        for sample in series["samples"]
    )
    # A future row retained for its older MAF reply keeps original speed bytes;
    # that out-of-window speed neither displaces the actual peak nor gets retimestamped.
    assert (await db_session.execute(select(OBDSample.__table__))).all() == before
