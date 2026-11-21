"""Persisted fuel observations follow MAF's phase without inventing coverage."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.db.models import OBDBundle, OBDDrive, OBDSample
from app.ingest.obd_reconciliation import (
    PROJECTION_VERSION,
    reconcile_all_drives,
    reconcile_drive_projection,
)

BASE = datetime(2026, 9, 30, tzinfo=UTC)


async def _stored_drive(session, *, plan, rows, finish_sequence=None, identifier="a", speed=60.0):
    finish_sequence = max(row[0] for row in rows) if finish_sequence is None else finish_sequence
    finish = BASE + timedelta(seconds=5 * finish_sequence)
    bundle = OBDBundle(
        drive_id=f"coverage-drive-{identifier}",
        bundle_hash=identifier * 64,
        schema_version=1,
        filename=f"coverage-drive-{identifier}.zip",
        vehicle_id="synthetic-vehicle",
        logger_id="synthetic-logger",
        logger_version="0.3.3",
        drive_started_at=BASE,
        drive_finished_at=finish,
    )
    session.add(bundle)
    await session.flush()
    drive = OBDDrive(
        bundle_id=bundle.id,
        drive_id=bundle.drive_id,
        vehicle_id=bundle.vehicle_id,
        started_at=BASE,
        finished_at=finish,
        completion_status="complete",
        clean_end=True,
        units={},
        manifest_json={"poll_plan_version": plan},
        summary_json={},
    )
    session.add(drive)
    await session.flush()
    for sequence, rate, consumption in rows:
        session.add(
            OBDSample(
                drive_db_id=drive.id,
                sample_id=f"{identifier}-coverage-sample-{sequence}",
                sequence=sequence,
                captured_at=BASE + timedelta(seconds=5 * sequence),
                ecu_data_status="live",
                vehicle_speed_kmh=speed,
                engine_rpm=1000.0,
                engine_load_pct=20.0,
                adapter_voltage_v=14.0,
                mass_air_flow_g_s=10.0 if rate is not None else None,
                estimated_fuel_rate_l_h=rate,
                estimated_fuel_consumption_l_100km=consumption,
                quality_json={"transport": "ok", "parser": "ok", "missing_pids": []},
                raw_json={"sequence": sequence, "rate": rate, "consumption": consumption},
            )
        )
    await session.flush()
    return drive


async def test_fleet_economy_uses_joint_observations_across_drives(db_session, client):
    missing = await _stored_drive(db_session, plan=7, rows=[(i, None, None) for i in range(7)])
    await reconcile_drive_projection(db_session, missing)
    await db_session.commit()
    summary_url = "/api/obd/drives/summary"
    fleet = (await client.get(summary_url)).json()
    assert fleet["total_fuel_used_l"] is None
    assert fleet["average_fuel_consumption_l_100km"] is None
    assert fleet["fuel_economy_coverage_pct"] == 0

    rows = [(i, 6 if i % 3 == 0 else None, None) for i in range(7)]
    idle = await _stored_drive(db_session, plan=7, rows=rows, identifier="b", speed=0)
    await reconcile_drive_projection(db_session, idle)
    assert idle.average_fuel_consumption_l_100km is None
    assert idle.summary_json["fuel_economy_fuel_l"] == pytest.approx(0.05)
    moving = await _stored_drive(db_session, plan=7, rows=rows, identifier="c")
    await reconcile_drive_projection(db_session, moving)
    await db_session.commit()
    fleet = (await client.get(summary_url)).json()
    assert fleet["total_distance_km"] == pytest.approx(1)
    assert fleet["fuel_economy_distance_km"] == pytest.approx(0.5)
    assert fleet["fuel_economy_fuel_l"] == pytest.approx(0.1)
    assert fleet["fuel_economy_coverage_pct"] == pytest.approx(50)
    assert fleet["fuel_economy_time_coverage_pct"] == pytest.approx(200 / 3)
    assert fleet["average_fuel_consumption_l_100km"] == pytest.approx(20)
    drives = (await client.get("/api/obd/drives")).json()["items"]
    covered = next(row for row in drives if row["drive_id"] == moving.drive_id)
    assert covered["fuel_economy_coverage_pct"] == pytest.approx(100)
    assert covered["average_fuel_consumption_l_100km"] == pytest.approx(10)


async def test_measured_zero_estimated_fuel_is_distinct_from_absent_maf(db_session, client):
    rows = [(i, 0.0 if i % 3 == 0 else None, None) for i in range(7)]
    drive = await _stored_drive(db_session, plan=7, rows=rows)
    await reconcile_drive_projection(db_session, drive)
    await db_session.commit()
    fleet = (await client.get("/api/obd/drives/summary")).json()
    assert fleet["total_fuel_used_l"] == 0
    assert fleet["average_fuel_consumption_l_100km"] == 0


async def test_overview_waits_for_historical_economy_reconciliation(db_session, client):
    rows = [(i, 6.0 if i % 3 == 0 else None, None) for i in range(7)]
    current = await _stored_drive(db_session, plan=7, rows=rows)
    await reconcile_drive_projection(db_session, current)
    historical = await _stored_drive(db_session, plan=7, rows=rows, identifier="b")
    historical.distance_km = 0.5
    historical.average_fuel_consumption_l_100km = 3.0
    await db_session.commit()

    url = "/api/obd/drives/summary"
    pending = (await client.get(url)).json()
    assert pending["total_distance_km"] == pytest.approx(1)
    assert pending["average_fuel_consumption_l_100km"] is None
    assert pending["fuel_economy_coverage_pct"] is None
    assert (await reconcile_all_drives())["ready"] == 2
    rebuilt = (await client.get(url)).json()
    assert rebuilt["average_fuel_consumption_l_100km"] == pytest.approx(10)
    assert rebuilt["fuel_economy_coverage_pct"] == 100


def _signals(drive):
    return {item["name"]: item for item in drive.gap_analysis_json["signals"]}


@pytest.mark.parametrize("plan", [3, 4, 5])
async def test_stored_derived_values_use_maf_phase_without_changing_samples_or_totals(
    db_session, plan
):
    rows = [(i, 6.0, 10.0) if i % 3 == 1 else (i, None, None) for i in range(7)]
    drive = await _stored_drive(db_session, plan=plan, rows=rows)
    before = (await db_session.execute(select(OBDSample.__table__))).all()

    result = await reconcile_drive_projection(db_session, drive)

    assert result["status"] == "ready"
    assert drive.gap_analysis_json["projection_version"] == PROJECTION_VERSION
    assert drive.sample_count == 7
    assert drive.estimated_fuel_used_l == pytest.approx(6 * 25 / 3600)
    assert drive.data_completeness_percentage == 100.0
    signals = _signals(drive)
    for name in ("estimated_fuel_rate", "estimated_fuel_consumption"):
        fuel = signals[name]
        assert fuel["pid"] is None
        assert fuel["provenance"] == "derived"
        assert fuel["expected_cadence_s"] == 15.0
        assert fuel["expected_observation_count"] == 2
        assert fuel["received_observation_count"] == 2
        assert fuel["missing_observation_count"] == 0
        assert fuel["missing_run_count"] == 0
        assert fuel["coverage_percentage"] == 100.0
        assert fuel["observation_count"] == signals["mass_air_flow"]["observation_count"]
    assert (await db_session.execute(select(OBDSample.__table__))).all() == before
    assert not (await reconcile_drive_projection(db_session, drive))["changed"]


async def test_missing_scheduled_values_sequences_and_tail_stay_missing(db_session):
    # Opportunities 1/4/7/10/13: 4 is absent, 7 is null, and 13 is beyond the last
    # row but inside the completed drive. A zero rate at 10 is a real observation.
    rows = [
        (i, 6.0 if i == 1 else 0.0 if i == 10 else None, 10.0 if i == 1 else None)
        for i in range(11)
        if i != 4
    ]
    drive = await _stored_drive(db_session, plan=5, rows=rows, finish_sequence=13)
    await reconcile_drive_projection(db_session, drive)
    fuel = _signals(drive)["estimated_fuel_rate"]
    assert fuel["expected_observation_count"] == 5
    assert fuel["received_observation_count"] == 2
    assert fuel["missing_observation_count"] == 3
    assert fuel["missing_run_count"] == 2
    assert fuel["longest_missing_run"] == 2
    assert fuel["coverage_percentage"] == 40.0
    consumption = _signals(drive)["estimated_fuel_consumption"]
    assert consumption["received_observation_count"] == 1
    assert consumption["missing_observation_count"] == 4
    assert consumption["missing_run_count"] == 1
    assert consumption["longest_missing_run"] == 4
    assert consumption["coverage_percentage"] == 20.0


@pytest.mark.parametrize("plan", [None, 2, 999])
async def test_legacy_every_cycle_fuel_schedule_remains_unchanged(db_session, plan):
    rows = [(0, 6.0, 10.0), (1, None, None), (2, 6.0, 10.0)]
    drive = await _stored_drive(db_session, plan=plan, rows=rows)
    await reconcile_drive_projection(db_session, drive)
    for name in ("estimated_fuel_rate", "estimated_fuel_consumption"):
        fuel = _signals(drive)[name]
        assert fuel["expected_cadence_s"] == 5.0
        assert fuel["expected_observation_count"] == 3
        assert fuel["received_observation_count"] == 2
        assert fuel["missing_observation_count"] == 1
        assert fuel["missing_run_count"] == 1


async def test_off_phase_value_does_not_hide_missing_scheduled_reading(db_session):
    drive = await _stored_drive(
        db_session, plan=5, rows=[(0, 6.0, 10.0), (1, None, None), (2, None, None)]
    )
    await reconcile_drive_projection(db_session, drive)
    for name in ("estimated_fuel_rate", "estimated_fuel_consumption"):
        fuel = _signals(drive)[name]
        assert fuel["observation_count"] == 1
        assert fuel["expected_observation_count"] == 1
        assert fuel["received_observation_count"] == 0
        assert fuel["missing_observation_count"] == 1
        assert fuel["coverage_percentage"] == 0.0
