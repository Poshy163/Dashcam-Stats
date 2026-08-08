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

from collections import Counter
from dataclasses import dataclass, field

import numpy as np

from app.ai.backend import LoadedModel, get_backend
from app.ai.detector import Detection2D, letterbox, nms
from app.ai.models import OCR_ALPHABET, REGISTRY, ensure_model
from app.ai.tracker import sharpness
from app.core.logging import get_logger
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
        self._model: LoadedModel | None = None
        self._spec = REGISTRY.get("plate-detector")
        self._loaded = False

    @property
    def available(self) -> bool:
        return self._model is not None

    async def load(self) -> bool:
        if self._loaded:
            return self.available
        self._loaded = True

        path = await ensure_model("plate-detector")
        if path is None:
            log.warning("plate detection unavailable: model could not be obtained")
            return False
        self._model = get_backend().load(path)
        return self.available

    async def detect(
        self, vehicle_crop: np.ndarray, *, min_width_px: int
    ) -> list[tuple[tuple[float, float, float, float], float]]:
        """Plate boxes within *vehicle_crop*, normalised to that crop, plus confidence."""
        if self._model is None or self._spec is None or vehicle_crop.size == 0:
            return []

        threshold = float(get_settings_service().get_nowait("plates.detection_confidence"))
        height, width = vehicle_crop.shape[:2]
        padded, scale, pad_x, pad_y = letterbox(vehicle_crop, self._spec.input_size)
        blob = padded[:, :, ::-1].transpose(2, 0, 1)[None].astype(np.float32) / 255.0

        try:
            outputs = await self._model.infer(blob)
        except Exception as exc:
            log.debug("plate detection inference failed", error=str(exc))
            return []

        pred = np.squeeze(outputs[0])
        if pred.ndim != 2:
            return []
        if pred.shape[0] < pred.shape[1]:
            pred = pred.transpose()

        scores = pred[:, 4] if pred.shape[1] >= 5 else np.zeros(len(pred))
        keep = scores >= threshold
        if not keep.any():
            return []

        boxes = pred[keep, :4]
        scores = scores[keep]
        cx = (boxes[:, 0] - pad_x) / scale
        cy = (boxes[:, 1] - pad_y) / scale
        bw = boxes[:, 2] / scale
        bh = boxes[:, 3] / scale
        xyxy = np.stack([cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2], axis=1)

        results: list[tuple[tuple[float, float, float, float], float]] = []
        for index in nms(xyxy, scores, 0.4):
            x1, y1, x2, y2 = xyxy[index]
            # Anything this narrow carries too few pixels per character to read; OCR would
            # return a confident-looking guess from noise.
            if (x2 - x1) < min_width_px:
                continue
            results.append(
                (
                    (
                        float(np.clip(x1 / width, 0, 1)),
                        float(np.clip(y1 / height, 0, 1)),
                        float(np.clip(x2 / width, 0, 1)),
                        float(np.clip(y2 / height, 0, 1)),
                    ),
                    float(scores[index]),
                )
            )
        return results


class PlateOCR:
    """CTC text recogniser for cropped plates."""

    def __init__(self) -> None:
        self._model: LoadedModel | None = None
        self._spec = REGISTRY.get("plate-ocr")
        self._loaded = False

    @property
    def available(self) -> bool:
        return self._model is not None

    async def load(self) -> bool:
        if self._loaded:
            return self.available
        self._loaded = True

        path = await ensure_model("plate-ocr")
        if path is None:
            log.warning("plate OCR unavailable: model could not be obtained")
            return False
        self._model = get_backend().load(path)
        return self.available

    @staticmethod
    def preprocess(crop: np.ndarray, size: tuple[int, int]) -> np.ndarray:
        """Grayscale, contrast-normalise and resize a plate crop."""
        gray = crop[..., 0] if crop.ndim == 3 else crop
        gray = gray.astype(np.float32)
        # Percentile stretch rather than min/max: a single specular highlight on a wet
        # plate would otherwise dominate the range and flatten the characters. A flat
        # crop (high == low) has no contrast to stretch and becomes uniformly black.
        low, high = np.percentile(gray, (2, 98))
        gray = np.clip((gray - low) / (high - low), 0, 1) if high > low else np.zeros_like(gray)

        target_w, target_h = size
        h, w = gray.shape[:2]
        ys = np.minimum((np.arange(target_h) * h // target_h), h - 1)
        xs = np.minimum((np.arange(target_w) * w // target_w), w - 1)
        return gray[ys][:, xs]

    async def read(self, crop: np.ndarray) -> tuple[str, float]:
        """Recognise text in a plate crop. Returns ``("", 0.0)`` when unreadable."""
        if self._model is None or self._spec is None or crop.size == 0:
            return "", 0.0

        prepared = self.preprocess(crop, self._spec.input_size)
        blob = prepared[None, None].astype(np.float32)

        try:
            outputs = await self._model.infer(blob)
        except Exception as exc:
            log.debug("plate OCR inference failed", error=str(exc))
            return "", 0.0

        return self._ctc_decode(np.squeeze(outputs[0]))

    @staticmethod
    def _ctc_decode(logits: np.ndarray) -> tuple[str, float]:
        """Greedy CTC decode with a genuine per-character confidence.

        The confidence returned is the mean softmax probability of the characters actually
        emitted — not a placeholder. It is shown to the user next to every plate, so it has
        to mean something.
        """
        if logits.ndim != 2 or logits.size == 0:
            return "", 0.0
        # Some exports put time on the last axis.
        if logits.shape[0] < logits.shape[1] and logits.shape[0] <= len(OCR_ALPHABET) + 1:
            logits = logits.transpose()

        shifted = logits - logits.max(axis=1, keepdims=True)
        exp = np.exp(shifted)
        probs = exp / np.clip(exp.sum(axis=1, keepdims=True), 1e-9, None)

        indices = probs.argmax(axis=1)
        confidences = probs[np.arange(len(indices)), indices]

        chars: list[str] = []
        kept: list[float] = []
        previous = -1
        for index, confidence in zip(indices, confidences):
            # CTC: index 0 is the blank, and repeats collapse unless separated by one.
            if index != previous and index != 0:
                position = int(index) - 1
                if 0 <= position < len(OCR_ALPHABET):
                    chars.append(OCR_ALPHABET[position])
                    kept.append(float(confidence))
            previous = int(index)

        if not chars:
            return "", 0.0
        return "".join(chars), float(np.mean(kept))


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
