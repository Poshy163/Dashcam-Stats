"""Persisted fuel observations follow MAF's phase without inventing coverage."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.db.models import OBDBundle, OBDDrive, OBDSample
from app.ingest.obd_reconciliation import reconcile_drive_projection

BASE = datetime(2026, 9, 30, tzinfo=UTC)


async def _stored_drive(session, *, plan, rows, finish_sequence=None):
    finish_sequence = max(row[0] for row in rows) if finish_sequence is None else finish_sequence
    finish = BASE + timedelta(seconds=5 * finish_sequence)
    bundle = OBDBundle(
        drive_id="coverage-drive",
        bundle_hash="a" * 64,
        schema_version=1,
        filename="coverage-drive.zip",
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
                sample_id=f"coverage-sample-{sequence}",
                sequence=sequence,
                captured_at=BASE + timedelta(seconds=5 * sequence),
                ecu_data_status="live",
                vehicle_speed_kmh=60.0,
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
    assert drive.gap_analysis_json["projection_version"] == 5
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
