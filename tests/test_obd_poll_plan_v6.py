"""Logger diagnostic reservations retain the nominal cadence without inventing coverage."""

from datetime import timedelta

import pytest
from sqlalchemy import select
from test_obd_derived_coverage import _stored_drive
from test_obd_server_import import BASE, make_bundle

from app.db.models import OBDSample
from app.ingest.obd_bundle import BundleError, validate_bundle
from app.ingest.obd_reconciliation import reconcile_drive_projection, specs_for_poll_plan


def _hardened_fields(plan):
    finished = (BASE + timedelta(seconds=5)).isoformat()
    return {
        "last_sample_at_utc": finished,
        "last_successful_obd_response_at_utc": finished,
        "termination_noticed_at_utc": finished,
        "finalised_at_utc": finished,
        "interruption_reason": None,
        "poll_plan_version": plan,
        "logger_version": "0.3.5",
    }


@pytest.mark.parametrize("plan", [2, 3, 4, 5, 6, 7])
def test_current_and_older_poll_plans_keep_the_hardened_bundle_contract(tmp_path, app_config, plan):
    lifecycle = _hardened_fields(plan)
    summary = {
        name: lifecycle[name]
        for name in (
            "last_sample_at_utc",
            "termination_noticed_at_utc",
            "finalised_at_utc",
            "interruption_reason",
        )
    }
    summary["completion_status"] = "complete"
    path = make_bundle(tmp_path, manifest_patch=lifecycle, summary_patch=summary)
    checked = validate_bundle(path, config=app_config)
    assert checked.manifest["poll_plan_version"] == plan
    assert checked.manifest["logger_version"] == "0.3.5"


def test_unknown_future_poll_plan_is_still_rejected(tmp_path, app_config):
    path = make_bundle(tmp_path, manifest_patch=_hardened_fields(8))
    with pytest.raises(BundleError, match="poll_plan_version is not supported"):
        validate_bundle(path, config=app_config)


def test_plan_six_retains_plan_five_nominal_signal_cadences():
    plan, specs = specs_for_poll_plan(6)
    assert plan == 6
    assert specs == specs_for_poll_plan(5)[1]
    by_name = {spec.name: spec for spec in specs}
    assert by_name["vehicle_speed"].cadence_s == 5
    assert by_name["mass_air_flow"].cadence_s == 15
    assert by_name["mil_on"].cadence_s == 60


async def test_reserved_cycle_omissions_remain_missing_without_rewriting_raw_samples(db_session):
    # Plan six reserves cycles 3,16,29 when diagnostics cannot fit normal cycles.
    # Cycle 16 replaces one scheduled MAF response; the other two reservations do
    # not coincide with MAF's phase. Primary speed/RPM/load remain present throughout.
    reserved = {3, 16, 29}
    rows = [
        (i, 6.0, 10.0) if i % 3 == 1 and i not in reserved else (i, None, None) for i in range(33)
    ]
    drive = await _stored_drive(db_session, plan=6, rows=rows)
    before = (await db_session.execute(select(OBDSample.__table__))).all()

    result = await reconcile_drive_projection(db_session, drive)

    assert result["status"] == "ready"
    assert drive.gap_analysis_json["poll_plan_version"] == 6
    signals = {signal["name"]: signal for signal in drive.gap_analysis_json["signals"]}
    assert signals["vehicle_speed"]["coverage_percentage"] == 100
    assert signals["engine_rpm"]["coverage_percentage"] == 100
    for name in ("mass_air_flow", "estimated_fuel_rate", "estimated_fuel_consumption"):
        assert signals[name]["expected_cadence_s"] == 15
        assert signals[name]["expected_observation_count"] == 11
        assert signals[name]["received_observation_count"] == 10
        assert signals[name]["missing_observation_count"] == 1
        assert signals[name]["coverage_percentage"] == pytest.approx(1000 / 11)
    assert (await db_session.execute(select(OBDSample.__table__))).all() == before
