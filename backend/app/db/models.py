"""SQLAlchemy models.

Design notes that matter:

* Media (thumbnails, crops) is never stored in the database -- only relative paths
  under ``/data/media``.
* ``telemetry_points`` is the highest-cardinality table (one row per second of
  footage). It is deliberately narrow and carries a covering index for the two
  access patterns that exist: "points for this recording in time order" and
  "points for this journey in time order".
* Detections are stored sparsely; the durable unit is ``tracked_objects`` (one row
  per physical vehicle seen), not the per-frame detection.
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


# --------------------------------------------------------------------------------------
# Enumerations
# --------------------------------------------------------------------------------------


class RecordingState(str, enum.Enum):
    DISCOVERED = "discovered"
    METADATA_EXTRACTED = "metadata_extracted"
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    IGNORED = "ignored"
    DELETED = "deleted"


class StageState(str, enum.Enum):
    """Per-stage progress, so a stage can be re-run without redoing the others."""

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


class JobState(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobKind(str, enum.Enum):
    PROCESS = "process"
    REPROCESS = "reprocess"
    SCAN = "scan"
    RETENTION = "retention"
    THUMBNAIL = "thumbnail"
    JOURNEY_REBUILD = "journey_rebuild"


class CameraRole(str, enum.Enum):
    FRONT = "front"
    REAR = "rear"
    CABIN = "cabin"
    OTHER = "other"


# --------------------------------------------------------------------------------------
# Core entities
# --------------------------------------------------------------------------------------


class Camera(Base):
    """A physical camera source, keyed by the token found in the filename.

    For this corpus the keys are ``camera_0`` (front) and ``camera_1`` (rear), but the
    mapping is data, not code, so other dashcams can be configured through the UI.
    """

    __tablename__ = "cameras"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    role: Mapped[CameraRole] = mapped_column(Enum(CameraRole), default=CameraRole.OTHER)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    recordings: Mapped[list[Recording]] = relationship(back_populates="camera")


class Journey(Base):
    """A contiguous drive, inferred from segment adjacency and GPS continuity."""

    __tablename__ = "journeys"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    duration_s: Mapped[float] = mapped_column(Float, default=0.0)

    # Derived from telemetry; null when the journey has no GPS fix at all.
    distance_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_speed_kmh: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_speed_kmh: Mapped[float | None] = mapped_column(Float, nullable=True)

    start_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    start_lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    end_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    end_lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    min_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    min_lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_lon: Mapped[float | None] = mapped_column(Float, nullable=True)

    has_gps: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    recording_count: Mapped[int] = mapped_column(Integer, default=0)
    vehicle_count: Mapped[int] = mapped_column(Integer, default=0)
    unique_plate_count: Mapped[int] = mapped_column(Integer, default=0)

    # Set when a user has manually split/merged, so the automatic builder leaves it alone.
    manual: Mapped[bool] = mapped_column(Boolean, default=False)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    recordings: Mapped[list[Recording]] = relationship(back_populates="journey")

    __table_args__ = (Index("ix_journeys_span", "started_at", "ended_at"),)


class Recording(Base):
    """One footage file on disk, plus everything we have learned about it."""

    __tablename__ = "recordings"

    id: Mapped[int] = mapped_column(primary_key=True)

    # Stored relative to the configured footage root so the root can be remounted
    # elsewhere without invalidating the index.
    rel_path: Mapped[str] = mapped_column(String(1024), unique=True, index=True)
    filename: Mapped[str] = mapped_column(String(512), index=True)

    camera_id: Mapped[int | None] = mapped_column(
        ForeignKey("cameras.id"), nullable=True, index=True
    )
    journey_id: Mapped[int | None] = mapped_column(
        ForeignKey("journeys.id", ondelete="SET NULL"), nullable=True, index=True
    )

    # --- change detection -------------------------------------------------------------
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    mtime_ns: Mapped[int] = mapped_column(Integer, default=0)
    # Cheap partial fingerprint (size + head/mid/tail digest). Full hashing the whole
    # library every scan is exactly what the design must avoid.
    fingerprint: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)

    # --- timing -----------------------------------------------------------------------
    # Parsed from the filename; the OSD clock supersedes it once telemetry is read.
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    # True when the start time came from decoded OSD rather than the filename.
    time_from_osd: Mapped[bool] = mapped_column(Boolean, default=False)

    # --- media properties -------------------------------------------------------------
    container: Mapped[str | None] = mapped_column(String(32), nullable=True)
    video_codec: Mapped[str | None] = mapped_column(String(32), nullable=True)
    video_profile: Mapped[str | None] = mapped_column(String(64), nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fps: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Kept separately because the container value is unreliable in this corpus.
    fps_container: Mapped[float | None] = mapped_column(Float, nullable=True)
    bitrate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pix_fmt: Mapped[str | None] = mapped_column(String(32), nullable=True)
    has_audio: Mapped[bool] = mapped_column(Boolean, default=False)
    audio_codec: Mapped[str | None] = mapped_column(String(32), nullable=True)
    audio_sample_rate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    audio_channels: Mapped[int | None] = mapped_column(Integer, nullable=True)
    probe_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # --- state ------------------------------------------------------------------------
    state: Mapped[RecordingState] = mapped_column(
        Enum(RecordingState), default=RecordingState.DISCOVERED, index=True
    )
    metadata_state: Mapped[StageState] = mapped_column(Enum(StageState), default=StageState.PENDING)
    telemetry_state: Mapped[StageState] = mapped_column(
        Enum(StageState), default=StageState.PENDING
    )
    detection_state: Mapped[StageState] = mapped_column(
        Enum(StageState), default=StageState.PENDING
    )
    plate_state: Mapped[StageState] = mapped_column(Enum(StageState), default=StageState.PENDING)

    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # --- rollups (denormalised so list views never aggregate) --------------------------
    has_gps: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    gps_point_count: Mapped[int] = mapped_column(Integer, default=0)
    telemetry_point_count: Mapped[int] = mapped_column(Integer, default=0)
    distance_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_speed_kmh: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_speed_kmh: Mapped[float | None] = mapped_column(Float, nullable=True)
    start_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    start_lon: Mapped[float | None] = mapped_column(Float, nullable=True)

    vehicle_count: Mapped[int] = mapped_column(Integer, default=0, index=True)
    plate_count: Mapped[int] = mapped_column(Integer, default=0, index=True)

    thumbnail_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    # --- bookkeeping ------------------------------------------------------------------
    ignored: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_scanned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Set when the file is gone from disk; the row is kept so history survives cleanup.
    file_missing: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    camera: Mapped[Camera | None] = relationship(back_populates="recordings")
    journey: Mapped[Journey | None] = relationship(back_populates="recordings")
    telemetry: Mapped[list[TelemetryPoint]] = relationship(
        back_populates="recording", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (
        Index("ix_recordings_state_started", "state", "started_at"),
        Index("ix_recordings_camera_started", "camera_id", "started_at"),
    )


class TelemetryPoint(Base):
    """One OSD sample. The OSD updates at 1 Hz, so this is ~1 row per second of footage."""

    __tablename__ = "telemetry_points"

    id: Mapped[int] = mapped_column(primary_key=True)
    recording_id: Mapped[int] = mapped_column(
        ForeignKey("recordings.id", ondelete="CASCADE"), index=True
    )
    journey_id: Mapped[int | None] = mapped_column(
        ForeignKey("journeys.id", ondelete="SET NULL"), nullable=True, index=True
    )

    # Offset within the recording, so the player can seek straight to it.
    t_offset_s: Mapped[float] = mapped_column(Float)
    # Wall-clock read from the OSD itself -- authoritative over the filename.
    captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Null when the dashcam had no fix. `E:00.0000 N:00.0000` is a no-fix marker and is
    # never stored as a real coordinate.
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    has_fix: Mapped[bool] = mapped_column(Boolean, default=False)
    speed_kmh: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Derived from consecutive fixes, not reported by the dashcam.
    heading_deg: Mapped[float | None] = mapped_column(Float, nullable=True)

    ocr_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    raw_text: Mapped[str | None] = mapped_column(String(255), nullable=True)

    recording: Mapped[Recording] = relationship(back_populates="telemetry")

    __table_args__ = (
        UniqueConstraint("recording_id", "t_offset_s", name="uq_telemetry_recording_offset"),
        Index("ix_telemetry_journey_time", "journey_id", "captured_at"),
        Index("ix_telemetry_recording_offset", "recording_id", "t_offset_s"),
        CheckConstraint("lat IS NULL OR (lat >= -90 AND lat <= 90)", name="ck_telemetry_lat"),
        CheckConstraint("lon IS NULL OR (lon >= -180 AND lon <= 180)", name="ck_telemetry_lon"),
    )


class Vehicle(Base):
    """A vehicle identity across sightings.

    Make/model/colour are nullable and stay null unless a model that actually performs
    that classification has populated them. Nothing infers them.
    """

    __tablename__ = "vehicles"

    id: Mapped[int] = mapped_column(primary_key=True)
    primary_plate_id: Mapped[int | None] = mapped_column(
        ForeignKey("plates.id", ondelete="SET NULL"), nullable=True, index=True
    )
    class_label: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)

    make: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    colour: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Which model produced make/model/colour, so unattributed guesses are impossible.
    classifier: Mapped[str | None] = mapped_column(String(64), nullable=True)

    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    observation_count: Mapped[int] = mapped_column(Integer, default=0)
    representative_crop_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class TrackedObject(Base):
    """One physical object followed across frames -- the durable unit of detection.

    Twenty seconds of the same car is one row here, not hundreds of detections.
    """

    __tablename__ = "tracked_objects"

    id: Mapped[int] = mapped_column(primary_key=True)
    recording_id: Mapped[int] = mapped_column(
        ForeignKey("recordings.id", ondelete="CASCADE"), index=True
    )
    journey_id: Mapped[int | None] = mapped_column(
        ForeignKey("journeys.id", ondelete="SET NULL"), nullable=True, index=True
    )
    vehicle_id: Mapped[int | None] = mapped_column(
        ForeignKey("vehicles.id", ondelete="SET NULL"), nullable=True, index=True
    )

    # Tracker-assigned id, unique only within its recording.
    track_key: Mapped[int] = mapped_column(Integer)
    class_label: Mapped[str] = mapped_column(String(32), index=True)
    confidence_max: Mapped[float] = mapped_column(Float, default=0.0)
    confidence_avg: Mapped[float] = mapped_column(Float, default=0.0)

    first_seen_offset_s: Mapped[float] = mapped_column(Float)
    last_seen_offset_s: Mapped[float] = mapped_column(Float)
    duration_s: Mapped[float] = mapped_column(Float, default=0.0)
    frame_count: Mapped[int] = mapped_column(Integer, default=0)

    first_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lon: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Frame chosen as most representative (largest, sharpest, highest confidence).
    best_frame_offset_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    best_bbox: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    crop_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    __table_args__ = (
        UniqueConstraint("recording_id", "track_key", name="uq_track_recording_key"),
        Index("ix_tracks_recording_time", "recording_id", "first_seen_offset_s"),
    )


class Detection(Base):
    """A sampled per-frame detection, retained sparsely to drive the timeline."""

    __tablename__ = "detections"

    id: Mapped[int] = mapped_column(primary_key=True)
    tracked_object_id: Mapped[int] = mapped_column(
        ForeignKey("tracked_objects.id", ondelete="CASCADE"), index=True
    )
    recording_id: Mapped[int] = mapped_column(
        ForeignKey("recordings.id", ondelete="CASCADE"), index=True
    )
    t_offset_s: Mapped[float] = mapped_column(Float)
    frame_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    class_label: Mapped[str] = mapped_column(String(32))
    confidence: Mapped[float] = mapped_column(Float)

    # Normalised 0..1 so bounding boxes survive a resolution change.
    x: Mapped[float] = mapped_column(Float)
    y: Mapped[float] = mapped_column(Float)
    w: Mapped[float] = mapped_column(Float)
    h: Mapped[float] = mapped_column(Float)

    __table_args__ = (Index("ix_detections_recording_time", "recording_id", "t_offset_s"),)


class Plate(Base):
    """A distinct licence plate identity."""

    __tablename__ = "plates"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Normalised form is the identity key; the raw OCR is always retained per observation.
    normalised_text: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    display_text: Mapped[str] = mapped_column(String(32))
    region: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Which AU pattern it matched, if any. Null means it did not match a known format.
    pattern_name: Mapped[str | None] = mapped_column(String(64), nullable=True)

    first_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    observation_count: Mapped[int] = mapped_column(Integer, default=0, index=True)
    journey_count: Mapped[int] = mapped_column(Integer, default=0)

    best_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    best_observation_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Future expansion: known/flagged plates and notifications.
    flagged: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    observations: Mapped[list[PlateObservation]] = relationship(
        back_populates="plate", cascade="all, delete-orphan", passive_deletes=True
    )


class PlateObservation(Base):
    """One reading of a plate, tied to a moment in a recording.

    Deduplication happens at the track level: many frames of the same plate on the same
    tracked vehicle collapse into a single observation carrying the winning vote.
    """

    __tablename__ = "plate_observations"

    id: Mapped[int] = mapped_column(primary_key=True)
    plate_id: Mapped[int] = mapped_column(ForeignKey("plates.id", ondelete="CASCADE"), index=True)
    recording_id: Mapped[int] = mapped_column(
        ForeignKey("recordings.id", ondelete="CASCADE"), index=True
    )
    journey_id: Mapped[int | None] = mapped_column(
        ForeignKey("journeys.id", ondelete="SET NULL"), nullable=True, index=True
    )
    camera_id: Mapped[int | None] = mapped_column(ForeignKey("cameras.id"), nullable=True)
    tracked_object_id: Mapped[int | None] = mapped_column(
        ForeignKey("tracked_objects.id", ondelete="SET NULL"), nullable=True, index=True
    )

    t_offset_s: Mapped[float] = mapped_column(Float)
    captured_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    first_seen_offset_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_seen_offset_s: Mapped[float | None] = mapped_column(Float, nullable=True)

    # The unmodified OCR output is kept next to the normalised value, always.
    raw_text: Mapped[str] = mapped_column(String(64))
    normalised_text: Mapped[str] = mapped_column(String(32), index=True)
    ocr_confidence: Mapped[float] = mapped_column(Float)
    detection_confidence: Mapped[float] = mapped_column(Float)
    # How many frames voted for this reading.
    vote_count: Mapped[int] = mapped_column(Integer, default=1)

    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lon: Mapped[float | None] = mapped_column(Float, nullable=True)

    plate_crop_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    vehicle_crop_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    frame_thumb_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    bbox: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    plate: Mapped[Plate] = relationship(back_populates="observations")

    __table_args__ = (
        # One observation per plate per tracked vehicle -- the dedup guarantee.
        UniqueConstraint(
            "plate_id", "recording_id", "tracked_object_id", name="uq_plateobs_plate_rec_track"
        ),
        Index("ix_plateobs_recording_time", "recording_id", "t_offset_s"),
        Index("ix_plateobs_plate_time", "plate_id", "captured_at"),
    )


# --------------------------------------------------------------------------------------
# Operational tables
# --------------------------------------------------------------------------------------


class ProcessingJob(Base):
    """Durable work queue. Claimed atomically so restarts never lose or duplicate work."""

    __tablename__ = "processing_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    recording_id: Mapped[int | None] = mapped_column(
        ForeignKey("recordings.id", ondelete="CASCADE"), nullable=True, index=True
    )
    kind: Mapped[JobKind] = mapped_column(Enum(JobKind), default=JobKind.PROCESS, index=True)
    # Which stages this job should run, e.g. ["metadata", "telemetry"].
    stages: Mapped[list | None] = mapped_column(JSON, nullable=True)
    state: Mapped[JobState] = mapped_column(Enum(JobState), default=JobState.QUEUED, index=True)

    priority: Mapped[int] = mapped_column(Integer, default=100, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)

    progress: Mapped[float] = mapped_column(Float, default=0.0)
    stage_current: Mapped[str | None] = mapped_column(String(32), nullable=True)
    speed_realtime: Mapped[float | None] = mapped_column(Float, nullable=True)
    decoder: Mapped[str | None] = mapped_column(String(32), nullable=True)
    inference_device: Mapped[str | None] = mapped_column(String(32), nullable=True)

    worker_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Lets a crashed worker's claim be reclaimed after a heartbeat timeout.
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    queued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Backoff gate for transient failures.
    not_before: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    __table_args__ = (Index("ix_jobs_claim", "state", "priority", "queued_at"),)


class ScanRun(Base):
    """One pass of the footage scanner."""

    __tablename__ = "scan_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    trigger: Mapped[str] = mapped_column(String(32), default="scheduled")

    files_seen: Mapped[int] = mapped_column(Integer, default=0)
    files_new: Mapped[int] = mapped_column(Integer, default=0)
    files_changed: Mapped[int] = mapped_column(Integer, default=0)
    files_missing: Mapped[int] = mapped_column(Integer, default=0)
    # Files skipped because they were still being written.
    files_unsettled: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[int] = mapped_column(Integer, default=0)
    duration_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class AppSetting(Base):
    """UI-editable configuration. Values are JSON so types survive round-tripping."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[dict | list | str | int | float | bool | None] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class LogEntry(Base):
    """Application log surfaced in the UI. Deliberately not one row per analysed frame."""

    __tablename__ = "log_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    level: Mapped[str] = mapped_column(String(16), index=True)
    logger: Mapped[str] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(Text)
    context: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    recording_id: Mapped[int | None] = mapped_column(
        ForeignKey("recordings.id", ondelete="CASCADE"), nullable=True, index=True
    )
    job_id: Mapped[int | None] = mapped_column(
        ForeignKey("processing_jobs.id", ondelete="CASCADE"), nullable=True, index=True
    )

    __table_args__ = (Index("ix_logs_level_ts", "level", "ts"),)


class OsdProfile(Base):
    """Where the burned-in telemetry lives and how to read it.

    Region is stored as fractions of frame width/height so a profile survives a
    resolution change. This exists because telemetry in this footage is pixels, not
    metadata -- see ARCHITECTURE.md section 1.3.
    """

    __tablename__ = "osd_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    # Fractional crop: x, y, w, h in 0..1
    region_x: Mapped[float] = mapped_column(Float, default=0.0)
    region_y: Mapped[float] = mapped_column(Float, default=0.94)
    region_w: Mapped[float] = mapped_column(Float, default=1.0)
    region_h: Mapped[float] = mapped_column(Float, default=0.06)

    pattern: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    auto_calibrated: Mapped[bool] = mapped_column(Boolean, default=False)
    # Optional restriction to a camera or resolution.
    applies_to_camera_id: Mapped[int | None] = mapped_column(
        ForeignKey("cameras.id", ondelete="SET NULL"), nullable=True
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RetentionRun(Base):
    """Record of a retention evaluation. In report-only mode nothing is deleted."""

    __tablename__ = "retention_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    trigger: Mapped[str] = mapped_column(String(32), default="scheduled")

    dry_run: Mapped[bool] = mapped_column(Boolean, default=True)
    # Set when a safety guard refused the run; `blocked_reason` says which.
    blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    blocked_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    bytes_before: Mapped[int] = mapped_column(Integer, default=0)
    bytes_limit: Mapped[int] = mapped_column(Integer, default=0)
    candidate_count: Mapped[int] = mapped_column(Integer, default=0)
    candidate_bytes: Mapped[int] = mapped_column(Integer, default=0)
    deleted_count: Mapped[int] = mapped_column(Integer, default=0)
    deleted_bytes: Mapped[int] = mapped_column(Integer, default=0)
    plan: Mapped[list | None] = mapped_column(JSON, nullable=True)
