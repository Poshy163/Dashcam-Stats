"""Budget admission preserves nominal expectations and immutable historical plans."""

import pytest
from sqlalchemy import select
from test_obd_derived_coverage import _stored_drive

from app.db.models import OBDSample
from app.ingest.obd_reconciliation import (
    _POLL_PLAN_SPECS,
    POLL_PLAN_VERSION,
    reconcile_drive_projection,
    specs_for_poll_plan,
)


def test_budget_plan_has_the_same_nominal_due_contract_as_five_and_six():
    assert POLL_PLAN_VERSION == 8
    plan, specs = specs_for_poll_plan(7)
    assert plan == 7
    assert specs == specs_for_poll_plan(5)[1] == specs_for_poll_plan(6)[1]
    assert _POLL_PLAN_SPECS[7][1] == _POLL_PLAN_SPECS[6][1] == _POLL_PLAN_SPECS[5][1]
    by_name = {spec.name: spec for spec in specs}
    for name in ("engine_rpm", "vehicle_speed", "engine_load", "adapter_voltage"):
        assert by_name[name].cadence_s == 5
    assert by_name["mass_air_flow"].cadence_s == 15
    assert by_name["distance_with_mil"].cadence_s == 60
    assert by_name["mil_on"].cadence_s == 60
    assert _POLL_PLAN_SPECS[7][1][0x21] == 1
    assert _POLL_PLAN_SPECS[7][1][0x01] == 5


@pytest.mark.parametrize("value", [None, True, "7", 0, 9, 999])
def test_unknown_plan_never_inherits_the_new_contract(value):
    plan, specs = specs_for_poll_plan(value)
    assert plan == 1
    assert next(spec for spec in specs if spec.name == "mass_air_flow").cadence_s == 5


@pytest.mark.parametrize("plan", [5, 6, 7])
async def test_budget_deferred_observation_stays_missing_and_reconciliation_is_idempotent(
    db_session, plan
):
    # MAF is due at 1/4/7/10. The command at 4 did not fit; a value at 5 must not
    # conceal that missed nominal opportunity. Fast samples continue every cycle.
    rows = [(i, 6.0, 10.0) if i in {1, 5, 7, 10} else (i, None, None) for i in range(13)]
    drive = await _stored_drive(db_session, plan=plan, rows=rows)
    before = (await db_session.execute(select(OBDSample.__table__))).all()

    result = await reconcile_drive_projection(db_session, drive)

    assert result["status"] == "ready"
    assert drive.gap_analysis_json["poll_plan_version"] == plan
    transport = drive.gap_analysis_json["transport"]
    assert transport["coverage_percentage"] == 100
    assert transport["sequence_gap_count"] == 0
    signals = {signal["name"]: signal for signal in drive.gap_analysis_json["signals"]}
    for name in ("vehicle_speed", "engine_rpm", "engine_load", "adapter_voltage"):
        assert signals[name]["expected_cadence_s"] == 5
        assert signals[name]["received_observation_count"] == 13
        assert signals[name]["coverage_percentage"] == 100
    for name in ("mass_air_flow", "estimated_fuel_rate", "estimated_fuel_consumption"):
        assert signals[name]["expected_cadence_s"] == 15
        assert signals[name]["observation_count"] == 4
        assert signals[name]["expected_observation_count"] == 4
        assert signals[name]["received_observation_count"] == 3
        assert signals[name]["missing_observation_count"] == 1
        assert signals[name]["coverage_percentage"] == 75
    assert (await db_session.execute(select(OBDSample.__table__))).all() == before
    assert not (await reconcile_drive_projection(db_session, drive))["changed"]
