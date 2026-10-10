"""Removing a source video must preserve finalized history, not revive unfinished work."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.db.models import (
    Journey,
    Plate,
    PlateObservation,
    Recording,
    RecordingState,
    StageState,
    TelemetryPoint,
    TrackedObject,
)
from app.pipeline.revisions import CURRENT_REVISIONS, INVALIDATED_REVISION
from app.retention.planner import RetentionCandidate, RetentionPlan, execute
from app.retention.safety import SafetyReport

BASE = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)


@pytest.fixture
async def analysed_history(db_session, app_config):
    journey = Journey(
        started_at=BASE,
        ended_at=BASE + timedelta(minutes=1),
        duration_s=60,
        avg_speed_kmh=40,
        max_speed_kmh=40,
        distance_m=1000,
        recording_count=1,
        has_gps=True,
        motion_json={"status": "moving"},
    )
    db_session.add(journey)
    await db_session.flush()
    recording = Recording(
        filename="historical.ts",
        rel_path="historical.ts",
        size_bytes=7,
        state=RecordingState.COMPLETED,
        journey_id=journey.id,
        started_at=BASE,
        ended_at=BASE + timedelta(minutes=1),
        duration_s=60,
        processed_at=BASE + timedelta(minutes=2),
        metadata_state=StageState.DONE,
        telemetry_state=StageState.DONE,
        detection_state=StageState.DONE,
        plate_state=StageState.DONE,
        metadata_revision=CURRENT_REVISIONS["metadata"],
        telemetry_revision=CURRENT_REVISIONS["telemetry"],
        detection_revision=CURRENT_REVISIONS["detection"],
        plate_revision=CURRENT_REVISIONS["plates"],
    )
    db_session.add(recording)
    await db_session.flush()
    for index in range(3):
        db_session.add(
            TelemetryPoint(
                recording_id=recording.id,
                journey_id=journey.id,
                t_offset_s=index * 10,
                captured_at=BASE + timedelta(seconds=index * 10),
                lat=-34.8 + index * 0.0005,
                lon=138.6,
                speed_kmh=40,
                has_fix=True,
                raw_text="retained original overlay",
            )
        )
    track = TrackedObject(
        recording_id=recording.id,
        journey_id=journey.id,
        track_key=1,
        class_label="car",
        first_seen_offset_s=1,
        last_seen_offset_s=2,
        frame_count=3,
    )
    plate = Plate(normalised_text="S192DKX", display_text="S192DKX")
    db_session.add_all([track, plate])
    await db_session.flush()
    db_session.add(
        PlateObservation(
            recording_id=recording.id,
            journey_id=journey.id,
            tracked_object_id=track.id,
            plate_id=plate.id,
            t_offset_s=1,
            raw_text="S192DKX original OCR",
            normalised_text="S192DKX",
            ocr_confidence=0.9,
            detection_confidence=0.9,
        )
    )
    path = app_config.footage_dir / recording.rel_path
    path.write_bytes(b"footage")
    await db_session.commit()
    return journey, recording, path


async def _derived_views(client, journey_id):
    status = (await client.get("/api/status")).json()["totals"]
    journeys = (await client.get("/api/journeys?include_parked=true")).json()
    heatmap = (await client.get(f"/api/map/heatmap?journey_id={journey_id}")).json()
    routes = (await client.get(f"/api/map/routes?journey_id={journey_id}")).json()
    return {
        "totals": {
            key: status[key]
            for key in ("journeys", "telemetry_points", "tracked_objects", "plates")
        },
        "journeys": [item["id"] for item in journeys["items"]],
        "heatmap": heatmap,
        "routes": routes,
    }


async def _retire(session, recording, *, discard=False):
    plan = RetentionPlan(
        deletion_enabled=True,
        exclude_from_stats=discard,
        safety=SafetyReport(ok=True, writable=True),
        candidates=[
            RetentionCandidate(
                recording_id=recording.id,
                rel_path=recording.rel_path,
                filename=recording.filename,
                size_bytes=recording.size_bytes,
                started_at=recording.started_at,
            )
        ],
    )
    run = await execute(session, plan, dry_run=False, trigger="test-history-retention")
    assert run.deleted_count == 1


async def test_size_retention_keeps_journey_maps_and_analysis_after_video_removal(
    client, db_session, analysed_history
):
    journey, recording, path = analysed_history
    before = await _derived_views(client, journey.id)
    assert before["totals"] == {
        "journeys": 1,
        "telemetry_points": 3,
        "tracked_objects": 1,
        "plates": 1,
    }
    assert before["routes"]["journeys"] == 1
    await _retire(db_session, recording)
    assert not path.exists()
    assert recording.state is RecordingState.DELETED
    assert recording.file_missing is True
    assert recording.ignored is False
    assert await _derived_views(client, journey.id) == before
    detail = await client.get(f"/api/journeys/{journey.id}")
    assert detail.status_code == 200
    assert detail.json()["recordings"][0]["file_missing"] is True
    assert detail.json()["route"]
    default_list = (await client.get("/api/journeys")).json()
    assert default_list["total"] == 1


async def test_intentionally_discarded_retention_tombstones_remain_hidden(
    client, db_session, analysed_history
):
    journey, recording, path = analysed_history
    await _retire(db_session, recording, discard=True)
    assert not path.exists()
    assert recording.ignored is True
    views = await _derived_views(client, journey.id)
    assert all(value == 0 for value in views["totals"].values())
    assert views["journeys"] == []
    assert views["heatmap"]["total_points"] == 0
    assert views["routes"]["journeys"] == 0
    assert (await client.get(f"/api/journeys/{journey.id}")).status_code == 404
    # Hiding the discarded history changes no stored analysis rows.
    assert await db_session.scalar(select(func.count(TelemetryPoint.id))) == 3
    assert await db_session.scalar(select(func.count(TrackedObject.id))) == 1
    assert await db_session.scalar(select(func.count(PlateObservation.id))) == 1


@pytest.mark.parametrize("hidden_reason", ["unfinished", "processing", "invalidated", "ignored"])
async def test_retirement_does_not_publish_unfinished_or_invalidated_analysis(
    client, db_session, analysed_history, hidden_reason
):
    journey, recording, _ = analysed_history
    recording.state = RecordingState.DELETED
    recording.file_missing = True
    if hidden_reason == "unfinished":
        # Revisions can commit before summarise. Their presence alone is insufficient.
        recording.processed_at = None
    elif hidden_reason == "processing":
        recording.state = RecordingState.PROCESSING
    elif hidden_reason == "invalidated":
        recording.telemetry_revision = INVALIDATED_REVISION
        recording.detection_revision = INVALIDATED_REVISION
        recording.plate_revision = INVALIDATED_REVISION
    else:
        recording.ignored = True
    await db_session.commit()
    views = await _derived_views(client, journey.id)
    assert all(value == 0 for value in views["totals"].values())
    assert views["journeys"] == []
    assert views["heatmap"]["total_points"] == 0
    assert views["routes"]["journeys"] == 0
    assert (await client.get(f"/api/journeys/{journey.id}")).status_code == 404


async def test_finalized_legacy_retired_history_with_null_revisions_is_visible(
    client, db_session, analysed_history
):
    journey, recording, _ = analysed_history
    recording.state = RecordingState.DELETED
    recording.file_missing = True
    recording.telemetry_revision = None
    recording.detection_revision = None
    recording.plate_revision = None
    await db_session.commit()
    views = await _derived_views(client, journey.id)
    assert views["totals"]["journeys"] == 1
    assert views["totals"]["telemetry_points"] == 3
    assert (await client.get(f"/api/journeys/{journey.id}")).status_code == 200
