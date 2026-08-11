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
import itertools
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
from app.ai.runtime import describe_runtime
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
from app.db.retry import write_with_retry
from app.hardware.ffmpeg import (
    DecodeError,
    FFmpegError,
    extract_frame,
    iter_frames,
    probe,
)
from app.journeys.builder import JourneyBuilder
from app.osd import (
    FixTrack,
    GlyphTemplates,
    Located,
    OsdRegion,
    TelemetryExtractor,
    clock_is_plausible,
    expected_time_from_filename,
    is_valid_coordinate,
    missing_characters,
    select_training_set,
)
from app.pipeline.telemetry_quality import quality_rollup, recover_from_paired_camera

log = get_logger(__name__)

ProgressCallback = Callable[[str, float], None]

#: Learned overlay templates are shared across every recording and rarely change.
_templates_cache: GlyphTemplates | None = None

#: How far the overlay clock may sit from the filename before it is treated as a misread.
#: Generous enough to absorb a camera whose clock drifts by a few minutes, tight enough
#: that a mangled hour or day digit is caught.
_MAX_OSD_FILENAME_DRIFT_S = 15 * 60


#: Why no stage writes ``journey_id`` on the rows it inserts.
#:
#: It is a denormalised pointer, and ``JourneyBuilder._attach`` owns it: ``stage_summarise``
#: backfills it the moment the journey is known, and every ``refresh`` re-points it. Setting
#: it at insert time as well bought nothing and cost correctness, because the value came
#: from ``recording.journey_id`` -- a number read into memory when the job started and
#: **stale by the time the row is written**.
#:
#: A rebuild deletes every automatic journey and creates new ones. It runs on the
#: scheduler's thread while a worker is mid-recording, so the worker's in-memory
#: ``journey_id`` routinely names a journey that no longer exists. The insert then fails
#: with ``FOREIGN KEY constraint failed`` -- observed seven times on the live library, most
#: recently while this was being written, each one costing a full reprocess of the
#: recording.
#:
#: The general rule this is an instance of: a derived value should have exactly one writer.
#: Two writers means one of them is working from a copy, and the copy is always the one
#: that goes stale.
_JOURNEY_ID_IS_DERIVED = True


class StageError(RuntimeError):
    """A stage failed for this recording. Carries whether retrying could ever help.

    Three outcomes, not two. ``permanent`` means no attempt will ever succeed, so the
    recording leaves the queue. ``transient`` means the fault was contention rather than
    the recording -- a write lock held elsewhere -- so it is retried *without* spending an
    attempt, because four collisions with a busy database must not add up to a permanently
    failed recording. Neither set is the ordinary case: retry, and count it.
    """

    def __init__(self, message: str, *, permanent: bool = False, transient: bool = False) -> None:
        super().__init__(message)
        self.permanent = permanent
        self.transient = transient


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

#: Fraction of the frame the smear may occupy before the frame is rejected.
#:
#: Measured both ways. Over 185 thumbnails sampled across the library, legitimate content
#: peaks at 0.008 while the two broken frames score 0.88 and 0.57. But the synthetic test
#: fixtures -- hard-edged colour bars with no sensor noise, which is about as adversarial
#: as this measurement gets -- score 0.10 to 0.18 when every flat row in the frame is
#: counted, which is why the count below is anchored to the bottom edge instead. Anchored,
#: the same fixture frames score 0.0000 and the artefact still scores 0.879.
_SMEAR_ROW_FRACTION = 0.10

#: Non-smeared rows tolerated before the run is considered ended, walking up from the
#: bottom. A couple of rows at the very edge often survive with real content in them.
_SMEAR_MAX_GAP = 8

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

    Only rows in an unbroken run reaching the bottom of the frame are counted, because
    that is what the artefact physically is: the decoder ran out of data and repeated
    downwards, so the smear always ends at the last row. Counting flat rows wherever they
    appeared caught the test fixtures instead -- synthetic colour bars with no sensor
    noise score 0.10 to 0.18 that way, above the threshold, so real frames were being
    thrown away. Anchored to the bottom the same frames score zero.
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

    counted = gap = 0
    for flag in smeared[::-1]:
        if flag:
            counted += 1
            gap = 0
        else:
            gap += 1
            if gap > _SMEAR_MAX_GAP:
                break
    return counted / float(smeared.size)


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
        # Retryable, not permanent. An absent file is very often an absent *share* -- the
        # same event the scanner refuses to read as deletion -- and the file itself may be
        # perfectly good. Condemning it here would take it out of the retry population and
        # out of every bulk requeue, so a mount that came back a minute later would leave
        # the recording unprocessed for good.
        raise StageError(f"{recording.filename} is no longer on disk")

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

    settings = get_settings_service()

    if recording.started_at is None:
        parsed = expected_time_from_filename(recording.filename)
        if parsed is not None:
            # Through the configured zone, exactly as the scanner does it. Stamping UTC on
            # a filename that carries local wall-clock time put the recording nine and a
            # half hours from where it belonged here, which tore its journey in half and
            # moved every detection in it to the wrong day. Two places parsed the same
            # filename and only one of them knew what timezone it was in.
            recording.started_at = parsed.replace(tzinfo=await settings.timezone()).astimezone(UTC)
    if recording.started_at and info.duration_s:
        recording.ended_at = recording.started_at + timedelta(seconds=info.duration_s)

    if progress:
        progress("metadata", 0.6)
    source_unusable = False
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
            source_unusable = True

    recording.probe_json = {
        "warnings": info.warnings,
        "pts_wrapped": info.pts_wrapped,
        "truncated": info.truncated,
        "source_damaged": bool(info.warnings),
        "source_unusable": source_unusable,
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
# Building an inference session is not cheap: OpenVINO parses the ONNX graph and compiles
# it for the target device, which takes seconds on the iGPU.
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
        max_recovery_gap_s=float(settings.get_nowait("telemetry.max_interpolation_gap_s")),
        hwaccel="auto" if await settings.hardware_acceleration() else "cpu",
    )

    tz = await settings.timezone()

    # Establish one wall-clock origin for the recording before timestamping any sample.
    # The overlay candidates are reconciled back to video offset zero by the extractor;
    # the filename is the independent sanity check. Every row then uses origin + video
    # offset, while the exact OCR clock and its delta are retained in quality_json.
    filename_time = expected_time_from_filename(recording.filename)
    osd_origin_ok = False
    if result.osd_start_time is not None:
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
            recording.started_at = result.osd_start_time.replace(tzinfo=tz).astimezone(UTC)
            recording.time_from_osd = True
            osd_origin_ok = True
    if not osd_origin_ok and filename_time is not None:
        recording.started_at = filename_time.replace(tzinfo=tz).astimezone(UTC)
        recording.time_from_osd = False
    if recording.started_at is not None and recording.duration_s:
        recording.ended_at = recording.started_at + timedelta(seconds=recording.duration_s)

    rows = []
    recovered_clocks = 0
    for sample in result.samples:
        overlay_captured = sample.overlay_captured_at or sample.captured_at
        if overlay_captured is not None and overlay_captured.tzinfo is None:
            overlay_captured = overlay_captured.replace(tzinfo=tz).astimezone(UTC)
        expected = (
            recording.started_at + timedelta(seconds=sample.t_offset_s)
            if recording.started_at
            else None
        )
        clock_delta = (
            (overlay_captured - expected).total_seconds()
            if overlay_captured is not None and expected is not None
            else None
        )
        overlay_clock_valid = clock_is_plausible(overlay_captured, expected) and (
            clock_delta is None or abs(clock_delta) <= 2.5
        )
        captured = expected or overlay_captured
        time_source = (
            "overlay" if overlay_captured is not None and overlay_clock_valid else "timeline"
        )
        time_status = sample.time_status
        problems = list(sample.problems)
        if overlay_captured is not None and not overlay_clock_valid:
            time_status = "rejected"
            problems.append(
                f"overlay clock differed from canonical timeline by {clock_delta:.3f}s"
                if clock_delta is not None
                else "overlay clock could not be reconciled"
            )
        if time_source == "timeline":
            recovered_clocks += 1

        quality = {
            "source": "overlay_ocr",
            "ocr_status": sample.ocr_status,
            "time_status": time_status,
            "time_source": time_source,
            "overlay_captured_at": overlay_captured.isoformat() if overlay_captured else None,
            "expected_at": expected.isoformat() if expected else None,
            "time_delta_s": round(clock_delta, 3) if clock_delta is not None else None,
            "gps_status": sample.gps_status,
            "gps_source": sample.gps_source,
            "interpolated": sample.gps_source == "interpolated",
            "candidate_count": sample.candidate_count,
            "problems": list(dict.fromkeys(problems)),
        }
        rows.append(
            {
                "recording_id": recording.id,
                # Deliberately not stamped here -- see _JOURNEY_ID_IS_DERIVED below.
                "journey_id": None,
                "t_offset_s": sample.t_offset_s,
                "captured_at": captured,
                "lat": sample.lat,
                "lon": sample.lon,
                "has_fix": sample.has_fix,
                "speed_kmh": sample.speed_kmh,
                "heading_deg": sample.heading_deg,
                "ocr_confidence": sample.ocr_confidence,
                "raw_text": sample.raw_text,
                "quality_json": quality,
            }
        )

    # Last gate before the database. Everything upstream refuses a bad coordinate already,
    # but this is the only place that is true of *every* route into `telemetry_points`, and
    # a row carrying has_fix with a coordinate that is not a place is the shape of defect
    # that then propagates into tracks, observations, journey bounds and the heat map --
    # each of which had grown its own filter for it.
    dropped = 0
    for row in rows:
        if row["has_fix"] and not is_valid_coordinate(row["lat"], row["lon"]):
            row["has_fix"] = False
            row["lat"] = row["lon"] = row["heading_deg"] = None
            row["quality_json"]["gps_status"] = "rejected"
            row["quality_json"]["gps_source"] = "none"
            row["quality_json"]["interpolated"] = False
            row["quality_json"]["problems"].append("coordinate failed final database validation")
            dropped += 1
    if dropped:
        log.warning(
            "refused to store coordinates that are not a place",
            recording=recording.filename,
            dropped=dropped,
            of=len(rows),
        )

    # Delete then insert, as one retryable unit.
    #
    # ``DELETE FROM telemetry_points WHERE recording_id = ?`` is the first write of this
    # stage, so it is where the transaction opens and where the write lock is taken -- and
    # it is verbatim the statement that failed four times running on a real recording while
    # a journey rebuild held the lock for longer than the busy timeout. Everything it needs
    # is already in ``rows``, so re-running it produces exactly the same result.
    async def write() -> None:
        await session.execute(
            delete(TelemetryPoint).where(TelemetryPoint.recording_id == recording.id)
        )
        if rows:
            await session.execute(TelemetryPoint.__table__.insert(), rows)

    await write_with_retry(session, write, what=f"store telemetry for {recording.filename}")

    recording.telemetry_point_count = len(rows)
    recording.gps_point_count = result.fix_count
    recording.has_gps = result.has_gps
    recording.distance_m = result.distance_m or None
    recording.avg_speed_kmh = result.avg_speed_kmh
    recording.max_speed_kmh = result.max_speed_kmh
    if result.first_fix:
        recording.start_lat = result.first_fix.lat
        recording.start_lon = result.first_fix.lon

    gaps, longest_gap, problem_count, no_fix_count, ocr_gap_count, rejected_count = quality_rollup(
        rows
    )
    recording.gps_gap_count = gaps
    recording.gps_longest_gap_s = longest_gap
    recording.telemetry_problem_count = problem_count
    recording.gps_no_fix_count = no_fix_count
    recording.gps_ocr_gap_count = ocr_gap_count
    recording.gps_rejected_count = rejected_count
    recording.gps_recovered_count = 0

    if bool(settings.get_nowait("events.detect_harsh_braking")):
        speeds = [
            (float(row["t_offset_s"]), float(row["speed_kmh"]))
            for row in rows
            if row["speed_kmh"] is not None
        ]
        threshold = float(settings.get_nowait("events.harsh_braking_kmh_s"))
        harshest = max(
            (
                (before_speed - after_speed) / max(0.1, after_t - before_t)
                for (before_t, before_speed), (after_t, after_speed) in itertools.pairwise(speeds)
                if after_t > before_t
            ),
            default=0.0,
        )
        if harshest >= threshold and not recording.event_type:
            recording.event_type = "harsh_braking"

    recording.telemetry_state = StageState.DONE
    paired_recoveries = await recover_from_paired_camera(session, recording)
    log.info(
        "read telemetry from the overlay",
        recording=recording.filename,
        recording_id=recording.id,
        points=len(rows),
        fixes=result.fix_count,
        # Both counts, because "few fixes" and "many rejected fixes" call for opposite
        # investigations -- the overlay region and the templates against the weather.
        rejected_positions=result.implausible_jumps + dropped,
        parse_failures=result.parse_failures,
        timeline_recoveries=recovered_clocks,
        first_fix_at=result.first_fix.captured_at.isoformat()
        if result.first_fix and result.first_fix.captured_at
        else None,
        clock_source="overlay" if recording.time_from_osd else "filename",
        started_at=recording.started_at.isoformat() if recording.started_at else None,
    )
    return StageResult(
        "telemetry",
        True,
        stats={
            "points": len(rows),
            "fixes": result.fix_count,
            "distance_m": result.distance_m,
            "parse_failures": result.parse_failures,
            "timeline_recoveries": recovered_clocks,
            "interpolated_fixes": sum(s.gps_source == "interpolated" for s in result.samples),
            "rejected_positions": result.implausible_jumps + dropped,
            "gps_gaps": recording.gps_gap_count,
            "longest_gps_gap_s": recording.gps_longest_gap_s,
            "paired_recoveries": paired_recoveries,
            "warnings": result.warnings,
            "decoder": result.decoder,
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


async def _fix_track(session: AsyncSession, recording_id: int) -> FixTrack:
    """Every usable fix of one recording, indexed for lookup by frame offset.

    One query per recording, not one per track. The previous shape --
    ``ORDER BY abs(t_offset_s - ?) LIMIT 1``, run once per tracked vehicle -- was both 410
    round trips on a busy clip and unable to express the only rules that matter: that a
    fix too far from the detection in time is not a location for it, and that the point
    between two fixes is a better answer than either of them.

    Scoped to this recording's own id, so telemetry can never migrate between recordings
    or between the front and rear stream of the same moment. They are separate files with
    separate overlays, sampled at their own rates, and each detection is placed by the
    clip it was detected in.
    """
    settings = get_settings_service()
    rows = (
        await session.execute(
            select(
                TelemetryPoint.t_offset_s,
                TelemetryPoint.lat,
                TelemetryPoint.lon,
                TelemetryPoint.captured_at,
                TelemetryPoint.speed_kmh,
            )
            .where(
                TelemetryPoint.recording_id == recording_id,
                TelemetryPoint.has_fix.is_(True),
                TelemetryPoint.lat.is_not(None),
                TelemetryPoint.lon.is_not(None),
            )
            .order_by(TelemetryPoint.t_offset_s.asc())
        )
    ).all()
    return FixTrack.from_rows(
        rows,
        max_fix_age_s=float(settings.get_nowait("telemetry.max_fix_age_s")),
        max_bracket_s=float(settings.get_nowait("telemetry.max_interpolation_gap_s")),
    )


def _moment(recording: Recording, offset_s: float, located: Located | None) -> datetime | None:
    """Wall-clock time of a frame, preferring the overlay clock over the filename.

    Both are available and they answer slightly different questions. The overlay clock is
    the camera's own reading at that second and is what the telemetry carries; the
    recording's start time plus the offset works even where the overlay was unreadable or
    the GPS was not locked, which is exactly when a sighting would otherwise have no time
    at all. Before this a track in a recording with no fixes got ``first_seen_at = None``
    and vanished from every date filter in the UI.
    """
    if located is not None and located.captured_at is not None:
        return located.captured_at
    if recording.started_at is None:
        return None
    return recording.started_at + timedelta(seconds=offset_s)


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
    decoder_used: str | None = None

    def note_decoder(decoder: str) -> None:
        nonlocal decoder_used
        if decoder_used != "software":
            decoder_used = decoder

    try:
        async for offset, frame in iter_frames(
            path,
            fps=sample_fps,
            hwaccel="auto" if await settings.hardware_acceleration() else "cpu",
            codec=recording.video_codec,
            on_decoder=note_decoder,
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
    # 410 tracks on a busy clip that is 410 encodes, and doing them after the delete meant
    # holding SQLite's write lock for all of it. See the longer note in stage_plates: the
    # same mistake there was costing whole recordings.
    fixes = await _fix_track(session, recording.id)
    unlocated = 0
    prepared = []
    for track in tracks:
        # Located at the moment it was first seen, which is the offset the UI deep-links
        # to and the offset `first_seen_at` describes. Nothing here reuses one track's
        # answer for another: each is placed independently at its own time.
        located = fixes.at(track.first_offset_s)
        if located is None:
            unlocated += 1
        crop_path = None
        if track.best_crop is not None and track.best_crop.size:
            crop_path = _save_jpeg(
                track.best_crop,
                _media_path("vehicles", f"{recording.id:08d}", f"{track.track_key:04d}.jpg"),
                quality,
            )
        prepared.append((track, located, crop_path))
        log.debug(
            "located a tracked object",
            # Never persisted: one row per tracked vehicle would make log_entries the
            # largest table in the schema. It is here for a live trace at debug level.
            db_log=False,
            recording=recording.filename,
            camera=camera.key if camera else None,
            track_key=track.track_key,
            class_label=track.class_label,
            confidence=round(track.confidence_max, 3),
            first_offset_s=round(track.first_offset_s, 3),
            **(located.as_log() if located else {"gps_source": "none"}),
        )

    # Delete then insert, as one retryable unit. ``DELETE FROM tracked_objects WHERE
    # recording_id = ?`` opens this stage's transaction and is the other statement that
    # failed its way through all four attempts on a real recording. Everything it needs is
    # in ``prepared``, so a retry writes exactly the same rows.
    async def write() -> None:
        await session.execute(
            delete(TrackedObject).where(TrackedObject.recording_id == recording.id)
        )

        for track, located, crop_path in prepared:
            stored = TrackedObject(
                recording_id=recording.id,
                # Deliberately not stamped here -- see _JOURNEY_ID_IS_DERIVED below.
                track_key=track.track_key,
                class_label=track.class_label,
                confidence_max=track.confidence_max,
                confidence_avg=track.confidence_avg,
                first_seen_offset_s=track.first_offset_s,
                last_seen_offset_s=track.last_offset_s,
                duration_s=track.duration_s,
                frame_count=track.frame_count,
                first_seen_at=_moment(recording, track.first_offset_s, located),
                # Its own offset, not a copy of the first. Both were stamped with the same
                # telemetry timestamp, so every track in the library claimed to have been
                # last seen at the instant it was first seen and every duration read as
                # zero in the timeline.
                last_seen_at=_moment(recording, track.last_offset_s, fixes.at(track.last_offset_s)),
                lat=located.lat if located else None,
                lon=located.lon if located else None,
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

    await write_with_retry(session, write, what=f"store detections for {recording.filename}")

    recording.vehicle_count = len(tracks)
    recording.detection_state = StageState.DONE
    if unlocated:
        log.info(
            "some sightings have no position",
            recording=recording.filename,
            unlocated=unlocated,
            of=len(tracks),
            fixes=len(fixes),
            reason=(
                "the recording has no GPS fix at all"
                if len(fixes) == 0
                else "no fix close enough in time to the detection"
            ),
        )
    return StageResult(
        "detection",
        True,
        stats={
            "frames_analysed": frames,
            "tracks": len(tracks),
            # Surfaced rather than inferred from a null: "no location" and "we never
            # looked" are different answers and the recording page shows them differently.
            "gps_fixes": len(fixes),
            "tracks_without_position": unlocated,
            "device": detector.device,
            "decoder": decoder_used,
        },
    )


# --------------------------------------------------------------------------------------
# Stage 4 - licence plates
# --------------------------------------------------------------------------------------


#: Readings to try both ways up before committing to an orientation for the recording.
_ORIENTATION_SAMPLE = 8

#: How decisively the mirrored side must win before the recording is treated as mirrored.
#:
#: Measured over 195 stored observations: the front camera votes 73 as-is against 5
#: mirrored, and the rear votes 4 as-is against 34 mirrored -- 14.6:1 and 8.5:1. A 3:1
#: threshold is nowhere near either, which is what makes it safe to decide automatically.
_ORIENTATION_RATIO = 3.0

#: Confidence a reading needs before its orientation vote counts at all.
_ORIENTATION_MIN_CONFIDENCE = 0.80


class _PlateOrientation:
    """Decides, per recording, whether plate crops need flipping before they are read.

    Some dashcams mirror the rear channel, so it reads like a rear-view mirror. Nothing in
    this pipeline knew that, so every plate the rear camera saw was recognised backwards:
    of 74 stored rear observations, 64% matched no Australian format, against 21% on the
    front. The tell is arithmetic rather than impressionistic -- 65% of unmatched
    seven-character reads end in "2", which is a mirrored leading "S", against a 3% base
    rate -- and the badge misread as "ATOYOT" is "TOYOTA" spelled backwards.

    Rather than a setting or an assumption about which camera is which, the orientation is
    *measured*: the first few readings are recognised both ways up, and each way scores a
    point when it produces a named Australian series at high confidence. Whichever side
    wins decisively is used for the rest of the recording. If neither wins, nothing is
    flipped -- so on footage that is not mirrored this costs a handful of extra OCR calls
    and changes no result.
    """

    __slots__ = ("_as_is", "_flipped", "_sampled", "mirrored")

    def __init__(self) -> None:
        self.mirrored: bool | None = None
        self._as_is = 0
        self._flipped = 0
        self._sampled = 0

    @staticmethod
    def _is_named_hit(text: str, confidence: float, region: str) -> bool:
        """A reading that looks like a real registration, not merely like characters."""
        if not text or confidence < _ORIENTATION_MIN_CONFIDENCE:
            return False
        result = normalise(text, region=region)
        return result.matched and result.state_hint is not None

    async def read(self, ocr, crop: np.ndarray, *, region: str) -> tuple[str, float]:
        """Recognise *crop*, establishing the recording's orientation while it does."""
        if self.mirrored is not None:
            return await ocr.read(_flip(crop) if self.mirrored else crop)

        as_is = await ocr.read(crop)
        flipped = await ocr.read(_flip(crop))
        self._as_is += self._is_named_hit(*as_is, region)
        self._flipped += self._is_named_hit(*flipped, region)
        self._sampled += 1

        if self._sampled >= _ORIENTATION_SAMPLE:
            # Decide, and stop paying for two reads. Ties and silence both mean "as-is",
            # which is the behaviour that predates this and the safe default.
            self.mirrored = self._flipped >= max(1.0, self._as_is * _ORIENTATION_RATIO)

        # Until the vote is in, take whichever reading actually looks like a plate.
        if self._is_named_hit(*flipped, region) and not self._is_named_hit(*as_is, region):
            return flipped
        return flipped if flipped[1] > as_is[1] and self._flipped > self._as_is else as_is

    def resolve(self) -> bool:
        """Settle the question for good, on however many samples there were.

        Needed because the crops are saved after the reading loop, and a recording with
        fewer than ``_ORIENTATION_SAMPLE`` readable plates never reached the decision point
        inside ``read`` -- so ``mirrored`` was still ``None`` and the previews were saved
        the way the camera produced them. On a short rear clip with two or three plates in
        it, that is every preview in the recording.

        The same evidence and the same ratio; only the sample count is relaxed, and the
        default when nothing decisive was seen is still "not mirrored".
        """
        if self.mirrored is None:
            self.mirrored = self._flipped >= max(1.0, self._as_is * _ORIENTATION_RATIO)
        return self.mirrored

    def describe(self) -> dict[str, object]:
        return {
            "mirrored": self.mirrored,
            "votes_as_is": self._as_is,
            "votes_mirrored": self._flipped,
            "orientation_samples": self._sampled,
        }


def _flip(crop: np.ndarray) -> np.ndarray:
    """Horizontally mirrored, contiguous because the recogniser wants a real array."""
    return np.ascontiguousarray(crop[:, ::-1])


def _readable(crop: np.ndarray | None, mirrored: bool) -> np.ndarray | None:
    """A crop the right way round for a human to look at.

    The orientation decision was applied to what the recogniser saw and to nothing else,
    so on a mirrored rear channel the OCR read ``S192DKX`` from a flipped copy while the
    preview beside it on the plate page was still the mirror image -- the plate backwards,
    with the text under it reading correctly, which looks like the *text* is wrong.

    Applied to the saved image only, and deliberately not to the frame. Detection,
    tracking and every stored bounding box stay in the source video's coordinate space,
    which is the space the player draws in; flipping the frame would put every box on the
    wrong side of it. A crop is a standalone picture with no coordinates in it, so turning
    it round costs nothing and changes nothing else.
    """
    if crop is None or not mirrored:
        return crop
    return _flip(crop)


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
    orientation = _PlateOrientation()
    store_unmatched = bool(settings.get_nowait("plates.store_unmatched"))
    rejected = 0
    decoders_used: set[str] = set()
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
                on_decoder=decoders_used.add,
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
            text, confidence = await orientation.read(ocr, reading.crop, region=region_setting)
            reading.raw_text = text
            reading.ocr_confidence = confidence

        vote = vote_track_plate(readings)
        if vote is None or vote.ocr_confidence < min_store:
            continue

        result = normalise(vote.text, region=region_setting)
        if not result.matched and not store_unmatched:
            # Confidence alone was the only gate, and confidence measures how sure the
            # recogniser is that it read the characters correctly -- not whether what it
            # read is a registration. It was completely right about "KEECE" on a door
            # decal (0.998), "ARROW" on a road sign (0.975) and "TOYOTA" on a tailgate
            # (0.929), and each became a plate card. Roughly half the plate database was
            # signage. The catalogue already knows the answer; nothing was asking it.
            rejected += 1
            continue

        hits.append(
            _PlateHit(
                track=track,
                result=result,
                vote=vote,
                vehicle_crop=vehicle_crop if save_crops else None,
            )
        )

    # Settle the orientation before any crop is written. Until this call a recording with
    # fewer readable plates than the sample size never reached a verdict at all.
    mirrored = orientation.resolve()

    # Positions are resolved here, at the offset each plate was actually read from, rather
    # than copied off the track. The track's coordinate belongs to its *first* frame,
    # which on a vehicle followed for twenty seconds is up to twenty seconds and several
    # hundred metres from the frame the plate was legible in -- and the observation then
    # carried that as the place the plate was seen.
    fixes = await _fix_track(session, recording.id)
    placed = [(hit, fixes.at(float(hit.track.best_frame_offset_s or 0.0))) for hit in hits]

    # Everything below writes, and only what is below. One observation per plate per
    # tracked vehicle, so reprocessing replaces rather than accumulates.
    #
    # The plates that are about to lose an observation are collected first. Rollups were
    # refreshed only for plates still present after the rewrite, so a plate that this
    # recording no longer sees kept a count that included the observation just deleted --
    # and reprocessing a recording whose reads improved left the old plate claiming
    # sightings that no longer exist anywhere.
    touched_plate_ids = set(
        (
            await session.execute(
                select(PlateObservation.plate_id.distinct()).where(
                    PlateObservation.recording_id == recording.id
                )
            )
        ).scalars()
    )

    async def write() -> None:
        await session.execute(
            delete(PlateObservation).where(PlateObservation.recording_id == recording.id)
        )
        await _write_observations(session, recording, placed, touched_plate_ids, camera, mirrored)
        await session.flush()
        await _refresh_plate_rollups(session, touched_plate_ids)

    await write_with_retry(session, write, what=f"store plates for {recording.filename}")

    stored = len(hits)
    recording.plate_count = stored
    recording.plate_state = StageState.DONE
    return StageResult(
        "plates",
        True,
        stats={
            "observations": stored,
            "tracks": len(tracks),
            # All surfaced deliberately: "0 plates" is otherwise indistinguishable from
            # "read plenty and refused them all", the orientation vote is the kind of
            # automatic decision that must be visible when it goes wrong, and a sighting
            # with no location is a result rather than a gap.
            "rejected_unmatched": rejected,
            "without_position": sum(1 for _, located in placed if located is None),
            "decoder": (
                "software"
                if "software" in decoders_used
                else "vaapi"
                if "vaapi" in decoders_used
                else None
            ),
            "device": describe_runtime().get("device"),
            **orientation.describe(),
        },
    )


async def _write_observations(
    session: AsyncSession,
    recording: Recording,
    placed: list,
    touched_plate_ids: set[int],
    camera: Camera | None,
    mirrored: bool,
) -> None:
    """Turn confirmed readings into rows. Split out so the write phase is one callable.

    Everything it needs was decided before the transaction opened, so re-running it after
    a lock collision produces the same rows -- which is what makes the retry safe.
    """
    settings = get_settings_service()
    save_crops = bool(settings.get_nowait("plates.save_crops"))
    quality = int(settings.get_nowait("general.thumbnail_quality"))

    for hit, located in placed:
        track, vote, result = hit.track, hit.vote, hit.result
        plate = await _upsert_plate(session, result, vote.ocr_confidence)
        touched_plate_ids.add(plate.id)

        plate_crop_path = vehicle_crop_path = None
        if save_crops:
            preview = _readable(vote.best.crop, mirrored)
            plate_crop_path = (
                _save_jpeg(
                    preview,
                    _media_path(
                        "plates", f"{plate.id:08d}", f"{recording.id:08d}_{track.track_key:04d}.jpg"
                    ),
                    quality,
                )
                if preview is not None
                else None
            )
            vehicle_preview = _readable(hit.vehicle_crop, mirrored)
            if vehicle_preview is not None:
                vehicle_crop_path = _save_jpeg(
                    vehicle_preview,
                    _media_path(
                        "plates",
                        f"{plate.id:08d}",
                        f"{recording.id:08d}_{track.track_key:04d}_vehicle.jpg",
                    ),
                    quality,
                )

        offset = float(track.best_frame_offset_s or 0.0)
        session.add(
            PlateObservation(
                plate_id=plate.id,
                recording_id=recording.id,
                # Deliberately not stamped here -- see _JOURNEY_ID_IS_DERIVED below.
                camera_id=recording.camera_id,
                tracked_object_id=track.id,
                t_offset_s=offset,
                # The moment the plate was read, not the moment the vehicle first appeared.
                captured_at=_moment(recording, offset, located),
                first_seen_offset_s=track.first_seen_offset_s,
                last_seen_offset_s=track.last_seen_offset_s,
                raw_text=vote.best.raw_text,
                normalised_text=result.normalised,
                ocr_confidence=vote.ocr_confidence,
                detection_confidence=vote.detection_confidence,
                vote_count=vote.vote_count,
                lat=located.lat if located else None,
                lon=located.lon if located else None,
                plate_crop_path=plate_crop_path,
                vehicle_crop_path=vehicle_crop_path,
                # `mirrored` records that the saved crop was turned round relative to the
                # frame, so the box and the picture can never be read as being in the same
                # coordinate space by mistake.
                bbox={"box": list(vote.best.bbox), "mirrored": mirrored},
            )
        )

        log.info(
            "plate sighting recorded",
            recording=recording.filename,
            recording_id=recording.id,
            camera=camera.key if camera else None,
            plate=result.normalised,
            raw_text=vote.best.raw_text,
            ocr_confidence=round(vote.ocr_confidence, 3),
            detection_confidence=round(vote.detection_confidence, 3),
            votes=vote.vote_count,
            track_key=track.track_key,
            frame_offset_s=round(offset, 3),
            mirrored_preview=mirrored,
            **(located.as_log() if located else {"gps_source": "none"}),
        )


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


async def _refresh_plate_rollups(session: AsyncSession, plate_ids: set[int]) -> None:
    """Recompute the stored counts for every plate this run touched.

    The caller passes the plates whose observations were *removed* as well as those
    written, which is the whole point: a plate that this recording no longer sees is
    exactly the one whose count is now wrong, and it is the one a "refresh what is here
    now" query cannot find.
    """
    orphaned: list[int] = []
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
        count = int(row[0] or 0)
        if count == 0 and not plate.flagged and not plate.notes:
            # No observation anywhere refers to this plate any more, so it is a reading
            # that a reprocess corrected. Kept only if a user has invested something in it
            # -- a flag or a note -- because deleting that is not ours to do.
            orphaned.append(plate_id)
            continue
        plate.observation_count = count
        plate.first_seen_at = row[1]
        plate.last_seen_at = row[2]
        plate.journey_count = int(row[3] or 0)
        plate.best_confidence = float(row[4] or 0.0)

    if orphaned:
        await session.execute(delete(Plate).where(Plate.id.in_(orphaned)))
        log.info("removed plates left with no observations", count=len(orphaned))


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
