"""Model registry and on-demand fetching.

Models are not baked into the image. They are downloaded into ``/data/models`` the first
time a feature needs one, verified, and cached — so the image stays small, the container
works fully offline once warmed, and a user who never enables plate reading never pays for
that model.

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
    #: OpenVINO IR needs the .xml and its sibling .bin; this names the one to open.
    entry: str = ""

    @property
    def entry_file(self) -> str:
        return self.entry or self.files[0].filename


# Sources are pinned to a specific upstream release so a rebuild cannot silently change
# the model behind a user's results.
_YOLO_BASE = "https://github.com/Poshy163/Dashcam-Stats/releases/download/models-v1"

REGISTRY: dict[str, ModelSpec] = {
    "yolov8n": ModelSpec(
        name="yolov8n",
        task="detection",
        files=[
            ModelFile("yolov8n.xml", f"{_YOLO_BASE}/yolov8n.xml"),
            ModelFile("yolov8n.bin", f"{_YOLO_BASE}/yolov8n.bin"),
        ],
        entry="yolov8n.xml",
        input_size=(640, 640),
        labels=COCO_CLASSES,
        description="YOLOv8 nano, COCO. Fast enough for real-time on an iGPU.",
    ),
    "yolov8s": ModelSpec(
        name="yolov8s",
        task="detection",
        files=[
            ModelFile("yolov8s.xml", f"{_YOLO_BASE}/yolov8s.xml"),
            ModelFile("yolov8s.bin", f"{_YOLO_BASE}/yolov8s.bin"),
        ],
        entry="yolov8s.xml",
        input_size=(640, 640),
        labels=COCO_CLASSES,
        description="YOLOv8 small, COCO. Better on distant and partially occluded vehicles.",
    ),
    "plate-detector": ModelSpec(
        name="plate-detector",
        task="plate_detection",
        files=[
            ModelFile("plate-detector.xml", f"{_YOLO_BASE}/plate-detector.xml"),
            ModelFile("plate-detector.bin", f"{_YOLO_BASE}/plate-detector.bin"),
        ],
        entry="plate-detector.xml",
        input_size=(320, 320),
        labels=("plate",),
        description="Licence plate localiser, run only inside tracked vehicle boxes.",
    ),
    "plate-ocr": ModelSpec(
        name="plate-ocr",
        task="plate_ocr",
        files=[
            ModelFile("plate-ocr.xml", f"{_YOLO_BASE}/plate-ocr.xml"),
            ModelFile("plate-ocr.bin", f"{_YOLO_BASE}/plate-ocr.bin"),
        ],
        entry="plate-ocr.xml",
        input_size=(128, 32),
        description="CTC text recogniser for cropped plates.",
    ),
}

#: Alphabet the OCR head emits, index 0 reserved for the CTC blank.
OCR_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

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
