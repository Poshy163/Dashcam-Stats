"""Model registry and on-demand fetching.

Models are not baked into the image. They are downloaded into ``/data/models`` the first
time a feature needs one, verified, and cached — so the image stays small, the container
works fully offline once warmed, and a user who never enables plate reading never pays for
that model.

**Weights come from upstream projects, never from this repository.** An earlier version
pointed at release assets on this repo that were never published, so every model 404'd and
every processing run logged a failure for a feature that could not possibly work. Hosting
weights here means maintaining them here; pointing at the projects that train and publish
them does not. The two used are both MIT licensed, matching this repository:

* `open-image-models <https://github.com/ankandrew/open-image-models>`_ — RF-DETR COCO
  detectors and YOLOv9 plate localisers, exported to ONNX with NMS already folded in.
* `fast-plate-ocr <https://github.com/ankandrew/fast-plate-ocr>`_ — plate text recognition.
  Its global model is trained on plates from 65-odd countries, Australia among them, over
  the alphabet ``0-9A-Z``.

Those projects also supply the inference code, which is why :mod:`app.ai.detector` and
:mod:`app.ai.plates` no longer hand-roll letterboxing, anchor decoding, NMS or CTC. Every
one of those is a place to be subtly and silently wrong — a transposed output layout or an
off-by-one alphabet yields confident nonsense rather than an error — and none of it had
ever run against real weights.

Nothing here raises on failure. ``ensure_model`` returns ``None`` when a model cannot be
obtained, and the caller reports the feature unavailable instead of failing a recording.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from pathlib import Path

from app.config import get_config
from app.core.logging import get_logger

log = get_logger(__name__)

#: COCO classes, in the order the detector's output channels use. Only the road-relevant
#: entries are ever surfaced, filtered by the processing.detection_classes setting.
COCO_CLASSES: tuple[str, ...] = (
    "person",
    "bicycle",
    "car",
    "motorcycle",
    "airplane",
    "bus",
    "train",
    "truck",
    "boat",
    "traffic light",
    "fire hydrant",
    "stop sign",
    "parking meter",
    "bench",
    "bird",
    "cat",
    "dog",
    "horse",
    "sheep",
    "cow",
    "elephant",
    "bear",
    "zebra",
    "giraffe",
    "backpack",
    "umbrella",
    "handbag",
    "tie",
    "suitcase",
    "frisbee",
    "skis",
    "snowboard",
    "sports ball",
    "kite",
    "baseball bat",
    "baseball glove",
    "skateboard",
    "surfboard",
    "tennis racket",
    "bottle",
    "wine glass",
    "cup",
    "fork",
    "knife",
    "spoon",
    "bowl",
    "banana",
    "apple",
    "sandwich",
    "orange",
    "broccoli",
    "carrot",
    "hot dog",
    "pizza",
    "donut",
    "cake",
    "chair",
    "couch",
    "potted plant",
    "bed",
    "dining table",
    "toilet",
    "tv",
    "laptop",
    "mouse",
    "remote",
    "keyboard",
    "cell phone",
    "microwave",
    "oven",
    "toaster",
    "sink",
    "refrigerator",
    "book",
    "clock",
    "vase",
    "scissors",
    "teddy bear",
    "hair drier",
    "toothbrush",
)

#: What the road cares about. Anything else COCO knows about is noise on a dashcam.
ROAD_CLASSES = frozenset({"car", "truck", "bus", "motorcycle", "bicycle", "person"})


@dataclass(slots=True)
class ModelFile:
    filename: str
    url: str
    sha256: str | None = None


@dataclass(slots=True)
class ModelSpec:
    name: str
    task: str
    files: list[ModelFile]
    input_size: tuple[int, int]
    labels: tuple[str, ...] = ()
    description: str = ""
    #: Which upstream inference implementation reads this file: ``rf_detr`` or ``yolo_v9``
    #: for detectors, ``cct`` for the OCR head. Detectors need it because a local ONNX
    #: file carries no hint of its own output layout.
    runtime: str = ""
    #: Names the file to open when a model is more than one file.
    entry: str = ""

    @property
    def entry_file(self) -> str:
        return self.entry or self.files[0].filename

    def file_path(self, filename: str) -> Path:
        return model_dir(self.name) / filename


# Pinned to specific upstream release assets, so a rebuild cannot silently change the model
# behind a user's results. Both tags are immutable published releases.
_OIM = "https://github.com/ankandrew/open-image-models/releases/download/assets"
_FPO = "https://github.com/ankandrew/cnn-ocr-lp/releases/download/arg-plates"

REGISTRY: dict[str, ModelSpec] = {
    "rfdetr-nano": ModelSpec(
        name="rfdetr-nano",
        task="detection",
        files=[ModelFile("rf-detr-nano-384-coco.onnx", f"{_OIM}/rf-detr-nano-384-coco.onnx")],
        input_size=(384, 384),
        runtime="rf_detr",
        description="RF-DETR nano, COCO. Fast enough for real-time on an iGPU.",
    ),
    "rfdetr-small": ModelSpec(
        name="rfdetr-small",
        task="detection",
        files=[ModelFile("rf-detr-small-512-coco.onnx", f"{_OIM}/rf-detr-small-512-coco.onnx")],
        input_size=(512, 512),
        runtime="rf_detr",
        description="RF-DETR small, COCO. Better on distant and partly occluded vehicles.",
    ),
    "rfdetr-medium": ModelSpec(
        name="rfdetr-medium",
        task="detection",
        files=[ModelFile("rf-detr-medium-576-coco.onnx", f"{_OIM}/rf-detr-medium-576-coco.onnx")],
        input_size=(576, 576),
        runtime="rf_detr",
        description="RF-DETR medium, COCO. Highest recall, noticeably slower.",
    ),
    "plate-detector": ModelSpec(
        name="plate-detector",
        task="plate_detection",
        files=[
            ModelFile(
                "yolo-v9-t-384-license-plates-end2end.onnx",
                f"{_OIM}/yolo-v9-t-384-license-plates-end2end.onnx",
            )
        ],
        input_size=(384, 384),
        labels=("plate",),
        runtime="yolo_v9",
        description="Licence plate localiser, run only inside tracked vehicle boxes.",
    ),
    "plate-ocr": ModelSpec(
        name="plate-ocr",
        task="plate_ocr",
        files=[
            ModelFile("cct_xs_v2_global.onnx", f"{_FPO}/cct_xs_v2_global.onnx"),
            # The config carries the alphabet, slot count and input geometry. Reading them
            # from the file the weights shipped with is the point: hard-coding an alphabet
            # that drifts out of step with the model produces confident wrong text rather
            # than a failure.
            ModelFile(
                "cct_xs_v2_global_plate_config.yaml",
                f"{_FPO}/cct_xs_v2_global_plate_config.yaml",
            ),
        ],
        entry="cct_xs_v2_global.onnx",
        input_size=(128, 64),
        runtime="cct",
        description="Plate text recognition, global model (includes Australian plates).",
    ),
}

#: Companion config for the OCR weights, by filename within the model directory.
OCR_CONFIG_FILE = "cct_xs_v2_global_plate_config.yaml"

#: Used when the configured detection model is not one the registry knows about, which
#: happens to a deployment carrying a setting from a retired registry.
DEFAULT_DETECTION_MODEL = "rfdetr-nano"

_locks: dict[str, asyncio.Lock] = {}


def model_dir(name: str) -> Path:
    return get_config().models_dir / name


def is_present(name: str) -> bool:
    spec = REGISTRY.get(name)
    if spec is None:
        return False
    directory = model_dir(name)
    return all((directory / f.filename).exists() for f in spec.files)


def _verify(path: Path, expected_sha256: str | None) -> bool:
    if not expected_sha256:
        return True
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest() == expected_sha256


async def _download(url: str, target: Path, sha256: str | None) -> bool:
    try:
        import httpx
    except ImportError:
        log.warning("httpx is not installed; cannot fetch models")
        return False

    target.parent.mkdir(parents=True, exist_ok=True)
    # Download beside the target and rename, so an interrupted fetch never leaves a
    # half-written file that looks present on the next start-up.
    partial = target.with_suffix(target.suffix + ".part")
    try:
        async with (
            httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client,
            client.stream("GET", url) as response,
        ):
            response.raise_for_status()
            with partial.open("wb") as fh:
                async for chunk in response.aiter_bytes(1 << 20):
                    fh.write(chunk)
    except Exception as exc:
        log.warning("model download failed", url=url, error=f"{type(exc).__name__}: {exc}")
        partial.unlink(missing_ok=True)
        return False

    if not _verify(partial, sha256):
        log.error("model checksum mismatch; discarding", url=url)
        partial.unlink(missing_ok=True)
        return False

    partial.replace(target)
    return True


async def ensure_model(name: str) -> Path | None:
    """Path to a usable model, fetching it once if needed. None when unavailable."""
    spec = REGISTRY.get(name)
    if spec is None:
        log.warning("unknown model requested", model=name)
        return None

    directory = model_dir(name)
    entry = directory / spec.entry_file

    if is_present(name):
        return entry

    lock = _locks.setdefault(name, asyncio.Lock())
    async with lock:
        if is_present(name):
            return entry

        log.info("fetching model", model=name, files=len(spec.files))
        for file in spec.files:
            target = directory / file.filename
            if target.exists():
                continue
            if not await _download(file.url, target, file.sha256):
                log.warning(
                    "model unavailable; the dependent feature will be disabled",
                    model=name,
                    file=file.filename,
                )
                return None

    return entry if is_present(name) else None


def describe_models() -> list[dict[str, object]]:
    """Registry state for the diagnostics page."""
    return [
        {
            "name": spec.name,
            "task": spec.task,
            "description": spec.description,
            "present": is_present(spec.name),
            "path": str(model_dir(spec.name)),
        }
        for spec in REGISTRY.values()
    ]
