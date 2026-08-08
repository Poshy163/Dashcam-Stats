"""Rejecting frames that decoded into nothing.

Damaged footage does not fail to decode -- it decodes into a flat fill, classically green,
because a zeroed chroma plane renders that way. Four segments in the real corpus do exactly
this. The thumbnail generator happily saved that fill, so the grid showed solid green
rectangles that looked like an application bug rather than a broken file.

Scoring a frame lets a bad one be rejected in favour of a different offset, and lets "no
thumbnail, source damaged" be an honest answer when no offset works. Measured against the
real corrupt files: the green frames score 0, and other offsets in the same files score
12-40, so three of the four now get a usable thumbnail instead of none.
"""

from __future__ import annotations

import numpy as np
import pytest

from app.pipeline.stages import frame_quality


def solid(colour: tuple[int, int, int], size=(240, 320)) -> np.ndarray:
    frame = np.zeros((*size, 3), dtype=np.uint8)
    frame[:, :] = colour
    return frame


def noisy(size=(240, 320), seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.integers(0, 255, (*size, 3), dtype=np.uint8)


class TestDecoderFill:
    def test_green_fill_scores_zero(self):
        """The exact artefact: BGR green with no detail."""
        assert frame_quality(solid((0, 255, 0))) == 0.0

    def test_near_green_fill_with_slight_noise_still_scores_zero(self):
        frame = solid((10, 240, 12))
        frame[::7, ::7] = (14, 244, 16)  # a little dither, still no picture
        assert frame_quality(frame) == 0.0

    @pytest.mark.parametrize("colour", [(0, 0, 0), (255, 255, 255), (128, 128, 128)])
    def test_any_flat_fill_scores_zero(self, colour):
        """Black and grey fills are just as useless as green ones."""
        assert frame_quality(solid(colour)) == 0.0

    def test_empty_or_missing_frame_scores_zero(self):
        assert frame_quality(None) == 0.0
        assert frame_quality(np.zeros((0, 0, 3), dtype=np.uint8)) == 0.0


class TestRealPictures:
    def test_a_detailed_frame_scores_well(self):
        assert frame_quality(noisy()) > 40.0

    def test_a_green_scene_is_not_mistaken_for_fill(self):
        """Foliage is green but still a picture; it must not be discarded."""
        rng = np.random.default_rng(1)
        frame = rng.integers(0, 255, (240, 320, 3), dtype=np.uint8)
        # Push it green while keeping real structure.
        frame[..., 1] = np.clip(frame[..., 1].astype(np.int32) + 40, 0, 255).astype(np.uint8)
        assert frame_quality(frame) > 0.0

    def test_half_damaged_frame_scores_below_a_clean_one(self):
        """Partially-decoded frames are the half-picture-half-green case in the corpus."""
        clean = noisy(seed=2)
        half = clean.copy()
        half[120:, :] = (0, 255, 0)
        assert frame_quality(half) < frame_quality(clean)

    def test_a_dim_night_frame_is_still_acceptable(self):
        """Most of this corpus is night driving; dark must not mean rejected."""
        rng = np.random.default_rng(3)
        frame = (rng.integers(0, 90, (240, 320, 3))).astype(np.uint8)
        assert frame_quality(frame) > 0.0


class TestSelectionOrder:
    def test_better_frames_win(self):
        assert frame_quality(noisy(seed=4)) > frame_quality(solid((0, 255, 0)))

    def test_scores_are_comparable_across_frames(self):
        """The chooser keeps the highest scorer, so ordering has to be meaningful."""
        flat = solid((30, 30, 30))
        textured = noisy(seed=5)
        scores = sorted([frame_quality(flat), frame_quality(textured)])
        assert scores == [0.0, frame_quality(textured)]
