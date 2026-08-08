"""Inference: object detection, tracking, plate reading and plate normalisation.

Every component degrades independently. Missing models or a missing inference runtime
disable one feature and report themselves unavailable — they never fail a recording or
stop the container from starting.
"""

from __future__ import annotations

from app.ai.backend import BackendInfo, InferenceBackend, get_backend
from app.ai.detector import Detection2D, ObjectDetector
from app.ai.models import COCO_CLASSES, ROAD_CLASSES, describe_models, ensure_model
from app.ai.normalise_au import NormalisedPlate, normalise, plate_similarity
from app.ai.plates import (
    PlateDetector,
    PlateOCR,
    PlateReading,
    PlateVote,
    crop_with_margin,
    plate_box_in_frame,
    select_ocr_candidates,
    vote_track_plate,
)
from app.ai.tracker import ByteTracker, Track

__all__ = [
    "COCO_CLASSES",
    "ROAD_CLASSES",
    "BackendInfo",
    "ByteTracker",
    "Detection2D",
    "InferenceBackend",
    "NormalisedPlate",
    "ObjectDetector",
    "PlateDetector",
    "PlateOCR",
    "PlateReading",
    "PlateVote",
    "Track",
    "crop_with_margin",
    "describe_models",
    "ensure_model",
    "get_backend",
    "normalise",
    "plate_box_in_frame",
    "plate_similarity",
    "select_ocr_candidates",
    "vote_track_plate",
]
