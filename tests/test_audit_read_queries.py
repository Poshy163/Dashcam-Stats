"""Visible aggregates, paginated triage and bounded OBD chart reads."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, event, select, text

from app.db.models import (
    OBDDiagnostic,
    OBDDrive,
    OBDSample,
    Plate,
    PlateObservation,
    Recording,
    RecordingState,
    StageState,
)
from app.db.session import get_engine, session_scope
from app.pipeline.revisions import CURRENT_REVISIONS

BASE = datetime(2026, 8, 8, 3, tzinfo=UTC)


async def test_recording_availability_filter_keeps_history_accessible(client):
    async with session_scope() as session:
        for name, missing, ignored in (
            ("available", False, False),
            ("missing", True, False),
            ("hidden", True, True),
        ):
            session.add(
                Recording(
                    rel_path=f"{name}.ts",
                    filename=f"{name}.ts",
                    file_missing=missing,
                    ignored=ignored,
                )
            )
    for availability, expected in (
        ("all", {"available.ts", "missing.ts"}),
        ("available", {"available.ts"}),
        ("missing", {"missing.ts"}),
    ):
        response = await client.get("/api/recordings", params={"availability": availability})
        assert response.status_code == 200
        assert {r["filename"] for r in response.json()["items"]} == expected
    assert (await client.get("/api/recordings", params={"availability": "typo"})).status_code == 422
    hidden = await client.get(
        "/api/recordings", params={"availability": "missing", "state": "hidden"}
    )
    assert [r["filename"] for r in hidden.json()["items"]] == ["hidden.ts"]


@pytest.mark.parametrize(
    "sort", ["last_seen_desc", "first_seen_desc", "observations_desc", "confidence_desc"]
)
async def test_plate_order_matches_visible_rollups_with_stable_pages(client, sort):
    async with session_scope() as session:
        for name, rank in (("OLDER", 1), ("NEWER", 2), ("TIED", 2)):
            plate = Plate(
                normalised_text=name,
                display_text=name,
                best_confidence=1.0 if rank == 1 else 0.1,
                observation_count=100 if rank == 1 else 1,
                first_seen_at=BASE + timedelta(days=20 - rank),
                last_seen_at=BASE + timedelta(days=20 - rank),
            )
            session.add(plate)
            await session.flush()
            for index in range(rank + 1):
                hidden = index == rank
                rec = Recording(
                    rel_path=f"{name}-{index}.ts",
                    filename=f"{name}-{index}.ts",
                    state=RecordingState.COMPLETED,
                    ignored=hidden,
                )
                session.add(rec)
                await session.flush()
                session.add(
                    PlateObservation(
                        plate_id=plate.id,
                        recording_id=rec.id,
                        t_offset_s=0,
                        captured_at=BASE + timedelta(days=10 if hidden else rank),
                        raw_text=name,
                        normalised_text=name,
                        ocr_confidence=0.99 if hidden else rank / 3,
                        detection_confidence=0.9,
                    )
                )
    names = []
    for page in (1, 2, 3):
        response = await client.get(
            "/api/plates", params={"sort": sort, "page": page, "page_size": 1}
        )
        assert response.status_code == 200, response.text
        assert response.json()["total"] == 3
        names += [p["normalised_text"] for p in response.json()["items"]]
    assert names == ["TIED", "NEWER", "OLDER"]


async def test_telemetry_triage_is_paginated_and_filtered_in_sql(client):
    async with session_scope() as session:
        for index in range(275):
            session.add(
                Recording(
                    rel_path=f"quality-{index}.ts",
                    filename=f"quality-{index}.ts",
                    state=RecordingState.COMPLETED,
                    telemetry_state=StageState.DONE,
                    telemetry_revision=CURRENT_REVISIONS["telemetry"],
                    telemetry_point_count=10,
                    gps_point_count=8,
                    gps_rejected_count=2,
                    started_at=BASE if index < 270 else BASE - timedelta(days=1),
                )
            )
        session.add(
            Recording(
                rel_path="outdated.ts",
                filename="outdated.ts",
                state=RecordingState.COMPLETED,
                telemetry_state=StageState.DONE,
                telemetry_revision="old",
                telemetry_point_count=10,
                gps_point_count=8,
                gps_rejected_count=2,
                started_at=BASE,
            )
        )
    ids = []
    for page, count in ((1, 100), (2, 100), (3, 70)):
        response = await client.get(
            "/api/telemetry/quality",
            params={
                "page": page,
                "page_size": 100,
                "reason": "gps_rejected",
                "date_from": "2026-08-08",
                "date_to": "2026-08-08",
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["issue_total"] == 270
        assert body["issue_page"] == page
        assert body["issue_pages"] == 3
        assert len(body["issues"]) == count
        assert all("gps_rejected" in row["reasons"] for row in body["issues"])
        ids.extend(row["recording_id"] for row in body["issues"])
    assert len(set(ids)) == 270
    assert ids == sorted(ids, reverse=True)
    assert (await client.get("/api/telemetry/quality", params={"reason": "bad"})).status_code == 422


@pytest.fixture
async def large_drive(db_session, app_config):
    from test_obd_server_import import make_bundle

    from app.ingest.obd_bundle import store_validated_bundle, validate_bundle

    drive_id = "audit_read_drive"
    path = make_bundle(app_config.obd_verified_dir, drive_id)
    checked = validate_bundle(path, config=app_config)
    async with session_scope() as session:
        await store_validated_bundle(session, checked)
        drive = await session.scalar(select(OBDDrive).where(OBDDrive.drive_id == drive_id))
        await session.execute(delete(OBDSample).where(OBDSample.drive_db_id == drive.id))
        drive.started_at = BASE
        drive.finished_at = BASE + timedelta(seconds=4999 * 5)
        drive.sample_count = 5000
        for index in range(5000):
            session.add(
                OBDSample(
                    drive_db_id=drive.id,
                    sample_id=f"audit-{index}",
                    sequence=index,
                    captured_at=BASE + timedelta(seconds=index * 5),
                    ecu_data_status="live",
                    engine_rpm=7500 if index == 3333 else 900 + index % 100,
                    vehicle_speed_kmh=200 if index == 2222 else index % 60,
                    adapter_voltage_v=14.1,
                    raw_json={"unused_producer_payload": "x" * 2048},
                )
            )
        for index in range(7):
            session.add(
                OBDDiagnostic(
                    drive_db_id=drive.id,
                    event_hash=f"audit-{index}",
                    observed_at=BASE + timedelta(seconds=index * 5),
                    kind="connection",
                    payload_json={"index": index},
                )
            )
    return drive_id


async def test_obd_chart_bound_preserves_extrema_and_full_resolution(client, large_drive):
    statements = []

    def capture(_connection, _cursor, statement, _params, _context, _many):
        statements.append(statement)

    engine = get_engine().sync_engine
    event.listen(engine, "before_cursor_execute", capture)
    try:
        response = await client.get(
            f"/api/obd/drives/{large_drive}/series",
            params={"max_points": 200, "signals": "vehicle_speed,engine_rpm"},
        )
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["sampling"]["downsampled"]
    assert body["sampling"]["total_sample_count"] == 5000
    assert 2 <= len(body["samples"]) <= 200
    assert body["samples"][0]["sequence"] == 0
    assert body["samples"][-1]["sequence"] == 4999
    assert max(r["engine_rpm"] for r in body["samples"]) == 7500
    assert max(r["vehicle_speed_kmh"] for r in body["samples"]) == 200
    assert "coolant_temperature_c" not in body["samples"][0]
    assert all("obd_samples.raw_json" not in sql for sql in statements)
    full = await client.get(f"/api/obd/drives/{large_drive}/series")
    assert full.status_code == 200, full.text
    assert len(full.json()["samples"]) == 5000
    assert not full.json()["sampling"]["downsampled"]
    assert full.json()["battery"] == body["battery"]
    assert len(response.content) < len(full.content) / 10


async def test_obd_time_window_diagnostic_pagination_and_query_plan(client, large_drive):
    response = await client.get(
        f"/api/obd/drives/{large_drive}/series",
        params={
            "start": BASE.isoformat(),
            "end": (BASE + timedelta(seconds=25)).isoformat(),
            "signals": "vehicle_speed",
            "diagnostic_page": 2,
            "diagnostic_page_size": 2,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert [r["sequence"] for r in body["samples"]] == list(range(6))
    assert body["diagnostic_total"] == 6
    assert body["diagnostic_pages"] == 3
    assert [d["payload"]["index"] for d in body["diagnostics"]] == [2, 3]
    async with session_scope() as session:
        rows = await session.execute(
            text(
                "EXPLAIN QUERY PLAN SELECT id FROM obd_samples WHERE drive_db_id=1 AND captured_at >= '2026-08-08' AND captured_at <= '2026-08-09'"
            )
        )
        assert any(
            "SEARCH obd_samples" in row[3] and "ix_obd_samples_drive_time" in row[3] for row in rows
        )
    for params in (
        {"start": "2026-08-08T00:00:00"},
        {"signals": "not-a-signal"},
        {"max_points": 2},
    ):
        assert (
            await client.get(f"/api/obd/drives/{large_drive}/series", params=params)
        ).status_code == 422
