"""Glyph segmentation, template learning and end-to-end overlay decoding.

Every bug guarded here was a real one found while validating against the live corpus, and
each produced *silently wrong* telemetry rather than an error:

* a height floor tuned for digits deleted the two-pixel ``-`` separator, which shifted
  every positional label and taught the classifier that ``-`` was a ``0``;
* tolerating a one-pixel gap welded adjacent glyphs together, so ``2026`` segmented as
  three characters;
* a training set with no ``9`` in any stable timestamp field made every 9 decode as a 0
  *at high confidence*, so no threshold could have caught it.
"""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pytest

from app.osd.glyphs import (
    REQUIRED_CHARACTERS,
    GlyphTemplates,
    TemplateLearner,
    binarise,
    decode_line,
    expected_time_from_filename,
    missing_characters,
    segment_glyphs,
    select_training_set,
    stable_field_digits,
)


def render_text(text: str, *, height: int = 50, char_w: int = 20, gap: int = 3) -> np.ndarray:
    """Draw a synthetic overlay strip with a distinct bitmap per character.

    Not a real font -- the point is to reproduce the *geometry* the segmenter depends on:
    separators much shorter than digits, a fixed baseline, a one-pixel-scale inter-glyph
    gap, and wide gaps between fields.
    """
    # Sized from the content: a fixed-width canvas silently clips the tail of a full
    # overlay line, which looks exactly like a segmentation bug.
    width = 8 + len(text) * (char_w + gap * 4)
    strip = np.zeros((height, width), dtype=np.uint8)
    x = 4
    baseline = 43
    digit_top = 10

    for ch in text:
        if ch == " ":
            x += char_w + gap * 4
            continue
        if ch == "-":
            # Two pixels tall, floating at mid height. The glyph a naive filter deletes.
            strip[25:27, x : x + char_w] = 255
        elif ch == ".":
            strip[baseline - 6 : baseline, x : x + 8] = 255
        elif ch == ":":
            strip[20:26, x : x + 7] = 255
            strip[baseline - 6 : baseline, x : x + 7] = 255
        else:
            # Give every other character a unique but stable pattern so the classifier has
            # something real to distinguish.
            seed = ord(ch) % 7
            strip[digit_top:baseline, x : x + char_w] = 255
            strip[digit_top + 4 + seed * 3 : digit_top + 7 + seed * 3, x + 4 : x + char_w - 4] = 0
        x += char_w + gap
    return strip


class TestSegmentation:
    def test_short_separators_are_not_discarded(self):
        # A '-' is two pixels tall. Losing it shifts every positional label after it.
        strip = render_text("2026-08-04")
        glyphs = segment_glyphs(binarise(strip))
        assert len(glyphs) == 10, f"expected 10 glyphs, got {len(glyphs)}"
        heights = [g.height for g in glyphs]
        assert min(heights) <= 3, "the short dash glyph was dropped"

    def test_adjacent_glyphs_are_not_merged(self):
        # gap=0 is deliberate: this font sets characters one pixel apart, so tolerating a
        # single blank column welds neighbours together.
        strip = render_text("2026")
        assert len(segment_glyphs(binarise(strip))) == 4

    def test_full_overlay_line_segments_into_the_expected_count(self):
        # 18 timestamp glyphs + E : 138 . 6769 + N : - 34 . 8088 + 68 + km/h
        text = "2026-08-04 17:44:39 E:138.6769 N:-34.8088 68 km/h"
        expected = sum(1 for c in text if c != " ")
        glyphs = segment_glyphs(binarise(render_text(text)))
        assert len(glyphs) == expected

    def test_noise_specks_are_rejected(self):
        strip = render_text("2026")
        far_right = strip.shape[1] - 20
        strip[5, far_right] = 255  # single pixel
        strip[7, far_right + 5 : far_right + 7] = 255  # two pixels
        assert len(segment_glyphs(binarise(strip))) == 4

    def test_empty_strip_yields_nothing(self):
        assert segment_glyphs(binarise(np.zeros((50, 900), dtype=np.uint8))) == []


class TestBinarise:
    def test_bright_text_survives_a_dark_background(self):
        strip = np.full((50, 200), 20, dtype=np.uint8)
        strip[10:40, 20:40] = 240
        mask = binarise(strip)
        assert mask[25, 30]
        assert not mask[5, 5]

    def test_dim_strip_falls_back_to_a_relative_threshold(self):
        # Heavily underexposed night frames never reach the absolute brightness floor; a
        # fixed threshold alone would return an entirely empty mask.
        strip = np.full((50, 200), 10, dtype=np.uint8)
        strip[10:40, 20:40] = 120
        assert binarise(strip).any()


class TestTemplateLearning:
    def _learn(self, samples: list[tuple[str, datetime]]) -> GlyphTemplates:
        learner = TemplateLearner()
        for text, when in samples:
            learner.observe_strip(binarise(render_text(text)), when)
        return learner.build(min_samples=1)

    def test_learns_digits_and_separators_by_position(self):
        templates = self._learn(
            [
                ("2026-08-04 17:44:39 E:138.6769 N:-34.8088 68 km/h", datetime(2026, 8, 4, 17)),
            ]
            * 3
        )
        for ch in "20648-:":
            assert ch in templates.templates, f"never learned {ch!r}"

    def test_round_trips_the_timestamp_it_was_trained_on(self):
        text = "2026-08-04 17:44:39 E:138.6769 N:-34.8088 68 km/h"
        templates = self._learn([(text, datetime(2026, 8, 4, 17))] * 3)
        decoded, confidence = decode_line(binarise(render_text(text)), templates)
        assert decoded.startswith("2026-08-04")
        assert confidence > 0.5

    def test_missing_characters_is_honest_about_gaps(self):
        templates = self._learn(
            [
                ("2026-08-04 17:44:39 E:138.6769 N:-34.8088 68 km/h", datetime(2026, 8, 4, 17)),
            ]
            * 3
        )
        # '5' and '9' never appear in the stable fields of this one timestamp.
        assert "5" in missing_characters(templates)


class TestDigitCoverage:
    """Selecting training footage that actually contains every digit."""

    def test_stable_field_digits_ignores_minutes_and_seconds(self):
        # Minutes and seconds drift against the filename; labelling from them would teach
        # the classifier a wrong digit on a two-second clock skew.
        digits = stable_field_digits(datetime(2026, 8, 4, 17, 55, 59))
        assert digits == set("2026080417")
        # 5 and 9 appear only in the minute and second fields, so they must not leak in.
        assert "5" not in digits
        assert "9" not in digits

    def test_selection_covers_all_ten_digits_when_possible(self):
        candidates = [
            (f"{d:%Y%m%d%H%M%S}_camera_0.ts", d)
            for d in [
                datetime(2026, 8, 4, 17),
                datetime(2026, 7, 31, 9),
                datetime(2026, 8, 5, 15),
                datetime(2026, 8, 3, 6),
            ]
        ]
        chosen = select_training_set(candidates, limit=4)
        covered: set[str] = set()
        for _, when in chosen:
            covered |= stable_field_digits(when)
        assert covered == set("0123456789"), f"missing {set('0123456789') - covered}"

    def test_selection_is_bounded_by_limit(self):
        candidates = [
            (f"2026080{i % 9 + 1}12{i:02d}00_camera_0.ts", datetime(2026, 8, (i % 9) + 1, 12))
            for i in range(40)
        ]
        assert len(select_training_set(candidates, limit=6)) <= 6

    def test_required_characters_covers_every_digit_and_separator(self):
        assert set("0123456789") <= REQUIRED_CHARACTERS
        assert {"-", ":", "."} <= REQUIRED_CHARACTERS


class TestFilenameTime:
    def test_parses_the_corpus_naming_convention(self):
        assert expected_time_from_filename("20260804174353_camera_0.ts") == datetime(
            2026, 8, 4, 17, 43, 53
        )

    @pytest.mark.parametrize("name", ["notatimestamp.ts", "2026_camera_0.ts", ""])
    def test_returns_none_rather_than_guessing(self, name):
        assert expected_time_from_filename(name) is None

    def test_rejects_an_impossible_encoded_date(self):
        assert expected_time_from_filename("20261399999999_camera_0.ts") is None


class TestTemplatePersistence:
    def test_round_trips_through_disk(self, tmp_path):
        learner = TemplateLearner()
        for _ in range(3):
            learner.observe_strip(
                binarise(render_text("2026-08-04 17:44:39 E:138.6769 N:-34.8088 68 km/h")),
                datetime(2026, 8, 4, 17),
            )
        original = learner.build(min_samples=1)

        path = tmp_path / "osd_templates.npz"
        original.save(path)
        loaded = GlyphTemplates.load(path)

        assert loaded is not None
        assert set(loaded.templates) == set(original.templates)
        for ch, tpl in original.templates.items():
            assert np.allclose(loaded.templates[ch], tpl)

    def test_missing_file_loads_as_none(self, tmp_path):
        assert GlyphTemplates.load(tmp_path / "absent.npz") is None
