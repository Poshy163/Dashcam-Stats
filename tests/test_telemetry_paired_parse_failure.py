"""Recover unreadable overlay GPS without reversing a real rejection."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.db.models import Camera, Recording, StageState, TelemetryPoint
from app.pipeline.telemetry_quality import (
    _is_explicitly_unavailable,
    recover_from_paired_camera,
)


def _unreadable_quality() -> dict:
    return {
        "source": "overlay_ocr",
        "ocr_status": "partial",
        "gps_status": "parse_failed",
        "gps_source": "none",
        "gps_reason": None,
        "time_status": "parse_failed",
        "time_source": "timeline",
        "interpolated": False,
        "problems": ["timestamp unreadable", "GPS fields unreadable"],
    }


def _unreadable_point(**overrides) -> TelemetryPoint:
    fields = {
        "t_offset_s": 0,
        "has_fix": False,
        "lat": None,
        "lon": None,
        "gps_quality": "rejected",
        "gps_reason": None,
        "breaks_segment": False,
        "quality_json": _unreadable_quality(),
    }
    fields.update(overrides)
    return TelemetryPoint(**fields)


@pytest.mark.parametrize(
    ("column_changes", "json_changes"),
    [
        ({"gps_quality": "no_fix"}, {}),
        ({"gps_reason": "invalid_lat_lon"}, {}),
        ({"gps_reason": "implied_speed_outlier"}, {}),
        ({"gps_reason": "timestamp_mismatch"}, {}),
        ({}, {"gps_reason": "coordinate_parse_failure"}),
        ({}, {"gps_reason": "isolated_position_outlier"}),
        ({}, {"gps_reason": "no_fix"}),
        ({}, {"gps_status": "no_fix"}),
        ({}, {"gps_status": "rejected"}),
        ({}, {"gps_status": "valid", "gps_source": "direct"}),
        ({}, {"gps_status": "low_confidence"}),
        ({}, {"gps_source": "paired_camera"}),
        ({}, {"gps_source": "interpolated"}),
        ({}, {"interpolated": True}),
        ({}, {"time_status": "rejected"}),
        ({}, {"time_status": "low_confidence"}),
        ({}, {"time_status": None}),
        ({}, {"time_source": None}),
        ({}, {"source": None}),
        ({"breaks_segment": True}, {}),
        ({"lat": -34.8}, {}),
        ({"lon": 138.6}, {}),
        ({"has_fix": True}, {}),
    ],
)
def test_parse_failure_exception_keeps_contradictory_evidence_blocked(column_changes, json_changes):
    point = _unreadable_point(**column_changes)
    point.quality_json.update(json_changes)
    assert _is_explicitly_unavailable(point, point.quality_json)


@pytest.mark.parametrize(
    ("time_status", "time_source"), [("parse_failed", "timeline"), ("valid", "overlay")]
)
def test_explicit_unreadable_field_is_not_a_disproven_position(time_status, time_source):
    point = _unreadable_point()
    point.quality_json.update(time_status=time_status, time_source=time_source)
    assert not _is_explicitly_unavailable(point, point.quality_json)


async def test_paired_recovery_fills_only_exact_time_parse_failure_and_preserves_trusted(
    db_session,
):
    cameras = list((await db_session.execute(select(Camera).order_by(Camera.id))).scalars())
    now = datetime(2026, 9, 28, 0, 0, tzinfo=UTC)
    recordings = [
        Recording(
            rel_path=f"parse-failure-camera-{camera.id}.ts",
            filename=f"parse-failure-camera-{camera.id}.ts",
            size_bytes=1,
            camera_id=camera.id,
            started_at=now,
            ended_at=now + timedelta(seconds=6),
            telemetry_state=StageState.DONE,
        )
        for camera in cameras[:2]
    ]
    db_session.add_all(recordings)
    await db_session.flush()
    target, source = recordings
    # The two-second target has no donor at its timestamp. The remaining cases
    # have donors but contain explicit evidence that recovery must not erase.
    targets = [
        _unreadable_point(recording_id=target.id, captured_at=now),
        _unreadable_point(
            recording_id=target.id,
            t_offset_s=1,
            captured_at=now + timedelta(seconds=1),
            has_fix=True,
            lat=-34.8,
            lon=138.6,
            gps_quality="valid",
            quality_json={"gps_status": "valid", "gps_source": "direct", "problems": []},
        ),
        _unreadable_point(
            recording_id=target.id,
            t_offset_s=2,
            captured_at=now + timedelta(seconds=2),
        ),
        _unreadable_point(
            recording_id=target.id,
            t_offset_s=3,
            captured_at=now + timedelta(seconds=3),
            gps_reason="isolated_position_outlier",
        ),
        _unreadable_point(
            recording_id=target.id,
            t_offset_s=4,
            captured_at=now + timedelta(seconds=4),
            quality_json={**_unreadable_quality(), "time_status": "rejected"},
        ),
        _unreadable_point(
            recording_id=target.id,
            t_offset_s=5,
            captured_at=now + timedelta(seconds=5),
            gps_quality="no_fix",
            quality_json={**_unreadable_quality(), "gps_status": "no_fix"},
        ),
    ]
    donors = [
        TelemetryPoint(
            recording_id=source.id,
            t_offset_s=offset,
            captured_at=now + timedelta(seconds=offset),
            has_fix=True,
            lat=-34.8,
            lon=138.6,
            speed_kmh=20,
            gps_quality="valid",
            quality_json={"gps_status": "valid", "gps_source": "direct"},
        )
        for offset in (0, 1, 3, 4, 5)
    ]
    db_session.add_all([*targets, *donors])
    await db_session.flush()
    protected_before = [
        (p.has_fix, p.lat, p.lon, p.gps_quality, p.gps_reason, deepcopy(p.quality_json))
        for p in targets[1:] + donors
    ]
    original_problems = list(targets[0].quality_json["problems"])

    assert await recover_from_paired_camera(db_session, target) == 1
    assert targets[0].has_fix
    assert targets[0].gps_quality == "interpolated"
    assert targets[0].quality_json["gps_source"] == "paired_camera"
    assert targets[0].quality_json["paired_recording_id"] == source.id
    assert targets[0].quality_json["problems"] == original_problems
    assert [
        (p.has_fix, p.lat, p.lon, p.gps_quality, p.gps_reason, p.quality_json)
        for p in targets[1:] + donors
    ] == protected_before
    assert target.gps_recovered_count == 1
    assert target.gps_point_count == 2
    assert target.gps_ocr_gap_count == 3
    assert target.gps_no_fix_count == 1
    assert await recover_from_paired_camera(db_session, target) == 0


async def test_one_way_recovery_never_fills_partner_holes(db_session):
    cameras = list((await db_session.execute(select(Camera).order_by(Camera.id))).scalars())
    now = datetime(2026, 9, 28, 0, 0, tzinfo=UTC)
    recordings = [
        Recording(
            rel_path=f"one-way-camera-{camera.id}.ts",
            filename=f"one-way-camera-{camera.id}.ts",
            size_bytes=1,
            camera_id=camera.id,
            started_at=now,
            ended_at=now + timedelta(seconds=2),
            telemetry_state=StageState.DONE,
        )
        for camera in cameras[:2]
    ]
    db_session.add_all(recordings)
    await db_session.flush()
    target, partner = recordings
    target_hole = _unreadable_point(recording_id=target.id, captured_at=now)
    partner_hole = _unreadable_point(
        recording_id=partner.id, t_offset_s=1, captured_at=now + timedelta(seconds=1)
    )
    db_session.add_all(
        [
            target_hole,
            partner_hole,
            TelemetryPoint(
                recording_id=target.id,
                t_offset_s=1,
                captured_at=now + timedelta(seconds=1),
                has_fix=True,
                lat=-34.8,
                lon=138.6,
                gps_quality="valid",
                quality_json={"gps_status": "valid", "gps_source": "direct"},
            ),
            TelemetryPoint(
                recording_id=partner.id,
                t_offset_s=0,
                captured_at=now,
                has_fix=True,
                lat=-34.8,
                lon=138.6,
                gps_quality="valid",
                quality_json={"gps_status": "valid", "gps_source": "direct"},
            ),
        ]
    )
    await db_session.flush()
    original_partner = deepcopy(partner_hole.quality_json)
    assert await recover_from_paired_camera(db_session, target, bidirectional=False) == 1
    assert target_hole.has_fix
    assert not partner_hole.has_fix
    assert partner_hole.quality_json == original_partner
    assert partner.gps_recovered_count == 0
    # Existing callers retain their two-way behavior.
    assert await recover_from_paired_camera(db_session, target) == 1
    assert partner_hole.has_fix


@pytest.mark.parametrize("original_speed", [None, 18.0])
async def test_refused_copy_restores_speed_and_recounts_net_zero_recovery(
    db_session, original_speed
):
    cameras = list((await db_session.execute(select(Camera).order_by(Camera.id))).scalars())
    now = datetime(2026, 9, 28, 0, 0, tzinfo=UTC)
    recordings = [
        Recording(
            rel_path=f"outlier-camera-{camera.id}.ts",
            filename=f"outlier-camera-{camera.id}.ts",
            size_bytes=1,
            camera_id=camera.id,
            started_at=now,
            ended_at=now + timedelta(seconds=3),
            telemetry_state=StageState.DONE,
        )
        for camera in cameras[:2]
    ]
    db_session.add_all(recordings)
    await db_session.flush()
    target, source = recordings
    target.gps_ocr_gap_count = 1
    target.gps_point_count = 2
    hole = _unreadable_point(
        recording_id=target.id,
        t_offset_s=1,
        captured_at=now + timedelta(seconds=1),
        speed_kmh=original_speed,
    )
    trusted = [
        TelemetryPoint(
            recording_id=target.id,
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
    donor = TelemetryPoint(
        recording_id=source.id,
        t_offset_s=1,
        captured_at=now + timedelta(seconds=1),
        has_fix=True,
        lat=-34.0,
        lon=138.6,
        speed_kmh=90,
        gps_quality="valid",
        quality_json={"gps_status": "valid", "gps_source": "direct"},
    )
    db_session.add_all([hole, *trusted, donor])
    await db_session.flush()
    protected_before = [
        (p.lat, p.lon, p.has_fix, p.speed_kmh, p.gps_quality, deepcopy(p.quality_json))
        for p in [*trusted, donor]
    ]
    assert await recover_from_paired_camera(db_session, target, bidirectional=False) == 0
    assert not hole.has_fix
    assert hole.lat is None and hole.lon is None
    assert hole.speed_kmh == original_speed
    assert hole.gps_reason == "implied_speed_outlier"
    assert target.gps_ocr_gap_count == 0
    assert target.gps_rejected_count == 1
    assert target.gps_point_count == 2
    assert target.gps_recovered_count == 0
    assert target.telemetry_problem_count == 1
    assert [
        (p.lat, p.lon, p.has_fix, p.speed_kmh, p.gps_quality, p.quality_json)
        for p in [*trusted, donor]
    ] == protected_before
    assert await recover_from_paired_camera(db_session, target, bidirectional=False) == 0
