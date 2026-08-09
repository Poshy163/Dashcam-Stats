"""Half the plate database was not plates.

Of 161 stored plates on the live library, 71 matched no Australian format: KEECE off a ute
door, ARROW off a road sign, ATOYOT off a tailgate, GRROWWRRR, DYJIALE, 5078, SL, TE. They
were stored because the only gate was OCR confidence, and confidence says how sure the
recogniser is that it read the characters correctly -- not that what it read is a
registration. It was completely right about all of them.

Two causes, and the larger one is not a filtering problem at all: the rear camera channel
is horizontally mirrored, so every plate it saw was recognised backwards. Of the 74 stored
rear observations 64% matched nothing, against 21% on the front, and the signature is
arithmetic -- 65% of unmatched seven-character reads end in "2", which is a mirrored
leading "S", against a 3% base rate. "ATOYOT" is "TOYOTA" spelled backwards.
"""

from __future__ import annotations

import numpy as np
import pytest

from app.ai.normalise_au import AU_PATTERNS, normalise
from app.pipeline.stages import _ORIENTATION_SAMPLE, _PlateOrientation

#: Read off vehicles and signage on the live library, with their stored confidences.
NOT_PLATES = [
    ("KEECE", 0.998),
    ("ARROW", 0.975),
    ("ATOYOT", 0.991),
    ("GRROWWRRR", 0.721),
    ("DYJIALE", 0.500),
    ("2369", 0.812),
    ("5078", 0.513),
]

#: Genuine South Australian and general-issue plates from the same library.
REAL_PLATES = ["S352CJS", "S292CXG", "S812AEU", "S387CTA", "HBB502", "EKD062", "JXJ121"]


class TestTheCatalogueRejectsWords:
    def test_a_blanket_six_letter_mask_is_not_a_shape(self):
        """AU:GEN:AAAAAA accepted any six-letter word, which is how TOYOTA got in."""
        names = {p.name for p in AU_PATTERNS}
        assert "AU:GEN:AAAAAA" not in names
        assert "AU:GEN:000000" not in names

    @pytest.mark.parametrize("text,confidence", NOT_PLATES)
    def test_signage_and_badges_do_not_match(self, text, confidence):
        result = normalise(text, region="AU")
        assert not result.matched, (
            f"{text!r} (stored at {confidence:.3f} confidence) still matches "
            f"{result.pattern_name}, so it would be kept as a plate"
        )

    @pytest.mark.parametrize("text", REAL_PLATES)
    def test_genuine_plates_still_match(self, text):
        result = normalise(text, region="AU")
        assert result.matched, f"{text} is a real plate and no longer matches"

    def test_the_mixed_generic_masks_survive(self):
        """Only the two blanket masks go; the useful splits stay."""
        names = {p.name for p in AU_PATTERNS}
        assert {"AU:GEN:AAAAA0", "AU:GEN:0AAAAA"} <= names


class _StubOCR:
    """Reads one string when the crop is the right way round and another when it is not.

    The pixels carry the answer: column 0 brighter than the last column means "as
    stored". Flipping the array swaps them, which is exactly what the real mirror does.
    """

    def __init__(self, upright, mirrored):
        self.upright, self.mirrored = upright, mirrored
        self.reads = 0

    async def read(self, crop):
        self.reads += 1
        forwards = crop[0, 0, 0] > crop[0, -1, 0]
        return self.upright if forwards else self.mirrored


def crop():
    """A crop whose left edge is bright, so flipping is detectable."""
    image = np.zeros((32, 64, 3), dtype=np.uint8)
    image[:, :8] = 255
    return image


class TestOrientationIsMeasuredNotAssumed:
    async def test_a_mirrored_recording_is_detected_and_flipped(self):
        """The rear channel: garbage upright, a real SA plate when flipped."""
        ocr = _StubOCR(upright=("MJQ0EE2", 0.98), mirrored=("S330DGM", 0.99))
        orientation = _PlateOrientation()

        for _ in range(_ORIENTATION_SAMPLE):
            text, _confidence = await orientation.read(ocr, crop(), region="AU")
            assert text == "S330DGM", "the readable orientation was not preferred"

        assert orientation.mirrored is True
        # Having decided, it stops paying for two reads per crop.
        before = ocr.reads
        await orientation.read(ocr, crop(), region="AU")
        assert ocr.reads == before + 1

    async def test_an_ordinary_recording_is_left_alone(self):
        """The front channel. Nothing is flipped and no result changes."""
        ocr = _StubOCR(upright=("S352CJS", 0.99), mirrored=("SJ253S", 0.40))
        orientation = _PlateOrientation()

        for _ in range(_ORIENTATION_SAMPLE):
            text, _confidence = await orientation.read(ocr, crop(), region="AU")
            assert text == "S352CJS"

        assert orientation.mirrored is False

    async def test_footage_with_no_readable_plates_defaults_to_as_is(self):
        """Silence must not be read as evidence of mirroring."""
        ocr = _StubOCR(upright=("KEECE", 0.99), mirrored=("ECEEK", 0.99))
        orientation = _PlateOrientation()
        for _ in range(_ORIENTATION_SAMPLE):
            await orientation.read(ocr, crop(), region="AU")
        assert orientation.mirrored is False

    async def test_a_single_lucky_flip_does_not_flip_the_recording(self):
        """One mirrored hit against several upright ones must not win."""
        orientation = _PlateOrientation()
        upright = _StubOCR(upright=("S352CJS", 0.99), mirrored=("junk", 0.10))
        for _ in range(_ORIENTATION_SAMPLE - 1):
            await orientation.read(upright, crop(), region="AU")
        lucky = _StubOCR(upright=("nope", 0.10), mirrored=("S292CXG", 0.99))
        await orientation.read(lucky, crop(), region="AU")
        assert orientation.mirrored is False
