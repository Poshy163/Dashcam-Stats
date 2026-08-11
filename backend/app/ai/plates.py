"""Licence plate detection, OCR and per-track voting.

Two decisions keep this affordable and accurate:

* Plates are searched for **only inside tracked vehicle boxes**, never across the whole
  frame. It is far cheaper and removes most false positives, since a plate that is not on
  a vehicle we detected is not one we could attribute anyway.
* OCR runs on **a handful of the best crops per tracked vehicle**, never every frame, and
  the readings then vote. A car followed for twenty seconds yields one observation with a
  vote count, not six hundred rows disagreeing about one character.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from app.ai.detector import Detection2D, load_detector
from app.ai.models import OCR_CONFIG_FILE, ensure_model, model_dir
from app.ai.runtime import onnx_providers
from app.ai.tracker import sharpness
from app.core.logging import get_logger
from app.core.resources import configure_opencv_threads, onnx_session_options
from app.core.settings_service import get_settings_service

log = get_logger(__name__)

#: Extra context around a plate box before OCR. Recognisers are trained on crops with a
#: little margin, and a box clipped to the characters loses the edges of the outer glyphs.
_CROP_MARGIN = 0.12


@dataclass(slots=True)
class PlateReading:
    raw_text: str
    ocr_confidence: float
    detection_confidence: float
    #: Plate box in coordinates normalised to the *full frame*.
    bbox: tuple[float, float, float, float]
    crop: np.ndarray | None = None
    vehicle_crop: np.ndarray | None = None
    offset_s: float = 0.0

    @property
    def usable(self) -> bool:
        return bool(self.raw_text) and self.ocr_confidence > 0.0


@dataclass(slots=True)
class PlateVote:
    """The winning reading for one tracked vehicle."""

    text: str
    ocr_confidence: float
    detection_confidence: float
    vote_count: int
    best: PlateReading
    alternatives: list[tuple[str, int]] = field(default_factory=list)


class PlateDetector:
    """Locates plates within a vehicle crop."""

    def __init__(self) -> None:
        self._detector: Any | None = None
        self._loaded = False

    @property
    def available(self) -> bool:
        return self._detector is not None

    async def load(self) -> bool:
        if self._loaded:
            return self.available
        self._loaded = True

        threshold = float(get_settings_service().get_nowait("plates.detection_confidence"))
        self._detector = await load_detector("plate-detector", conf_thresh=threshold)
        if self._detector is None:
            log.warning("plate detection unavailable: model could not be obtained")
            return False
        return True

    async def detect(
        self, vehicle_crop: np.ndarray, *, min_width_px: int
    ) -> list[tuple[tuple[float, float, float, float], float]]:
        """Plate boxes within *vehicle_crop*, normalised to that crop, plus confidence."""
        if self._detector is None or vehicle_crop.size == 0:
            return []

        height, width = vehicle_crop.shape[:2]
        if not height or not width:
            return []

        try:
            # The exported graph has NMS folded in, so what comes back is already the
            # final box set rather than raw anchors.
            predictions = await asyncio.to_thread(self._detector.predict, vehicle_crop)
        except Exception as exc:
            log.debug("plate detection inference failed", error=str(exc))
            return []

        results: list[tuple[tuple[float, float, float, float], float]] = []
        for prediction in predictions:
            box = prediction.bounding_box
            # Anything this narrow carries too few pixels per character to read; OCR would
            # return a confident-looking guess from noise.
            if (box.x2 - box.x1) < min_width_px:
                continue
            results.append(
                (
                    (
                        float(np.clip(box.x1 / width, 0, 1)),
                        float(np.clip(box.y1 / height, 0, 1)),
                        float(np.clip(box.x2 / width, 0, 1)),
                        float(np.clip(box.y2 / height, 0, 1)),
                    ),
                    float(prediction.confidence),
                )
            )
        return results


class PlateOCR:
    """Text recogniser for cropped plates.

    Inference belongs to `fast-plate-ocr <https://github.com/ankandrew/fast-plate-ocr>`_,
    including the alphabet, slot count and geometry, all of which it reads from the config
    file shipped alongside the weights. Those used to be hard-coded here, which is a quiet
    way to be wrong: an alphabet one character out of step with the model does not raise,
    it returns confidently misspelt plates.
    """

    def __init__(self) -> None:
        self._recogniser: Any | None = None
        self._loaded = False

    @property
    def available(self) -> bool:
        return self._recogniser is not None

    async def load(self) -> bool:
        if self._loaded:
            return self.available
        self._loaded = True

        path = await ensure_model("plate-ocr")
        if path is None:
            log.warning("plate OCR unavailable: model could not be obtained")
            return False

        config_path = model_dir("plate-ocr") / OCR_CONFIG_FILE
        if not config_path.exists():
            log.warning("plate OCR unavailable: model config is missing", path=str(config_path))
            return False

        try:
            from fast_plate_ocr import LicensePlateRecognizer
        except ImportError:
            log.warning("fast-plate-ocr is not installed; plate OCR unavailable")
            return False

        def build() -> Any:
            configure_opencv_threads()
            return LicensePlateRecognizer(
                onnx_model_path=path,
                plate_config_path=config_path,
                providers=list(onnx_providers()),
                sess_options=onnx_session_options(),
            )

        try:
            self._recogniser = await asyncio.to_thread(build)
        except Exception as exc:
            log.warning(
                "plate OCR failed to load",
                error=f"{type(exc).__name__}: {exc}",
            )
            return False
        return True

    async def read(self, crop: np.ndarray) -> tuple[str, float]:
        """Recognise text in a plate crop. Returns ``("", 0.0)`` when unreadable."""
        if self._recogniser is None or crop.size == 0:
            return "", 0.0

        # The model's config declares RGB input and the library takes arrays at face value,
        # so a BGR crop straight from the decoder would be read with its red and blue
        # channels swapped -- no error, just a worse answer.
        if crop.ndim == 3 and crop.shape[2] == 3:
            crop = crop[:, :, ::-1]
        image = np.ascontiguousarray(crop, dtype=np.uint8)

        try:
            predictions = await asyncio.to_thread(
                self._recogniser.run, image, return_confidence=True
            )
        except Exception as exc:
            log.debug("plate OCR inference failed", error=str(exc))
            return "", 0.0

        if not predictions:
            return "", 0.0

        prediction = predictions[0]
        text = str(prediction.plate or "").strip()
        if not text:
            return "", 0.0

        # The mean probability of the characters actually emitted. This is shown to the
        # user beside every plate, so it has to mean something rather than be a placeholder.
        probs = prediction.char_probs
        confidence = float(np.mean(np.asarray(probs)[: len(text)])) if probs is not None else 0.0
        return text, confidence


def select_ocr_candidates(readings: list[PlateReading], limit: int) -> list[PlateReading]:
    """Pick the crops most likely to read correctly.

    Bigger and sharper wins. Running OCR on every frame of a track would cost tens of
    times more for a worse answer, because most frames of a passing vehicle are motion
    blurred or too small.
    """
    scored: list[tuple[float, PlateReading]] = []
    for reading in readings:
        if reading.crop is None or reading.crop.size == 0:
            continue
        height, width = reading.crop.shape[:2]
        score = width * height * (1.0 + sharpness(reading.crop) / 500.0)
        scored.append((score, reading))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [reading for _, reading in scored[:limit]]


def vote_track_plate(readings: list[PlateReading]) -> PlateVote | None:
    """Combine several readings of one vehicle into a single answer.

    Voting happens per character position as well as per whole string: separate reads of
    the same plate usually differ by one character, so a positional vote recovers the
    correct text even when no single reading is right end to end.
    """
    usable = [r for r in readings if r.usable]
    if not usable:
        return None

    lengths = Counter(len(r.raw_text) for r in usable)
    modal_length, _ = lengths.most_common(1)[0]
    same_length = [r for r in usable if len(r.raw_text) == modal_length]
    if not same_length:
        same_length = usable

    # Weight by OCR confidence and crop area: a large, confident read should outvote a
    # small blurry one rather than counting equally.
    def weight(reading: PlateReading) -> float:
        area = 1.0
        if reading.crop is not None and reading.crop.size:
            h, w = reading.crop.shape[:2]
            area = float(w * h)
        return reading.ocr_confidence * (1.0 + np.log1p(area) / 10.0)

    voted: list[str] = []
    for position in range(modal_length):
        tally: Counter[str] = Counter()
        for reading in same_length:
            tally[reading.raw_text[position]] += weight(reading)  # type: ignore[assignment]
        voted.append(tally.most_common(1)[0][0])
    text = "".join(voted)

    best = max(same_length, key=lambda r: r.ocr_confidence)
    mean_confidence = float(np.mean([r.ocr_confidence for r in same_length]))
    alternatives = Counter(r.raw_text for r in usable if r.raw_text != text)

    return PlateVote(
        text=text,
        ocr_confidence=round(mean_confidence, 4),
        detection_confidence=round(
            float(np.mean([r.detection_confidence for r in same_length])), 4
        ),
        vote_count=len(same_length),
        best=best,
        alternatives=alternatives.most_common(3),
    )


def crop_with_margin(
    frame: np.ndarray, box: tuple[float, float, float, float], margin: float = _CROP_MARGIN
) -> np.ndarray | None:
    """Crop a normalised box out of a frame with a little breathing room."""
    height, width = frame.shape[:2]
    x1, y1, x2, y2 = box
    dx = (x2 - x1) * margin
    dy = (y2 - y1) * margin
    px1 = max(0, int((x1 - dx) * width))
    py1 = max(0, int((y1 - dy) * height))
    px2 = min(width, int((x2 + dx) * width))
    py2 = min(height, int((y2 + dy) * height))
    if px2 <= px1 or py2 <= py1:
        return None
    return frame[py1:py2, px1:px2].copy()


def plate_box_in_frame(
    vehicle: Detection2D, plate_box: tuple[float, float, float, float]
) -> tuple[float, float, float, float]:
    """Rebase a plate box from vehicle-crop coordinates to full-frame coordinates."""
    x1, y1, x2, y2 = plate_box
    return (
        vehicle.x + x1 * vehicle.w,
        vehicle.y + y1 * vehicle.h,
        vehicle.x + x2 * vehicle.w,
        vehicle.y + y2 * vehicle.h,
    )
