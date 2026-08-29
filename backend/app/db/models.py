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
from sqlalchemy.types import TypeDecorator


def utcnow() -> datetime:
    return datetime.now(UTC)


class UtcDateTime(TypeDecorator):
    """A ``DateTime`` that is always timezone-aware UTC in Python.

    SQLite has no timezone-aware column type: an aware datetime goes in and a **naive**
    one comes back. That leaked in three directions before this existed --

    * comparing a stored timestamp against a freshly computed one raised
      ``TypeError: can't compare offset-naive and offset-aware datetimes``, which broke
      journey clustering;
    * the API serialised naive values with no offset, so ``new Date()`` in the browser
      read UTC as local time and every timestamp in the UI was wrong by the local offset;
    * arithmetic between two stored values silently assumed they shared a zone.

    Normalising on the way in and stamping UTC on the way out fixes all three at the
    source, so nothing downstream has to remember. The stored representation does not
    change, so no migration is needed.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, _dialect: object) -> datetime | None:
        if value is None:
            return None
        # A naive value reaching the database is taken to be UTC: everything here
        # converts before storing, so that assumption holds.
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, _dialect: object) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class Base(DeclarativeBase):
    pass


# --------------------------------------------------------------------------------------
# Enumerations
# --------------------------------------------------------------------------------------


class RecordingState(str, enum.Enum):
    """Lifecycle of one file, from being noticed on disk to being analysed.

    ``FAILED`` and ``INVALID`` are deliberately different answers. ``FAILED`` means an
    attempt did not succeed and another one might -- a busy share, a lock, a decoder that
    fell over -- so the recording stays in the retry population. ``INVALID`` means the
    source itself cannot ever be processed: zero bytes, no video stream, not media. There
    is no attempt count that turns one of those into a result, so they leave the queue
    entirely rather than consuming a slot on every bulk requeue.
    """

    DISCOVERED = "discovered"
    #: On disk but still growing, or not yet observed twice with the same size. Nothing
    #: is analysed from this state -- see :mod:`app.scanner.stability`.
    SETTLING = "settling"
    METADATA_EXTRACTED = "metadata_extracted"
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    #: Permanently unprocessable. `error_message` says why.
    INVALID = "invalid"
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


class OBDBundleState(str, enum.Enum):
    """Durable state of the OBD copy/import pipeline.

    This is deliberately separate from :class:`JobState`.  A media job can be rebuilt
    from the footage, while an OBD bundle is itself the irreplaceable source until Home
    Assistant acknowledges it.  Folding the two queues together would also let a slow HA
    outage consume media workers, which is exactly the coupling the backup path must avoid.
    """

    WAITING_FOR_BACKUP = "waiting_for_backup"
    COPYING = "copying"
    VALIDATING = "validating"
    READY_TO_IMPORT = "ready_to_import"
    IMPORTING = "importing"
    IMPORTED = "imported"
    RETRY_WAIT = "retry_wait"
    FAILED = "failed"
    QUARANTINED = "quarantined"


#: The values ``processing_jobs.priority`` is meant to take. Lower runs first, and these
#: three tiers are the whole ordering policy -- within a tier the claim works through the
#: footage chronologically, so the tier is the only thing that can put one recording in
#: front of an older one.
#:
#: Kept beside the column rather than in :mod:`app.workers.queue` because the scanner sets
#: this too, and it cannot import the queue: ``app.workers`` imports the scheduler, which
#: imports the scanner. Two copies of a number that decides ordering would disagree
#: eventually, and the symptom would be a queue that quietly ran in the wrong order.
#:
#: ``THUMBNAIL_PRIORITY`` is what makes the thumbnail-first pass first: a thumbnail costs a
#: couple of ffmpeg seeks against minutes for a full analysis, so the whole library can be
#: given pictures in the time it would take to analyse a handful of clips.
THUMBNAIL_PRIORITY = 0
NEW_FOOTAGE_PRIORITY = 100
BULK_PRIORITY = 200


#: Keys inside ``recordings.probe_json`` that survive a re-probe.
#:
#: Everything else in that blob describes what ffprobe just said, so the metadata stage
#: replaces it wholesale — which is correct for the container facts and wrong for the one
#: kind of entry that is not a probe result at all: a marker recording that a decision
#: about this recording has already been reconsidered once.
#:
#: ``media_failure_reviewed`` is the case that bit. ``queue.reconcile_media_failure_hides``
#: un-hides footage that was condemned while the media stack itself was failing, sets this
#: marker so the same row is not reconsidered on every restart, and requeues it. The
#: metadata stage then rewrote ``probe_json`` from the fresh probe and took the marker with
#: it — so genuinely damaged footage was un-hidden, fully re-decoded and re-hidden on every
#: single container restart, which is exactly the loop the marker exists to prevent.
#:
#: ``damaged_policy`` is the second. ``app.damaged_policy`` records the state a recording
#: was in before it was hidden, and ``_restore_policy_hidden`` needs that block to put the
#: row back when the policy is switched to "keep" or the recording stops being damaged.
#: Losing it to a re-probe meant a hidden recording could never be restored at all: the
#: restore looked for the block, did not find it, reported "kept", and left ``ignored`` set.
#:
#: Kept beside the column rather than in :mod:`app.workers.queue` so the writer and the
#: reader agree without either importing the other.
DURABLE_PROBE_KEYS: tuple[str, ...] = ("media_failure_reviewed", "damaged_policy")


def carry_probe_markers(previous: object, fresh: dict) -> dict:
    """Merge the durable markers of *previous* into a freshly probed ``probe_json``."""
    if not isinstance(previous, dict):
        return fresh
    for key in DURABLE_PROBE_KEYS:
        if key in previous:
            fresh[key] = previous[key]
    return fresh


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
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)

    recordings: Mapped[list[Recording]] = relationship(back_populates="camera")


class Journey(Base):
    """A contiguous drive, inferred from segment adjacency and GPS continuity."""

    __tablename__ = "journeys"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(UtcDateTime, index=True)
    ended_at: Mapped[datetime] = mapped_column(UtcDateTime)
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

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, onupdate=utcnow)

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
    # The stat the fingerprint above was taken from, which is the only durable evidence
    # that *these* bytes have been read.
    #
    # Comparing the live stat against ``size_bytes``/``mtime_ns`` cannot answer that, and
    # the scanner used to try. Those two columns are rewritten on every scan so the
    # stability check has something to compare against next time, so "the stat moved" is
    # only ever true for the *one* scan that saw it move -- and that scan holds the file
    # for a settling observation and returns before fingerprinting it. By the next scan
    # the stat matches again and the cheap path skips the file, so a recording whose file
    # was replaced kept the analysis of the bytes that are no longer there, and one that
    # had never been analysed stayed in SETTLING for good.
    fingerprint_size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fingerprint_mtime_ns: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # --- timing -----------------------------------------------------------------------
    # Parsed from the filename; the OSD clock supersedes it once telemetry is read.
    started_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True, index=True)
    ended_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
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

    # Version stamps make analysis changes explicit. A completed stage whose stored
    # revision differs from the current pipeline can be requeued without decoding the
    # rest of the recording again.
    metadata_revision: Mapped[str | None] = mapped_column(String(32), nullable=True)
    telemetry_revision: Mapped[str | None] = mapped_column(String(32), nullable=True)
    detection_revision: Mapped[str | None] = mapped_column(String(32), nullable=True)
    plate_revision: Mapped[str | None] = mapped_column(String(32), nullable=True)

    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    # --- rollups (denormalised so list views never aggregate) --------------------------
    has_gps: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    gps_point_count: Mapped[int] = mapped_column(Integer, default=0)
    telemetry_point_count: Mapped[int] = mapped_column(Integer, default=0)
    distance_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_speed_kmh: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_speed_kmh: Mapped[float | None] = mapped_column(Float, nullable=True)
    start_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    start_lon: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Telemetry health rollups keep the quality dashboard cheap even for libraries with
    # millions of one-second samples.
    gps_gap_count: Mapped[int] = mapped_column(Integer, default=0)
    gps_longest_gap_s: Mapped[float] = mapped_column(Float, default=0.0)
    gps_recovered_count: Mapped[int] = mapped_column(Integer, default=0)
    gps_no_fix_count: Mapped[int] = mapped_column(Integer, default=0)
    gps_ocr_gap_count: Mapped[int] = mapped_column(Integer, default=0)
    gps_rejected_count: Mapped[int] = mapped_column(Integer, default=0)
    telemetry_problem_count: Mapped[int] = mapped_column(Integer, default=0)

    vehicle_count: Mapped[int] = mapped_column(Integer, default=0, index=True)
    plate_count: Mapped[int] = mapped_column(Integer, default=0, index=True)

    thumbnail_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    # --- bookkeeping ------------------------------------------------------------------
    ignored: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    # A protected/event recording is never eligible for retention cleanup.
    protected: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    event_type: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    event_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    last_scanned_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    # Set when the file is gone from disk; the row is kept so history survives cleanup.
    file_missing: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    deleted_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

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
    captured_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

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
    # Field-level extraction/validation provenance. JSON keeps this extensible without
    # widening the highest-cardinality table for every diagnostic status we learn to keep.
    quality_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # How much weight this position carries: valid / interpolated / rejected / no_fix.
    # A column rather than another key in `quality_json` because this is the one piece of
    # provenance that every *query* needs. The heat map aggregates in SQL, and filtering on
    # a JSON field there means either a scan or a functional index; more importantly, the
    # distinction it encodes was previously unavailable to SQL at all, so the heat layer
    # had to settle for `has_fix` and drew interpolated fills at full weight alongside
    # positions that were actually read.
    gps_quality: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Why there is no trusted position here, from `app.osd.reasons.GpsReason`. Null when
    # the position was accepted. Stable identifiers, so "what are we losing fixes to" is a
    # GROUP BY rather than an afternoon reading logs.
    gps_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # True when the route must be cut before this sample instead of joined to the previous
    # one. Decided once, where the timing and the gaps are known, so that every map draws
    # the same breaks rather than each re-deriving them from whatever it happened to load.
    breaks_segment: Mapped[bool] = mapped_column(Boolean, default=False)

    recording: Mapped[Recording] = relationship(back_populates="telemetry")

    __table_args__ = (
        UniqueConstraint("recording_id", "t_offset_s", name="uq_telemetry_recording_offset"),
        Index("ix_telemetry_journey_time", "journey_id", "captured_at"),
        Index("ix_telemetry_recording_offset", "recording_id", "t_offset_s"),
        # The heat map's hot path: every drawable position, in journey order. Including
        # the quality keeps that query index-only rather than visiting rows to discard
        # the rejected ones.
        Index("ix_telemetry_quality", "gps_quality", "journey_id"),
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

    first_seen_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    observation_count: Mapped[int] = mapped_column(Integer, default=0)
    representative_crop_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, onupdate=utcnow)


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

    first_seen_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True, index=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

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

    first_seen_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True, index=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True, index=True)
    observation_count: Mapped[int] = mapped_column(Integer, default=0, index=True)
    journey_count: Mapped[int] = mapped_column(Integer, default=0)

    best_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    best_observation_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Future expansion: known/flagged plates and notifications.
    flagged: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    dismissed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, onupdate=utcnow)

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
    captured_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True, index=True)
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
    resource_state: Mapped[str | None] = mapped_column(String(64), nullable=True)

    worker_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Lets a crashed worker's claim be reclaimed after a heartbeat timeout.
    heartbeat_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    queued_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, index=True)
    started_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    # Backoff gate for transient failures.
    not_before: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True, index=True)

    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    __table_args__ = (Index("ix_jobs_claim", "state", "priority", "queued_at"),)


class ScanRun(Base):
    """One pass of the footage scanner."""

    __tablename__ = "scan_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
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


class IngestRun(Base):
    """One pull of footage off the head unit.

    History and "when did this last work", nothing more. Live progress stays in memory in
    ``app.ingest.status``: a run lasts a minute or two and reports bytes several times a
    second, which is not worth a write each, and nobody asks the database what is happening
    right now -- they ask the status endpoint.
    """

    __tablename__ = "ingest_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    trigger: Mapped[str] = mapped_column(String(32), default="auto")

    #: One of RunState: ok, partial, error, offline, unauthorized, cancelled, idle.
    state: Mapped[str] = mapped_column(String(32), default="ok", index=True)
    files_transferred: Mapped[int] = mapped_column(Integer, default=0)
    bytes_transferred: Mapped[int] = mapped_column(Integer, default=0)
    throughput_mbs_avg: Mapped[float] = mapped_column(Float, default=0.0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class OBDBundle(Base):
    """One immutable logger export and its independent Home Assistant queue state."""

    __tablename__ = "obd_bundles"

    id: Mapped[int] = mapped_column(primary_key=True)
    drive_id: Mapped[str] = mapped_column(String(64), index=True)
    bundle_hash: Mapped[str] = mapped_column(String(64), index=True)
    schema_version: Mapped[int] = mapped_column(Integer)
    filename: Mapped[str] = mapped_column(String(255), unique=True)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)

    vehicle_id: Mapped[str] = mapped_column(String(128), index=True)
    adapter_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    logger_id: Mapped[str] = mapped_column(String(128))
    logger_version: Mapped[str] = mapped_column(String(64))
    drive_started_at: Mapped[datetime] = mapped_column(UtcDateTime, index=True)
    drive_finished_at: Mapped[datetime] = mapped_column(UtcDateTime)
    sample_count: Mapped[int] = mapped_column(Integer, default=0)
    diagnostic_count: Mapped[int] = mapped_column(Integer, default=0)
    # False means the row is a durable rejection record created before an untrusted
    # manifest could be parsed. Filename/hash/error remain authoritative; the remaining
    # metadata fields are safe placeholders until manual validation promotes the row.
    metadata_trusted: Mapped[bool] = mapped_column(Boolean, default=True, index=True)

    # Kept as a string so new states can be rolled forward without SQLite having to
    # rebuild a native Enum constraint.  Values are owned by OBDBundleState above.
    state: Mapped[str] = mapped_column(
        String(32), default=OBDBundleState.WAITING_FOR_BACKUP.value, index=True
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True, index=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_kind: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    last_http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)

    copied_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    import_started_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    imported_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True, index=True)
    remote_deleted_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, onupdate=utcnow)

    duplicate: Mapped[bool] = mapped_column(Boolean, default=False)
    ha_result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    validation_warnings: Mapped[list | None] = mapped_column(JSON, nullable=True)

    drive: Mapped[OBDDrive | None] = relationship(back_populates="bundle", uselist=False)

    __table_args__ = (
        UniqueConstraint(
            "drive_id",
            "bundle_hash",
            "schema_version",
            name="uq_obd_bundle_identity",
        ),
        Index("ix_obd_bundle_claim", "state", "next_attempt_at", "drive_started_at", "id"),
    )


class OBDDrive(Base):
    """High-level drive metadata and rollups retained by the analytics server."""

    __tablename__ = "obd_drives"

    id: Mapped[int] = mapped_column(primary_key=True)
    bundle_id: Mapped[int] = mapped_column(
        ForeignKey("obd_bundles.id", ondelete="CASCADE"), unique=True, index=True
    )
    drive_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    vehicle_id: Mapped[str] = mapped_column(String(128), index=True)
    started_at: Mapped[datetime] = mapped_column(UtcDateTime, index=True)
    finished_at: Mapped[datetime] = mapped_column(UtcDateTime)
    original_timezone: Mapped[str | None] = mapped_column(String(128), nullable=True)
    start_reason: Mapped[str | None] = mapped_column(String(128), nullable=True)
    stop_reason: Mapped[str | None] = mapped_column(String(128), nullable=True)
    obd_protocol: Mapped[str | None] = mapped_column(String(256), nullable=True)
    completion_status: Mapped[str] = mapped_column(String(32))
    clean_end: Mapped[bool] = mapped_column(Boolean, default=False)

    duration_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    distance_km: Mapped[float | None] = mapped_column(Float, nullable=True)
    average_speed_kmh: Mapped[float | None] = mapped_column(Float, nullable=True)
    maximum_speed_kmh: Mapped[float | None] = mapped_column(Float, nullable=True)
    average_rpm: Mapped[float | None] = mapped_column(Float, nullable=True)
    maximum_rpm: Mapped[float | None] = mapped_column(Float, nullable=True)
    idle_duration_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    estimated_fuel_used_l: Mapped[float | None] = mapped_column(Float, nullable=True)
    average_fuel_consumption_l_100km: Mapped[float | None] = mapped_column(Float, nullable=True)
    maximum_coolant_temperature_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    maximum_engine_load_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    missing_data_duration_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    expected_sample_count: Mapped[int] = mapped_column(Integer, default=0)
    received_sample_percentage: Mapped[float | None] = mapped_column(Float, nullable=True)
    sample_count: Mapped[int] = mapped_column(Integer, default=0)
    error_count: Mapped[int] = mapped_column(Integer, default=0)
    dtcs_observed: Mapped[list | None] = mapped_column(JSON, nullable=True)
    units: Mapped[dict] = mapped_column(JSON)
    manifest_json: Mapped[dict] = mapped_column(JSON)
    summary_json: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)

    bundle: Mapped[OBDBundle] = relationship(back_populates="drive")
    samples: Mapped[list[OBDSample]] = relationship(
        back_populates="drive", cascade="all, delete-orphan"
    )
    diagnostics: Mapped[list[OBDDiagnostic]] = relationship(
        back_populates="drive", cascade="all, delete-orphan"
    )


class OBDSample(Base):
    """One logger sample.  Units are explicit in ``OBDDrive.units`` and column names."""

    __tablename__ = "obd_samples"

    id: Mapped[int] = mapped_column(primary_key=True)
    drive_db_id: Mapped[int] = mapped_column(
        ForeignKey("obd_drives.id", ondelete="CASCADE"), index=True
    )
    sample_id: Mapped[str] = mapped_column(String(96), unique=True)
    sequence: Mapped[int] = mapped_column(Integer)
    captured_at: Mapped[datetime] = mapped_column(UtcDateTime, index=True)
    ecu_data_status: Mapped[str] = mapped_column(String(32))

    engine_rpm: Mapped[float | None] = mapped_column(Float, nullable=True)
    vehicle_speed_kmh: Mapped[float | None] = mapped_column(Float, nullable=True)
    coolant_temperature_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    intake_air_temperature_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    engine_load_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    throttle_position_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    timing_advance_deg: Mapped[float | None] = mapped_column(Float, nullable=True)
    mass_air_flow_g_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    short_term_fuel_trim_bank_1_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    long_term_fuel_trim_bank_1_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    fuel_system_status: Mapped[str | None] = mapped_column(String(128), nullable=True)
    oxygen_sensors_present: Mapped[list | None] = mapped_column(JSON, nullable=True)
    obd_standard: Mapped[str | None] = mapped_column(String(128), nullable=True)
    distance_with_mil_km: Mapped[float | None] = mapped_column(Float, nullable=True)
    oxygen_sensor_1_voltage_v: Mapped[float | None] = mapped_column(Float, nullable=True)
    oxygen_sensor_1_short_term_fuel_trim_pct: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )
    oxygen_sensor_2_voltage_v: Mapped[float | None] = mapped_column(Float, nullable=True)
    oxygen_sensor_2_short_term_fuel_trim_pct: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )
    adapter_voltage_v: Mapped[float | None] = mapped_column(Float, nullable=True)
    estimated_fuel_rate_l_h: Mapped[float | None] = mapped_column(Float, nullable=True)
    estimated_fuel_consumption_l_100km: Mapped[float | None] = mapped_column(Float, nullable=True)
    quality_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    raw_json: Mapped[dict] = mapped_column(JSON)

    drive: Mapped[OBDDrive] = relationship(back_populates="samples")

    __table_args__ = (
        UniqueConstraint("drive_db_id", "sequence", name="uq_obd_sample_drive_sequence"),
        Index("ix_obd_samples_drive_time", "drive_db_id", "captured_at"),
    )


class OBDDiagnostic(Base):
    """Sparse diagnostic/connection event, separate from high-frequency samples."""

    __tablename__ = "obd_diagnostics"

    id: Mapped[int] = mapped_column(primary_key=True)
    drive_db_id: Mapped[int] = mapped_column(
        ForeignKey("obd_drives.id", ondelete="CASCADE"), index=True
    )
    event_hash: Mapped[str] = mapped_column(String(64))
    observed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(64), index=True)
    payload_json: Mapped[dict] = mapped_column(JSON)

    drive: Mapped[OBDDrive] = relationship(back_populates="diagnostics")

    __table_args__ = (
        UniqueConstraint("drive_db_id", "event_hash", name="uq_obd_diagnostic_event"),
    )


class AppSetting(Base):
    """UI-editable configuration. Values are JSON so types survive round-tripping."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[dict | list | str | int | float | bool | None] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, onupdate=utcnow)


class AuthCredential(Base):
    """The single sign-in account, when the operator has configured one.

    Deliberately not in ``app_settings``. Every value in that table is echoed back by
    ``GET /api/settings`` to render the Settings page, so a password living there would be
    handed to any browser that asked for it -- and the settings cache is snapshotted into
    worker threads besides. A password hash belongs in neither place.

    One row, id 1. Multiple accounts would need a permission model to be worth anything,
    and this application has exactly one kind of user: the person who owns the footage.
    """

    __tablename__ = "auth_credentials"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(128))
    #: ``scrypt$<n>$<r>$<p>$<salt-b64>$<hash-b64>``. See ``app.auth.passwords``.
    password_hash: Mapped[str] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, onupdate=utcnow)


class AuthSession(Base):
    """One signed-in browser.

    The cookie carries a random token; only its SHA-256 lives here. A database that leaks
    -- through the backup download on the Settings page, most plausibly -- therefore hands
    over no usable session. Unsalted SHA-256 is the right choice for exactly this input and
    no other: the token is 256 bits of ``secrets`` output, so there is no dictionary to
    build and nothing for a salt to defend against, and the lookup has to be a single
    indexed equality match on the hot path.
    """

    __tablename__ = "auth_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime, index=True)
    last_used_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    #: True when the visitor ticked "stay signed in"; drives the cookie lifetime and is
    #: shown on the sessions list so a long-lived login is visible as one.
    remembered: Mapped[bool] = mapped_column(Boolean, default=False)
    #: Truncated User-Agent, so a stale session can be recognised before it is revoked.
    user_agent: Mapped[str | None] = mapped_column(String(256), nullable=True)
    #: The address the session was created from. Never used for authorisation -- it is
    #: proxy-supplied and trivially spoofed -- only to help the operator recognise a row.
    created_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)


class LogEntry(Base):
    """Application log surfaced in the UI. Deliberately not one row per analysed frame."""

    __tablename__ = "log_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, index=True)
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
    region_y: Mapped[float] = mapped_column(Float, default=0.9537)
    region_w: Mapped[float] = mapped_column(Float, default=1.0)
    region_h: Mapped[float] = mapped_column(Float, default=0.0463)

    pattern: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    auto_calibrated: Mapped[bool] = mapped_column(Boolean, default=False)
    # Optional restriction to a camera or resolution.
    applies_to_camera_id: Mapped[int | None] = mapped_column(
        ForeignKey("cameras.id", ondelete="SET NULL"), nullable=True
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)


class RetentionRun(Base):
    """Record of a retention evaluation. In report-only mode nothing is deleted."""

    __tablename__ = "retention_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
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
