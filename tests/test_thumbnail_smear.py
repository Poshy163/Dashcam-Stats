"""A decoder smear must not be chosen as a thumbnail, and must not outrank a real frame.

When a transport stream is cut short the decoder still produces frames for the missing
tail: it repeats the last macroblock row it managed to decode all the way down. The result
is a field of vertical stripes, and it is invisible to a variance test because it is not
flat -- it is the opposite of flat.

Measured on the live server, for recording 676 (a 1 MiB file truncated mid-copy):
  t=0.416 / 0.832 / 1.0 / 1.248  -> one byte-identical smeared frame, frame_quality 80.01
  t=0.0                          -> a real picture,                   frame_quality 34.41
So the corruption scored more than twice the good frame, and because 80.01 clears the
"good enough, stop looking" break, the offset that would have worked was never tried.
"""

from __future__ import annotations

import numpy as np
import pytest

from app.pipeline.stages import _SMEAR_ROW_FRACTION, frame_quality, smear_fraction

rng = np.random.default_rng(20260809)


def smeared_frame(height=1080, width=1920, rows_intact=120):
    """A real scene at the top, the last decoded row replicated down the rest.

    The replicated row is deliberately high-contrast, because that is what makes this
    failure dangerous rather than merely ugly: repeating a detailed row gives the frame
    enormous horizontal variance and therefore an excellent variance score. The live
    example measured std 81.02 overall, with the pure-smear bottom half scoring higher
    than the top -- which is how it beat the real frame 80.01 to 34.41.
    """
    frame = scene(height, width)
    detailed = np.clip(rng.normal(128, 70, size=(width,)), 0, 255).astype(np.uint8)
    frame[rows_intact:] = detailed[None, :, None]
    return frame


def scene(height=1080, width=1920):
    """Something with structure in both directions, like a road ahead."""
    ys = np.linspace(0, 6 * np.pi, height)[:, None]
    xs = np.linspace(0, 8 * np.pi, width)[None, :]
    base = 110 + 55 * np.sin(xs) * np.cos(ys) + 30 * np.sin(ys * 2)
    noisy = base + rng.normal(0, 6, size=(height, width))
    return np.repeat(np.clip(noisy, 0, 255).astype(np.uint8)[:, :, None], 3, axis=2)


class TestTheSmearIsRecognised:
    def test_a_replicated_row_field_scores_high(self):
        assert smear_fraction(smeared_frame()) > 0.5

    def test_a_real_scene_scores_near_zero(self):
        value = smear_fraction(scene())
        assert value < _SMEAR_ROW_FRACTION / 4, f"a real frame scored {value:.4f}"

    def test_a_smeared_frame_is_rejected_outright(self):
        assert frame_quality(smeared_frame()) == 0.0

    def test_the_good_frame_from_the_same_file_survives(self):
        assert frame_quality(scene()) > 0.0

    def test_the_corruption_no_longer_outranks_the_picture(self):
        """The exact inversion measured live: 80.01 for the smear, 34.41 for the frame."""
        assert frame_quality(smeared_frame()) < frame_quality(scene())


class TestItDoesNotRejectRealContent:
    """The rule needs all three conditions; any two of them catch ordinary footage."""

    def test_flat_overcast_sky_above_a_road(self):
        frame = scene()
        # A large, genuinely low-contrast region -- the case a naive dy test would fail.
        frame[:400] = np.clip(200 + rng.normal(0, 3, size=(400, 1920, 3)), 0, 255).astype(np.uint8)
        assert smear_fraction(frame) < _SMEAR_ROW_FRACTION

    def test_night_driving(self):
        dark = np.clip(rng.normal(14, 5, size=(1080, 1920, 3)), 0, 255).astype(np.uint8)
        assert smear_fraction(dark) < _SMEAR_ROW_FRACTION

    def test_vertical_posts_against_a_gradient(self):
        """The deliberate adversary: strong vertical structure and a smooth backdrop."""
        gradient = np.linspace(40, 210, 1080)[:, None] * np.ones((1, 1920))
        posts = np.zeros((1080, 1920))
        posts[:, ::40] = 90
        frame = np.clip(gradient + posts + rng.normal(0, 4, size=(1080, 1920)), 0, 255)
        frame = np.repeat(frame.astype(np.uint8)[:, :, None], 3, axis=2)
        assert smear_fraction(frame) < _SMEAR_ROW_FRACTION, (
            "a fence of posts over a smooth sky was mistaken for decoder smear"
        )

    @pytest.mark.parametrize("size", [(4, 4), (1, 1920), (200, 3)])
    def test_degenerate_shapes_do_not_raise(self, size):
        assert smear_fraction(np.zeros((*size, 3), dtype=np.uint8)) == 0.0


class TestTheSearchReachesTheGoodFrame:
    def test_the_start_of_the_clip_is_tried_early(self):
        """Truncated files are broken at the end, so 0.0 is the offset most likely to work.

        It used to be last in the list, behind an early break that a smear could satisfy.
        """
        import inspect

        from app.pipeline import stages

        source = inspect.getsource(stages._choose_thumbnail)
        offsets = source.split("candidates = [t for t in (")[1].split(")")[0]
        order = [o.strip() for o in offsets.split(",")]
        assert order.index("0.0") <= 1, f"0.0 is not tried early enough: {order}"


@pytest.mark.needs_ffmpeg
class TestAgainstRealDecodedFrames:
    """The synthetic frames above nearly let a false positive through.

    Counting every flat row wherever it appeared, the project's own fixture clips scored
    0.10 to 0.18 -- above the threshold -- so ``frame_quality`` returned 0.0 for perfectly
    good frames and the thumbnail search fell through to whichever offset happened to
    squeak past. The whole suite still passed, because one offset always did.

    Test-pattern video is about as adversarial as this measurement gets: hard vertical
    colour bars with no sensor noise, which is exactly the shape the artefact has. Real
    footage sits three orders of magnitude below the threshold either way, so it could
    never have shown this. These assertions run against actually decoded frames for that
    reason.
    """

    async def test_no_real_frame_is_mistaken_for_a_smear(self, front_clip, rear_clip):
        from app.hardware.ffmpeg import extract_frame

        checked = 0
        for clip in (front_clip, rear_clip):
            for offset in (0.0, 1.0, 2.0, 3.0):
                frame = await extract_frame(clip, offset)
                if frame is None:
                    continue
                checked += 1
                score = smear_fraction(frame)
                assert score < _SMEAR_ROW_FRACTION, (
                    f"{clip.name} at t={offset} scored {score:.4f} against a threshold of "
                    f"{_SMEAR_ROW_FRACTION}; a real frame is being thrown away"
                )
                assert frame_quality(frame) > 0.0
        assert checked >= 6, f"only {checked} frames decoded; the fixtures may be missing"

    async def test_a_real_frame_with_its_tail_replicated_is_caught(self, front_clip):
        """The artefact, built from real decoded pixels rather than a synthetic pattern.

        The replicated row is the most horizontally detailed one in the frame, not an
        arbitrary one, because the fixture is a test pattern: only 26 of its 1080 rows
        carry enough detail to look like anything at all, the rest being flat colour bars
        whose mean horizontal gradient is 0.24. Replicating a flat row downwards produces
        a flat frame, which is a different artefact that the variance test already
        catches. The decoder repeats whatever it last decoded, and on real footage that is
        always scene detail -- which is what makes the smear invisible to variance and
        needs this check.
        """
        from app.hardware.ffmpeg import extract_frame

        frame = await extract_frame(front_clip, 1.0)
        assert frame is not None

        grey = frame.mean(axis=2)
        detailed = int(np.argmax(np.abs(np.diff(grey, axis=1)).mean(axis=1)))

        damaged = frame.copy()
        intact = int(damaged.shape[0] * 0.2)
        damaged[intact:] = frame[detailed]

        assert smear_fraction(damaged) > _SMEAR_ROW_FRACTION
        assert frame_quality(damaged) == 0.0
        assert frame_quality(frame) > 0.0, "the undamaged frame must still be usable"
