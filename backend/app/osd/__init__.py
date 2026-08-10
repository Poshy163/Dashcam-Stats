"""Reading the dashcam's burned-in telemetry overlay.

This footage carries no GPS metadata of any kind — no data PID, no H.264 SEI user-data,
no sidecar files. Date, coordinates and speed exist only as pixels drawn along the bottom
of every frame, refreshed once per second. Everything the application knows about where
and how fast the car was going comes through here.

See ``ARCHITECTURE.md`` section 4 for how that was established and why the design is
shaped this way.
"""

from __future__ import annotations

from app.osd.engine import (
    TelemetryExtractor,
    TelemetryResult,
    TelemetrySample,
)
from app.osd.geo import bearing_deg, haversine_m
from app.osd.glyphs import (
    GlyphTemplates,
    TemplateLearner,
    binarise,
    decode_line,
    expected_time_from_filename,
    missing_characters,
    segment_glyphs,
    select_training_set,
)
from app.osd.locate import FixSample, FixTrack, Located
from app.osd.parser import OsdReading, enforce_monotonic, parse_osd_text
from app.osd.region import DEFAULT_REGION, OsdRegion, calibrate
from app.osd.validate import clock_is_plausible, coordinate_problem, is_valid_coordinate

__all__ = [
    "DEFAULT_REGION",
    "FixSample",
    "FixTrack",
    "GlyphTemplates",
    "Located",
    "OsdReading",
    "OsdRegion",
    "TelemetryExtractor",
    "TelemetryResult",
    "TelemetrySample",
    "TemplateLearner",
    "bearing_deg",
    "binarise",
    "calibrate",
    "clock_is_plausible",
    "coordinate_problem",
    "decode_line",
    "enforce_monotonic",
    "expected_time_from_filename",
    "haversine_m",
    "is_valid_coordinate",
    "missing_characters",
    "parse_osd_text",
    "segment_glyphs",
    "select_training_set",
]
