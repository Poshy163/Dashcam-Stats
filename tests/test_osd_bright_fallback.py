"""Conservative background recovery uses synthetic positions, never private footage."""

from datetime import datetime

import cv2
import numpy as np
import pytest

from app.osd import glyphs
from app.osd.parser import parse_osd_text

LINE = "2026-01-02 12:34:56 E:123.4567 N:-12.3471 40 km/h"


def rendered(text=LINE):
    image = np.full((50, 1700), 10, dtype=np.uint8)
    left = 3
    for char in text:
        if char == " ":
            left += 26
            continue
        cv2.putText(image, char, (left, 40), cv2.FONT_HERSHEY_SIMPLEX, 1, 225, 2, cv2.LINE_8)
        left += cv2.getTextSize(char, cv2.FONT_HERSHEY_SIMPLEX, 1, 2)[0][0] + 3
    return image


@pytest.fixture
def templates():
    training = "-0123456789.:ENkm/h"
    pieces = glyphs.segment_glyphs(glyphs.binarise(rendered(training)))
    assert len(pieces) == len(training)
    return glyphs.GlyphTemplates(
        {char: glyphs.normalise(piece.bitmap) for char, piece in zip(training, pieces)}
    )


def noisy(image):
    result = image.copy()
    # A bright scene stripe welds glyphs at the old threshold, without changing text.
    result[27:29] = np.maximum(result[27:29], 180)
    return result


def test_bright_scene_recovers_complete_agreeing_reading(templates):
    image = noisy(rendered())
    old_text, old_confidence = glyphs.decode_line(glyphs.binarise(image), templates)
    assert not parse_osd_text(old_text, confidence=old_confidence).has_fix
    mask, text, confidence = glyphs.decode_strip(image, templates)
    reading = parse_osd_text(text, confidence=confidence)
    assert reading.has_fix
    assert (reading.lat, reading.lon, reading.speed_kmh) == (-12.3471, 123.4567, 40)
    assert reading.captured_at == datetime(2026, 1, 2, 12, 34, 56)
    assert glyphs.decode_line(mask, templates) == (text, confidence)


@pytest.mark.parametrize(
    "text", [LINE, LINE.replace("123.4567", "00.0000").replace("-12.3471", "00.0000")]
)
def test_valid_and_explicit_no_fix_are_unchanged_without_fallback(text, templates, monkeypatch):
    image = rendered(text)
    expected_mask = glyphs.binarise(image)
    expected = glyphs.decode_line(expected_mask, templates)

    def unexpected(*args, **kwargs):
        pytest.fail("readable original must not enter fallback")

    monkeypatch.setattr(cv2, "connectedComponentsWithStats", unexpected)
    mask, decoded, confidence = glyphs.decode_strip(image, templates)
    assert np.array_equal(mask, expected_mask)
    assert (decoded, confidence) == expected


@pytest.mark.parametrize(
    "partial",
    [
        # A speed digit became the fourth decimal of a truncated latitude.
        LINE.replace("3471 40 km/h", "347 0 E/h"),
        # A wrong but well-shaped coordinate on a low contrast background.
        LINE.replace("3471 40 km/h", "3484 /"),
    ],
)
def test_partial_position_can_be_corrected_by_clearer_printed_fields(partial, templates):
    image = rendered()
    baseline = (glyphs.binarise(image), partial, 0.90)
    initial = parse_osd_text(partial)
    assert initial.has_fix and initial.speed_kmh is None and initial.lat != -12.3471
    mask, text, confidence = glyphs._recheck_partial_position(image, templates, baseline, initial)
    corrected = parse_osd_text(text)
    assert (corrected.lat, corrected.lon, corrected.speed_kmh) == (-12.3471, 123.4567, 40)
    assert glyphs.decode_line(mask, templates) == (text, confidence)


def test_partial_position_is_preserved_on_disagreement_or_missing_speed_boundary(
    templates, monkeypatch
):
    original = LINE.replace("3471 40 km/h", "3484 /")
    image = rendered()
    baseline = (glyphs.binarise(image), original, 0.90)
    initial = parse_osd_text(original)

    def masks(gray, threshold):
        text = LINE if threshold == 190 else LINE.replace("3471", "3472")
        return glyphs.binarise(rendered(text))

    monkeypatch.setattr(glyphs, "_bright_mask", masks)
    assert glyphs._recheck_partial_position(image, templates, baseline, initial) is baseline
    monkeypatch.setattr(glyphs, "_bright_mask", lambda *_: glyphs.binarise(rendered(original)))
    assert glyphs._recheck_partial_position(image, templates, baseline, initial) is baseline


def test_only_unanimous_printed_boundaries_can_overrule_confident_split_digit(
    templates, monkeypatch
):
    image = rendered()
    mask = glyphs.binarise(image)
    _, confidence = glyphs.decode_line(mask, templates)
    for partial, correct in [
        (LINE.replace("3471 40 km/h", "347 0"), True),
        (LINE.replace("3471 40 km/h", "3470"), False),
    ]:
        baseline = (mask, partial, confidence)
        initial = parse_osd_text(partial)
        result = glyphs._recheck_partial_position(image, templates, baseline, initial)
        assert (result is not baseline) == correct

    partial = LINE.replace("3471 40 km/h", "347 0")
    baseline = (mask, partial, confidence)
    monkeypatch.setattr(
        glyphs,
        "_bright_mask",
        lambda gray, threshold: mask if threshold < 210 else np.zeros_like(mask),
    )
    assert (
        glyphs._recheck_partial_position(image, templates, baseline, parse_osd_text(partial))
        is baseline
    )


def test_fused_digits_require_strong_full_height_matches(templates):
    mask = glyphs.binarise(rendered())
    parts = glyphs.segment_glyphs(mask)
    chars = LINE.replace(" ", "")
    index = chars.index("3471") + 2
    left, right = parts[index : index + 2]
    assert templates.classify(left.bitmap)[0] == "7"
    assert templates.classify(right.bitmap)[0] == "1"
    # This synthetic font has more left padding on '1' than the camera font. Keep
    # the native-sized four-pixel gap rather than asking the fallback to erase it.
    shift = right.x0 - left.x1 - 4
    right_bitmap = right.bitmap.copy()
    mask[:, right.x0 : right.x1] = False
    mask[right.y0 : right.y1, right.x0 - shift : right.x1 - shift] = right_bitmap
    right_start = right.x0 - shift
    fused = mask.copy()
    fused[25, left.x1 - 1 : right_start + 1] = True
    assert len(glyphs.segment_glyphs(fused)) == len(parts) - 1
    recovered = glyphs._split_fused_digits(fused, templates)
    assert recovered is not None
    assert glyphs.decode_line(recovered, templates)[0] == glyphs.decode_line(mask, templates)[0]


def test_missing_fraction_digit_cannot_borrow_speed(templates):
    image = noisy(rendered(LINE.replace("3471 40", "347 40")))
    expected = glyphs.decode_line(glyphs.binarise(image), templates)
    _, text, confidence = glyphs.decode_strip(image, templates)
    assert (text, confidence) == expected


def test_near_tied_different_digit_splits_are_rejected(templates, monkeypatch):
    mask = glyphs.binarise(rendered())
    pieces = glyphs.segment_glyphs(mask)
    index = LINE.replace(" ", "").index("3471") + 2
    left, right = pieces[index : index + 2]
    shift = right.x0 - left.x1 - 4
    right_bitmap = right.bitmap.copy()
    mask[:, right.x0 : right.x1] = False
    mask[right.y0 : right.y1, right.x0 - shift : right.x1 - shift] = right_bitmap
    mask[25, left.x1 - 1 : right.x0 - shift + 1] = True
    classifications = set()

    def equally_plausible(bitmap):
        if bitmap.shape[1] >= 30:
            return "N", 0.2
        char = "7" if bitmap.shape[1] >= 14 else "1"
        classifications.add(char)
        return char, 0.96

    monkeypatch.setattr(templates, "classify", equally_plausible)
    assert glyphs._split_fused_digits(mask, templates) is None
    assert classifications == {"7", "1"}


def test_single_threshold_is_not_enough(templates, monkeypatch):
    monkeypatch.setattr(glyphs, "_BRIGHT_THRESHOLDS", (210,))
    image = noisy(rendered())
    expected = glyphs.decode_line(glyphs.binarise(image), templates)
    _, text, confidence = glyphs.decode_strip(image, templates)
    assert (text, confidence) == expected


def test_disagreeing_masks_preserve_failure(templates, monkeypatch):
    image = noisy(rendered())
    baseline = glyphs.decode_line(glyphs.binarise(image), templates)
    original = glyphs.decode_line
    candidate_calls = 0

    def differing(mask, templates):
        nonlocal candidate_calls
        text, confidence = original(mask, templates)
        if glyphs._COMPLETE_OVERLAY_RE.fullmatch(text):
            candidate_calls += 1
            if candidate_calls > 2:
                return text.replace("123.4567", "123.4568"), confidence
        return text, confidence

    monkeypatch.setattr(glyphs, "decode_line", differing)
    _, text, confidence = glyphs.decode_strip(image, templates)
    assert candidate_calls > 2
    assert (text, confidence) == baseline


def test_scene_only_noise_does_not_become_telemetry(templates):
    rng = np.random.default_rng(201)
    image = rng.integers(150, 235, size=(50, 1700), dtype=np.uint8)
    expected = glyphs.decode_line(glyphs.binarise(image), templates)
    _, text, confidence = glyphs.decode_strip(image, templates)
    assert (text, confidence) == expected


@pytest.mark.parametrize(
    "damaged", [LINE.replace("2026-01", "2026-99"), LINE.replace("40 km/h", "999 km/h")]
)
def test_complete_shape_still_requires_valid_clock_and_speed(damaged, templates):
    image = noisy(rendered(damaged))
    expected = glyphs.decode_line(glyphs.binarise(image), templates)
    _, text, confidence = glyphs.decode_strip(image, templates)
    assert (text, confidence) == expected
