"""Road-object detection.

Boxes come back normalised to 0..1 rather than in pixels, so a detection stays meaningful
if the same recording is later re-encoded at a different resolution, and the database
never has to store a frame size alongside every row.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from app.ai.backend import LoadedModel, get_backend
from app.ai.models import REGISTRY, ROAD_CLASSES, ensure_model
from app.core.logging import get_logger
from app.core.settings_service import get_settings_service

log = get_logger(__name__)


@dataclass(slots=True)
class Detection2D:
    class_label: str
    confidence: float
    #: Normalised centre-free box: x, y = top-left, all in 0..1 of the frame.
    x: float
    y: float
    w: float
    h: float

    @property
    def area(self) -> float:
        return self.w * self.h

    def to_pixels(self, width: int, height: int) -> tuple[int, int, int, int]:
        return (
            max(0, int(self.x * width)),
            max(0, int(self.y * height)),
            min(width, int((self.x + self.w) * width)),
            min(height, int((self.y + self.h) * height)),
        )


def letterbox(frame: np.ndarray, size: tuple[int, int]) -> tuple[np.ndarray, float, int, int]:
    """Resize preserving aspect ratio and pad to *size*.

    Distorting the aspect ratio measurably hurts recall on the narrow, distant vehicles
    that dominate dashcam footage, so the padding is worth the wasted pixels.
    """
    target_w, target_h = size
    h, w = frame.shape[:2]
    scale = min(target_w / w, target_h / h)
    new_w, new_h = max(1, round(w * scale)), max(1, round(h * scale))

    # Nearest-neighbour via index arrays keeps this dependency-free; the detector is not
    # sensitive to resampling quality at these scales.
    ys = np.minimum((np.arange(new_h) / scale).astype(np.int32), h - 1)
    xs = np.minimum((np.arange(new_w) / scale).astype(np.int32), w - 1)
    resized = frame[ys][:, xs]

    canvas = np.full((target_h, target_w, frame.shape[2]), 114, dtype=frame.dtype)
    pad_x = (target_w - new_w) // 2
    pad_y = (target_h - new_h) // 2
    canvas[pad_y : pad_y + new_h, pad_x : pad_x + new_w] = resized
    return canvas, scale, pad_x, pad_y


def nms(boxes: np.ndarray, scores: np.ndarray, threshold: float) -> list[int]:
    """Greedy non-maximum suppression on xyxy boxes. Pure numpy — no torch, no scipy."""
    if boxes.size == 0:
        return []
    x1, y1, x2, y2 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
    areas = np.maximum(0.0, x2 - x1) * np.maximum(0.0, y2 - y1)
    order = scores.argsort()[::-1]

    keep: list[int] = []
    while order.size:
        best = int(order[0])
        keep.append(best)
        if order.size == 1:
            break
        rest = order[1:]
        xx1 = np.maximum(x1[best], x1[rest])
        yy1 = np.maximum(y1[best], y1[rest])
        xx2 = np.minimum(x2[best], x2[rest])
        yy2 = np.minimum(y2[best], y2[rest])
        inter = np.maximum(0.0, xx2 - xx1) * np.maximum(0.0, yy2 - yy1)
        union = areas[best] + areas[rest] - inter
        iou = np.where(union > 0, inter / union, 0.0)
        order = rest[iou <= threshold]
    return keep


class ObjectDetector:
    """YOLO-style detector over sampled frames."""

    def __init__(self, model_name: str | None = None) -> None:
        self._model_name = model_name
        self._model: LoadedModel | None = None
        self._spec = None
        self._loaded = False

    @property
    def available(self) -> bool:
        return self._model is not None

    @property
    def device(self) -> str | None:
        return self._model.device if self._model else None

    async def load(self) -> bool:
        if self._loaded:
            return self.available
        self._loaded = True

        settings = get_settings_service()
        name = self._model_name or str(settings.get_nowait("processing.detection_model"))
        spec = REGISTRY.get(name)
        if spec is None:
            log.warning("unknown detection model configured", model=name)
            return False

        path = await ensure_model(name)
        if path is None:
            log.warning(
                "object detection is unavailable: model could not be obtained",
                model=name,
            )
            return False

        self._spec = spec
        self._model = get_backend().load(path)
        return self.available

    async def detect(
        self, frame: np.ndarray, *, classes: frozenset[str] | None = None
    ) -> list[Detection2D]:
        """Detect road objects in one BGR frame."""
        if self._model is None or self._spec is None:
            return []

        settings = get_settings_service()
        threshold = float(settings.get_nowait("processing.detection_confidence"))
        wanted = classes if classes is not None else await settings.detection_classes()
        wanted = frozenset(w for w in wanted if w in ROAD_CLASSES) or ROAD_CLASSES

        height, width = frame.shape[:2]
        padded, scale, pad_x, pad_y = letterbox(frame, self._spec.input_size)

        # BGR->RGB, HWC->CHW, 0..1, batch of one.
        blob = padded[:, :, ::-1].transpose(2, 0, 1)[None].astype(np.float32) / 255.0

        try:
            outputs = await self._model.infer(blob)
        except Exception as exc:
            log.warning("inference failed", error=f"{type(exc).__name__}: {exc}")
            return []

        return self._decode(outputs[0], threshold, wanted, scale, pad_x, pad_y, width, height)

    def _decode(
        self,
        raw: np.ndarray,
        threshold: float,
        wanted: frozenset[str],
        scale: float,
        pad_x: int,
        pad_y: int,
        width: int,
        height: int,
    ) -> list[Detection2D]:
        # YOLOv8 emits (1, 4 + num_classes, num_anchors); transpose to per-anchor rows.
        pred = np.squeeze(raw)
        if pred.ndim != 2:
            return []
        if pred.shape[0] < pred.shape[1]:
            pred = pred.transpose()

        labels = self._spec.labels if self._spec else ()
        num_classes = pred.shape[1] - 4
        if num_classes <= 0 or not labels:
            return []

        scores_all = pred[:, 4 : 4 + num_classes]
        class_ids = scores_all.argmax(axis=1)
        scores = scores_all[np.arange(scores_all.shape[0]), class_ids]

        keep_mask = scores >= threshold
        if not keep_mask.any():
            return []

        boxes_cxcywh = pred[keep_mask, :4]
        scores = scores[keep_mask]
        class_ids = class_ids[keep_mask]

        # Undo the letterbox: remove padding, then the scale factor.
        cx = (boxes_cxcywh[:, 0] - pad_x) / scale
        cy = (boxes_cxcywh[:, 1] - pad_y) / scale
        bw = boxes_cxcywh[:, 2] / scale
        bh = boxes_cxcywh[:, 3] / scale
        xyxy = np.stack([cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2], axis=1)

        results: list[Detection2D] = []
        # Suppress per class: a car and a truck overlapping is normal and both are real.
        for class_id in np.unique(class_ids):
            label = labels[class_id] if class_id < len(labels) else None
            if label is None or label not in wanted:
                continue
            idx = np.flatnonzero(class_ids == class_id)
            for local in nms(xyxy[idx], scores[idx], 0.45):
                x1, y1, x2, y2 = xyxy[idx[local]]
                x1 = float(np.clip(x1, 0, width))
                y1 = float(np.clip(y1, 0, height))
                x2 = float(np.clip(x2, 0, width))
                y2 = float(np.clip(y2, 0, height))
                if x2 <= x1 or y2 <= y1:
                    continue
                results.append(
                    Detection2D(
                        class_label=label,
                        confidence=float(scores[idx[local]]),
                        x=x1 / width,
                        y=y1 / height,
                        w=(x2 - x1) / width,
                        h=(y2 - y1) / height,
                    )
                )
        return results
