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
    ("events", "Events", "Protection and automatic driving-event rules"),
    ("storage", "Storage", "Retention limits and cleanup behaviour"),
    ("ingest", "Backup / Ingest", "Pulling footage off the dashcam head unit"),
    ("maps", "Maps", "Map tiles and rendering"),
    ("security", "Access", "Whether this deployment asks for a password"),
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
        "scanner.damaged_footage_action",
        "Damaged footage",
        "select",
        "hide",
        "scanner",
        "What to do after a stable file is positively classified as permanently unusable "
        "(empty, no video stream, or no decodable frame). Recoverable container warnings "
        "do not trigger this policy. Hide keeps the source and analysis history but removes "
        "it from normal library views. Delete removes only the source footage, retains "
        "derived history, and is blocked unless "
        "the footage mount passes all deletion safety checks and is writable. Existing "
        "damaged files are reconciled on the next scan.",
        choices=(
            ("keep", "Keep and show"),
            ("hide", "Hide / blacklist (recommended)"),
            ("delete", "Permanently delete source footage"),
        ),
        dangerous=True,
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
    # --------------------------------------------------------------- events
    SettingDef(
        "events.detect_harsh_braking",
        "Detect harsh braking",
        "bool",
        False,
        "events",
        "Mark clips where decoded speed drops unusually quickly. Marked events are kept "
        "when the retention setting to keep events is enabled.",
    ),
    SettingDef(
        "events.harsh_braking_kmh_s",
        "Harsh braking threshold",
        "float",
        10.0,
        "events",
        "Speed reduction per second that marks a harsh-braking event.",
        minimum=3.0,
        maximum=30.0,
        unit="km/h/s",
        requires="events.detect_harsh_braking",
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
        "Stored telemetry points per second. The reader checks multiple candidate frames "
        "inside each interval and keeps the best independently parsed fields.",
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
        "Values below this confidence are retained for diagnostics but are not trusted as "
        "direct telemetry.",
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
    SettingDef(
        "telemetry.max_fix_age_s",
        "Maximum GPS age for a sighting",
        "float",
        3.0,
        "telemetry",
        "How far in time a GPS reading may sit from a detection and still be used as its "
        "location. The overlay updates every second, so this only ever bites when the "
        "camera had lost its lock — and then the last known position is not where the "
        "vehicle was. Sightings beyond this are recorded with no location rather than a "
        "wrong one.",
        minimum=0.5,
        maximum=120.0,
        unit="seconds",
    ),
    SettingDef(
        "telemetry.max_interpolation_gap_s",
        "Maximum gap to interpolate across",
        "float",
        15.0,
        "telemetry",
        "When overlay OCR misses positions between two trustworthy GPS readings closer "
        "together than this, route samples and detections are interpolated between them. "
        "Explicit camera no-fix readings are never filled, and wider gaps stay unlocated.",
        minimum=0.0,
        maximum=300.0,
        unit="seconds",
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
        "plates.store_unmatched",
        "Keep readings that match no plate format",
        "bool",
        False,
        "plates",
        "Off by default. Confidence says how sure the recogniser is that it read the "
        "characters correctly, not that what it read is a registration -- so a door "
        "decal, a tailgate badge and a road sign all became plates at high confidence. "
        "Turn this on to keep them anyway, for example if your plates are personalised "
        "and fit none of the known Australian formats.",
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
    # --------------------------------------------------------------- security
    SettingDef(
        "security.require_login",
        "Require sign-in",
        "bool",
        False,
        "security",
        "Ask for a username and password before anything is shown. Off by default, which "
        "is right for the trusted LAN this was built for and wrong the moment the app has "
        "a public hostname. Set the account below first — this cannot be switched on "
        "without one, because a deployment that demands a password nobody holds is one "
        "nobody can open.",
    ),
    SettingDef(
        "security.remember_days",
        "Stay signed in for",
        "int",
        30,
        "security",
        "How long a browser that ticked “Stay signed in” keeps its session. Sessions "
        "without it last twelve hours and end when the browser closes. Changing this "
        "affects sessions created afterwards; sign the existing ones out below to apply "
        "it now.",
        minimum=1,
        maximum=365,
        unit="days",
        requires="security.require_login",
    ),
    SettingDef(
        "security.api_key",
        "API key",
        "string",
        "",
        "security",
        "A standing key that stands in for the username and password, for callers that "
        "cannot be asked to type them. The dashcam's own screen is the reason it exists: "
        "when a transfer starts the car is sent to this app's Backup page, and there is "
        "nobody in the driver's seat to sign in — the key rides along in that address and "
        "is swapped for a cookie the moment it arrives, so it does not stay in the car's "
        "browser history. Scripts can send it as an X-API-Key header instead. "
        "Blank switches it off, and blanking it is how you revoke one. "
        "Treat it as the password: it reaches everything the account reaches, so anyone "
        "holding it can read your footage and change these settings. At least 24 "
        "characters; use the Generate button rather than inventing one.",
        requires="security.require_login",
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
        "Threads available to each processing job for FFmpeg, OpenCV and CPU inference. "
        "0 automatically shares CPU capacity between workers, capped at four threads per job.",
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
    # ---------------------------------------------------------------- ingest
    SettingDef(
        "ingest.enabled",
        "Pull footage from the head unit",
        "bool",
        False,
        "ingest",
        "Watch for the dashcam's head unit on the network and copy new recordings into the "
        "footage directory automatically. The footage directory must be mounted "
        "read-write for this.",
    ),
    SettingDef(
        "ingest.unit_adb_address",
        "Head unit address",
        "string",
        "192.168.1.122:5555",
        "ingest",
        "host:port of the head unit's ADB service. Used only as a control channel — to ask "
        "what is on the card and to start the transfer. The recordings themselves travel "
        "over their own socket, which is roughly three times faster.",
        requires="ingest.enabled",
    ),
    SettingDef(
        "ingest.data_port",
        "Transfer port",
        "int",
        9000,
        "ingest",
        "Port the head unit listens on for the bulk transfer. The app connects out to it; "
        "nothing needs to be opened on this side.",
        minimum=1024,
        maximum=65535,
        requires="ingest.enabled",
    ),
    SettingDef(
        "ingest.source_path_override",
        "Card path override",
        "string",
        "",
        "ingest",
        "Leave blank to find the card automatically. The TF card's volume id changes every "
        "time it is reformatted, so /storage/Tfcard/DCIM/Video is tried first because the "
        "platform recreates that symlink at every mount.",
        requires="ingest.enabled",
    ),
    SettingDef(
        "ingest.poll_interval_s",
        "Presence check interval",
        "int",
        2,
        "ingest",
        "How often to look for the head unit. The unit has no battery, so it is only on the "
        "network while the engine is running — often only a minute or two, and seconds spent "
        "not noticing it are footage left on the card. A check costs one connection attempt "
        "that fails immediately when the car is out, so checking often is cheap.",
        minimum=1,
        maximum=300,
        unit="seconds",
        requires="ingest.enabled",
    ),
    SettingDef(
        "ingest.listen_timeout_s",
        "Transfer timeout",
        "int",
        180,
        "ingest",
        "How long the head unit keeps serving before giving up, so a window that closes "
        "mid-transfer cannot leave a listener holding the port.",
        minimum=30,
        maximum=3600,
        unit="seconds",
        requires="ingest.enabled",
    ),
    SettingDef(
        "ingest.skip_active_seconds",
        "Ignore recordings newer than",
        "int",
        15,
        "ingest",
        "Both cameras write continuously while the car runs. The newest segment of each is "
        "open in the recorder, and copying it would produce a truncated file that looks "
        "complete.",
        minimum=5,
        maximum=600,
        unit="seconds",
        requires="ingest.enabled",
    ),
    SettingDef(
        "ingest.camera_filter",
        "Cameras to copy",
        "select",
        "both",
        "ingest",
        "Copying only the road-facing lens halves the data if the interior camera is not wanted.",
        choices=(("both", "Both cameras"), ("camera_0", "Front only"), ("camera_1", "Rear only")),
        requires="ingest.enabled",
    ),
    SettingDef(
        "ingest.transfer_order",
        "Copy order",
        "select",
        "oldest_first",
        "ingest",
        "Oldest first keeps the library contiguous and is right while the camera is nearly "
        "caught up. Switch to newest first if the backlog is permanently larger than one "
        "driveway window — otherwise every window goes on the oldest recordings and today's "
        "drive is never reached.",
        choices=(("oldest_first", "Oldest first"), ("newest_first", "Newest first")),
        requires="ingest.enabled",
    ),
    SettingDef(
        "ingest.show_on_unit",
        "Show the backup page on the dashcam screen",
        "bool",
        False,
        "ingest",
        "Opens this app's Backup page on the head unit's own screen when a transfer starts, "
        "so the car shows what is being copied. Nothing is installed for it. The address is "
        "worked out on its own — whatever you open this dashboard on is what the car is sent "
        "to — so there is normally nothing to configure. It only fires when there is "
        "something to copy, and nothing puts the previous screen back afterwards. Worth "
        "knowing that a transfer can begin while you are still manoeuvring, so this can take "
        "the screen over mid-park.",
        requires="ingest.enabled",
    ),
    SettingDef(
        "ingest.unit_display_url",
        "Dashcam screen address",
        "string",
        "",
        "ingest",
        "Leave blank. The address is normally learned from however you open this dashboard, "
        "which is by definition one that works on this network. Fill it in only if the car "
        "needs a different one — behind a reverse proxy whose hostname the head unit cannot "
        "resolve, for instance. Example: http://192.168.1.16:8199/backup.",
        requires="ingest.show_on_unit",
    ),
    SettingDef(
        "ingest.learned_origin",
        "Address the car will be sent to",
        "string",
        "",
        "ingest",
        "Worked out on its own from however this dashboard was last opened, and kept so it "
        "survives a restart — the car usually arrives long before anybody next opens the "
        "app, and before this was stored those transfers ran with nothing on the screen. "
        "Shown here so you can see where the car is actually being pointed. Overridden by "
        "the address above if you set one.",
        read_only=True,
        requires="ingest.show_on_unit",
    ),
    SettingDef(
        "ingest.include_locked",
        "Also copy protected recordings",
        "bool",
        True,
        "ingest",
        "The camera moves a clip you protect into its own folder on the card, out of the "
        "ordinary listing — so without this, the one recording you deliberately marked as "
        "worth keeping is the one recording that never gets backed up. On by default. "
        "Turn it off only if you want protected clips left on the card.",
        requires="ingest.enabled",
    ),
    SettingDef(
        "ingest.min_uptime_s",
        "Only start a backup after the unit has been running for",
        "int",
        120,
        "ingest",
        "Waits until the head unit has been powered on for this long before an automatic "
        "backup begins, so footage is pulled when you arrive home rather than as you leave. "
        "The unit has no battery — it boots when the engine starts — so its running time is "
        "the length of the current drive: a car pulling back onto the driveway has been "
        "going for the whole trip, while one pulling off it has only just booted. A backup "
        "held for this reason shows on the Backup page and is re-checked every few seconds "
        "while the car is here, so a genuine arrival starts the moment the threshold is "
        "crossed. Set it above the longest your car idles on the driveway before setting "
        "off, and below your shortest trip; 0 turns the wait off. Manual backups ignore it.",
        minimum=0,
        maximum=3600,
        unit="s",
        requires="ingest.enabled",
    ),
    SettingDef(
        "ingest.quiet_radios",
        "Turn off Bluetooth and the hotspot while copying",
        "bool",
        False,
        "ingest",
        "The head unit's WiFi is a single-stream chip shared with its Bluetooth and its "
        "own hotspot, and the transfer already runs at that radio's measured ceiling — "
        "anything else using it is paid for in footage left on the card. This turns them "
        "off while recordings are moving and back on when the run ends. It waits until "
        "the unit has been on the network for ten seconds, so a car that is only turning "
        "around keeps its phone connection. If the engine stops mid-transfer, a watchdog "
        "left on the unit turns Bluetooth back on by itself, and anything still off is "
        "restored the moment the unit is next seen. Bluetooth is turned off first on "
        "purpose: some units re-arm their hotspot within seconds while Bluetooth is on. "
        "If your unit still refuses to stop its hotspot, the refusal is shown below in "
        "the unit's own words and nothing else changes.",
        requires="ingest.enabled",
    ),
    SettingDef(
        "ingest.unit_health_watch",
        "Watch the recorder while the car is away",
        "bool",
        True,
        "ingest",
        "Leaves a tiny watcher (a one-kilobyte shell script, not an app) running on the "
        "head unit that checks every twenty seconds that recordings are still being "
        "written, the card is still writable and not nearly full — the silent failures "
        "that otherwise show up as missing or glitchy footage days later. It keeps "
        "watching after the car drives off, and whatever it saw is collected the next "
        "time the car appears: problems go to the log, the summary below, and the "
        "webhook if one is set. Nothing is installed — deleting two files from the "
        "unit's temp directory removes every trace.",
        requires="ingest.enabled",
    ),
    SettingDef(
        "ingest.unit_health",
        "What the recorder watcher last saw",
        "string",
        "",
        "ingest",
        "The verdict from the most recent collection: how much running time was watched, "
        "across how many trips, and anything that went wrong — the recorder stalling, the "
        "card flipping read-only, the recording folder disappearing, space running out. "
        "'The recorder looked healthy throughout' is the answer this should always give.",
        read_only=True,
        requires="ingest.unit_health_watch",
    ),
    SettingDef(
        "ingest.radios_pending_restore",
        "Radios awaiting restore",
        "string",
        "",
        "ingest",
        "Set while something the transfer turned off has not yet been confirmed back on, "
        "so a silent phone is never a mystery. Cleared the moment a restore succeeds — "
        "normally at the end of the same transfer, otherwise the next time the unit "
        "appears on the network.",
        read_only=True,
        requires="ingest.quiet_radios",
    ),
    SettingDef(
        "ingest.adb_root",
        "Let the app restart the dashcam's ADB as root",
        "bool",
        False,
        "ingest",
        "Android only lets root stop a hotspot, so without this the hotspot keeps "
        "sharing the transfer's radio however the setting above is set. This runs "
        "'adb root' once per transfer, before any copying starts, which works only if "
        "the unit runs a debuggable build — most do not, and on those it is asked once, "
        "answered no, and never asked again. Two things worth knowing before turning it "
        "on: the unit's ADB stays root until the engine stops (it reverts on its own at "
        "the next start, and nothing here puts it back sooner), and restarting ADB "
        "briefly drops the control channel — if it does not come back, that window is "
        "lost and the next engine start fixes it. Whether your unit allows it at all is "
        "reported below.",
        dangerous=True,
        requires="ingest.quiet_radios",
    ),
    SettingDef(
        "ingest.adb_root_state",
        "Whether this unit allows ADB root",
        "string",
        "",
        "ingest",
        "What the head unit answered the last time it was asked to restart its ADB as "
        "root. A production build refuses permanently, and the app stops asking; that "
        "is a property of the unit's firmware and nothing here can change it.",
        read_only=True,
        requires="ingest.adb_root",
    ),
    SettingDef(
        "ingest.hotspot_refusal",
        "Why the hotspot could not be stopped",
        "string",
        "",
        "ingest",
        "The head unit's own words from the last time it refused to stop its hotspot "
        "for a transfer. Android only lets root stop a soft AP, so on most units this "
        "reads as a SecurityException for uid 2000 — permanent until the unit's ADB "
        "runs as root, and nothing this app can work around. Turn the hotspot off on "
        "the unit itself if the throughput matters. Cleared automatically if a stop "
        "ever succeeds.",
        read_only=True,
        requires="ingest.quiet_radios",
    ),
    SettingDef(
        "ingest.wifi_band",
        "WiFi band for transfers",
        "select",
        "any",
        "ingest",
        "The head unit's single-stream WiFi moves ~32 MB/s on 5 GHz and ~5 MB/s on "
        "2.4 GHz — and once it joins 2.4 it stays there, because Android does not roam "
        "off a link that still works. 'Prefer' notes the slow band in the log and copies "
        "anyway; 'Require' holds the transfer while the unit is on 2.4 GHz and re-checks "
        "every half minute for as long as the car is here, telling you on the Backup page "
        "whether 5 GHz is even in range from where you park. Nothing here can move the "
        "unit between bands: the only way to do that without root is to switch its WiFi "
        "off and on, and on a unit with no battery an engine stopping at the wrong "
        "instant would leave it with WiFi disabled and unreachable for good. Give the "
        "router a 5 GHz-only SSID and point the car at it — that fixes it properly.",
        choices=(
            ("any", "Copy on any band"),
            ("prefer_5ghz", "Prefer 5 GHz (warn, but copy)"),
            ("require_5ghz", "Require 5 GHz (hold until it connects on 5 GHz)"),
        ),
        requires="ingest.enabled",
    ),
    SettingDef(
        "ingest.delete_after_verify",
        "Delete from the card after copying",
        "bool",
        False,
        "ingest",
        "Off by default. A file is only ever removed from the card once a byte-complete "
        "copy of it is in the footage directory — including recordings copied by an "
        "earlier run, so switching this on reclaims the space taken by everything already "
        "backed up rather than only by what is copied from now on.",
        dangerous=True,
        requires="ingest.enabled",
    ),
    SettingDef(
        "ingest.ha_webhook_url",
        "Home Assistant webhook",
        "string",
        "",
        "ingest",
        "Posted to when a transfer starts, finishes or fails — this is what reaches a "
        "phone. Blank disables it. Example: "
        "http://homeassistant:8123/api/webhook/dashcam_backup",
        requires="ingest.enabled",
    ),
    SettingDef(
        "ingest.ha_mqtt_enabled",
        "Publish to MQTT",
        "bool",
        False,
        "ingest",
        "Publishes Home Assistant discovery topics so the entities appear on their own. "
        "Optional: the status endpoint already works as a REST sensor with no broker.",
        requires="ingest.enabled",
    ),
    SettingDef(
        "ingest.ha_mqtt_host",
        "MQTT host",
        "string",
        "",
        "ingest",
        "Broker hostname or address.",
        requires="ingest.ha_mqtt_enabled",
    ),
    SettingDef(
        "ingest.ha_mqtt_port",
        "MQTT port",
        "int",
        1883,
        "ingest",
        "Broker port.",
        minimum=1,
        maximum=65535,
        requires="ingest.ha_mqtt_enabled",
    ),
    SettingDef(
        "ingest.ha_mqtt_user",
        "MQTT username",
        "string",
        "",
        "ingest",
        "Leave blank for an anonymous broker.",
        requires="ingest.ha_mqtt_enabled",
    ),
    SettingDef(
        "ingest.ha_mqtt_pass",
        "MQTT password",
        "string",
        "",
        "ingest",
        "Leave blank for an anonymous broker.",
        requires="ingest.ha_mqtt_enabled",
    ),
    SettingDef(
        "ingest.ha_mqtt_base_topic",
        "MQTT base topic",
        "string",
        "dashcam/backup",
        "ingest",
        "State topics are published beneath this prefix.",
        requires="ingest.ha_mqtt_enabled",
    ),
)


SETTINGS_BY_KEY: dict[str, SettingDef] = {s.key: s for s in SETTINGS}

DEFAULTS: dict[str, Any] = {s.key: s.default for s in SETTINGS}


def category_of(key: str) -> str | None:
    d = SETTINGS_BY_KEY.get(key)
    return d.category if d else None
