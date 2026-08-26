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
from app.ai.openvino_session import gpu_backend_disabled, use_openvino_session
from app.core.logging import get_logger
from app.core.resources import configure_opencv_threads, onnx_session_options
from app.core.settings_service import get_settings_service
from app.hardware.ffmpeg import intel_media_lock

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
        kwargs = {
            "backend": spec.runtime,
            "class_labels": labels,
            "conf_thresh": conf_thresh,
            "providers": ["CPUExecutionProvider"],
            "sess_options": onnx_session_options(),
        }
        if spec.task == "plate_detection":
            # Keep the tiny plate models off the shared iGPU. Holding RF-DETR, the plate
            # detector and OCR graph on Alder Lake beside VAAPI exhausted OpenCL resources
            # even when requests were serialised. ORT CPU is bounded by our native thread
            # budget and this model now sees only a handful of stored vehicle crops.
            return create_detector(path, **kwargs)

        if spec.runtime == "rf_detr":
            from open_image_models.detection.core.rf_detr.inference import RFDETRDetector

            owner = RFDETRDetector
        elif spec.runtime == "yolo_v9":
            from open_image_models.detection.core.yolo_v9.inference import YoloV9Detector

            owner = YoloV9Detector
        else:
            raise ValueError(f"unsupported detector runtime: {spec.runtime}")
        with use_openvino_session(owner):
            return create_detector(path, **kwargs)

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

    def __init__(self, model_name: str | None = None, *, confidence: float | None = None) -> None:
        self._model_name = model_name
        #: The threshold this instance is compiled for. Passed in by the shared cache,
        #: which keys on it, rather than read here -- so one object cannot be built for a
        #: value the cache filed it under something else for.
        self._confidence = confidence
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
        if self._detector is None:
            return None
        model = getattr(self._detector, "model", None)
        if model is None:
            return None
        device = getattr(model, "device", None)
        if device:
            return str(device)
        providers = getattr(model, "get_providers", lambda: [])()
        return str(providers[0]) if providers else None

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

        threshold = (
            float(self._confidence)
            if self._confidence is not None
            else float(settings.get_nowait("processing.detection_confidence"))
        )
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

    def _demote_to_cpu(self, reason: str) -> None:
        """Ask the underlying session to leave the iGPU, if it is one that can.

        Reaches through the upstream detector because that object owns the session this
        module installed into it; a plain ONNX Runtime CPU session has no such method and
        needs none.
        """
        model = getattr(self._detector, "model", None)
        ensure_cpu = getattr(model, "ensure_cpu", None)
        if callable(ensure_cpu):
            with contextlib.suppress(Exception):
                ensure_cpu(reason)

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

        device = (self.device or "").upper()
        if "GPU" in device:
            # Two independent reasons to get off this chip, and only one of them used to be
            # asked about.
            #
            # `gpu_safe()` reports stuck *ffmpeg* children and says nothing about OpenVINO.
            # When a `CL_OUT_OF_RESOURCES` takes the inference context down, the session
            # records the verdict durably and re-raises -- but the upstream library catches
            # every exception from a batch and returns empty lists, so nothing here ever saw
            # it. The session stayed compiled for a device that cannot serve it, every
            # subsequent frame failed the same way, and the stage recorded "0 vehicles" as a
            # success for the whole remaining queue.
            #
            # Reading the recorded verdict is what closes that: the demotion then happens
            # from an ordinary call site rather than from inside the driver's failing stack,
            # which is the thing the session's own comment rules out.
            failure = gpu_backend_disabled()
            if failure or not intel_media_lock().gpu_safe():
                self._demote_to_cpu(
                    failure or intel_media_lock().unhealthy or "the Intel media slot is unhealthy"
                )
                device = (self.device or "").upper()

        try:
            guard = intel_media_lock() if "GPU" in device else contextlib.nullcontext()
            # VAAPI and OpenVINO are both stable independently on this iGPU, but the
            # driver's shared OpenCL/media resources fail when they execute together.
            # Detection feeds frames in bounded decoded batches, so no decoder is held by
            # this same task while inference takes the common slot.
            async with guard:
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
