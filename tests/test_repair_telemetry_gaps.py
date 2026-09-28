"""Historical paired recovery is backed up, transactional and bounded to idle targets."""

from __future__ import annotations

import importlib.util
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, text

from app.db.backup import validate_database
from app.db.models import Camera, JobState, ProcessingJob, Recording, StageState, TelemetryPoint
from app.db.session import get_session_factory
from app.pipeline.revisions import CURRENT_REVISIONS


@pytest.fixture
def repair_module():
    path = Path(__file__).resolve().parents[1] / "backend/scripts/repair_telemetry_gaps.py"
    spec = importlib.util.spec_from_file_location("repair_telemetry_gaps_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
async def paired_library(db_session):
    cameras = list((await db_session.execute(select(Camera).order_by(Camera.id))).scalars())
    now = datetime(2026, 9, 28, 0, 0, tzinfo=UTC)
    recordings = [
        Recording(
            rel_path=f"repair-camera-{camera.id}.ts",
            filename=f"repair-camera-{camera.id}.ts",
            size_bytes=1,
            camera_id=camera.id,
            started_at=now,
            ended_at=now + timedelta(seconds=1),
            telemetry_state=StageState.DONE,
            telemetry_revision=CURRENT_REVISIONS["telemetry"],
            telemetry_point_count=1,
        )
        for camera in cameras[:2]
    ]
    db_session.add_all(recordings)
    await db_session.flush()
    target, donor = recordings
    target.gps_ocr_gap_count = target.gps_gap_count = target.telemetry_problem_count = 1
    donor.gps_point_count = 1
    db_session.add_all(
        [
            TelemetryPoint(
                recording_id=target.id,
                t_offset_s=0,
                captured_at=now,
                has_fix=False,
                gps_quality="rejected",
                quality_json={
                    "source": "overlay_ocr",
                    "ocr_status": "partial",
                    "gps_status": "parse_failed",
                    "gps_source": "none",
                    "gps_reason": None,
                    "time_status": "valid",
                    "time_source": "overlay",
                    "problems": ["GPS fields unreadable"],
                },
            ),
            TelemetryPoint(
                recording_id=donor.id,
                t_offset_s=0,
                captured_at=now,
                has_fix=True,
                lat=-34.8,
                lon=138.6,
                gps_quality="valid",
                speed_kmh=20,
                quality_json={"gps_status": "valid", "gps_source": "direct"},
            ),
        ]
    )
    await db_session.commit()
    return target.id, donor.id


async def _snapshot():
    async with get_session_factory()() as session:
        return {
            table: (await session.execute(text(f"SELECT * FROM {table} ORDER BY id"))).all()
            for table in ("recordings", "telemetry_points", "processing_jobs")
        }


async def test_preview_rolls_back_real_changes_then_apply_is_backed_up_and_idempotent(
    db_session,
    paired_library,
    repair_module,
):
    target_id, donor_id = paired_library
    before = await _snapshot()
    preview = await repair_module.repair(apply=False, limit=10, after_id=0, recording_id=None)
    assert preview["recovered_points"] == 1
    assert preview["changed_recordings"] == 1
    assert preview["modified_recordings"] == 1
    assert preview["backup"] is None
    assert await _snapshot() == before

    applied = await repair_module.repair(apply=True, limit=10, after_id=0, recording_id=None)
    assert applied["recovered_points"] == 1
    assert applied["modified_recordings"] == 1
    backup = Path(applied["backup"])
    validate_database(backup)
    with sqlite3.connect(backup) as conn:
        assert conn.execute(
            "SELECT has_fix FROM telemetry_points WHERE recording_id=?", (target_id,)
        ).fetchone() == (0,)
    async with get_session_factory()() as session:
        target = await session.get(Recording, target_id)
        assert target.gps_recovered_count == target.gps_point_count == 1
        assert target.gps_ocr_gap_count == 0
        donor = await session.get(Recording, donor_id)
        assert donor.gps_recovered_count == 0
    after = await _snapshot()
    repeated = await repair_module.repair(apply=True, limit=10, after_id=0, recording_id=None)
    assert repeated["recovered_points"] == 0
    assert repeated["changed_recordings"] == 0
    assert repeated["modified_recordings"] == 0
    assert await _snapshot() == after


async def test_backup_failure_prevents_any_repair(
    db_session, paired_library, repair_module, monkeypatch
):
    before = await _snapshot()

    def failed_backup(*_args):
        raise OSError("backup storage unavailable")

    def unexpected_session():
        pytest.fail("database repair opened after backup failure")

    monkeypatch.setattr(repair_module, "create_pre_migration_backup", failed_backup)
    monkeypatch.setattr(repair_module, "get_session_factory", unexpected_session)
    with pytest.raises(OSError, match="backup storage unavailable"):
        await repair_module.repair(apply=True, limit=10, after_id=0, recording_id=None)
    assert await _snapshot() == before


async def test_refused_attempt_is_reported_modified_without_claiming_recovered_points(
    db_session,
    paired_library,
    repair_module,
):
    target_id, donor_id = paired_library
    target = await db_session.get(Recording, target_id)
    source = await db_session.get(Recording, donor_id)
    now = target.started_at
    target.ended_at = source.ended_at = now + timedelta(seconds=3)
    target.telemetry_point_count = 3
    target.gps_point_count = 2
    hole = (
        await db_session.scalars(
            select(TelemetryPoint).where(TelemetryPoint.recording_id == target_id)
        )
    ).one()
    donor = (
        await db_session.scalars(
            select(TelemetryPoint).where(TelemetryPoint.recording_id == donor_id)
        )
    ).one()
    hole.t_offset_s = donor.t_offset_s = 1
    hole.captured_at = donor.captured_at = now + timedelta(seconds=1)
    donor.lat = -34.0  # A valid place, but not reachable from this target's own fixes.
    db_session.add_all(
        [
            TelemetryPoint(
                recording_id=target_id,
                t_offset_s=offset,
                captured_at=now + timedelta(seconds=offset),
                has_fix=True,
                lat=-34.8,
                lon=138.6,
                speed_kmh=0,
                gps_quality="valid",
                quality_json={"gps_status": "valid", "gps_source": "direct", "problems": []},
            )
            for offset in (0, 2)
        ]
    )
    await db_session.commit()
    before = await _snapshot()
    preview = await repair_module.repair(apply=False, limit=10, after_id=0, recording_id=target_id)
    assert preview["recovered_points"] == preview["changed_recordings"] == 0
    assert preview["modified_recordings"] == 1
    assert await _snapshot() == before
    applied = await repair_module.repair(apply=True, limit=10, after_id=0, recording_id=target_id)
    assert applied["recovered_points"] == applied["changed_recordings"] == 0
    assert applied["modified_recordings"] == 1
    async with get_session_factory()() as session:
        target = await session.get(Recording, target_id)
        point = (
            await session.scalars(
                select(TelemetryPoint).where(
                    TelemetryPoint.recording_id == target_id, TelemetryPoint.t_offset_s == 1
                )
            )
        ).one()
        assert target.gps_ocr_gap_count == 0
        assert target.gps_rejected_count == 1
        assert point.speed_kmh is None
        assert not point.has_fix


@pytest.mark.parametrize("state", [JobState.QUEUED, JobState.RUNNING])
async def test_active_target_jobs_are_skipped_under_lock(
    db_session,
    paired_library,
    repair_module,
    monkeypatch,
    state,
):
    target_id, _ = paired_library
    db_session.add(ProcessingJob(recording_id=target_id, state=state))
    await db_session.commit()
    before = await _snapshot()

    async def unexpected_repair(*_args, **_kwargs):
        pytest.fail("active recording was repaired")

    monkeypatch.setattr(repair_module, "recover_from_paired_camera", unexpected_repair)
    result = await repair_module.repair(apply=True, limit=10, after_id=0, recording_id=target_id)
    assert result["skipped"] == 1
    assert result["examined"] == result["recovered_points"] == 0
    assert await _snapshot() == before


async def test_exception_after_flushed_changes_rolls_back_the_recording(
    db_session,
    paired_library,
    repair_module,
    monkeypatch,
):
    before = await _snapshot()
    original = repair_module.recover_from_paired_camera

    async def fail_after_flush(session, recording, **kwargs):
        assert await original(session, recording, **kwargs) == 1
        await session.flush()
        raise RuntimeError("simulated failure after SQL writes")

    monkeypatch.setattr(repair_module, "recover_from_paired_camera", fail_after_flush)
    with pytest.raises(RuntimeError, match="simulated failure"):
        await repair_module.repair(apply=True, limit=10, after_id=0, recording_id=None)
    assert await _snapshot() == before


async def test_modification_count_includes_changes_flushed_by_partner_queries(
    db_session, paired_library, repair_module, monkeypatch
):
    before = await _snapshot()
    original = repair_module.recover_from_paired_camera

    async def flushed_repair(session, recording, **kwargs):
        recovered = await original(session, recording, **kwargs)
        await session.flush()
        assert not session.dirty
        return recovered

    monkeypatch.setattr(repair_module, "recover_from_paired_camera", flushed_repair)
    result = await repair_module.repair(apply=False, limit=10, after_id=0, recording_id=None)
    assert result["changed_recordings"] == result["modified_recordings"] == 1
    assert await _snapshot() == before


async def test_maintenance_preserves_other_missing_rows_in_an_eligible_recording(
    db_session,
    paired_library,
    repair_module,
):
    target_id, donor_id = paired_library
    target = await db_session.get(Recording, target_id)
    donor = await db_session.get(Recording, donor_id)
    now = target.started_at
    target.ended_at = donor.ended_at = now + timedelta(seconds=4)
    target.telemetry_point_count = target.gps_ocr_gap_count = 4
    other_holes = [
        TelemetryPoint(
            recording_id=target_id,
            t_offset_s=offset,
            captured_at=now + timedelta(seconds=offset),
            has_fix=False,
            gps_quality=verdict,
            quality_json={"gps_status": status, "gps_source": source, "problems": []},
        )
        for offset, verdict, status, source in (
            (1, "valid", "valid", "direct"),
            (2, "interpolated", "interpolated", "interpolated"),
            (3, None, "missing", None),
        )
    ]
    donors = [
        TelemetryPoint(
            recording_id=donor_id,
            t_offset_s=offset,
            captured_at=now + timedelta(seconds=offset),
            has_fix=True,
            lat=-34.8,
            lon=138.6,
            gps_quality="valid",
            quality_json={"gps_status": "valid", "gps_source": "direct"},
        )
        for offset in (1, 2, 3)
    ]
    db_session.add_all([*other_holes, *donors])
    await db_session.commit()
    async with get_session_factory()() as session:
        before = (
            await session.execute(
                text(
                    "SELECT * FROM telemetry_points WHERE recording_id=:rid AND t_offset_s>0 ORDER BY id"
                ),
                {"rid": target_id},
            )
        ).all()
    applied = await repair_module.repair(apply=True, limit=10, after_id=0, recording_id=target_id)
    assert applied["recovered_points"] == 1
    async with get_session_factory()() as session:
        after = (
            await session.execute(
                text(
                    "SELECT * FROM telemetry_points WHERE recording_id=:rid AND t_offset_s>0 ORDER BY id"
                ),
                {"rid": target_id},
            )
        ).all()
    assert after == before
