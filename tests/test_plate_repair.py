"""Historical repair must be measurable and keep the OCR text attached to its pixels."""

from __future__ import annotations

import numpy as np
import pytest
from sqlalchemy import select

from app.ai.normalise_au import normalise
from app.ai.plates import PlateReading, vote_track_plate
from app.api.routes.content import _observation_out, plate_quality
from app.db.models import Camera, CameraRole, PlateObservation, Recording, StageState, TrackedObject
from app.pipeline import stages
from app.pipeline.plate_repair import validation_key
from app.pipeline.revisions import CURRENT_REVISIONS, outdated_stages


@pytest.mark.parametrize("text", ["S123ABC", "ABC123", "AA123B", "SEA12B", "SXA12B"])
def test_sa_catalogue_covers_standard_legacy_premium_and_euro(text):
    result = normalise(text, region="AU-SA")
    assert result.normalised == text
    assert result.matched and "SA" in result.plausible_states
    assert result.substitutions == 0


async def test_sa_preference_breaks_a_close_tie_but_preserves_clear_interstate_reads():
    from tests.test_plate_quality import _StubOCR, crop

    orientation = stages._PlateOrientation()
    close = _StubOCR(upright=("1ABC234", 0.96), mirrored=("S123ABC", 0.96))
    assert await orientation.read(close, crop(), region="AU-SA") == ("S123ABC", 0.96)
    interstate = _StubOCR(upright=("1ABC234", 0.99), mirrored=("S123ABC", 0.85))
    assert await orientation.read(interstate, crop(), region="AU-SA") == ("1ABC234", 0.99)
    assert normalise("1ABC234", region="AU-SA").normalised == "1ABC234"


async def test_coverage_counts_stale_failed_and_excluded_recordings(db_session):
    camera = Camera(key="rear-test", name="Rear", role=CameraRole.REAR)
    db_session.add(camera)
    await db_session.flush()
    for index, (revision, state, missing) in enumerate(
        [
            (CURRENT_REVISIONS["plates"], StageState.DONE, False),
            ("plates-v3", StageState.DONE, False),
            ("plates-v3", StageState.FAILED, False),
            ("plates-v3", StageState.DONE, True),
            (CURRENT_REVISIONS["plates"], StageState.PENDING, False),
        ]
    ):
        db_session.add(
            Recording(
                rel_path=f"repair-{index}.ts",
                filename=f"repair-{index}.ts",
                size_bytes=100,
                camera_id=camera.id,
                plate_revision=revision,
                plate_state=state,
                file_missing=missing,
                probe_json={"plate_validation_key": validation_key()},
            )
        )
    await db_session.flush()
    result = await plate_quality(db_session)
    assert result["revision"] == "plates-v4"
    rear = result["cameras"][0]
    assert rear["eligible_recordings"] == 4
    assert rear["current_recordings"] == 1
    assert rear["remaining_recordings"] == 3
    assert rear["failed_recordings"] == 1
    assert rear["excluded_recordings"] == 1
    assert rear["observations"] == 0


async def test_saved_text_and_preview_use_each_readings_orientation(db_session, monkeypatch):
    recording = Recording(rel_path="repair.ts", filename="repair.ts", size_bytes=100)
    db_session.add(recording)
    await db_session.flush()
    crop = np.zeros((20, 60, 3), dtype=np.uint8)
    crop[:, :10] = 255
    placed = []
    for index, mirrored in enumerate([True, False]):
        track = TrackedObject(
            recording_id=recording.id,
            track_key=index,
            class_label="car",
            confidence_max=0.9,
            confidence_avg=0.9,
            first_seen_offset_s=0,
            last_seen_offset_s=2,
            duration_s=2,
            frame_count=5,
            best_frame_offset_s=1,
        )
        db_session.add(track)
        reading = PlateReading(
            raw_text=f"ABC12{index}",
            ocr_confidence=0.99,
            detection_confidence=0.95,
            bbox=(0.1, 0.2, 0.3, 0.4),
            crop=crop,
            mirrored=mirrored,
            orientation_margin=0.2,
        )
        placed.append(
            (
                stages._PlateHit(
                    track, normalise(reading.raw_text), vote_track_plate([reading]), crop
                ),
                None,
            )
        )
    await db_session.flush()
    saved = []

    async def save(batch, quality):
        saved.extend(image.copy() for image, _ in batch)
        return [f"test-{len(saved)}-{i}.jpg" for i in range(len(batch))]

    monkeypatch.setattr(stages, "_save_jpegs", save)
    await stages._write_observations(db_session, recording, placed, set(), None)
    await db_session.flush()
    observations = list(
        (await db_session.execute(select(PlateObservation).order_by(PlateObservation.id))).scalars()
    )
    assert len(observations) == 2
    assert np.array_equal(saved[0], crop[:, ::-1])
    assert np.array_equal(saved[1], crop[:, ::-1])
    assert np.array_equal(saved[2], crop)
    assert np.array_equal(saved[3], crop)
    for observation, mirrored in zip(observations, [True, False], strict=True):
        assert observation.bbox["box"] == [0.1, 0.2, 0.3, 0.4]
        assert observation.bbox["mirrored"] is mirrored
        out = _observation_out(observation, recording.filename, "Rear")
        assert out.ocr_mirrored is mirrored
        assert out.orientation_method == "per-crop-dual-ocr-v1"
    coverage = await plate_quality(db_session)
    assert coverage["cameras"][0]["orientation_checked_observations"] == 2
    observations[0].bbox = {"mirrored": True}
    assert _observation_out(observations[0], None, None).ocr_mirrored is None


def test_old_plate_output_requires_only_a_plate_rebuild():
    recording = Recording(
        **{
            f"{stage if stage != 'plates' else 'plate'}_revision": revision
            for stage, revision in CURRENT_REVISIONS.items()
        }
    )
    recording.plate_revision = "plates-v3"
    assert outdated_stages(recording) == ["plates"]
