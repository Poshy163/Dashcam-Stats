"""Telemetry extraction: decode the overlay strip of a recording into GPS samples.

This is the only source of position, speed and authoritative wall-clock time for this
footage, because the files carry no telemetry metadata whatsoever.

Two derivations happen here that the dashcam does not provide:

* **Heading** is computed from consecutive fixes. It is a bearing between two points, not
  a compass reading, and is stored as such.
* **Distance** accumulates only between fixes far enough apart to exceed the overlay's
  own coordinate quantisation. At 4 decimal places a stationary car jitters by roughly
  11 m per sample; integrating that noise over a two-minute parked segment would invent
  several hundred metres of travel and corrupt every journey total.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np

from app.core.logging import get_logger
from app.hardware.ffmpeg import DecodeError, FFmpegError, iter_frames, probe
from app.osd.glyphs import (
    GlyphTemplates,
    TemplateLearner,
    binarise,
    decode_line,
    expected_time_from_filename,
    learn_structural,
    merge_templates,
    missing_characters,
)
from app.osd.parser import OsdReading, enforce_monotonic, parse_osd_text
from app.osd.region import OsdRegion, calibrate

log = get_logger(__name__)

EARTH_RADIUS_M = 6_371_008.8

#: Ceiling on how fast the vehicle could actually be moving between two fixes. Well
#: above any road speed, so it only ever catches OCR damage rather than fast driving.
_MAX_PLAUSIBLE_SPEED_KMH = 400.0


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


@dataclass(slots=True)
class TelemetrySample:
    """One stored telemetry point, ready to become a ``TelemetryPoint`` row."""

    t_offset_s: float
    captured_at: datetime | None
    lat: float | None
    lon: float | None
    has_fix: bool
    speed_kmh: float | None
    heading_deg: float | None
    ocr_confidence: float
    raw_text: str


@dataclass(slots=True)
class TelemetryResult:
    samples: list[TelemetrySample] = field(default_factory=list)
    frames_read: int = 0
    parse_failures: int = 0
    low_confidence: int = 0

    distance_m: float = 0.0
    max_speed_kmh: float | None = None
    avg_speed_kmh: float | None = None
    first_fix: TelemetrySample | None = None
    last_fix: TelemetrySample | None = None
    #: Overlay clock at the first readable frame. More trustworthy than the filename,
    #: which is only the moment the camera opened the file.
    osd_start_time: datetime | None = None
    #: Fixes discarded for implying impossible movement -- misread coordinates.
    implausible_jumps: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def fix_count(self) -> int:
        return sum(1 for s in self.samples if s.has_fix)

    @property
    def has_gps(self) -> bool:
        return self.fix_count > 0


class TelemetryExtractor:
    """Reads the burned-in overlay from recordings.

    Templates are learned once from the footage itself and cached on disk; see
    ``app.osd.glyphs`` for why that works without shipping a font.
    """

    def __init__(
        self,
        templates: GlyphTemplates | None = None,
        *,
        template_path: Path | None = None,
    ) -> None:
        self._templates = templates
        self._template_path = template_path

    # -- templates ---------------------------------------------------------------------

    @property
    def templates(self) -> GlyphTemplates | None:
        return self._templates

    @property
    def ready(self) -> bool:
        return self._templates is not None and not missing_characters(self._templates)

    def load_templates(self, path: Path) -> bool:
        loaded = GlyphTemplates.load(path)
        if loaded is None:
            return False
        self._templates = loaded
        self._template_path = path
        missing = missing_characters(loaded)
        if missing:
            # A partial set is worse than none: an unlearned digit does not come back as
            # "unknown", it comes back as the nearest-looking learned glyph with a high
            # score, so bad values would flow through with convincing confidence.
            log.warning(
                "loaded OSD templates are incomplete; telemetry will be re-learned",
                missing=sorted(missing),
            )
            return False
        return True

    async def learn_templates(
        self,
        recordings: list[Path],
        *,
        region: OsdRegion,
        frames_per_recording: int = 10,
        # 1 fps rather than something sparser: a character needs several samples before it
        # is averaged into a template, and a sparse rate over a short recording yields one
        # or two strips, so the rarest digits are collected too few times and dropped —
        # which produces a silently incomplete template set rather than an error.
        sample_fps: float = 1.0,
    ) -> GlyphTemplates:
        """Bootstrap templates from real footage.

        The caller is expected to have chosen *recordings* with
        ``glyphs.select_training_set`` so that all ten digits appear in the timestamp
        fields that are safe to label.
        """
        learner = TemplateLearner()
        used = 0

        for path in recordings:
            expected = expected_time_from_filename(path.name)
            if expected is None:
                continue
            try:
                async for _, frame in self._iter_strips(
                    path, region, fps=sample_fps, limit=frames_per_recording
                ):
                    learner.observe_strip(binarise(frame), expected)
                used += 1
            except (FFmpegError, OSError) as exc:
                log.debug("skipped during template learning", file=path.name, error=str(exc))

        templates = learner.build()
        log.info(
            "learned OSD glyph templates",
            recordings=used,
            characters=templates.characters,
            samples=learner.total_samples,
        )

        # 'N' and '.' have no fixed glyph index, so they need a second pass that reads
        # the surrounding characters to locate them.
        structural = TemplateLearner()
        for path in recordings[:5]:
            try:
                async for _, frame in self._iter_strips(path, region, fps=sample_fps, limit=6):
                    learn_structural(structural, templates, binarise(frame))
            except (FFmpegError, OSError):
                continue
        templates = merge_templates(templates, structural.build(min_samples=1))

        missing = missing_characters(templates)
        if missing:
            log.warning("OSD templates incomplete", missing=sorted(missing))

        self._templates = templates
        if self._template_path is not None:
            templates.save(self._template_path)
        return templates

    # -- extraction --------------------------------------------------------------------

    async def _iter_strips(
        self,
        path: Path,
        region: OsdRegion,
        *,
        fps: float,
        limit: int | None = None,
        width: int | None = None,
        height: int | None = None,
        hwaccel: str = "auto",
    ):
        if width is None or height is None:
            info = await probe(path)
            width, height = info.width or 1920, info.height or 1080
        crop = region.to_crop(width, height)
        count = 0
        async for offset, frame in iter_frames(
            path, fps=fps, crop=crop, grayscale=True, hwaccel=hwaccel
        ):
            yield offset, frame
            count += 1
            if limit is not None and count >= limit:
                return

    async def calibrate_region(
        self, path: Path, *, samples: int = 5, hwaccel: str = "auto"
    ) -> OsdRegion | None:
        """Locate the overlay by sampling full frames from a recording."""
        frames: list[np.ndarray] = []
        try:
            async for _, frame in iter_frames(
                path, fps=0.5, grayscale=True, hwaccel=hwaccel, duration=samples * 2.0
            ):
                frames.append(frame)
                if len(frames) >= samples:
                    break
        except (FFmpegError, OSError) as exc:
            log.warning("calibration decode failed", file=path.name, error=str(exc))
            return None
        return calibrate(frames)

    async def extract(
        self,
        path: Path,
        *,
        region: OsdRegion,
        sample_fps: float = 1.0,
        min_confidence: float = 0.6,
        max_speed_kmh: float = 300.0,
        min_move_m: float = 12.0,
        hwaccel: str = "auto",
    ) -> TelemetryResult:
        """Decode telemetry for one recording.

        ``sample_fps`` defaults to 1.0 because the overlay updates once per second.
        Sampling faster costs proportionally more decode time and returns duplicates.
        """
        result = TelemetryResult()
        if self._templates is None:
            result.warnings.append("no OSD glyph templates available")
            return result

        try:
            info = await probe(path)
        except FFmpegError as exc:
            result.warnings.append(f"probe failed: {exc}")
            return result

        width, height = info.width or 1920, info.height or 1080
        readings: list[tuple[float, OsdReading]] = []

        try:
            async for offset, frame in self._iter_strips(
                path, region, fps=sample_fps, width=width, height=height, hwaccel=hwaccel
            ):
                result.frames_read += 1
                text, confidence = decode_line(binarise(frame), self._templates)
                reading = parse_osd_text(text, confidence=confidence, max_speed_kmh=max_speed_kmh)
                if not reading.valid:
                    result.parse_failures += 1
                    continue
                if confidence < min_confidence:
                    result.low_confidence += 1
                    continue
                readings.append((offset, reading))
        except DecodeError as exc:
            # Partial telemetry from a damaged file is still worth keeping.
            result.warnings.append(f"decode ended early: {exc}")
        except FFmpegError as exc:
            result.warnings.append(f"decode failed: {exc}")
            return result

        ordered = enforce_monotonic([r for _, r in readings])
        kept = {id(r) for r in ordered}
        readings = [(o, r) for o, r in readings if id(r) in kept]

        result.samples = [
            TelemetrySample(
                t_offset_s=round(offset, 3),
                captured_at=reading.captured_at,
                lat=reading.lat,
                lon=reading.lon,
                has_fix=reading.has_fix,
                speed_kmh=reading.speed_kmh,
                heading_deg=None,
                ocr_confidence=reading.confidence,
                raw_text=reading.raw_text[:255],
            )
            for offset, reading in readings
        ]

        self._derive(result, min_move_m=min_move_m)
        if result.samples:
            result.osd_start_time = result.samples[0].captured_at
        return result

    @staticmethod
    def _seconds_between(a: TelemetrySample, b: TelemetrySample) -> float:
        """Elapsed time between two samples, preferring the overlay clock.

        Falls back to the decode offset, which is what exists when a timestamp failed to
        parse but the coordinates did.
        """
        if a.captured_at is not None and b.captured_at is not None:
            gap = abs((b.captured_at - a.captured_at).total_seconds())
            if gap > 0:
                return gap
        return max(0.0, b.t_offset_s - a.t_offset_s)

    @staticmethod
    def _derive(result: TelemetryResult, *, min_move_m: float) -> None:
        """Fill in heading and the journey rollups, discarding impossible movement."""
        # Metres a vehicle could plausibly cover in one second. Anything beyond this
        # between consecutive fixes is a misread coordinate, not travel.
        max_step_m = _MAX_PLAUSIBLE_SPEED_KMH * 1000.0 / 3600.0
        rejected: list[TelemetrySample] = []

        fixes = [s for s in result.samples if s.has_fix and s.lat is not None and s.lon is not None]
        if fixes:
            result.first_fix, result.last_fix = fixes[0], fixes[-1]

        # `anchor` stays on the last point that moved a real distance. Advancing it on
        # every sample instead would re-admit the jitter this threshold exists to reject,
        # since each individual hop clears nothing but they sum to hundreds of metres.
        anchor: TelemetrySample | None = None
        for sample in fixes:
            if anchor is None:
                anchor = sample
                continue

            step = haversine_m(anchor.lat, anchor.lon, sample.lat, sample.lon)  # type: ignore[arg-type]

            # An OCR misread that lands inside the valid coordinate range still passes
            # every per-point check: 138.6769 read as 13.8769 is a perfectly legal
            # longitude about 13,000 km away. Only comparing consecutive fixes catches it,
            # and without this a single bad digit turned a two-minute clip into a
            # 31,000 km journey. Reject the *sample*, not the anchor -- the anchor is the
            # last position we still trust.
            gap_s = TelemetryExtractor._seconds_between(anchor, sample)
            if step > max_step_m * max(1.0, gap_s):
                sample.has_fix = False
                sample.lat = None
                sample.lon = None
                rejected.append(sample)
                continue

            if step < min_move_m:
                # Within the overlay's 4-decimal-place quantisation: not movement, and a
                # bearing between two jitter samples would be meaningless too.
                continue
            result.distance_m += step
            sample.heading_deg = round(
                bearing_deg(anchor.lat, anchor.lon, sample.lat, sample.lon),
                1,  # type: ignore[arg-type]
            )
            anchor = sample

        if rejected:
            result.implausible_jumps = len(rejected)
            result.warnings.append(
                f"discarded {len(rejected)} position(s) implying travel faster than "
                f"{_MAX_PLAUSIBLE_SPEED_KMH:.0f} km/h; these are misread coordinates"
            )
            # first/last fix may have just been invalidated.
            still_fixed = [s for s in result.samples if s.has_fix]
            result.first_fix = still_fixed[0] if still_fixed else None
            result.last_fix = still_fixed[-1] if still_fixed else None

        speeds = [s.speed_kmh for s in result.samples if s.speed_kmh is not None]
        if speeds:
            result.max_speed_kmh = max(speeds)
            # Averaged over moving samples only. Including long idle periods would report
            # a "trip average" of a few km/h for any journey with traffic lights.
            moving = [v for v in speeds if v > 1.0]
            result.avg_speed_kmh = round(sum(moving) / len(moving), 1) if moving else 0.0
