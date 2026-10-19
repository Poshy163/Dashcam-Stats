"""A rollback must not turn retryable stage writes into synchronous ORM lazy loads."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import numpy as np
import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.sql.dml import Delete

from app.ai.detector import Detection2D
from app.core.settings_service import get_settings_service
from app.db import retry
from app.db.models import Detection, Recording, StageState, TelemetryPoint, TrackedObject
from app.db.session import get_session_factory
from app.osd.engine import TelemetryResult, TelemetrySample
from app.pipeline import stages

BASE = datetime(2026, 10, 19, tzinfo=UTC)


async def _seed(session):
    recording = Recording(
        filename="20261019120000_camera_0.ts",
        rel_path="20261019120000_camera_0.ts",
        started_at=BASE,
        ended_at=BASE + timedelta(seconds=4),
        duration_s=4,
        width=32,
        height=32,
        time_from_osd=False,
    )
    session.add(recording)
    await session.flush()
    session.add(
        TelemetryPoint(recording_id=recording.id, t_offset_s=99, captured_at=BASE, has_fix=False)
    )
    session.add(
        TrackedObject(
            recording_id=recording.id,
            track_key=99,
            class_label="old",
            confidence_max=0.5,
            confidence_avg=0.5,
            first_seen_offset_s=0,
            last_seen_offset_s=1,
            duration_s=1,
            frame_count=2,
        )
    )
    await session.commit()
    return recording


@pytest.fixture
def fake_analysis(monkeypatch):
    """Run the real stage bodies with deterministic decoded frames and OCR output."""

    class Detector:
        device = "CPU"
        available = True

        async def detect(self, frame, **kwargs):
            return [Detection2D("car", 0.9, 0.2, 0.2, 0.4, 0.4)]

    async def frames(*args, **kwargs):
        for offset in range(4):
            yield float(offset), np.zeros((32, 32, 3), dtype=np.uint8)

    async def save_crops(batch, quality):
        return [None] * len(batch)

    sample = TelemetrySample(
        t_offset_s=1,
        captured_at=datetime(2026, 10, 19, 12, 0, 3),
        lat=-34.8,
        lon=138.6,
        has_fix=True,
        speed_kmh=40,
        heading_deg=90,
        ocr_confidence=0.99,
        raw_text="new overlay",
        time_status="valid",
        gps_status="valid",
        gps_source="direct",
        ocr_status="valid",
    )
    telemetry = TelemetryResult(
        samples=[sample],
        osd_start_time=datetime(2026, 10, 19, 12, 0, 2),
        first_fix=sample,
        last_fix=sample,
        avg_speed_kmh=40,
        max_speed_kmh=40,
    )
    monkeypatch.setattr(stages, "_shared_detector", AsyncMock(return_value=Detector()))
    monkeypatch.setattr(stages, "iter_frames", frames)
    monkeypatch.setattr(stages, "_save_jpegs", save_crops)
    monkeypatch.setattr(stages, "_get_templates", AsyncMock(return_value=object()))
    monkeypatch.setattr(stages.TelemetryExtractor, "extract", AsyncMock(return_value=telemetry))
    monkeypatch.setattr(stages, "recover_from_paired_camera", AsyncMock(return_value=0))
    retry_locked = retry.retry_on_locked

    async def quick_retry(*args, **kwargs):
        return await retry_locked(*args, **kwargs, base_delay_s=0.001)

    monkeypatch.setattr(retry, "retry_on_locked", quick_retry)


def _fail_writes(session, monkeypatch, model, failure_point, *, always=False):
    """Fail a real stage transaction, allowing rollback to expire mapped attributes."""
    execute, commit = session.execute, session.commit
    attempts = []

    async def fail_execute(statement, *args, **kwargs):
        if isinstance(statement, Delete) and statement.table.name == model.__tablename__:
            attempts.append(True)
            if failure_point == "delete" and (always or len(attempts) == 1):
                raise OperationalError("DELETE", {}, sqlite3.OperationalError("database is locked"))
        return await execute(statement, *args, **kwargs)

    async def fail_commit():
        if failure_point == "commit" and (always or len(attempts) == 1):
            raise OperationalError("COMMIT", {}, sqlite3.OperationalError("database is locked"))
        return await commit()

    monkeypatch.setattr(session, "execute", fail_execute)
    monkeypatch.setattr(session, "commit", fail_commit)
    return attempts


@pytest.mark.parametrize("failure_point", ["delete", "commit"])
@pytest.mark.parametrize(
    "stage_name,model", [("telemetry", TelemetryPoint), ("detect", TrackedObject)]
)
async def test_stage_retry_replaces_rows_once_and_keeps_recording_usable(
    db_session, monkeypatch, fake_analysis, stage_name, model, failure_point
):
    settings = get_settings_service()
    await settings.set_many(
        {"general.timezone": "Australia/Adelaide", "processing.motion_gating": False}
    )
    recording = await _seed(db_session)
    recording_id = recording.id
    attempts = _fail_writes(db_session, monkeypatch, model, failure_point)

    result = await getattr(stages, f"stage_{stage_name}")(db_session, recording)
    await db_session.flush()

    assert result.ok
    assert len(attempts) == 2
    # Attribute reads after the writer also exercise the real logging/rollup path.
    assert recording.id == recording_id
    assert recording.filename == "20261019120000_camera_0.ts"
    if stage_name == "telemetry":
        assert recording.telemetry_state is StageState.DONE
        assert recording.time_from_osd is True
        expected = datetime(2026, 10, 19, 1, 30, 2, tzinfo=UTC)
        assert recording.started_at == expected
        assert recording.ended_at == expected + timedelta(seconds=4)
        points = (await db_session.scalars(select(TelemetryPoint))).all()
        assert len(points) == 1
        assert points[0].t_offset_s == 1
        assert points[0].captured_at == expected + timedelta(seconds=1)
        assert recording.telemetry_point_count == recording.gps_point_count == 1
    else:
        assert recording.detection_state is StageState.DONE
        tracks = (await db_session.scalars(select(TrackedObject))).all()
        assert len(tracks) == 1
        assert tracks[0].class_label == "car"
        assert tracks[0].first_seen_at == BASE
        assert tracks[0].last_seen_at == BASE + timedelta(seconds=3)
        detections = (await db_session.scalars(select(Detection))).all()
        assert detections
        assert {row.tracked_object_id for row in detections} == {tracks[0].id}


@pytest.mark.parametrize(
    "stage_name,model", [("telemetry", TelemetryPoint), ("detect", TrackedObject)]
)
async def test_exhausted_stage_replacement_preserves_previously_committed_results(
    db_session, monkeypatch, fake_analysis, stage_name, model
):
    recording = await _seed(db_session)
    recording_id = recording.id
    with monkeypatch.context() as contention:
        attempts = _fail_writes(db_session, contention, model, "commit", always=True)
        with pytest.raises(OperationalError, match="database is locked"):
            await getattr(stages, f"stage_{stage_name}")(db_session, recording)
        await db_session.rollback()

    assert len(attempts) == 4
    async with get_session_factory()() as other:
        rows = (await other.scalars(select(model).where(model.recording_id == recording_id))).all()
        assert len(rows) == 1
        if stage_name == "telemetry":
            assert rows[0].t_offset_s == 99
        else:
            assert rows[0].class_label == "old"
