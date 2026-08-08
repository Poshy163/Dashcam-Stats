"""Initial schema.

Written by hand from app/db/models.py. Enum columns are rendered exactly as SQLAlchemy
renders them for a Python enum -- VARCHAR holding the member *name*, with no CHECK
constraint (SQLAlchemy 2.0 defaults ``Enum.create_constraint`` to False).

Revision ID: 0001
Revises:
Create Date: 2026-08-08
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


RECORDING_STATE = sa.Enum(
    "DISCOVERED",
    "METADATA_EXTRACTED",
    "QUEUED",
    "PROCESSING",
    "COMPLETED",
    "FAILED",
    "IGNORED",
    "DELETED",
    name="recordingstate",
)
STAGE_STATE = sa.Enum("PENDING", "RUNNING", "DONE", "FAILED", "SKIPPED", name="stagestate")
JOB_STATE = sa.Enum("QUEUED", "RUNNING", "COMPLETED", "FAILED", "CANCELLED", name="jobstate")
JOB_KIND = sa.Enum(
    "PROCESS",
    "REPROCESS",
    "SCAN",
    "RETENTION",
    "THUMBNAIL",
    "JOURNEY_REBUILD",
    name="jobkind",
)
CAMERA_ROLE = sa.Enum("FRONT", "REAR", "CABIN", "OTHER", name="camerarole")


def upgrade() -> None:
    # -- cameras ---------------------------------------------------------------------
    op.create_table(
        "cameras",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("role", CAMERA_ROLE, nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_cameras_key"), "cameras", ["key"], unique=True)

    # -- journeys --------------------------------------------------------------------
    op.create_table(
        "journeys",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_s", sa.Float(), nullable=False),
        sa.Column("distance_m", sa.Float(), nullable=True),
        sa.Column("avg_speed_kmh", sa.Float(), nullable=True),
        sa.Column("max_speed_kmh", sa.Float(), nullable=True),
        sa.Column("start_lat", sa.Float(), nullable=True),
        sa.Column("start_lon", sa.Float(), nullable=True),
        sa.Column("end_lat", sa.Float(), nullable=True),
        sa.Column("end_lon", sa.Float(), nullable=True),
        sa.Column("min_lat", sa.Float(), nullable=True),
        sa.Column("min_lon", sa.Float(), nullable=True),
        sa.Column("max_lat", sa.Float(), nullable=True),
        sa.Column("max_lon", sa.Float(), nullable=True),
        sa.Column("has_gps", sa.Boolean(), nullable=False),
        sa.Column("recording_count", sa.Integer(), nullable=False),
        sa.Column("vehicle_count", sa.Integer(), nullable=False),
        sa.Column("unique_plate_count", sa.Integer(), nullable=False),
        sa.Column("manual", sa.Boolean(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_journeys_has_gps"), "journeys", ["has_gps"], unique=False)
    op.create_index(op.f("ix_journeys_started_at"), "journeys", ["started_at"], unique=False)
    op.create_index("ix_journeys_span", "journeys", ["started_at", "ended_at"], unique=False)

    # -- plates ----------------------------------------------------------------------
    op.create_table(
        "plates",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("normalised_text", sa.String(length=32), nullable=False),
        sa.Column("display_text", sa.String(length=32), nullable=False),
        sa.Column("region", sa.String(length=16), nullable=True),
        sa.Column("pattern_name", sa.String(length=64), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("observation_count", sa.Integer(), nullable=False),
        sa.Column("journey_count", sa.Integer(), nullable=False),
        sa.Column("best_confidence", sa.Float(), nullable=False),
        sa.Column("best_observation_id", sa.Integer(), nullable=True),
        sa.Column("flagged", sa.Boolean(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_plates_first_seen_at"), "plates", ["first_seen_at"], unique=False)
    op.create_index(op.f("ix_plates_flagged"), "plates", ["flagged"], unique=False)
    op.create_index(op.f("ix_plates_last_seen_at"), "plates", ["last_seen_at"], unique=False)
    op.create_index(
        op.f("ix_plates_normalised_text"), "plates", ["normalised_text"], unique=True
    )
    op.create_index(
        op.f("ix_plates_observation_count"), "plates", ["observation_count"], unique=False
    )

    # -- vehicles --------------------------------------------------------------------
    op.create_table(
        "vehicles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("primary_plate_id", sa.Integer(), nullable=True),
        sa.Column("class_label", sa.String(length=32), nullable=True),
        sa.Column("make", sa.String(length=64), nullable=True),
        sa.Column("model", sa.String(length=64), nullable=True),
        sa.Column("colour", sa.String(length=32), nullable=True),
        sa.Column("classifier", sa.String(length=64), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("observation_count", sa.Integer(), nullable=False),
        sa.Column("representative_crop_path", sa.String(length=1024), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["primary_plate_id"], ["plates.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_vehicles_class_label"), "vehicles", ["class_label"], unique=False)
    op.create_index(
        op.f("ix_vehicles_primary_plate_id"), "vehicles", ["primary_plate_id"], unique=False
    )

    # -- recordings ------------------------------------------------------------------
    op.create_table(
        "recordings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("rel_path", sa.String(length=1024), nullable=False),
        sa.Column("filename", sa.String(length=512), nullable=False),
        sa.Column("camera_id", sa.Integer(), nullable=True),
        sa.Column("journey_id", sa.Integer(), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("mtime_ns", sa.Integer(), nullable=False),
        sa.Column("fingerprint", sa.String(length=128), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_s", sa.Float(), nullable=True),
        sa.Column("time_from_osd", sa.Boolean(), nullable=False),
        sa.Column("container", sa.String(length=32), nullable=True),
        sa.Column("video_codec", sa.String(length=32), nullable=True),
        sa.Column("video_profile", sa.String(length=64), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("fps", sa.Float(), nullable=True),
        sa.Column("fps_container", sa.Float(), nullable=True),
        sa.Column("bitrate", sa.Integer(), nullable=True),
        sa.Column("pix_fmt", sa.String(length=32), nullable=True),
        sa.Column("has_audio", sa.Boolean(), nullable=False),
        sa.Column("audio_codec", sa.String(length=32), nullable=True),
        sa.Column("audio_sample_rate", sa.Integer(), nullable=True),
        sa.Column("audio_channels", sa.Integer(), nullable=True),
        sa.Column("probe_json", sa.JSON(), nullable=True),
        sa.Column("state", RECORDING_STATE, nullable=False),
        sa.Column("metadata_state", STAGE_STATE, nullable=False),
        sa.Column("telemetry_state", STAGE_STATE, nullable=False),
        sa.Column("detection_state", STAGE_STATE, nullable=False),
        sa.Column("plate_state", STAGE_STATE, nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("error_count", sa.Integer(), nullable=False),
        sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("has_gps", sa.Boolean(), nullable=False),
        sa.Column("gps_point_count", sa.Integer(), nullable=False),
        sa.Column("telemetry_point_count", sa.Integer(), nullable=False),
        sa.Column("distance_m", sa.Float(), nullable=True),
        sa.Column("avg_speed_kmh", sa.Float(), nullable=True),
        sa.Column("max_speed_kmh", sa.Float(), nullable=True),
        sa.Column("start_lat", sa.Float(), nullable=True),
        sa.Column("start_lon", sa.Float(), nullable=True),
        sa.Column("vehicle_count", sa.Integer(), nullable=False),
        sa.Column("plate_count", sa.Integer(), nullable=False),
        sa.Column("thumbnail_path", sa.String(length=1024), nullable=True),
        sa.Column("ignored", sa.Boolean(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_scanned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("file_missing", sa.Boolean(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["camera_id"], ["cameras.id"]),
        sa.ForeignKeyConstraint(["journey_id"], ["journeys.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_recordings_camera_id"), "recordings", ["camera_id"], unique=False)
    op.create_index(op.f("ix_recordings_file_missing"), "recordings", ["file_missing"], unique=False)
    op.create_index(op.f("ix_recordings_filename"), "recordings", ["filename"], unique=False)
    op.create_index(op.f("ix_recordings_fingerprint"), "recordings", ["fingerprint"], unique=False)
    op.create_index(op.f("ix_recordings_has_gps"), "recordings", ["has_gps"], unique=False)
    op.create_index(op.f("ix_recordings_ignored"), "recordings", ["ignored"], unique=False)
    op.create_index(op.f("ix_recordings_journey_id"), "recordings", ["journey_id"], unique=False)
    op.create_index(op.f("ix_recordings_plate_count"), "recordings", ["plate_count"], unique=False)
    op.create_index(op.f("ix_recordings_rel_path"), "recordings", ["rel_path"], unique=True)
    op.create_index(op.f("ix_recordings_started_at"), "recordings", ["started_at"], unique=False)
    op.create_index(op.f("ix_recordings_state"), "recordings", ["state"], unique=False)
    op.create_index(
        op.f("ix_recordings_vehicle_count"), "recordings", ["vehicle_count"], unique=False
    )
    op.create_index(
        "ix_recordings_camera_started", "recordings", ["camera_id", "started_at"], unique=False
    )
    op.create_index(
        "ix_recordings_state_started", "recordings", ["state", "started_at"], unique=False
    )

    # -- telemetry_points ------------------------------------------------------------
    op.create_table(
        "telemetry_points",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("recording_id", sa.Integer(), nullable=False),
        sa.Column("journey_id", sa.Integer(), nullable=True),
        sa.Column("t_offset_s", sa.Float(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lat", sa.Float(), nullable=True),
        sa.Column("lon", sa.Float(), nullable=True),
        sa.Column("has_fix", sa.Boolean(), nullable=False),
        sa.Column("speed_kmh", sa.Float(), nullable=True),
        sa.Column("heading_deg", sa.Float(), nullable=True),
        sa.Column("ocr_confidence", sa.Float(), nullable=True),
        sa.Column("raw_text", sa.String(length=255), nullable=True),
        sa.CheckConstraint("lat IS NULL OR (lat >= -90 AND lat <= 90)", name="ck_telemetry_lat"),
        sa.CheckConstraint("lon IS NULL OR (lon >= -180 AND lon <= 180)", name="ck_telemetry_lon"),
        sa.ForeignKeyConstraint(["journey_id"], ["journeys.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["recording_id"], ["recordings.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "recording_id", "t_offset_s", name="uq_telemetry_recording_offset"
        ),
    )
    op.create_index(
        op.f("ix_telemetry_points_journey_id"), "telemetry_points", ["journey_id"], unique=False
    )
    op.create_index(
        op.f("ix_telemetry_points_recording_id"),
        "telemetry_points",
        ["recording_id"],
        unique=False,
    )
    op.create_index(
        "ix_telemetry_journey_time",
        "telemetry_points",
        ["journey_id", "captured_at"],
        unique=False,
    )
    op.create_index(
        "ix_telemetry_recording_offset",
        "telemetry_points",
        ["recording_id", "t_offset_s"],
        unique=False,
    )

    # -- tracked_objects -------------------------------------------------------------
    op.create_table(
        "tracked_objects",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("recording_id", sa.Integer(), nullable=False),
        sa.Column("journey_id", sa.Integer(), nullable=True),
        sa.Column("vehicle_id", sa.Integer(), nullable=True),
        sa.Column("track_key", sa.Integer(), nullable=False),
        sa.Column("class_label", sa.String(length=32), nullable=False),
        sa.Column("confidence_max", sa.Float(), nullable=False),
        sa.Column("confidence_avg", sa.Float(), nullable=False),
        sa.Column("first_seen_offset_s", sa.Float(), nullable=False),
        sa.Column("last_seen_offset_s", sa.Float(), nullable=False),
        sa.Column("duration_s", sa.Float(), nullable=False),
        sa.Column("frame_count", sa.Integer(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lat", sa.Float(), nullable=True),
        sa.Column("lon", sa.Float(), nullable=True),
        sa.Column("best_frame_offset_s", sa.Float(), nullable=True),
        sa.Column("best_bbox", sa.JSON(), nullable=True),
        sa.Column("crop_path", sa.String(length=1024), nullable=True),
        sa.ForeignKeyConstraint(["journey_id"], ["journeys.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["recording_id"], ["recordings.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["vehicle_id"], ["vehicles.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("recording_id", "track_key", name="uq_track_recording_key"),
    )
    op.create_index(
        op.f("ix_tracked_objects_class_label"), "tracked_objects", ["class_label"], unique=False
    )
    op.create_index(
        op.f("ix_tracked_objects_first_seen_at"),
        "tracked_objects",
        ["first_seen_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_tracked_objects_journey_id"), "tracked_objects", ["journey_id"], unique=False
    )
    op.create_index(
        op.f("ix_tracked_objects_recording_id"), "tracked_objects", ["recording_id"], unique=False
    )
    op.create_index(
        op.f("ix_tracked_objects_vehicle_id"), "tracked_objects", ["vehicle_id"], unique=False
    )
    op.create_index(
        "ix_tracks_recording_time",
        "tracked_objects",
        ["recording_id", "first_seen_offset_s"],
        unique=False,
    )

    # -- detections ------------------------------------------------------------------
    op.create_table(
        "detections",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tracked_object_id", sa.Integer(), nullable=False),
        sa.Column("recording_id", sa.Integer(), nullable=False),
        sa.Column("t_offset_s", sa.Float(), nullable=False),
        sa.Column("frame_index", sa.Integer(), nullable=True),
        sa.Column("class_label", sa.String(length=32), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("x", sa.Float(), nullable=False),
        sa.Column("y", sa.Float(), nullable=False),
        sa.Column("w", sa.Float(), nullable=False),
        sa.Column("h", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["recording_id"], ["recordings.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tracked_object_id"], ["tracked_objects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_detections_recording_id"), "detections", ["recording_id"], unique=False
    )
    op.create_index(
        op.f("ix_detections_tracked_object_id"), "detections", ["tracked_object_id"], unique=False
    )
    op.create_index(
        "ix_detections_recording_time", "detections", ["recording_id", "t_offset_s"], unique=False
    )

    # -- plate_observations ----------------------------------------------------------
    op.create_table(
        "plate_observations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("plate_id", sa.Integer(), nullable=False),
        sa.Column("recording_id", sa.Integer(), nullable=False),
        sa.Column("journey_id", sa.Integer(), nullable=True),
        sa.Column("camera_id", sa.Integer(), nullable=True),
        sa.Column("tracked_object_id", sa.Integer(), nullable=True),
        sa.Column("t_offset_s", sa.Float(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_seen_offset_s", sa.Float(), nullable=True),
        sa.Column("last_seen_offset_s", sa.Float(), nullable=True),
        sa.Column("raw_text", sa.String(length=64), nullable=False),
        sa.Column("normalised_text", sa.String(length=32), nullable=False),
        sa.Column("ocr_confidence", sa.Float(), nullable=False),
        sa.Column("detection_confidence", sa.Float(), nullable=False),
        sa.Column("vote_count", sa.Integer(), nullable=False),
        sa.Column("lat", sa.Float(), nullable=True),
        sa.Column("lon", sa.Float(), nullable=True),
        sa.Column("plate_crop_path", sa.String(length=1024), nullable=True),
        sa.Column("vehicle_crop_path", sa.String(length=1024), nullable=True),
        sa.Column("frame_thumb_path", sa.String(length=1024), nullable=True),
        sa.Column("bbox", sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(["camera_id"], ["cameras.id"]),
        sa.ForeignKeyConstraint(["journey_id"], ["journeys.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["plate_id"], ["plates.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["recording_id"], ["recordings.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["tracked_object_id"], ["tracked_objects.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "plate_id", "recording_id", "tracked_object_id", name="uq_plateobs_plate_rec_track"
        ),
    )
    op.create_index(
        op.f("ix_plate_observations_captured_at"),
        "plate_observations",
        ["captured_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_plate_observations_journey_id"),
        "plate_observations",
        ["journey_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_plate_observations_normalised_text"),
        "plate_observations",
        ["normalised_text"],
        unique=False,
    )
    op.create_index(
        op.f("ix_plate_observations_plate_id"), "plate_observations", ["plate_id"], unique=False
    )
    op.create_index(
        op.f("ix_plate_observations_recording_id"),
        "plate_observations",
        ["recording_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_plate_observations_tracked_object_id"),
        "plate_observations",
        ["tracked_object_id"],
        unique=False,
    )
    op.create_index(
        "ix_plateobs_plate_time", "plate_observations", ["plate_id", "captured_at"], unique=False
    )
    op.create_index(
        "ix_plateobs_recording_time",
        "plate_observations",
        ["recording_id", "t_offset_s"],
        unique=False,
    )

    # -- processing_jobs -------------------------------------------------------------
    op.create_table(
        "processing_jobs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("recording_id", sa.Integer(), nullable=True),
        sa.Column("kind", JOB_KIND, nullable=False),
        sa.Column("stages", sa.JSON(), nullable=True),
        sa.Column("state", JOB_STATE, nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("progress", sa.Float(), nullable=False),
        sa.Column("stage_current", sa.String(length=32), nullable=True),
        sa.Column("speed_realtime", sa.Float(), nullable=True),
        sa.Column("decoder", sa.String(length=32), nullable=True),
        sa.Column("inference_device", sa.String(length=32), nullable=True),
        sa.Column("worker_id", sa.String(length=64), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("not_before", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(["recording_id"], ["recordings.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_processing_jobs_kind"), "processing_jobs", ["kind"], unique=False)
    op.create_index(
        op.f("ix_processing_jobs_not_before"), "processing_jobs", ["not_before"], unique=False
    )
    op.create_index(
        op.f("ix_processing_jobs_priority"), "processing_jobs", ["priority"], unique=False
    )
    op.create_index(
        op.f("ix_processing_jobs_queued_at"), "processing_jobs", ["queued_at"], unique=False
    )
    op.create_index(
        op.f("ix_processing_jobs_recording_id"), "processing_jobs", ["recording_id"], unique=False
    )
    op.create_index(op.f("ix_processing_jobs_state"), "processing_jobs", ["state"], unique=False)
    op.create_index(
        "ix_jobs_claim", "processing_jobs", ["state", "priority", "queued_at"], unique=False
    )

    # -- scan_runs -------------------------------------------------------------------
    op.create_table(
        "scan_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("trigger", sa.String(length=32), nullable=False),
        sa.Column("files_seen", sa.Integer(), nullable=False),
        sa.Column("files_new", sa.Integer(), nullable=False),
        sa.Column("files_changed", sa.Integer(), nullable=False),
        sa.Column("files_missing", sa.Integer(), nullable=False),
        sa.Column("files_unsettled", sa.Integer(), nullable=False),
        sa.Column("errors", sa.Integer(), nullable=False),
        sa.Column("duration_s", sa.Float(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_scan_runs_started_at"), "scan_runs", ["started_at"], unique=False)

    # -- app_settings ----------------------------------------------------------------
    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("value", sa.JSON(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )

    # -- log_entries -----------------------------------------------------------------
    op.create_table(
        "log_entries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("level", sa.String(length=16), nullable=False),
        sa.Column("logger", sa.String(length=64), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("context", sa.JSON(), nullable=True),
        sa.Column("recording_id", sa.Integer(), nullable=True),
        sa.Column("job_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["job_id"], ["processing_jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["recording_id"], ["recordings.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_log_entries_job_id"), "log_entries", ["job_id"], unique=False)
    op.create_index(op.f("ix_log_entries_level"), "log_entries", ["level"], unique=False)
    op.create_index(
        op.f("ix_log_entries_recording_id"), "log_entries", ["recording_id"], unique=False
    )
    op.create_index(op.f("ix_log_entries_ts"), "log_entries", ["ts"], unique=False)
    op.create_index("ix_logs_level_ts", "log_entries", ["level", "ts"], unique=False)

    # -- osd_profiles ----------------------------------------------------------------
    op.create_table(
        "osd_profiles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("region_x", sa.Float(), nullable=False),
        sa.Column("region_y", sa.Float(), nullable=False),
        sa.Column("region_w", sa.Float(), nullable=False),
        sa.Column("region_h", sa.Float(), nullable=False),
        sa.Column("pattern", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("auto_calibrated", sa.Boolean(), nullable=False),
        sa.Column("applies_to_camera_id", sa.Integer(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["applies_to_camera_id"], ["cameras.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_index(op.f("ix_osd_profiles_active"), "osd_profiles", ["active"], unique=False)

    # -- retention_runs --------------------------------------------------------------
    op.create_table(
        "retention_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("trigger", sa.String(length=32), nullable=False),
        sa.Column("dry_run", sa.Boolean(), nullable=False),
        sa.Column("blocked", sa.Boolean(), nullable=False),
        sa.Column("blocked_reason", sa.Text(), nullable=True),
        sa.Column("bytes_before", sa.Integer(), nullable=False),
        sa.Column("bytes_limit", sa.Integer(), nullable=False),
        sa.Column("candidate_count", sa.Integer(), nullable=False),
        sa.Column("candidate_bytes", sa.Integer(), nullable=False),
        sa.Column("deleted_count", sa.Integer(), nullable=False),
        sa.Column("deleted_bytes", sa.Integer(), nullable=False),
        sa.Column("plan", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_retention_runs_started_at"), "retention_runs", ["started_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_retention_runs_started_at"), table_name="retention_runs")
    op.drop_table("retention_runs")

    op.drop_index(op.f("ix_osd_profiles_active"), table_name="osd_profiles")
    op.drop_table("osd_profiles")

    op.drop_index("ix_logs_level_ts", table_name="log_entries")
    op.drop_index(op.f("ix_log_entries_ts"), table_name="log_entries")
    op.drop_index(op.f("ix_log_entries_recording_id"), table_name="log_entries")
    op.drop_index(op.f("ix_log_entries_level"), table_name="log_entries")
    op.drop_index(op.f("ix_log_entries_job_id"), table_name="log_entries")
    op.drop_table("log_entries")

    op.drop_table("app_settings")

    op.drop_index(op.f("ix_scan_runs_started_at"), table_name="scan_runs")
    op.drop_table("scan_runs")

    op.drop_index("ix_jobs_claim", table_name="processing_jobs")
    op.drop_index(op.f("ix_processing_jobs_state"), table_name="processing_jobs")
    op.drop_index(op.f("ix_processing_jobs_recording_id"), table_name="processing_jobs")
    op.drop_index(op.f("ix_processing_jobs_queued_at"), table_name="processing_jobs")
    op.drop_index(op.f("ix_processing_jobs_priority"), table_name="processing_jobs")
    op.drop_index(op.f("ix_processing_jobs_not_before"), table_name="processing_jobs")
    op.drop_index(op.f("ix_processing_jobs_kind"), table_name="processing_jobs")
    op.drop_table("processing_jobs")

    op.drop_index("ix_plateobs_recording_time", table_name="plate_observations")
    op.drop_index("ix_plateobs_plate_time", table_name="plate_observations")
    op.drop_index(op.f("ix_plate_observations_tracked_object_id"), table_name="plate_observations")
    op.drop_index(op.f("ix_plate_observations_recording_id"), table_name="plate_observations")
    op.drop_index(op.f("ix_plate_observations_plate_id"), table_name="plate_observations")
    op.drop_index(op.f("ix_plate_observations_normalised_text"), table_name="plate_observations")
    op.drop_index(op.f("ix_plate_observations_journey_id"), table_name="plate_observations")
    op.drop_index(op.f("ix_plate_observations_captured_at"), table_name="plate_observations")
    op.drop_table("plate_observations")

    op.drop_index("ix_detections_recording_time", table_name="detections")
    op.drop_index(op.f("ix_detections_tracked_object_id"), table_name="detections")
    op.drop_index(op.f("ix_detections_recording_id"), table_name="detections")
    op.drop_table("detections")

    op.drop_index("ix_tracks_recording_time", table_name="tracked_objects")
    op.drop_index(op.f("ix_tracked_objects_vehicle_id"), table_name="tracked_objects")
    op.drop_index(op.f("ix_tracked_objects_recording_id"), table_name="tracked_objects")
    op.drop_index(op.f("ix_tracked_objects_journey_id"), table_name="tracked_objects")
    op.drop_index(op.f("ix_tracked_objects_first_seen_at"), table_name="tracked_objects")
    op.drop_index(op.f("ix_tracked_objects_class_label"), table_name="tracked_objects")
    op.drop_table("tracked_objects")

    op.drop_index("ix_telemetry_recording_offset", table_name="telemetry_points")
    op.drop_index("ix_telemetry_journey_time", table_name="telemetry_points")
    op.drop_index(op.f("ix_telemetry_points_recording_id"), table_name="telemetry_points")
    op.drop_index(op.f("ix_telemetry_points_journey_id"), table_name="telemetry_points")
    op.drop_table("telemetry_points")

    op.drop_index("ix_recordings_state_started", table_name="recordings")
    op.drop_index("ix_recordings_camera_started", table_name="recordings")
    op.drop_index(op.f("ix_recordings_vehicle_count"), table_name="recordings")
    op.drop_index(op.f("ix_recordings_state"), table_name="recordings")
    op.drop_index(op.f("ix_recordings_started_at"), table_name="recordings")
    op.drop_index(op.f("ix_recordings_rel_path"), table_name="recordings")
    op.drop_index(op.f("ix_recordings_plate_count"), table_name="recordings")
    op.drop_index(op.f("ix_recordings_journey_id"), table_name="recordings")
    op.drop_index(op.f("ix_recordings_ignored"), table_name="recordings")
    op.drop_index(op.f("ix_recordings_has_gps"), table_name="recordings")
    op.drop_index(op.f("ix_recordings_fingerprint"), table_name="recordings")
    op.drop_index(op.f("ix_recordings_filename"), table_name="recordings")
    op.drop_index(op.f("ix_recordings_file_missing"), table_name="recordings")
    op.drop_index(op.f("ix_recordings_camera_id"), table_name="recordings")
    op.drop_table("recordings")

    op.drop_index(op.f("ix_vehicles_primary_plate_id"), table_name="vehicles")
    op.drop_index(op.f("ix_vehicles_class_label"), table_name="vehicles")
    op.drop_table("vehicles")

    op.drop_index(op.f("ix_plates_observation_count"), table_name="plates")
    op.drop_index(op.f("ix_plates_normalised_text"), table_name="plates")
    op.drop_index(op.f("ix_plates_last_seen_at"), table_name="plates")
    op.drop_index(op.f("ix_plates_flagged"), table_name="plates")
    op.drop_index(op.f("ix_plates_first_seen_at"), table_name="plates")
    op.drop_table("plates")

    op.drop_index("ix_journeys_span", table_name="journeys")
    op.drop_index(op.f("ix_journeys_started_at"), table_name="journeys")
    op.drop_index(op.f("ix_journeys_has_gps"), table_name="journeys")
    op.drop_table("journeys")

    op.drop_index(op.f("ix_cameras_key"), table_name="cameras")
    op.drop_table("cameras")
