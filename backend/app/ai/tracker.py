"""Multi-object tracking (ByteTrack), dependency-free.

The durable unit of detection is the *track*, not the frame. Twenty seconds of following
the same car becomes one row with a first and last sighting rather than several hundred
unrelated detections — which is what makes the database size proportional to the number of
vehicles seen instead of the number of frames analysed.

One thing to keep in mind while reading the gating constants: this runs over *sampled*
frames, typically 4 fps, not consecutive video frames. A vehicle moves eight times further
between updates than it would at 30 fps, so association tolerances are correspondingly
loose and a plain IoU tracker tuned for video would lose every track.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from app.ai.detector import Detection2D

#: Detections above this go into the first association pass; those below get the second.
#: Splitting the two is the whole idea of ByteTrack: low-confidence boxes are usually
#: real objects that are occluded, and matching them to *existing* tracks recovers them
#: without letting them spawn spurious new ones.
HIGH_CONFIDENCE = 0.5

#: Generous because of the sampling gap described above.
MATCH_IOU_HIGH = 0.25
MATCH_IOU_LOW = 0.15

#: How many consecutive sampled frames a track may go unmatched before it is closed.
DEFAULT_TRACK_BUFFER = 8


def iou_matrix(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Pairwise IoU between two sets of xyxy boxes."""
    if a.size == 0 or b.size == 0:
        return np.zeros((len(a), len(b)), dtype=np.float32)

    area_a = np.maximum(0.0, a[:, 2] - a[:, 0]) * np.maximum(0.0, a[:, 3] - a[:, 1])
    area_b = np.maximum(0.0, b[:, 2] - b[:, 0]) * np.maximum(0.0, b[:, 3] - b[:, 1])

    lt = np.maximum(a[:, None, :2], b[None, :, :2])
    rb = np.minimum(a[:, None, 2:], b[None, :, 2:])
    wh = np.clip(rb - lt, 0.0, None)
    inter = wh[..., 0] * wh[..., 1]
    union = area_a[:, None] + area_b[None, :] - inter
    return np.where(union > 0, inter / union, 0.0).astype(np.float32)


def greedy_match(scores: np.ndarray, threshold: float) -> list[tuple[int, int]]:
    """Highest-score-first assignment.

    Greedy rather than Hungarian: with the handful of objects a dashcam frame contains,
    the optimal assignment and the greedy one almost always agree, and this keeps the
    module free of a scipy dependency.
    """
    pairs: list[tuple[int, int]] = []
    if scores.size == 0:
        return pairs

    work = scores.copy()
    while True:
        index = int(work.argmax())
        row, col = divmod(index, work.shape[1])
        if work[row, col] < threshold:
            break
        pairs.append((row, col))
        work[row, :] = -1.0
        work[:, col] = -1.0
        if (work < threshold).all():
            break
    return pairs


class _Kalman:
    """Constant-velocity filter on (cx, cy, area, aspect).

    Tracking centre and area rather than corners keeps the state stable while a vehicle
    approaches and its box grows — the usual dashcam case.
    """

    def __init__(self, box: np.ndarray) -> None:
        cx, cy, w, h = _to_cxcywh(box)
        self.mean = np.array([cx, cy, w * h, w / max(h, 1e-6), 0.0, 0.0, 0.0], dtype=np.float64)
        self.covariance = np.eye(7, dtype=np.float64) * 10.0

    def predict(self) -> None:
        self.mean[0] += self.mean[4]
        self.mean[1] += self.mean[5]
        self.mean[2] = max(1e-6, self.mean[2] + self.mean[6])
        self.covariance += np.eye(7) * 1.0

    def update(self, box: np.ndarray) -> None:
        cx, cy, w, h = _to_cxcywh(box)
        measurement = np.array([cx, cy, w * h, w / max(h, 1e-6)], dtype=np.float64)
        # Fixed gain rather than a full Kalman update: the measurement noise here is
        # dominated by detector jitter, which is roughly constant, so the extra matrix
        # algebra buys nothing measurable.
        gain = 0.6
        self.mean[4] = (measurement[0] - self.mean[0]) * gain
        self.mean[5] = (measurement[1] - self.mean[1]) * gain
        self.mean[6] = (measurement[2] - self.mean[2]) * gain
        self.mean[:4] = self.mean[:4] + (measurement - self.mean[:4]) * gain
        self.covariance *= 0.7

    @property
    def box(self) -> np.ndarray:
        cx, cy, area, aspect = self.mean[:4]
        w = float(np.sqrt(max(area * aspect, 1e-9)))
        h = float(area / max(w, 1e-9))
        return np.array([cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2])


def _to_cxcywh(box: np.ndarray) -> tuple[float, float, float, float]:
    x1, y1, x2, y2 = box
    return (x1 + x2) / 2, (y1 + y2) / 2, max(x2 - x1, 1e-6), max(y2 - y1, 1e-6)


def sharpness(image: np.ndarray) -> float:
    """Laplacian variance — higher is crisper. Used to pick a track's best crop."""
    if image.size == 0:
        return 0.0
    gray = image[..., 0].astype(np.float32) if image.ndim == 3 else image.astype(np.float32)
    if gray.shape[0] < 3 or gray.shape[1] < 3:
        return 0.0
    lap = (
        -4.0 * gray[1:-1, 1:-1]
        + gray[:-2, 1:-1]
        + gray[2:, 1:-1]
        + gray[1:-1, :-2]
        + gray[1:-1, 2:]
    )
    return float(lap.var())


@dataclass(slots=True)
class Track:
    """One physical object followed across sampled frames."""

    track_key: int
    class_label: str
    first_offset_s: float
    last_offset_s: float
    frame_count: int = 1
    confidence_max: float = 0.0
    confidence_sum: float = 0.0
    box: np.ndarray = field(default_factory=lambda: np.zeros(4))

    best_offset_s: float | None = None
    best_score: float = -1.0
    best_box: tuple[float, float, float, float] | None = None
    best_crop: np.ndarray | None = None

    #: Sampled per-frame boxes, kept sparsely for the recording timeline.
    samples: list[tuple[float, float, float, float, float, float]] = field(default_factory=list)

    _kalman: _Kalman | None = None
    _misses: int = 0

    @property
    def confidence_avg(self) -> float:
        return self.confidence_sum / max(1, self.frame_count)

    @property
    def duration_s(self) -> float:
        return max(0.0, self.last_offset_s - self.first_offset_s)

    def consider_best(
        self, offset_s: float, box: np.ndarray, confidence: float, crop: np.ndarray | None
    ) -> None:
        """Keep the most usable frame of this object for cropping and plate reading.

        Bigger, sharper and more confident all matter, and a large blurry box is worse for
        OCR than a smaller crisp one — hence the product rather than area alone.
        """
        width = max(0.0, box[2] - box[0])
        height = max(0.0, box[3] - box[1])
        area = width * height
        score = area * confidence * (1.0 + sharpness(crop) / 1000.0 if crop is not None else 1.0)
        if score > self.best_score:
            self.best_score = score
            self.best_offset_s = offset_s
            self.best_box = (float(box[0]), float(box[1]), float(box[2]), float(box[3]))
            self.best_crop = crop


class ByteTracker:
    """ByteTrack over sampled frames, one instance per recording."""

    def __init__(self, *, track_buffer: int = DEFAULT_TRACK_BUFFER) -> None:
        self._active: list[Track] = []
        self._finished: list[Track] = []
        self._next_key = 1
        self._track_buffer = track_buffer

    def update(
        self,
        detections: list[Detection2D],
        offset_s: float,
        frame: np.ndarray | None = None,
    ) -> list[Track]:
        for track in self._active:
            if track._kalman is not None:
                track._kalman.predict()
                track.box = track._kalman.box

        high = [d for d in detections if d.confidence >= HIGH_CONFIDENCE]
        low = [d for d in detections if d.confidence < HIGH_CONFIDENCE]

        unmatched = list(range(len(self._active)))
        unmatched = self._associate(high, unmatched, MATCH_IOU_HIGH, offset_s, frame)
        # Second pass over the low-confidence boxes, existing tracks only. These recover
        # objects the detector half-lost to occlusion; letting them start new tracks would
        # fill the database with noise.
        unmatched = self._associate(low, unmatched, MATCH_IOU_LOW, offset_s, frame, allow_new=False)

        for index in unmatched:
            track = self._active[index]
            track._misses += 1

        survivors: list[Track] = []
        for track in self._active:
            if track._misses > self._track_buffer:
                self._finished.append(track)
            else:
                survivors.append(track)
        self._active = survivors
        return self._active

    def _associate(
        self,
        detections: list[Detection2D],
        candidate_indices: list[int],
        threshold: float,
        offset_s: float,
        frame: np.ndarray | None,
        *,
        allow_new: bool = True,
    ) -> list[int]:
        if not detections:
            return candidate_indices

        det_boxes = np.array(
            [[d.x, d.y, d.x + d.w, d.y + d.h] for d in detections], dtype=np.float32
        )
        track_boxes = (
            np.array([self._active[i].box for i in candidate_indices], dtype=np.float32)
            if candidate_indices
            else np.zeros((0, 4), dtype=np.float32)
        )

        scores = iou_matrix(track_boxes, det_boxes)
        # Never merge a person into a car track: a class change mid-track is a mismatch,
        # not a reclassification.
        for row, track_index in enumerate(candidate_indices):
            for col, detection in enumerate(detections):
                if self._active[track_index].class_label != detection.class_label:
                    scores[row, col] = -1.0

        matched_dets: set[int] = set()
        still_unmatched = set(candidate_indices)

        for row, col in greedy_match(scores, threshold):
            track = self._active[candidate_indices[row]]
            detection = detections[col]
            self._absorb(track, detection, det_boxes[col], offset_s, frame)
            matched_dets.add(col)
            still_unmatched.discard(candidate_indices[row])

        if allow_new:
            for col, detection in enumerate(detections):
                if col in matched_dets:
                    continue
                self._start(detection, det_boxes[col], offset_s, frame)

        return sorted(still_unmatched)

    def _absorb(
        self,
        track: Track,
        detection: Detection2D,
        box: np.ndarray,
        offset_s: float,
        frame: np.ndarray | None,
    ) -> None:
        track.last_offset_s = offset_s
        track.frame_count += 1
        track.confidence_sum += detection.confidence
        track.confidence_max = max(track.confidence_max, detection.confidence)
        track._misses = 0
        track.box = box
        if track._kalman is not None:
            track._kalman.update(box)
        track.samples.append(
            (offset_s, detection.confidence, detection.x, detection.y, detection.w, detection.h)
        )
        track.consider_best(offset_s, box, detection.confidence, _crop(frame, detection))

    def _start(
        self,
        detection: Detection2D,
        box: np.ndarray,
        offset_s: float,
        frame: np.ndarray | None,
    ) -> None:
        track = Track(
            track_key=self._next_key,
            class_label=detection.class_label,
            first_offset_s=offset_s,
            last_offset_s=offset_s,
            confidence_max=detection.confidence,
            confidence_sum=detection.confidence,
            box=box,
        )
        track._kalman = _Kalman(box)
        track.samples.append(
            (offset_s, detection.confidence, detection.x, detection.y, detection.w, detection.h)
        )
        track.consider_best(offset_s, box, detection.confidence, _crop(frame, detection))
        self._next_key += 1
        self._active.append(track)

    def finish(self, *, min_frames: int = 2) -> list[Track]:
        """Close all tracks and return the ones worth storing."""
        tracks = self._finished + self._active
        self._finished, self._active = [], []
        return [t for t in tracks if t.frame_count >= min_frames]


def _crop(frame: np.ndarray | None, detection: Detection2D) -> np.ndarray | None:
    if frame is None:
        return None
    height, width = frame.shape[:2]
    x1, y1, x2, y2 = detection.to_pixels(width, height)
    if x2 <= x1 or y2 <= y1:
        return None
    return frame[y1:y2, x1:x2].copy()
