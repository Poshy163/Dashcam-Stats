"""Positions already in the database, and getting them put right.

The source fixes in :mod:`app.osd.parser` and :mod:`app.osd.track_quality` stop new bad
coordinates arriving. They do nothing whatsoever for the ones already stored, and on the
library this was built against that is several thousand positions across 995 recordings —
including the ones drawing straight lines across the heat map today.

Two mechanisms cover that, and both are tested here because they answer different halves of
the question. Migration ``0009`` re-judges every stored position from the coordinates alone,
which is fast and needs no video. Bumping the telemetry revision marks every recording as
outdated so the pipeline rebuilds the parts a migration cannot: what the OCR should have
read, and which holes may honestly be interpolated across.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.db.models import Camera, CameraRole, Recording, RecordingState, StageState, TelemetryPoint
from app.osd.reasons import GpsQuality, GpsReason
from app.pipeline.revisions import CURRENT_REVISIONS, outdated_stages

BASE = datetime(2026, 8, 4, 11, 11, 50, tzinfo=UTC)
LAT, LON = -34.7981, 138.6510


async def _recording(session, *, revision: str = "telemetry-v5") -> Recording:
    # The seed already provides the front and rear cameras, so take the existing row
    # rather than inserting a second one against a unique key.
    camera = (
        (await session.execute(select(Camera).where(Camera.role == CameraRole.FRONT)))
        .scalars()
        .first()
    )
    if camera is None:
        camera = Camera(key="camera_0", name="Front", role=CameraRole.FRONT)
        session.add(camera)
        await session.flush()
    recording = Recording(
        filename="20260804111150_camera_0.ts",
        rel_path="20260804111150_camera_0.ts",
        camera_id=camera.id,
        started_at=BASE,
        ended_at=BASE + timedelta(seconds=30),
        duration_s=30.0,
        state=RecordingState.COMPLETED,
        telemetry_state=StageState.DONE,
        telemetry_revision=revision,
    )
    session.add(recording)
    await session.flush()
    return recording


class TestTheStoredVerdictSurvives:
    """The four states have to reach the database, or no consumer can act on them."""

    async def test_a_rejected_position_is_stored_with_its_reason(self, db_session):
        recording = await _recording(db_session)
        db_session.add(
            TelemetryPoint(
                recording_id=recording.id,
                t_offset_s=0.0,
                captured_at=BASE,
                lat=None,
                lon=None,
                has_fix=False,
                gps_quality=str(GpsQuality.REJECTED),
                gps_reason=str(GpsReason.ISOLATED_POSITION_OUTLIER),
            )
        )
        await db_session.flush()

        row = (
            await db_session.execute(
                select(TelemetryPoint).where(TelemetryPoint.recording_id == recording.id)
            )
        ).scalar_one()
        assert row.gps_quality == "rejected"
        assert row.gps_reason == "isolated_position_outlier"

    async def test_the_four_states_are_all_representable(self, db_session):
        recording = await _recording(db_session)
        for offset, quality in enumerate(
            (GpsQuality.VALID, GpsQuality.INTERPOLATED, GpsQuality.REJECTED, GpsQuality.NO_FIX)
        ):
            db_session.add(
                TelemetryPoint(
                    recording_id=recording.id,
                    t_offset_s=float(offset),
                    captured_at=BASE + timedelta(seconds=offset),
                    has_fix=quality in {GpsQuality.VALID, GpsQuality.INTERPOLATED},
                    lat=LAT if quality in {GpsQuality.VALID, GpsQuality.INTERPOLATED} else None,
                    lon=LON if quality in {GpsQuality.VALID, GpsQuality.INTERPOLATED} else None,
                    gps_quality=str(quality),
                )
            )
        await db_session.flush()

        stored = (
            (
                await db_session.execute(
                    select(TelemetryPoint.gps_quality)
                    .where(TelemetryPoint.recording_id == recording.id)
                    .order_by(TelemetryPoint.t_offset_s)
                )
            )
            .scalars()
            .all()
        )
        assert stored == ["valid", "interpolated", "rejected", "no_fix"]

    async def test_a_segment_break_is_queryable(self, db_session):
        """The route layer reads this in SQL; it cannot unpack a JSON blob per row."""
        recording = await _recording(db_session)
        db_session.add(
            TelemetryPoint(
                recording_id=recording.id,
                t_offset_s=0.0,
                captured_at=BASE,
                lat=LAT,
                lon=LON,
                has_fix=True,
                gps_quality=str(GpsQuality.VALID),
                breaks_segment=True,
            )
        )
        await db_session.flush()

        broken = (
            (
                await db_session.execute(
                    select(TelemetryPoint).where(TelemetryPoint.breaks_segment.is_(True))
                )
            )
            .scalars()
            .all()
        )
        assert len(broken) == 1


class TestExistingRecordingsAreRebuilt:
    """Case 14. A fix that only helps new footage does not fix this library."""

    def test_the_telemetry_revision_moved(self):
        """Recordings carry the revision that produced them, and v4 predates every rule
        added here. If this did not change, nothing already processed would be revisited."""
        assert CURRENT_REVISIONS["telemetry"] == "telemetry-v5"

    def test_a_recording_processed_by_the_old_pipeline_is_outdated(self):
        class Stub:
            metadata_revision = CURRENT_REVISIONS["metadata"]
            telemetry_revision = "telemetry-v4"
            detection_revision = CURRENT_REVISIONS["detection"]
            plate_revision = CURRENT_REVISIONS["plates"]

        assert "telemetry" in outdated_stages(Stub())

    def test_a_freshly_processed_recording_is_not_outdated(self):
        class Stub:
            metadata_revision = CURRENT_REVISIONS["metadata"]
            telemetry_revision = CURRENT_REVISIONS["telemetry"]
            detection_revision = CURRENT_REVISIONS["detection"]
            plate_revision = CURRENT_REVISIONS["plates"]

        assert outdated_stages(Stub()) == []

    def test_rebuilding_telemetry_also_rebuilds_what_was_derived_from_it(self):
        """Sightings carry a copy of a telemetry coordinate. Correcting the source without
        recomputing them leaves the wrong position drawn on the map indefinitely."""
        from app.pipeline.orchestrator import expand_stages

        stages = expand_stages(["telemetry"])
        assert "detection" in stages
        assert "plates" in stages


class TestTheMigrationRepairsStoredRows:
    """What migration 0009 does, exercised directly on its own repair logic."""

    def test_an_isolated_outlier_among_stored_rows_is_identified(self):
        from app.osd.track_quality import Fix, classify

        step = 50.0 / 3.6 / 111_320.0
        fixes = [Fix(t_s=float(i), lat=LAT + i * step, lon=LON) for i in range(30)]
        # The exact fault seen in recording 335: three digits lost from the longitude.
        fixes[15] = Fix(t_s=15.0, lat=fixes[15].lat, lon=138.6)

        verdicts = classify(fixes)
        assert verdicts[15].quality is GpsQuality.REJECTED
        assert sum(v.quality is GpsQuality.REJECTED for v in verdicts) == 1

    @pytest.mark.parametrize(
        ("raw", "has_fix", "expected_quality"),
        [
            (None, True, "valid"),
            ('{"gps_source": "interpolated"}', True, "interpolated"),
            ('{"gps_source": "paired_camera"}', True, "interpolated"),
            ('{"gps_status": "no_fix"}', False, "no_fix"),
            ('{"gps_status": "rejected"}', False, "rejected"),
            (None, False, "rejected"),
        ],
    )
    def test_existing_rows_are_labelled_from_what_was_recorded_at_the_time(
        self, raw, has_fix, expected_quality
    ):
        """Rows predating `quality_json` keep their positions rather than being condemned
        for a field that did not exist when they were written."""
        import importlib.util
        from pathlib import Path

        path = (
            Path(__file__).resolve().parents[1]
            / "backend"
            / "migrations"
            / "versions"
            / "0009_gps_quality.py"
        )
        spec = importlib.util.spec_from_file_location("migration_0009", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        quality, _reason = module._quality_from_json(raw, has_fix)
        assert quality == expected_quality
