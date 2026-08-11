"""Road-object detection.

Boxes come back normalised to 0..1 rather than in pixels, so a detection stays meaningful
if the same recording is later re-encoded at a different resolution, and the database
never has to store a frame size alongside every row.

Inference itself belongs to `open-image-models
<https://github.com/ankandrew/open-image-models>`_, the project that publishes the
weights. Letterboxing, anchor decoding and NMS used to live here, written against a model
that was never actually published — and that kind of code fails silently rather than
loudly: a transposed output layout produces plausible boxes in the wrong places, not an
exception. Keeping the pre- and post-processing with the weights removes the whole class
of mismatch.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from typing import Any

import numpy as np

from app.ai.models import DEFAULT_DETECTION_MODEL, REGISTRY, ROAD_CLASSES, ensure_model
from app.ai.runtime import onnx_providers
from app.core.logging import get_logger
from app.core.resources import configure_opencv_threads, onnx_session_options
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


def _class_labels(spec: Any) -> Any | None:
    """The label mapping a model's output class IDs actually mean.

    Not simply ``spec.labels``. The COCO detectors emit IDs on the 91-entry COCO space,
    which starts at 1 and has gaps where categories were retired; the contiguous 80-name
    tuple in :mod:`app.ai.models` is a different indexing of the same dataset. Handing the
    contiguous list over as a positional mapping silently shifts every label — a car
    reported as a bicycle, a person as a bus — with nothing failing to signal it. So the
    mapping is taken from the project that exported the weights.
    """
    if spec.runtime == "rf_detr":
        try:
            from open_image_models.detection.core.coco import COCO_CLASSES as UPSTREAM_COCO
        except ImportError:
            log.warning("open-image-models is not installed; detection unavailable")
            return None
        return UPSTREAM_COCO
    return list(spec.labels)


async def load_detector(name: str, *, conf_thresh: float | None = None) -> Any | None:
    """Build an upstream detector for a registry entry, or ``None`` if unobtainable.

    Shared by this module and :mod:`app.ai.plates`: both localise objects in a frame, and
    only the weights and labels differ.
    """
    spec = REGISTRY.get(name)
    if spec is None:
        log.warning("unknown model requested", model=name)
        return None

    path = await ensure_model(name)
    if path is None:
        return None

    try:
        from open_image_models import create_detector
    except ImportError:
        log.warning("open-image-models is not installed; detection unavailable", model=name)
        return None

    labels = _class_labels(spec)
    if labels is None:
        return None

    def build() -> Any:
        configure_opencv_threads()
        return create_detector(
            path,
            backend=spec.runtime,
            class_labels=labels,
            conf_thresh=conf_thresh,
            providers=list(onnx_providers()),
            sess_options=onnx_session_options(),
        )

    try:
        # Building a session compiles the graph and can take seconds on first use; off the
        # event loop so the API stays responsive while a worker warms up.
        return await asyncio.to_thread(build)
    except Exception as exc:
        log.warning(
            "detector failed to load",
            model=name,
            error=f"{type(exc).__name__}: {exc}",
        )
        return None


class ObjectDetector:
    """Road-object detector over sampled frames."""

    def __init__(self, model_name: str | None = None) -> None:
        self._model_name = model_name
        self._detector: Any | None = None
        self._spec = None
        self._loaded = False

    @property
    def available(self) -> bool:
        return self._detector is not None

    @property
    def device(self) -> str | None:
        """Where inference runs, as something a person can read.

        A provider entry is a ``(name, options)`` pair once it carries a device, so
        returning it whole put the entire tuple repr into the Queue page's device column.
        The useful answer is the device itself -- GPU or CPU -- because that is the one
        thing anyone checks this for.
        """
        providers = onnx_providers()
        if not providers or self._detector is None:
            return None
        first = providers[0]
        if isinstance(first, tuple):
            return str(first[1].get("device_type") or first[0])
        return str(first)

    async def load(self) -> bool:
        if self._loaded:
            return self.available
        self._loaded = True

        settings = get_settings_service()
        name = self._model_name or str(settings.get_nowait("processing.detection_model"))
        spec = REGISTRY.get(name)
        if spec is None:
            # A stored setting naming a model the registry has since dropped. A migration
            # rewrites the known cases, but falling back beats switching detection off over
            # a stale string -- the alternative is a silent loss of a whole feature.
            fallback = DEFAULT_DETECTION_MODEL
            log.warning(
                "unknown detection model configured; falling back",
                model=name,
                using=fallback,
            )
            name = fallback
            spec = REGISTRY.get(name)
            if spec is None:
                return False

        threshold = float(settings.get_nowait("processing.detection_confidence"))
        detector = await load_detector(name, conf_thresh=threshold)
        if detector is None:
            log.warning(
                "object detection is unavailable: model could not be obtained",
                model=name,
            )
            return False

        self._spec = spec
        self._detector = detector
        return True

    async def detect(
        self, frame: np.ndarray, *, classes: frozenset[str] | None = None
    ) -> list[Detection2D]:
        """Detect road objects in one BGR frame."""
        if self._detector is None:
            return []

        settings = get_settings_service()
        wanted = classes if classes is not None else await settings.detection_classes()
        wanted = frozenset(w for w in wanted if w in ROAD_CLASSES) or ROAD_CLASSES

        height, width = frame.shape[:2]
        if not height or not width:
            return []

        try:
            predictions = await asyncio.to_thread(self._detector.predict, frame)
        except Exception as exc:
            log.warning("inference failed", error=f"{type(exc).__name__}: {exc}")
            return []

        results: list[Detection2D] = []
        for prediction in predictions:
            label = str(getattr(prediction, "label", "") or "")
            if label not in wanted:
                continue
            box = prediction.bounding_box
            x1 = float(np.clip(box.x1, 0, width))
            y1 = float(np.clip(box.y1, 0, height))
            x2 = float(np.clip(box.x2, 0, width))
            y2 = float(np.clip(box.y2, 0, height))
            if x2 <= x1 or y2 <= y1:
                continue
            results.append(
                Detection2D(
                    class_label=label,
                    confidence=float(prediction.confidence),
                    x=x1 / width,
                    y=y1 / height,
                    w=(x2 - x1) / width,
                    h=(y2 - y1) / height,
                )
            )
        return results

    async def aclose(self) -> None:
        detector = self._detector
        self._detector = None
        if detector is not None:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(lambda: getattr(detector, "close", lambda: None)())
