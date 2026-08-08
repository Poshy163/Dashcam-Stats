"""Declarative definition of every UI-editable setting.

This module is the single source of truth: it drives defaults, validation, the
``/api/settings`` payload, and the rendering of the Settings page. Adding a setting
here is all that is required to make it appear in the UI.

Deployment configuration (paths, port) lives in environment variables and is
deliberately *not* here -- see ``app.config``. Everything else is editable at runtime
and takes effect without a restart.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

SettingType = Literal["bool", "int", "float", "string", "select", "path", "bytes"]


@dataclass(frozen=True)
class SettingDef:
    key: str
    label: str
    type: SettingType
    default: Any
    category: str
    description: str = ""
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[tuple[str, str], ...] = ()
    unit: str | None = None
    # Settings that can destroy data are flagged so the UI can confirm them.
    dangerous: bool = False
    # Shown but not editable (derived/detected values).
    read_only: bool = False
    requires: str | None = None  # key of a bool setting that gates this one


CATEGORIES: tuple[tuple[str, str, str], ...] = (
    ("general", "General", "Timezone, footage location and thumbnails"),
    ("scanner", "Scanner", "How often new footage is discovered"),
    ("processing", "Processing", "Workers, hardware acceleration and detection models"),
    ("telemetry", "Telemetry", "Reading the burned-in GPS/speed overlay"),
    ("plates", "Licence Plates", "Plate detection, OCR and deduplication"),
    ("journeys", "Journeys", "How recordings are grouped into drives"),
    ("storage", "Storage", "Retention limits and cleanup behaviour"),
    ("maps", "Maps", "Map tiles and rendering"),
    ("advanced", "Advanced", "Logging, database and diagnostics"),
)


SETTINGS: tuple[SettingDef, ...] = (
    # ---------------------------------------------------------------- general
    SettingDef(
        "general.timezone",
        "Timezone",
        "string",
        "Australia/Adelaide",
        "general",
        "IANA timezone used to interpret filename timestamps and display times. The "
        "dashcam's on-screen clock is local time with no zone information, so this must "
        "match the camera's configured zone.",
    ),
    SettingDef(
        "general.footage_dir",
        "Dashcam directory",
        "path",
        "/dashcam",
        "general",
        "Where raw footage is mounted. Must be inside the container's allowed roots.",
    ),
    SettingDef(
        "general.media_extensions",
        "Footage file extensions",
        "string",
        ".ts,.mp4,.mov,.avi,.mkv",
        "general",
        "Comma-separated list of extensions treated as footage.",
    ),
    SettingDef(
        "general.thumbnail_width",
        "Thumbnail width",
        "int",
        480,
        "general",
        "Width of generated recording thumbnails.",
        minimum=160,
        maximum=1920,
        unit="px",
    ),
    SettingDef(
        "general.thumbnail_quality",
        "Thumbnail quality",
        "int",
        80,
        "general",
        "JPEG quality for thumbnails and crops.",
        minimum=40,
        maximum=100,
    ),
    SettingDef(
        "general.stream_cache_gb",
        "Playback cache size",
        "float",
        5.0,
        "general",
        "Browsers cannot play the camera's MPEG-TS files, so clips are remuxed to MP4 the "
        "first time they are viewed and cached here. The copies are disposable and live "
        "in /data; the least recently watched are evicted once this limit is reached.",
        minimum=0.5,
        maximum=500.0,
        unit="GB",
    ),
    # ---------------------------------------------------------------- scanner
    SettingDef(
        "scanner.enabled",
        "Scan automatically",
        "bool",
        True,
        "scanner",
        "Periodically look for new footage.",
    ),
    SettingDef(
        "scanner.interval_minutes",
        "Scan interval",
        "int",
        60,
        "scanner",
        "Minutes between automatic scans.",
        minimum=1,
        maximum=10080,
        unit="minutes",
        requires="scanner.enabled",
    ),
    SettingDef(
        "scanner.auto_process",
        "Process new recordings automatically",
        "bool",
        True,
        "scanner",
        "Queue newly discovered recordings for analysis as soon as they are found.",
    ),
    SettingDef(
        "scanner.settle_seconds",
        "File settle time",
        "int",
        90,
        "scanner",
        "A file must be unchanged for this long before it is processed. The dashcam writes "
        "directly to the share, so a segment may still be growing when it is first seen.",
        minimum=0,
        maximum=3600,
        unit="seconds",
    ),
    SettingDef(
        "scanner.follow_symlinks",
        "Follow symlinks",
        "bool",
        False,
        "scanner",
        "Leave off unless you deliberately use symlinked footage directories.",
    ),
    SettingDef(
        "scanner.fingerprint_bytes",
        "Fingerprint sample size",
        "int",
        1048576,
        "scanner",
        "Bytes read from the head, middle and tail of a file to fingerprint it. Full-file "
        "hashing of a multi-terabyte library every scan is never done.",
        minimum=65536,
        maximum=16777216,
        unit="bytes",
    ),
    # ------------------------------------------------------------- processing
    SettingDef(
        "processing.max_workers",
        "Maximum concurrent processing jobs",
        "int",
        2,
        "processing",
        "How many recordings are analysed at once.",
        minimum=1,
        maximum=16,
    ),
    SettingDef(
        "processing.hardware_acceleration",
        "Use hardware acceleration",
        "bool",
        True,
        "processing",
        "Use the iGPU via VAAPI for decoding and OpenVINO for inference where available. "
        "Falls back to CPU automatically when unavailable.",
    ),
    SettingDef(
        "processing.decoder_preference",
        "Decoder",
        "select",
        "auto",
        "processing",
        "Which decode path to prefer.",
        choices=(
            ("auto", "Auto-detect"),
            ("vaapi", "VAAPI (iGPU)"),
            ("qsv", "Intel QuickSync"),
            ("cpu", "CPU (software)"),
        ),
    ),
    SettingDef(
        "processing.inference_device",
        "AI inference device",
        "select",
        "auto",
        "processing",
        "OpenVINO device for detection and OCR.",
        choices=(("auto", "Auto-detect"), ("GPU", "iGPU"), ("CPU", "CPU"), ("NPU", "NPU")),
    ),
    SettingDef(
        "processing.detection_enabled",
        "Detect objects",
        "bool",
        True,
        "processing",
        "Detect vehicles, pedestrians and cyclists.",
    ),
    SettingDef(
        "processing.detection_model",
        "Detection model",
        "select",
        "rfdetr-nano",
        "processing",
        "Model used for road-object detection.",
        choices=(
            ("rfdetr-nano", "RF-DETR nano (fast)"),
            ("rfdetr-small", "RF-DETR small (balanced)"),
            ("rfdetr-medium", "RF-DETR medium (accurate)"),
        ),
        requires="processing.detection_enabled",
    ),
    SettingDef(
        "processing.detection_confidence",
        "Detection confidence threshold",
        "float",
        0.35,
        "processing",
        "Detections below this score are discarded.",
        minimum=0.05,
        maximum=0.95,
        requires="processing.detection_enabled",
    ),
    SettingDef(
        "processing.frame_sample_fps",
        "Detection sampling rate",
        "float",
        4.0,
        "processing",
        "Frames per second analysed for object detection. Tracking fills the gaps, so this "
        "does not need to match the source frame rate.",
        minimum=0.5,
        maximum=30.0,
        unit="fps",
        requires="processing.detection_enabled",
    ),
    SettingDef(
        "processing.detection_classes",
        "Detected classes",
        "string",
        "car,truck,bus,motorcycle,bicycle,person",
        "processing",
        "Comma-separated COCO class names to keep.",
        requires="processing.detection_enabled",
    ),
    SettingDef(
        "processing.track_min_frames",
        "Minimum frames per track",
        "int",
        2,
        "processing",
        "Tracks seen in fewer frames than this are discarded as noise.",
        minimum=1,
        maximum=30,
    ),
    SettingDef(
        "processing.process_rear_camera",
        "Analyse rear camera",
        "bool",
        True,
        "processing",
        "Run object and plate detection on rear-facing recordings as well as front.",
    ),
    SettingDef(
        "processing.retry_max_attempts",
        "Retry attempts",
        "int",
        3,
        "processing",
        "How many times a transiently failing recording is retried before giving up.",
        minimum=0,
        maximum=10,
    ),
    # -------------------------------------------------------------- telemetry
    SettingDef(
        "telemetry.enabled",
        "Extract telemetry",
        "bool",
        True,
        "telemetry",
        "Read the burned-in date, GPS and speed overlay. This footage carries no telemetry "
        "metadata, so this OCR pass is the only source of GPS.",
    ),
    SettingDef(
        "telemetry.sample_fps",
        "Telemetry sampling rate",
        "float",
        1.0,
        "telemetry",
        "The overlay updates once per second, so sampling faster gains nothing.",
        minimum=0.1,
        maximum=5.0,
        unit="fps",
        requires="telemetry.enabled",
    ),
    SettingDef(
        "telemetry.min_confidence",
        "Minimum OCR confidence",
        "float",
        0.6,
        "telemetry",
        "Telemetry points read below this confidence are discarded.",
        minimum=0.0,
        maximum=1.0,
        requires="telemetry.enabled",
    ),
    SettingDef(
        "telemetry.engine",
        "Telemetry OCR engine",
        "select",
        "glyph",
        "telemetry",
        "Glyph template matching is tuned to this dashcam's fixed overlay font and is both "
        "faster and more accurate than general OCR. Use the general engine only if your "
        "camera's font differs.",
        choices=(
            ("glyph", "Glyph templates (recommended)"),
            ("paddle", "General OCR"),
            ("both", "Glyph, fall back to general OCR"),
        ),
        requires="telemetry.enabled",
    ),
    SettingDef(
        "telemetry.auto_calibrate",
        "Auto-calibrate overlay region",
        "bool",
        True,
        "telemetry",
        "Locate the overlay automatically on first use rather than assuming a fixed position.",
        requires="telemetry.enabled",
    ),
    SettingDef(
        "telemetry.max_speed_kmh",
        "Implausible speed cutoff",
        "float",
        300.0,
        "telemetry",
        "Readings above this are treated as OCR errors and dropped.",
        minimum=50.0,
        maximum=999.0,
        unit="km/h",
    ),
    SettingDef(
        "telemetry.min_move_metres",
        "Movement threshold",
        "float",
        12.0,
        "telemetry",
        "GPS is quantised to about 11 m, so movement below this is treated as jitter and "
        "excluded from distance totals.",
        minimum=0.0,
        maximum=100.0,
        unit="m",
    ),
    # ----------------------------------------------------------------- plates
    SettingDef(
        "plates.enabled",
        "Detect licence plates",
        "bool",
        True,
        "plates",
        "Find and read licence plates on detected vehicles.",
    ),
    SettingDef(
        "plates.detection_confidence",
        "Plate detection threshold",
        "float",
        0.4,
        "plates",
        "Minimum score for a plate bounding box.",
        minimum=0.05,
        maximum=0.95,
        requires="plates.enabled",
    ),
    SettingDef(
        "plates.ocr_confidence",
        "OCR confidence threshold",
        "float",
        0.5,
        "plates",
        "Readings below this are stored but marked uncertain rather than discarded.",
        minimum=0.0,
        maximum=1.0,
        requires="plates.enabled",
    ),
    SettingDef(
        "plates.min_store_confidence",
        "Minimum confidence to store",
        "float",
        0.3,
        "plates",
        "Readings below this are not stored at all.",
        minimum=0.0,
        maximum=1.0,
        requires="plates.enabled",
    ),
    SettingDef(
        "plates.region",
        "Plate region",
        "select",
        "AU",
        "plates",
        "Normalisation rules applied to OCR output. Raw OCR is always kept regardless.",
        choices=(
            ("AU", "Australia (all states)"),
            ("AU-SA", "South Australia only"),
            ("none", "No regional normalisation"),
        ),
        requires="plates.enabled",
    ),
    SettingDef(
        "plates.max_ocr_per_track",
        "Maximum OCR reads per vehicle",
        "int",
        5,
        "plates",
        "Best few crops of a tracked vehicle are read and voted on. OCR is never run on "
        "every frame.",
        minimum=1,
        maximum=30,
        requires="plates.enabled",
    ),
    SettingDef(
        "plates.save_crops",
        "Save plate crops",
        "bool",
        True,
        "plates",
        "Store a tight plate crop and a wider vehicle crop for each observation.",
        requires="plates.enabled",
    ),
    SettingDef(
        "plates.save_frame_thumb",
        "Save full-frame thumbnail",
        "bool",
        False,
        "plates",
        "Also store a full frame per observation. Uses noticeably more disk.",
        requires="plates.enabled",
    ),
    SettingDef(
        "plates.min_plate_width",
        "Minimum plate width",
        "int",
        48,
        "plates",
        "Plates narrower than this in pixels are too small to read reliably.",
        minimum=16,
        maximum=400,
        unit="px",
        requires="plates.enabled",
    ),
    SettingDef(
        "plates.dedupe_window_s",
        "Deduplication window",
        "float",
        120.0,
        "plates",
        "Repeat readings of the same plate within this window collapse into one observation.",
        minimum=0.0,
        maximum=3600.0,
        unit="seconds",
        requires="plates.enabled",
    ),
    # --------------------------------------------------------------- journeys
    SettingDef(
        "journeys.enabled",
        "Group recordings into journeys",
        "bool",
        True,
        "journeys",
        "Cluster consecutive recordings into drives.",
    ),
    SettingDef(
        "journeys.gap_minutes",
        "Journey gap threshold",
        "float",
        5.0,
        "journeys",
        "A gap longer than this between recordings starts a new journey. Five minutes "
        "matches the observed pattern in this footage.",
        minimum=0.5,
        maximum=180.0,
        unit="minutes",
        requires="journeys.enabled",
    ),
    SettingDef(
        "journeys.min_recordings",
        "Minimum recordings per journey",
        "int",
        1,
        "journeys",
        "Journeys with fewer recordings than this are not created.",
        minimum=1,
        maximum=50,
        requires="journeys.enabled",
    ),
    SettingDef(
        "journeys.use_gps_continuity",
        "Use GPS continuity",
        "bool",
        True,
        "journeys",
        "Also split a journey when consecutive recordings are implausibly far apart.",
        requires="journeys.enabled",
    ),
    SettingDef(
        "journeys.max_jump_km",
        "Maximum positional jump",
        "float",
        5.0,
        "journeys",
        "A larger gap between the end of one recording and the start of the next splits "
        "the journey.",
        minimum=0.1,
        maximum=500.0,
        unit="km",
        requires="journeys.use_gps_continuity",
    ),
    # ---------------------------------------------------------------- storage
    SettingDef(
        "storage.max_footage_gb",
        "Maximum dashcam footage",
        "int",
        150,
        "storage",
        "Total size allowed in the footage directory before cleanup applies.",
        minimum=1,
        maximum=1048576,
        unit="GB",
    ),
    SettingDef(
        "storage.delete_oldest",
        "Delete oldest footage when limit exceeded",
        "bool",
        True,
        "storage",
        "Whether the retention plan proposes deleting the oldest eligible footage. Nothing "
        "is deleted unless deletion is separately enabled below.",
    ),
    SettingDef(
        "storage.enable_deletion",
        "Actually delete files",
        "bool",
        False,
        "storage",
        "Off by default: retention reports what it would remove without touching anything. "
        "Turning this on still requires the footage directory to be mounted writable.",
        dangerous=True,
    ),
    SettingDef(
        "storage.min_age_days",
        "Minimum footage age before deletion",
        "int",
        0,
        "storage",
        "Recordings younger than this are never eligible.",
        minimum=0,
        maximum=3650,
        unit="days",
    ),
    SettingDef(
        "storage.keep_with_detections",
        "Keep recordings containing detections",
        "bool",
        False,
        "storage",
        "Exempt any recording with a detected vehicle or plate.",
    ),
    SettingDef(
        "storage.keep_events",
        "Keep event/emergency recordings",
        "bool",
        True,
        "storage",
        "Exempt recordings flagged as events. This dashcam does not mark events itself, so "
        "this applies only to recordings flagged by the harsh-braking heuristic or by hand.",
    ),
    SettingDef(
        "storage.cleanup_enabled",
        "Run cleanup automatically",
        "bool",
        True,
        "storage",
        "Evaluate retention on a schedule. In report-only mode this just refreshes the plan.",
    ),
    SettingDef(
        "storage.cleanup_interval_minutes",
        "Cleanup interval",
        "int",
        360,
        "storage",
        "Minutes between automatic retention evaluations.",
        minimum=5,
        maximum=10080,
        unit="minutes",
        requires="storage.cleanup_enabled",
    ),
    SettingDef(
        "storage.min_expected_files",
        "Safety: minimum expected files",
        "int",
        5,
        "storage",
        "If the footage directory contains fewer files than this, retention refuses to run. "
        "This is what stops an unmounted or empty share from being read as permission to "
        "delete everything.",
        minimum=1,
        maximum=100000,
    ),
    SettingDef(
        "storage.require_mountpoint",
        "Safety: require a real mount",
        "bool",
        True,
        "storage",
        "Refuse retention unless the footage directory is a mount point distinct from its "
        "parent filesystem.",
    ),
    SettingDef(
        "storage.max_delete_fraction",
        "Safety: maximum deleted per run",
        "float",
        0.25,
        "storage",
        "Refuse any plan that would remove more than this fraction of indexed footage in a "
        "single run.",
        minimum=0.01,
        maximum=1.0,
    ),
    # ------------------------------------------------------------------- maps
    SettingDef(
        "maps.tile_url",
        "Map tile URL",
        "string",
        "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
        "maps",
        "Leaflet tile template. Defaults to OpenStreetMap, which needs no API key.",
    ),
    SettingDef(
        "maps.attribution",
        "Map attribution",
        "string",
        "&copy; OpenStreetMap contributors",
        "maps",
        "Attribution text shown on the map. Keep this accurate for your tile provider.",
    ),
    SettingDef(
        "maps.max_zoom",
        "Maximum zoom",
        "int",
        19,
        "maps",
        "",
        minimum=1,
        maximum=22,
    ),
    SettingDef(
        "maps.route_simplify_m",
        "Route simplification",
        "float",
        5.0,
        "maps",
        "Douglas-Peucker tolerance when sending routes to the browser. Keeps long journeys "
        "responsive without visibly changing the line.",
        minimum=0.0,
        maximum=100.0,
        unit="m",
    ),
    # --------------------------------------------------------------- advanced
    SettingDef(
        "advanced.log_level",
        "Log level",
        "select",
        "INFO",
        "advanced",
        "",
        choices=(("DEBUG", "Debug"), ("INFO", "Info"), ("WARNING", "Warning"), ("ERROR", "Error")),
    ),
    SettingDef(
        "advanced.log_retention_days",
        "Log retention",
        "int",
        30,
        "advanced",
        "Application log entries older than this are pruned.",
        minimum=1,
        maximum=365,
        unit="days",
    ),
    SettingDef(
        "advanced.keep_sparse_detections",
        "Store per-frame detections",
        "bool",
        True,
        "advanced",
        "Keep sampled per-frame detection rows to drive the recording timeline. Tracks are "
        "always kept regardless.",
    ),
    SettingDef(
        "advanced.detection_store_stride",
        "Detection storage stride",
        "int",
        4,
        "advanced",
        "Store one in N sampled detections per track. Higher values save space.",
        minimum=1,
        maximum=100,
        requires="advanced.keep_sparse_detections",
    ),
    SettingDef(
        "advanced.ffmpeg_threads",
        "FFmpeg threads",
        "int",
        0,
        "advanced",
        "0 lets FFmpeg decide.",
        minimum=0,
        maximum=64,
    ),
    SettingDef(
        "advanced.job_heartbeat_timeout_s",
        "Job heartbeat timeout",
        "int",
        300,
        "advanced",
        "A running job whose worker stops reporting for this long is reclaimed.",
        minimum=30,
        maximum=7200,
        unit="seconds",
    ),
)


SETTINGS_BY_KEY: dict[str, SettingDef] = {s.key: s for s in SETTINGS}

DEFAULTS: dict[str, Any] = {s.key: s.default for s in SETTINGS}


def category_of(key: str) -> str | None:
    d = SETTINGS_BY_KEY.get(key)
    return d.category if d else None
