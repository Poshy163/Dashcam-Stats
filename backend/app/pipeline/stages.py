"""Processing stages.

Each stage is independently re-runnable and owns exactly one ``StageState`` column, so
"reprocess telemetry only" never re-runs detection and reprocessing anything never
duplicates a row.

Cost discipline, which is what makes this viable over terabytes:

* telemetry decodes a thin overlay strip at 1 fps — the rate the overlay itself updates;
* detection samples a few frames a second and stores *tracks*, not frames;
* plate OCR runs on a handful of crops per tracked vehicle, never per frame.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.detector import ObjectDetector
from app.ai.normalise_au import normalise
from app.ai.plates import (
    PlateDetector,
    PlateOCR,
    PlateReading,
    crop_with_margin,
    plate_box_in_frame,
    select_ocr_candidates,
    vote_track_plate,
)
from app.ai.tracker import ByteTracker
from app.config import get_config
from app.core.logging import get_logger
from app.core.paths import relative_to_media, resolve_footage_path
from app.core.settings_service import get_settings_service
from app.db.models import (
    Camera,
    CameraRole,
    Detection,
    OsdProfile,
    Plate,
    PlateObservation,
    Recording,
    RecordingState,
    StageState,
    TelemetryPoint,
    TrackedObject,
)
from app.hardware.ffmpeg import (
    DecodeError,
    FFmpegError,
    extract_frame,
    iter_frames,
    probe,
)
from app.journeys.builder import JourneyBuilder
from app.osd import (
    GlyphTemplates,
    OsdRegion,
    TelemetryExtractor,
    expected_time_from_filename,
    missing_characters,
    select_training_set,
)

log = get_logger(__name__)

ProgressCallback = Callable[[str, float], None]

#: Learned overlay templates are shared across every recording and rarely change.
_templates_cache: GlyphTemplates | None = None

#: How far the overlay clock may sit from the filename before it is treated as a misread.
#: Generous enough to absorb a camera whose clock drifts by a few minutes, tight enough
#: that a mangled hour or day digit is caught.
_MAX_OSD_FILENAME_DRIFT_S = 15 * 60


class StageError(RuntimeError):
    """A stage failed for this recording. Carries whether retrying could ever help."""

    def __init__(self, message: str, *, permanent: bool = False) -> None:
        super().__init__(message)
        self.permanent = permanent


@dataclass(slots=True)
class StageResult:
    name: str
    ok: bool
    detail: str | None = None
    stats: dict[str, object] | None = None


def _media_path(*parts: str) -> Path:
    path = get_config().media_dir.joinpath(*parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _save_jpeg(image: np.ndarray, path: Path, quality: int = 85) -> str | None:
    """Write a BGR array as JPEG and return its media-relative path."""
    try:
        import cv2

        path.parent.mkdir(parents=True, exist_ok=True)
        if cv2.imwrite(str(path), image, [int(cv2.IMWRITE_JPEG_QUALITY), quality]):
            return relative_to_media(path)
    except Exception as exc:
        log.debug("could not write crop", path=str(path), error=str(exc))
    return None


#: A frame this flat carries no picture. Decoders emit a solid fill -- classically green
#: in YUV, where a zeroed chroma plane renders as green -- when reference frames are
#: missing, which is exactly what the damaged segments in this corpus produce.
_FLAT_FRAME_STDDEV = 12.0

#: How far the green channel may dominate before the frame is treated as decoder fill
#: rather than a genuinely green scene. Real foliage never comes close.
_GREEN_DOMINANCE = 1.6

#: Fraction of rows that may look like a column smear before the frame is rejected.
#:
#: Measured over 185 thumbnails sampled across the whole library plus decoded frames from
#: the two broken clips: legitimate content peaks at 0.008 and averages 0.0002, while the
#: smeared frames score 0.88 and 0.57. The threshold sits twelve times above anything real.
_SMEAR_ROW_FRACTION = 0.10

#: A row counts as smeared when it has horizontal detail, almost no vertical difference
#: from the row below it, and the second is small even relative to the first. All three
#: are needed: the first two alone would catch a genuinely flat sky.
_SMEAR_MIN_DX = 2.0
_SMEAR_MAX_DY = 1.0
_SMEAR_DY_DX_RATIO = 0.25

#: Columns to reduce to before measuring, so the score does not depend on resolution.
_SMEAR_SAMPLE_COLUMNS = 480

#: The burnt-in telemetry banner runs along the bottom and is legitimately low-contrast
#: vertically, so it is excluded rather than allowed to bias the measurement.
_SMEAR_IGNORE_BOTTOM = 0.08


def smear_fraction(frame: np.ndarray) -> float:
    """How much of the frame is a replicated row rather than a picture.

    When a transport stream is cut short, the decoder still emits frames for the missing
    tail: it takes the last macroblock row it managed to decode and repeats it downwards.
    The result is a field of vertical stripes, which is the single most common bad
    thumbnail in this library -- and it is invisible to a variance test, because it is not
    flat. It is the opposite of flat. Measured on the live server, one such frame scored
    80.01 on ``frame_quality`` while the clean frame from the same file scored 34.41, so
    the corruption was not merely tolerated, it was *preferred*.

    What separates it is direction. A real scene varies both across and down; a smear
    varies only across. Per row this compares the mean absolute difference to the row
    below against the mean absolute difference along the row itself.
    """
    if frame is None or frame.size == 0:
        return 0.0

    grey = frame.mean(axis=2) if frame.ndim == 3 else frame.astype(np.float64)
    height, width = grey.shape[:2]
    if height < 8 or width < 8:
        return 0.0

    keep = max(4, int(height * (1.0 - _SMEAR_IGNORE_BOTTOM)))
    grey = grey[:keep]
    if width > _SMEAR_SAMPLE_COLUMNS:
        columns = np.linspace(0, width - 1, _SMEAR_SAMPLE_COLUMNS).astype(np.int32)
        grey = grey[:, columns]

    dx = np.abs(np.diff(grey, axis=1)).mean(axis=1)[:-1]
    dy = np.abs(np.diff(grey, axis=0)).mean(axis=1)
    if dx.size == 0:
        return 0.0

    smeared = (dx > _SMEAR_MIN_DX) & (dy < _SMEAR_MAX_DY) & (dy < _SMEAR_DY_DX_RATIO * dx)
    return float(smeared.mean())


def frame_quality(frame: np.ndarray) -> float:
    """Score how much of a real picture a frame contains. Higher is better, 0 is unusable.

    Damaged footage does not fail to decode; it decodes into something. A missing
    reference frame yields a flat fill, and the resulting thumbnail looks like an
    application bug rather than a broken file. Scoring the frame lets a bad one be
    rejected in favour of another offset, and lets "no thumbnail" be an honest answer.
    """
    if frame is None or frame.size == 0:
        return 0.0

    # Detail. A real scene has structure at every scale; a fill has none.
    detail = float(frame.std())
    if detail < _FLAT_FRAME_STDDEV:
        return 0.0

    # Checked before the variance tests below, because a smear passes all of them with a
    # high score. This is the failure the user actually sees in the recordings list.
    if smear_fraction(frame) > _SMEAR_ROW_FRACTION:
        return 0.0

    if frame.ndim == 3 and frame.shape[2] >= 3:
        blue, green, red = (float(frame[..., i].mean()) for i in range(3))
        others = max(1.0, (blue + red) / 2.0)
        if green / others > _GREEN_DOMINANCE:
            # Overwhelmingly green with little else: decoder fill, not a scene.
            return 0.0

        # A frame that is largely one flat region -- half picture, half fill, which is how
        # partially-damaged segments decode -- scores below a clean one.
        halves = [frame[: frame.shape[0] // 2], frame[frame.shape[0] // 2 :]]
        detail = min(float(h.std()) for h in halves if h.size) or detail

    return detail


async def _choose_thumbnail(
    source: Path,
    target: Path,
    *,
    duration_s: float | None,
    codec: str | None,
    width: int,
    quality: int,
) -> bool:
    """Write the best thumbnail the file can offer, or none at all.

    Several offsets are tried rather than one. A single fixed seek lands on a broken
    frame often enough in damaged footage, and the first few seconds of any clip are the
    most likely to be mid-GOP after a truncated write.
    """
    span = duration_s or 4.0
    # Spread across the clip, with the start second rather than last.
    #
    # The old order put 0.0 at the end, on the reasoning that damage concentrates in the
    # first seconds. On this corpus the opposite is true: the files that produce a bad
    # thumbnail are truncated at the *end*, so every offset derived from the span lands in
    # the missing tail and only 0.0 has a picture. Combined with the early break below,
    # 0.0 was then never reached -- the smear scored 80 and stopped the search on the first
    # candidate, while the frame that would have worked sat unused at the end of the list.
    candidates = [t for t in (span * 0.5, 0.0, span * 0.25, span * 0.75, 1.0) if t < span]

    best_frame: np.ndarray | None = None
    best_score = 0.0

    for offset in candidates:
        try:
            frame = await extract_frame(source, offset, codec=codec)
        except FFmpegError:
            continue
        if frame is None:
            continue
        score = frame_quality(frame)
        if score > best_score:
            best_frame, best_score = frame, score
        # Good enough; no point decoding further.
        if best_score > 40.0:
            break

    if best_frame is None or best_score <= 0.0:
        return False

    scale = width / best_frame.shape[1]
    if 0 < scale < 1:
        height = max(1, int(best_frame.shape[0] * scale))
        ys = (np.arange(height) / scale).astype(np.int32).clip(0, best_frame.shape[0] - 1)
        xs = (np.arange(width) / scale).astype(np.int32).clip(0, best_frame.shape[1] - 1)
        best_frame = best_frame[ys][:, xs]

    return _save_jpeg(best_frame, target, quality) is not None


# --------------------------------------------------------------------------------------
# Stage 1 - inspection
# --------------------------------------------------------------------------------------


async def stage_inspect(
    session: AsyncSession, recording: Recording, *, progress: ProgressCallback | None = None
) -> StageResult:
    """Read container metadata and generate a thumbnail."""
    path = resolve_footage_path(recording.rel_path)
    if not path.exists():
        recording.file_missing = True
        raise StageError(f"{recording.filename} is no longer on disk", permanent=True)

    try:
        info = await probe(path)
    except FFmpegError as exc:
        # Empty files and files with no video stream will never succeed, however many
        # times we retry them.
        raise StageError(str(exc), permanent=True) from exc

    recording.container = info.container
    recording.video_codec = info.video_codec
    recording.video_profile = info.video_profile
    recording.width = info.width
    recording.height = info.height
    recording.fps = info.fps
    recording.fps_container = info.fps_container
    recording.bitrate = info.bitrate
    recording.pix_fmt = info.pix_fmt
    recording.has_audio = info.has_audio
    recording.audio_codec = info.audio_codec
    recording.audio_sample_rate = info.audio_sample_rate
    recording.audio_channels = info.audio_channels
    recording.duration_s = info.duration_s
    recording.size_bytes = info.size_bytes
    recording.probe_json = {"warnings": info.warnings, "pts_wrapped": info.pts_wrapped}

    if recording.started_at is None:
        parsed = expected_time_from_filename(recording.filename)
        if parsed is not None:
            recording.started_at = parsed.replace(tzinfo=UTC)
    if recording.started_at and info.duration_s:
        recording.ended_at = recording.started_at + timedelta(seconds=info.duration_s)

    if progress:
        progress("metadata", 0.6)

    settings = get_settings_service()
    if not recording.thumbnail_path:
        thumb = _media_path("thumbnails", f"{recording.id:08d}.jpg")
        chosen = await _choose_thumbnail(
            path,
            thumb,
            duration_s=info.duration_s,
            codec=info.video_codec,
            width=int(settings.get_nowait("general.thumbnail_width")),
            quality=int(settings.get_nowait("general.thumbnail_quality")),
        )
        if chosen:
            recording.thumbnail_path = relative_to_media(thumb)
        else:
            # No frame in the file decoded to a usable picture. Saying so is more useful
            # than a green rectangle that looks like the application misbehaved.
            info.warnings.append(
                "no usable frame could be decoded; the source file appears to be damaged"
            )

    recording.probe_json = {
        "warnings": info.warnings,
        "pts_wrapped": info.pts_wrapped,
        "truncated": info.truncated,
        "source_damaged": bool(info.warnings),
    }

    recording.metadata_state = StageState.DONE
    if recording.state is RecordingState.DISCOVERED:
        recording.state = RecordingState.METADATA_EXTRACTED

    return StageResult(
        "metadata",
        True,
        stats={
            "duration_s": info.duration_s,
            "resolution": info.resolution,
            "fps": info.fps,
            "warnings": info.warnings,
        },
    )


# --------------------------------------------------------------------------------------
# Stage 2 - telemetry
# --------------------------------------------------------------------------------------


async def _active_profile(session: AsyncSession) -> OsdRegion:
    profile = (
        await session.execute(select(OsdProfile).where(OsdProfile.active.is_(True)).limit(1))
    ).scalar_one_or_none()
    if profile is None:
        return OsdRegion()
    return OsdRegion(x=profile.region_x, y=profile.region_y, w=profile.region_w, h=profile.region_h)


async def _get_templates(
    session: AsyncSession, extractor: TelemetryExtractor, region: OsdRegion
) -> GlyphTemplates | None:
    """Load learned overlay templates, learning them from the library on first use."""
    global _templates_cache
    if _templates_cache is not None:
        extractor._templates = _templates_cache
        return _templates_cache

    path = get_config().data_dir / "osd_templates.npz"
    if extractor.load_templates(path):
        _templates_cache = extractor.templates
        return _templates_cache

    # Choose training footage that actually contains every digit. Sampling the first few
    # files is not enough: a week of recordings can easily contain no 9 in any stable
    # timestamp field, and an unlearned digit decodes as its nearest lookalike at high
    # confidence, so no threshold would catch it.
    rows = (
        await session.execute(
            select(Recording.rel_path, Recording.filename)
            .where(Recording.file_missing.is_(False), Recording.ignored.is_(False))
            .order_by(Recording.started_at.asc())
            .limit(500)
        )
    ).all()

    candidates: list[tuple[str, datetime]] = []
    by_name: dict[str, str] = {}
    for rel_path, filename in rows:
        when = expected_time_from_filename(filename)
        if when is not None:
            candidates.append((filename, when))
            by_name[filename] = rel_path

    if not candidates:
        return None

    chosen = select_training_set(candidates, limit=10)
    paths = [resolve_footage_path(by_name[name]) for name, _ in chosen]
    paths = [p for p in paths if p.exists()]
    if not paths:
        return None

    extractor._template_path = path
    templates = await extractor.learn_templates(paths, region=region)
    missing = missing_characters(templates)
    if missing:
        log.warning(
            "overlay templates are incomplete; telemetry accuracy will suffer",
            missing=sorted(missing),
        )
    _templates_cache = templates
    return templates


# Loaded models, kept for the life of the process and shared by every worker.
#
# Building an inference session is not cheap: ONNX Runtime parses the graph and the
# OpenVINO provider compiles it for the target device, which takes seconds on the iGPU.
# Constructing the detector per recording paid that on all 678 of them, twice over with two
# workers -- most of the wall-clock cost of a short clip, and a large allocation churned
# each time, which is the shape of the container being OOM-killed mid-run.
#
# Keyed by model name so changing the setting rebuilds rather than silently keeping the old
# network. The lock stops two workers compiling the same graph at once.
_detector_cache: dict[str, ObjectDetector] = {}
_plate_models: tuple[PlateDetector, PlateOCR] | None = None
_model_lock = asyncio.Lock()


async def _shared_detector(name: str) -> ObjectDetector | None:
    async with _model_lock:
        cached = _detector_cache.get(name)
        if cached is not None:
            return cached if cached.available else None
        detector = ObjectDetector(name)
        if not await detector.load():
            # Cached even when unavailable, so a missing model is not re-attempted -- and
            # re-downloaded -- once per recording for the length of the queue.
            _detector_cache[name] = detector
            return None
        _detector_cache[name] = detector
        return detector


async def _shared_plate_models() -> tuple[PlateDetector, PlateOCR] | None:
    global _plate_models
    async with _model_lock:
        if _plate_models is None:
            detector, ocr = PlateDetector(), PlateOCR()
            ok = await detector.load() and await ocr.load()
            _plate_models = (detector, ocr)
            if not ok:
                return None
        detector, ocr = _plate_models
        return (detector, ocr) if detector.available and ocr.available else None


async def warm_models() -> None:
    """Build the inference sessions before any job needs one.

    Compiling a network for the iGPU takes on the order of a minute the first time, and
    the OpenVINO cache only helps on subsequent starts. Doing it here means that cost lands
    once, in the background, on a process that has just started -- rather than inside the
    first recording's detection stage, where it looks exactly like a stalled job.

    Failures are deliberately swallowed. This is an optimisation: if a model cannot be
    built now, the stage that needs it will report itself unavailable in the normal way,
    and the container must still start.
    """
    settings = get_settings_service()
    try:
        if bool(settings.get_nowait("processing.detection_enabled")):
            await _shared_detector(str(settings.get_nowait("processing.detection_model")))
        if bool(settings.get_nowait("plates.enabled")):
            await _shared_plate_models()
    except Exception as exc:
        log.warning("could not warm inference models", error=f"{type(exc).__name__}: {exc}")


async def stage_telemetry(
    session: AsyncSession, recording: Recording, *, progress: ProgressCallback | None = None
) -> StageResult:
    """Decode the burned-in overlay into telemetry points."""
    settings = get_settings_service()
    if not bool(settings.get_nowait("telemetry.enabled")):
        recording.telemetry_state = StageState.SKIPPED
        return StageResult("telemetry", True, "disabled")

    path = resolve_footage_path(recording.rel_path)
    region = await _active_profile(session)
    extractor = TelemetryExtractor()

    if await _get_templates(session, extractor, region) is None:
        recording.telemetry_state = StageState.SKIPPED
        return StageResult("telemetry", True, "no overlay templates available")

    if progress:
        progress("telemetry", 0.1)

    result = await extractor.extract(
        path,
        region=region,
        sample_fps=float(settings.get_nowait("telemetry.sample_fps")),
        min_confidence=float(settings.get_nowait("telemetry.min_confidence")),
        max_speed_kmh=float(settings.get_nowait("telemetry.max_speed_kmh")),
        min_move_m=float(settings.get_nowait("telemetry.min_move_metres")),
        hwaccel="auto" if await settings.hardware_acceleration() else "cpu",
    )

    # Reprocessing replaces the previous pass rather than appending to it.
    await session.execute(delete(TelemetryPoint).where(TelemetryPoint.recording_id == recording.id))

    tz = await settings.timezone()
    rows = []
    for sample in result.samples:
        captured = sample.captured_at
        if captured is not None and captured.tzinfo is None:
            # The overlay prints local wall-clock time with no zone; attach the configured
            # one so every stored timestamp is comparable.
            captured = captured.replace(tzinfo=tz).astimezone(UTC)
        rows.append(
            {
                "recording_id": recording.id,
                "journey_id": recording.journey_id,
                "t_offset_s": sample.t_offset_s,
                "captured_at": captured,
                "lat": sample.lat,
                "lon": sample.lon,
                "has_fix": sample.has_fix,
                "speed_kmh": sample.speed_kmh,
                "heading_deg": sample.heading_deg,
                "ocr_confidence": sample.ocr_confidence,
                "raw_text": sample.raw_text,
            }
        )

    if rows:
        await session.execute(TelemetryPoint.__table__.insert(), rows)

    recording.telemetry_point_count = len(rows)
    recording.gps_point_count = result.fix_count
    recording.has_gps = result.has_gps
    recording.distance_m = result.distance_m or None
    recording.avg_speed_kmh = result.avg_speed_kmh
    recording.max_speed_kmh = result.max_speed_kmh
    if result.first_fix:
        recording.start_lat = result.first_fix.lat
        recording.start_lon = result.first_fix.lon

    # The overlay clock is the camera's own time and beats the filename, which only
    # records when the file was opened -- but only when the two agree. Both come from the
    # same device, so a large disagreement means the overlay was misread, not that the
    # filename is wrong. Accepting a bad reading here is expensive: started_at drives
    # journey clustering, so one mangled digit can tear a drive apart and file a recording
    # under a time it never happened.
    if result.osd_start_time is not None:
        filename_time = expected_time_from_filename(recording.filename)
        drift = (
            abs((result.osd_start_time - filename_time).total_seconds())
            if filename_time is not None
            else 0.0
        )
        if filename_time is not None and drift > _MAX_OSD_FILENAME_DRIFT_S:
            log.warning(
                "ignoring overlay timestamp: it disagrees with the filename",
                file=recording.filename,
                osd=result.osd_start_time.isoformat(),
                filename_time=filename_time.isoformat(),
                drift_s=round(drift),
            )
        else:
            localised = result.osd_start_time.replace(tzinfo=tz).astimezone(UTC)
            recording.started_at = localised
            recording.time_from_osd = True
            if recording.duration_s:
                recording.ended_at = localised + timedelta(seconds=recording.duration_s)

    recording.telemetry_state = StageState.DONE
    return StageResult(
        "telemetry",
        True,
        stats={
            "points": len(rows),
            "fixes": result.fix_count,
            "distance_m": result.distance_m,
            "parse_failures": result.parse_failures,
            "warnings": result.warnings,
        },
    )


# --------------------------------------------------------------------------------------
# Stage 3 - object detection and tracking
# --------------------------------------------------------------------------------------


def _should_analyse(recording: Recording, camera: Camera | None) -> bool:
    if camera is None:
        return True
    if camera.role is CameraRole.REAR:
        return bool(get_settings_service().get_nowait("processing.process_rear_camera"))
    return True


async def _nearest_fix(
    session: AsyncSession, recording_id: int, offset_s: float
) -> tuple[float | None, float | None, datetime | None]:
    """The telemetry fix closest to an offset, for stamping a detection with a location."""
    row = (
        await session.execute(
            select(TelemetryPoint.lat, TelemetryPoint.lon, TelemetryPoint.captured_at)
            .where(
                TelemetryPoint.recording_id == recording_id,
                TelemetryPoint.has_fix.is_(True),
            )
            .order_by(func.abs(TelemetryPoint.t_offset_s - offset_s))
            .limit(1)
        )
    ).first()
    return row if row else (None, None, None)


async def stage_detect(
    session: AsyncSession, recording: Recording, *, progress: ProgressCallback | None = None
) -> StageResult:
    """Detect and track road objects across sampled frames."""
    settings = get_settings_service()
    if not bool(settings.get_nowait("processing.detection_enabled")):
        recording.detection_state = StageState.SKIPPED
        return StageResult("detection", True, "disabled")

    camera = await session.get(Camera, recording.camera_id) if recording.camera_id else None
    if not _should_analyse(recording, camera):
        recording.detection_state = StageState.SKIPPED
        return StageResult("detection", True, "rear camera analysis disabled")

    detector = await _shared_detector(str(settings.get_nowait("processing.detection_model")))
    if detector is None:
        recording.detection_state = StageState.SKIPPED
        return StageResult("detection", True, "detection model unavailable")

    path = resolve_footage_path(recording.rel_path)
    sample_fps = float(settings.get_nowait("processing.frame_sample_fps"))
    tracker = ByteTracker()
    duration = recording.duration_s or 0.0
    frames = 0

    try:
        async for offset, frame in iter_frames(
            path,
            fps=sample_fps,
            hwaccel="auto" if await settings.hardware_acceleration() else "cpu",
            codec=recording.video_codec,
        ):
            detections = await detector.detect(frame)
            tracker.update(detections, offset, frame)
            frames += 1
            if progress and duration:
                progress("detection", min(0.95, offset / duration))
    except DecodeError as exc:
        # Partial results from a damaged file are still worth keeping.
        log.warning("detection ended early", file=recording.filename, error=str(exc))
    except FFmpegError as exc:
        raise StageError(f"decode failed: {exc}") from exc

    tracks = tracker.finish(min_frames=int(settings.get_nowait("processing.track_min_frames")))

    keep_detections = bool(settings.get_nowait("advanced.keep_sparse_detections"))
    stride = max(1, int(settings.get_nowait("advanced.detection_store_stride")))
    quality = int(settings.get_nowait("general.thumbnail_quality"))

    # Position lookups and JPEG encoding first, both outside the write transaction. With
    # 410 tracks on a busy clip that is 410 queries and 410 encodes, and doing them after
    # the delete meant holding SQLite's write lock for all of it. See the longer note in
    # stage_plates: the same mistake there was costing whole recordings.
    prepared = []
    for track in tracks:
        lat, lon, captured = await _nearest_fix(session, recording.id, track.first_offset_s)
        crop_path = None
        if track.best_crop is not None and track.best_crop.size:
            crop_path = _save_jpeg(
                track.best_crop,
                _media_path("vehicles", f"{recording.id:08d}", f"{track.track_key:04d}.jpg"),
                quality,
            )
        prepared.append((track, lat, lon, captured, crop_path))

    await session.execute(delete(TrackedObject).where(TrackedObject.recording_id == recording.id))

    for track, lat, lon, captured, crop_path in prepared:
        stored = TrackedObject(
            recording_id=recording.id,
            journey_id=recording.journey_id,
            track_key=track.track_key,
            class_label=track.class_label,
            confidence_max=track.confidence_max,
            confidence_avg=track.confidence_avg,
            first_seen_offset_s=track.first_offset_s,
            last_seen_offset_s=track.last_offset_s,
            duration_s=track.duration_s,
            frame_count=track.frame_count,
            first_seen_at=captured,
            last_seen_at=captured,
            lat=lat,
            lon=lon,
            best_frame_offset_s=track.best_offset_s,
            best_bbox=list(track.best_box) if track.best_box else None,
            crop_path=crop_path,
        )
        session.add(stored)
        await session.flush()

        if keep_detections:
            for index, (offset, confidence, x, y, w, h) in enumerate(track.samples):
                if index % stride:
                    continue
                session.add(
                    Detection(
                        tracked_object_id=stored.id,
                        recording_id=recording.id,
                        t_offset_s=offset,
                        class_label=track.class_label,
                        confidence=confidence,
                        x=x,
                        y=y,
                        w=w,
                        h=h,
                    )
                )

    recording.vehicle_count = len(tracks)
    recording.detection_state = StageState.DONE
    return StageResult(
        "detection",
        True,
        stats={
            "frames_analysed": frames,
            "tracks": len(tracks),
            "device": detector.device,
        },
    )


# --------------------------------------------------------------------------------------
# Stage 4 - licence plates
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class _PlateHit:
    """A confirmed read, held until the stage's single write phase at the end."""

    track: TrackedObject
    result: object
    vote: object
    #: Kept only when crops are being saved, since it is the one large field here.
    vehicle_crop: np.ndarray | None


async def stage_plates(
    session: AsyncSession, recording: Recording, *, progress: ProgressCallback | None = None
) -> StageResult:
    """Read plates from tracked vehicles and upsert the plate database."""
    settings = get_settings_service()
    if not bool(settings.get_nowait("plates.enabled")):
        recording.plate_state = StageState.SKIPPED
        return StageResult("plates", True, "disabled")

    camera = await session.get(Camera, recording.camera_id) if recording.camera_id else None
    if not _should_analyse(recording, camera):
        recording.plate_state = StageState.SKIPPED
        return StageResult("plates", True, "rear camera analysis disabled")

    models = await _shared_plate_models()
    if models is None:
        recording.plate_state = StageState.SKIPPED
        return StageResult("plates", True, "plate models unavailable")
    detector, ocr = models

    tracks = list(
        (
            await session.execute(
                select(TrackedObject).where(
                    TrackedObject.recording_id == recording.id,
                    TrackedObject.class_label.in_(["car", "truck", "bus", "motorcycle"]),
                )
            )
        ).scalars()
    )
    if not tracks:
        recording.plate_state = StageState.DONE
        return StageResult("plates", True, "no vehicles to inspect")

    path = resolve_footage_path(recording.rel_path)
    min_width = int(settings.get_nowait("plates.min_plate_width"))
    max_reads = int(settings.get_nowait("plates.max_ocr_per_track"))
    min_store = float(settings.get_nowait("plates.min_store_confidence"))
    save_crops = bool(settings.get_nowait("plates.save_crops"))
    region_setting = str(settings.get_nowait("plates.region"))
    quality = int(settings.get_nowait("general.thumbnail_quality"))

    # Read every plate first, write them all at the end.
    #
    # The delete used to sit here, at the top. It opens SQLite's write transaction, and
    # nothing commits until the stage returns, so the loop below -- an ffmpeg seek, a
    # detector pass and an OCR pass per tracked vehicle, up to 410 of them, about 150
    # seconds -- ran with the write lock held the entire time. Everything else that writes
    # queued behind it: the other worker's detection stage lost 150 seconds of finished GPU
    # work on its first write, the scheduler's reclaim task failed a third of the time, the
    # log sink stalled so the Logs page went quiet exactly when it was needed, and two
    # recordings burned all four attempts and were marked permanently failed for a reason
    # that had nothing to do with the footage. Two workers behaved as one.
    #
    # Holding results in memory costs little: only readings that beat min_store are kept,
    # which is around one track in a hundred.
    hits: list[_PlateHit] = []
    for index, track in enumerate(tracks):
        if track.best_frame_offset_s is None or not track.best_bbox:
            continue
        if progress:
            progress("plates", (index + 1) / max(1, len(tracks)))

        frame = None
        try:
            async for _, decoded in iter_frames(
                path,
                start=track.best_frame_offset_s,
                duration=0.5,
                fps=None,
                hwaccel="auto" if await settings.hardware_acceleration() else "cpu",
                codec=recording.video_codec,
            ):
                frame = decoded
                break
        except FFmpegError:
            continue
        if frame is None:
            continue

        height, width = frame.shape[:2]
        bx1, by1, bx2, by2 = track.best_bbox
        vehicle = _as_detection(bx1, by1, bx2, by2, track.class_label, track.confidence_max)
        vehicle_crop = crop_with_margin(
            frame, (vehicle.x, vehicle.y, vehicle.x + vehicle.w, vehicle.y + vehicle.h), 0.02
        )
        if vehicle_crop is None or vehicle_crop.size == 0:
            continue

        boxes = await detector.detect(vehicle_crop, min_width_px=min_width)
        readings: list[PlateReading] = []
        for plate_box, confidence in boxes:
            frame_box = plate_box_in_frame(vehicle, plate_box)
            crop = crop_with_margin(frame, frame_box)
            if crop is None:
                continue
            readings.append(
                PlateReading(
                    raw_text="",
                    ocr_confidence=0.0,
                    detection_confidence=confidence,
                    bbox=frame_box,
                    crop=crop,
                    vehicle_crop=vehicle_crop,
                    offset_s=track.best_frame_offset_s,
                )
            )

        for reading in select_ocr_candidates(readings, max_reads):
            text, confidence = await ocr.read(reading.crop)
            reading.raw_text = text
            reading.ocr_confidence = confidence

        vote = vote_track_plate(readings)
        if vote is None or vote.ocr_confidence < min_store:
            continue

        hits.append(
            _PlateHit(
                track=track,
                result=normalise(vote.text, region=region_setting),
                vote=vote,
                vehicle_crop=vehicle_crop if save_crops else None,
            )
        )

    # Everything below writes, and only what is below. One observation per plate per
    # tracked vehicle, so reprocessing replaces rather than accumulates.
    await session.execute(
        delete(PlateObservation).where(PlateObservation.recording_id == recording.id)
    )

    for hit in hits:
        track, vote, result = hit.track, hit.vote, hit.result
        plate = await _upsert_plate(session, result, vote.ocr_confidence)

        plate_crop_path = vehicle_crop_path = None
        if save_crops:
            plate_crop_path = (
                _save_jpeg(
                    vote.best.crop,
                    _media_path(
                        "plates", f"{plate.id:08d}", f"{recording.id:08d}_{track.track_key:04d}.jpg"
                    ),
                    quality,
                )
                if vote.best.crop is not None
                else None
            )
            if hit.vehicle_crop is not None:
                vehicle_crop_path = _save_jpeg(
                    hit.vehicle_crop,
                    _media_path(
                        "plates",
                        f"{plate.id:08d}",
                        f"{recording.id:08d}_{track.track_key:04d}_vehicle.jpg",
                    ),
                    quality,
                )

        session.add(
            PlateObservation(
                plate_id=plate.id,
                recording_id=recording.id,
                journey_id=recording.journey_id,
                camera_id=recording.camera_id,
                tracked_object_id=track.id,
                t_offset_s=track.best_frame_offset_s,
                captured_at=track.first_seen_at,
                first_seen_offset_s=track.first_seen_offset_s,
                last_seen_offset_s=track.last_seen_offset_s,
                raw_text=vote.best.raw_text,
                normalised_text=result.normalised,
                ocr_confidence=vote.ocr_confidence,
                detection_confidence=vote.detection_confidence,
                vote_count=vote.vote_count,
                lat=track.lat,
                lon=track.lon,
                plate_crop_path=plate_crop_path,
                vehicle_crop_path=vehicle_crop_path,
                bbox={"box": list(vote.best.bbox)},
            )
        )

    stored = len(hits)
    await session.flush()
    await _refresh_plate_rollups(session, recording.id)

    recording.plate_count = stored
    recording.plate_state = StageState.DONE
    return StageResult("plates", True, stats={"observations": stored, "tracks": len(tracks)})


def _as_detection(x1, y1, x2, y2, label, confidence):
    from app.ai.detector import Detection2D  # local import keeps the module import graph flat

    return Detection2D(
        class_label=label,
        confidence=confidence,
        x=float(x1),
        y=float(y1),
        w=float(x2 - x1),
        h=float(y2 - y1),
    )


async def _upsert_plate(session: AsyncSession, result, confidence: float) -> Plate:
    """Find or create the plate identity for a normalised reading."""
    key = result.normalised or result.raw.upper()
    plate = (
        await session.execute(select(Plate).where(Plate.normalised_text == key))
    ).scalar_one_or_none()

    if plate is None:
        plate = Plate(
            normalised_text=key,
            display_text=result.display_text,
            region=result.state_hint,
            # Null pattern means the reading matched no known format. The UI shows that
            # as unverified rather than presenting a guess as a confirmed plate.
            pattern_name=result.pattern_name,
            best_confidence=confidence,
        )
        session.add(plate)
        await session.flush()
    elif confidence > plate.best_confidence:
        plate.best_confidence = confidence
        if result.pattern_name and not plate.pattern_name:
            plate.pattern_name = result.pattern_name
            plate.region = result.state_hint
    return plate


async def _refresh_plate_rollups(session: AsyncSession, recording_id: int) -> None:
    plate_ids = list(
        (
            await session.execute(
                select(PlateObservation.plate_id.distinct()).where(
                    PlateObservation.recording_id == recording_id
                )
            )
        ).scalars()
    )
    for plate_id in plate_ids:
        row = (
            await session.execute(
                select(
                    func.count(PlateObservation.id),
                    func.min(PlateObservation.captured_at),
                    func.max(PlateObservation.captured_at),
                    func.count(func.distinct(PlateObservation.journey_id)),
                    func.max(PlateObservation.ocr_confidence),
                ).where(PlateObservation.plate_id == plate_id)
            )
        ).one()
        plate = await session.get(Plate, plate_id)
        if plate is None:
            continue
        plate.observation_count = int(row[0] or 0)
        plate.first_seen_at = row[1]
        plate.last_seen_at = row[2]
        plate.journey_count = int(row[3] or 0)
        plate.best_confidence = float(row[4] or 0.0)


# --------------------------------------------------------------------------------------
# Stage 5 - summarise
# --------------------------------------------------------------------------------------


async def stage_summarise(
    session: AsyncSession, recording: Recording, *, progress: ProgressCallback | None = None
) -> StageResult:
    """Roll up counts, attach a journey, and mark the recording finished."""
    recording.vehicle_count = int(
        (
            await session.execute(
                select(func.count(TrackedObject.id)).where(
                    TrackedObject.recording_id == recording.id
                )
            )
        ).scalar()
        or 0
    )
    recording.plate_count = int(
        (
            await session.execute(
                select(func.count(PlateObservation.id)).where(
                    PlateObservation.recording_id == recording.id
                )
            )
        ).scalar()
        or 0
    )

    journey = await JourneyBuilder().assign_recording(session, recording)

    # Detections and telemetry are written before the journey is known, so backfill the
    # pointer rather than leaving them orphaned from journey-scoped queries.
    if journey is not None:
        for model in (TelemetryPoint, TrackedObject, PlateObservation):
            await session.execute(
                model.__table__.update()
                .where(model.recording_id == recording.id)
                .values(journey_id=journey.id)
            )

    recording.processed_at = datetime.now(UTC)
    recording.state = RecordingState.COMPLETED
    recording.error_message = None
    return StageResult("summarise", True, stats={"journey_id": journey.id if journey else None})


STAGES: dict[str, Callable] = {
    "metadata": stage_inspect,
    "telemetry": stage_telemetry,
    "detection": stage_detect,
    "plates": stage_plates,
    "summarise": stage_summarise,
}

STAGE_ORDER: tuple[str, ...] = ("metadata", "telemetry", "detection", "plates", "summarise")
