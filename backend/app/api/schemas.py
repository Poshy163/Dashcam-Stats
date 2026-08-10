"""API response and request models.

Two rules run through all of these:

* Absolute filesystem paths never cross the API boundary. Clients get ids and
  media-relative paths, which are resolved server-side through the path guards.
* A plate is never returned without its confidence and its raw OCR text. The UI is
  required to present uncertainty rather than assert correctness, and it can only do that
  if the data carries it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.api.deps import SQLITE_MAX_INT

T = TypeVar("T")


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Paginated(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int
    page_size: int
    pages: int


class CameraOut(ApiModel):
    id: int
    key: str
    name: str
    role: str


class RecordingOut(ApiModel):
    id: int
    filename: str
    rel_path: str
    camera: CameraOut | None = None
    journey_id: int | None = None

    started_at: datetime | None = None
    ended_at: datetime | None = None
    duration_s: float | None = None
    time_from_osd: bool = False

    size_bytes: int = 0
    video_codec: str | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    has_audio: bool = False

    state: str
    metadata_state: str
    telemetry_state: str
    detection_state: str
    plate_state: str
    error_message: str | None = None

    has_gps: bool = False
    gps_point_count: int = 0
    distance_m: float | None = None
    avg_speed_kmh: float | None = None
    max_speed_kmh: float | None = None

    vehicle_count: int = 0
    plate_count: int = 0
    thumbnail_path: str | None = None
    file_missing: bool = False

    #: Carried only so the two fields below can be derived from it; the raw probe output
    #: is internal detail and never reaches a client.
    probe_json: dict[str, Any] | None = Field(default=None, exclude=True)

    #: Problems found in the source file itself -- a wrapped PTS, an unreadable frame, a
    #: frame rate the container lied about. Surfaced so a damaged recording reads as a bad
    #: file rather than as the application misbehaving.
    warnings: list[str] = Field(default_factory=list)
    source_damaged: bool = False

    @model_validator(mode="after")
    def _lift_probe_warnings(self) -> RecordingOut:
        if isinstance(self.probe_json, dict):
            self.warnings = [str(w) for w in self.probe_json.get("warnings") or []]
            self.source_damaged = bool(self.probe_json.get("source_damaged")) or bool(self.warnings)
        return self


class TelemetryPointOut(ApiModel):
    t_offset_s: float
    captured_at: datetime | None = None
    lat: float | None = None
    lon: float | None = None
    #: When false the camera had no satellite lock. lat/lon are null and must not be
    #: plotted — the overlay prints a zero placeholder, which is not a position.
    has_fix: bool = False
    speed_kmh: float | None = None
    #: Derived from consecutive fixes, not reported by the camera.
    heading_deg: float | None = None
    ocr_confidence: float | None = None


class JourneyOut(ApiModel):
    id: int
    title: str | None = None
    started_at: datetime
    ended_at: datetime
    duration_s: float
    distance_m: float | None = None
    avg_speed_kmh: float | None = None
    max_speed_kmh: float | None = None
    has_gps: bool = False
    recording_count: int = 0
    vehicle_count: int = 0
    unique_plate_count: int = 0
    start_lat: float | None = None
    start_lon: float | None = None
    end_lat: float | None = None
    end_lon: float | None = None
    manual: bool = False


class JourneyDetailOut(JourneyOut):
    recordings: list[RecordingOut] = Field(default_factory=list)
    #: Drawable segments, each ``[lat, lon, recording_id, t_offset_s]``.
    #:
    #: Segments rather than one line, because a journey is not one unbroken road: signal is
    #: lost under cover, readings that fail validation are dropped, and clips can have
    #: minutes between them. Joining across those draws a road that was never taken.
    #:
    #: Each point carries where it came from. Without that the map could only guess, and it
    #: guessed badly — the click handler treated a point's position in the array as elapsed
    #: seconds into the journey's first recording, so clicking the far end of a drive
    #: opened the wrong clip at a time that had nothing to do with the place clicked.
    route: list[list[tuple[float, float, int, float]]] = Field(default_factory=list)


class TrackedObjectOut(ApiModel):
    id: int
    recording_id: int
    class_label: str
    confidence_max: float
    first_seen_offset_s: float
    last_seen_offset_s: float
    duration_s: float
    frame_count: int
    lat: float | None = None
    lon: float | None = None
    crop_path: str | None = None


class PlateOut(ApiModel):
    id: int
    display_text: str
    normalised_text: str
    #: Null means the reading matched no known Australian format. The UI flags that
    #: rather than presenting a guess as a confirmed plate.
    pattern_name: str | None = None
    region: str | None = None
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    observation_count: int = 0
    journey_count: int = 0
    best_confidence: float = 0.0
    flagged: bool = False
    notes: str | None = None
    representative_crop_path: str | None = None
    representative_vehicle_path: str | None = None


class PlateObservationOut(ApiModel):
    id: int
    plate_id: int
    recording_id: int
    recording_filename: str | None = None
    journey_id: int | None = None
    camera_name: str | None = None
    t_offset_s: float
    captured_at: datetime | None = None
    #: The untouched OCR output, always kept beside the normalised value.
    raw_text: str
    normalised_text: str
    ocr_confidence: float
    detection_confidence: float
    vote_count: int = 1
    lat: float | None = None
    lon: float | None = None
    plate_crop_path: str | None = None
    vehicle_crop_path: str | None = None


class VehicleOut(ApiModel):
    id: int
    class_label: str | None = None
    primary_plate: PlateOut | None = None
    #: Where and when this was seen, so the sighting leads back to the footage.
    #:
    #: The row always carried these; the schema simply dropped them, which left the
    #: Vehicles page showing ten thousand crops with no way to reach any of the recordings
    #: they came from.
    recording_id: int | None = None
    first_seen_offset_s: float | None = None
    lat: float | None = None
    lon: float | None = None
    #: Null unless a classifier actually produced them. Nothing is inferred.
    make: str | None = None
    model: str | None = None
    colour: str | None = None
    classifier: str | None = None
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    observation_count: int = 0
    representative_crop_path: str | None = None


class JobOut(ApiModel):
    id: int
    recording_id: int | None = None
    recording_filename: str | None = None
    kind: str
    stages: list[str] | None = None
    state: str
    progress: float = 0.0
    stage_current: str | None = None
    speed_realtime: float | None = None
    decoder: str | None = None
    inference_device: str | None = None
    attempts: int = 0
    max_attempts: int = 3
    queued_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error_message: str | None = None


class QueueStatsOut(BaseModel):
    queued: int = 0
    running: int = 0
    failed: int = 0
    completed: int = 0
    completed_today: int = 0
    paused: bool = False


class LogEntryOut(ApiModel):
    id: int
    ts: datetime
    level: str
    logger: str
    message: str
    context: dict[str, Any] | None = None
    recording_id: int | None = None
    job_id: int | None = None


class SettingOut(BaseModel):
    key: str
    label: str
    type: str
    value: Any = None
    default: Any = None
    description: str = ""
    minimum: float | None = None
    maximum: float | None = None
    choices: list[dict[str, str]] = Field(default_factory=list)
    unit: str | None = None
    dangerous: bool = False
    read_only: bool = False
    requires: str | None = None
    is_default: bool = True


class SettingCategoryOut(BaseModel):
    key: str
    label: str
    description: str = ""
    settings: list[SettingOut] = Field(default_factory=list)


class SettingsUpdate(BaseModel):
    values: dict[str, Any]


class SettingsReset(BaseModel):
    keys: list[str]


class ReprocessRequest(BaseModel):
    #: "metadata" | "telemetry" | "detection" | "plates" | "everything"
    stages: list[str] = Field(default_factory=lambda: ["everything"])


class ReprocessAllRequest(ReprocessRequest):
    #: Limit the rerun to recordings that previously failed, rather than the whole library.
    only_failed: bool = False


#: A row id inside a request body. Path and query bounds do not reach here, and
#: ``Journey.id.in_(journey_ids)`` binds these straight to SQLite. See deps.SQLITE_MAX_INT.
BodyRowId = Annotated[int, Field(ge=1, le=SQLITE_MAX_INT)]


class MergeRequest(BaseModel):
    #: Bounded in length as well: this is a UI multi-select, and the library holds ~130
    #: journeys, so a list longer than this is not something a person assembled.
    journey_ids: list[BodyRowId] = Field(max_length=1000)


class SplitRequest(BaseModel):
    at_recording_id: BodyRowId


class PlatePatch(BaseModel):
    flagged: bool | None = None
    notes: str | None = None


class SafetyCheckOut(BaseModel):
    name: str
    passed: bool
    reason: str | None = None


class SafetyReportOut(BaseModel):
    ok: bool
    checks: list[SafetyCheckOut] = Field(default_factory=list)
    blocked_reason: str | None = None
    footage_dir: str | None = None
    file_count: int = 0
    total_bytes: int = 0
    writable: bool = False


class RetentionCandidateOut(BaseModel):
    recording_id: int
    filename: str
    started_at: datetime | None = None
    size_bytes: int
    reason: str


class RetentionPlanOut(BaseModel):
    bytes_before: int
    bytes_limit: int
    bytes_to_free: int
    would_delete_count: int
    would_free_bytes: int
    over_limit: bool
    blocked: bool
    blocked_reason: str | None = None
    #: False in the recommended read-only deployment: the plan is a report, not an action.
    deletion_enabled: bool
    candidates: list[RetentionCandidateOut] = Field(default_factory=list)
    skipped: dict[str, int] = Field(default_factory=dict)
    safety: SafetyReportOut | None = None


class StatusTotals(BaseModel):
    recordings: int = 0
    journeys: int = 0
    telemetry_points: int = 0
    tracked_objects: int = 0
    plates: int = 0
    footage_bytes: int = 0
    footage_files: int = 0
    duration_s: float = 0.0


class StatusProcessing(BaseModel):
    completed: int = 0
    pending: int = 0
    processing: int = 0
    failed: int = 0
    #: Files that can never be processed -- empty, or carrying no video stream. Counted
    #: separately from `failed` and shown separately, because they used to be counted as
    #: failures and were then removed from the retry population: without a home of their
    #: own they would simply disappear from the dashboard, which is the one place an
    #: operator would look to find out why three files never finish.
    invalid: int = 0
    #: On disk but still being written, so not yet work that is waiting.
    settling: int = 0
    recordings_today: int = 0
    throughput_per_hour: float | None = None


class StatusStorage(BaseModel):
    limit_bytes: int = 0
    used_bytes: int = 0
    deletion_enabled: bool = False
    footage_writable: bool = False


class FeatureStatus(BaseModel):
    """Whether an optional analysis feature can actually run, and whether it has.

    Exists so the UI can tell two very different zeroes apart. "0 vehicles seen" across
    674 recordings reads as a finding — nothing was out there — when the truth was that
    the detection models 404'd on every attempt and the stage never ran at all. A count
    is only meaningful once you know the thing producing it was working.
    """

    key: str
    label: str
    #: The user has this feature switched on in settings.
    enabled: bool
    #: Everything it needs is present: models downloaded, libraries importable.
    ready: bool
    #: Why it is not ready, in words a user can act on. None when it is.
    blocked_reason: str | None = None
    #: Results actually recorded so far, so "ready but never run" is distinguishable
    #: from "ran and genuinely found nothing".
    results: int = 0


class StatusOut(BaseModel):
    totals: StatusTotals
    processing: StatusProcessing
    storage: StatusStorage
    latest_journey: JourneyOut | None = None
    hardware: dict[str, Any]
    features: list[FeatureStatus] = Field(default_factory=list)
    version: str


class SearchResults(BaseModel):
    plates: list[PlateOut] = Field(default_factory=list)
    recordings: list[RecordingOut] = Field(default_factory=list)
    journeys: list[JourneyOut] = Field(default_factory=list)


class HealthOut(BaseModel):
    status: str
    database: str
    scanner: str
    worker: str
    version: str
    detail: dict[str, Any] = Field(default_factory=dict)


class HeatmapOut(BaseModel):
    """Grid cells with a visit count, ready to be drawn as a heat layer.

    ``points`` is deliberately a bare ``[lat, lon, weight]`` triple rather than an object
    per cell. There can be twenty thousand of them, and the field names would be most of
    the payload.
    """

    points: list[list[float]] = Field(default_factory=list)
    #: Decimal places the grid was rounded to: 2 is about 1.1 km, 3 about 110 m, 4 about 11 m.
    precision: int
    cells: int
    #: Visits in the busiest cell. The client normalises its colour ramp against this.
    max_weight: int
    #: Total fixes represented, which is also seconds of driving at the 1 Hz sample rate.
    total_points: int
    average_speed_kmh: float | None = None
    #: True when the cell cap was hit, so the map is showing the densest cells only.
    truncated: bool = False


class RoutesOut(BaseModel):
    """Every drive as drawable lines, to trace the roads under the heat map.

    ``lines`` is a list of polylines, each a list of ``[lat, lon]`` pairs. Bare pairs
    rather than objects because there are tens of thousands of them and field names would
    be most of the payload.

    A drive is several lines, not one. The camera loses its lock in tunnels, rejected
    readings leave holes, and a journey stitches together clips minutes apart — joining
    across any of those draws a road that does not exist.
    """

    lines: list[list[list[float]]] = Field(default_factory=list)
    journeys: int = 0
    segments: int = 0
    points: int = 0
    #: Simplification tolerance actually applied, in metres.
    simplify_m: float = 0.0
    #: True when the point cap was reached, so the network shown is incomplete.
    truncated: bool = False
