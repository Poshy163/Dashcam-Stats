"""Rear-camera plate previews must read the right way round.

The orientation vote already existed and worked -- it is what lets the recogniser read a
mirrored rear channel at all. What it was never applied to was the *picture*. OCR read a
flipped copy of the crop, the crop itself was saved exactly as the camera produced it, and
the plate page ended up showing a backwards plate above correctly-read text, which looks
like the text is wrong rather than the image.

The fix has to stop at the crop. Flipping the frame instead would put every stored
bounding box on the wrong side of a video the player still shows unmirrored, so detection,
tracking and every ``bbox`` stay in the source's coordinate space and only the standalone
preview image is turned round.
"""

from __future__ import annotations

import numpy as np
import pytest

from app.pipeline.stages import _PlateOrientation, _readable


def asymmetric() -> np.ndarray:
    """A crop that cannot be confused with its own mirror image."""
    crop = np.zeros((8, 16, 3), dtype=np.uint8)
    crop[:, :4] = 255
    return crop


class TestTurningThePreviewRound:
    def test_a_mirrored_recording_gets_an_un_mirrored_preview(self):
        crop = asymmetric()
        preview = _readable(crop, mirrored=True)
        assert preview is not None
        assert np.array_equal(preview, crop[:, ::-1]), "the rear preview was saved mirrored"

    def test_the_front_camera_is_untouched(self):
        crop = asymmetric()
        assert np.array_equal(_readable(crop, mirrored=False), crop)

    def test_flipping_twice_is_the_original(self):
        crop = asymmetric()
        assert np.array_equal(_readable(_readable(crop, True), True), crop)

    def test_nothing_to_flip_is_not_an_error(self):
        assert _readable(None, mirrored=True) is None

    def test_the_array_is_real_rather_than_a_reversed_view(self):
        """cv2.imwrite and the recogniser both want contiguous memory; a negative-stride
        view silently produces the wrong bytes rather than an error."""
        preview = _readable(asymmetric(), mirrored=True)
        assert preview.flags["C_CONTIGUOUS"]


class TestSettlingTheVote:
    """``mirrored`` was decided only after eight sampled readings, and the crops are saved
    after the loop -- so a recording with fewer readable plates than that reached the write
    phase with the question still open and saved every preview unflipped."""

    def test_a_short_recording_still_reaches_a_verdict(self):
        orientation = _PlateOrientation()
        assert orientation.mirrored is None
        # Three readings, all of which looked like real registrations upside down.
        orientation._flipped = 3
        orientation._sampled = 3
        assert orientation.resolve() is True

    def test_silence_means_not_mirrored(self):
        orientation = _PlateOrientation()
        assert orientation.resolve() is False, (
            "with no evidence either way the safe default is to change nothing"
        )

    def test_a_decision_already_made_is_not_revisited(self):
        orientation = _PlateOrientation()
        orientation.mirrored = False
        orientation._flipped = 99
        assert orientation.resolve() is False

    def test_a_narrow_win_does_not_flip_the_recording(self):
        """The measured ratio on real footage is 14.6:1 one way and 8.5:1 the other, so a
        3:1 threshold is nowhere near either. Four against three is noise."""
        orientation = _PlateOrientation()
        orientation._flipped, orientation._as_is, orientation._sampled = 4, 3, 7
        assert orientation.resolve() is False

    def test_the_verdict_is_reported(self):
        orientation = _PlateOrientation()
        orientation._flipped, orientation._sampled = 5, 5
        orientation.resolve()
        described = orientation.describe()
        assert described["mirrored"] is True
        assert described["votes_mirrored"] == 5
        assert described["orientation_samples"] == 5


class TestBoundingBoxesAreNotMoved:
    async def test_the_stored_box_stays_in_the_source_frame(self, db_session, monkeypatch):
        """A flipped preview must not imply a flipped coordinate space.

        Runs the real stage with a detector that reports one plate at a known place, and
        checks the stored box against the frame rather than against the picture.
        """
        from sqlalchemy import select

        from app.db.models import PlateObservation, Recording, RecordingState, TrackedObject
        from app.pipeline import stages

        recording = Recording(
            rel_path="mirror.ts",
            filename="20260803130528_camera_1.ts",
            size_bytes=1024,
            state=RecordingState.PROCESSING,
            duration_s=60.0,
        )
        db_session.add(recording)
        await db_session.flush()
        db_session.add(
            TrackedObject(
                recording_id=recording.id,
                track_key=1,
                class_label="car",
                confidence_max=0.9,
                confidence_avg=0.85,
                first_seen_offset_s=1.0,
                last_seen_offset_s=3.0,
                duration_s=2.0,
                frame_count=8,
                best_frame_offset_s=2.0,
                # Normalised to the frame, as the tracker stores them: pixels 100..300
                # across and 100..250 down on the 640x480 frame decoded below.
                best_bbox=[100 / 640, 100 / 480, 300 / 640, 250 / 480],
            )
        )
        await db_session.flush()

        class FakeDetector:
            async def detect(self, image, *, min_width_px=0):
                # Left third of the vehicle crop, in crop-normalised coordinates.
                return [((0.1, 0.6, 0.4, 0.8), 0.95)]

        class FakeOCR:
            async def read(self, image):
                return "S192DKX", 0.97

        async def fake_models():
            return FakeDetector(), FakeOCR()

        async def fake_iter_frames(path, **kwargs):
            yield 0.0, np.full((480, 640, 3), 120, dtype=np.uint8)

        monkeypatch.setattr(stages, "_shared_plate_models", fake_models)
        monkeypatch.setattr(stages, "iter_frames", fake_iter_frames)
        monkeypatch.setattr(stages, "resolve_footage_path", lambda *a, **k: "mirror.ts")

        result = await stages.stage_plates(db_session, recording)
        assert result.ok

        observation = (
            await db_session.execute(
                select(PlateObservation).where(PlateObservation.recording_id == recording.id)
            )
        ).scalar_one()

        box = observation.bbox["box"]
        assert box[0] < box[2] and box[1] < box[3], "the stored box is inside out"
        # The plate sat on the left of the vehicle box, and the stored coordinates still
        # say so -- they describe the video, which the player shows unmirrored.
        assert box[0] == pytest.approx(100 / 640 + 0.1 * (200 / 640), abs=0.02)
        assert "mirrored" in observation.bbox, (
            "nothing records whether the saved crop was turned round relative to the frame"
        )
