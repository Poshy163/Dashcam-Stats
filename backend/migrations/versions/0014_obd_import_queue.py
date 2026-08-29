"""Durable OBD bundle, drive history and Home Assistant import queue.

Revision ID: 0014
Revises: 0013
Create Date: 2026-08-29

The OBD queue is intentionally not a kind of ``processing_job``.  Footage analysis may
be paused, reset or rebuilt; an acknowledged OBD export has a different lifecycle and
must keep retrying independently while Home Assistant is unavailable.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "obd_bundles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("drive_id", sa.String(length=64), nullable=False),
        sa.Column("bundle_hash", sa.String(length=64), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False, unique=True),
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("vehicle_id", sa.String(length=128), nullable=False),
        sa.Column("adapter_id", sa.String(length=128), nullable=True),
        sa.Column("logger_id", sa.String(length=128), nullable=False),
        sa.Column("logger_version", sa.String(length=64), nullable=False),
        sa.Column("drive_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("drive_finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("diagnostic_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("metadata_trusted", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "state", sa.String(length=32), nullable=False, server_default="waiting_for_backup"
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("failure_kind", sa.String(length=32), nullable=True),
        sa.Column("last_http_status", sa.Integer(), nullable=True),
        sa.Column("copied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("import_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("remote_deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duplicate", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("ha_result", sa.JSON(), nullable=True),
        sa.Column("validation_warnings", sa.JSON(), nullable=True),
        sa.UniqueConstraint(
            "drive_id", "bundle_hash", "schema_version", name="uq_obd_bundle_identity"
        ),
    )
    op.create_index("ix_obd_bundles_drive_id", "obd_bundles", ["drive_id"])
    op.create_index("ix_obd_bundles_bundle_hash", "obd_bundles", ["bundle_hash"])
    op.create_index("ix_obd_bundles_vehicle_id", "obd_bundles", ["vehicle_id"])
    op.create_index("ix_obd_bundles_drive_started_at", "obd_bundles", ["drive_started_at"])
    op.create_index("ix_obd_bundles_state", "obd_bundles", ["state"])
    op.create_index("ix_obd_bundles_next_attempt_at", "obd_bundles", ["next_attempt_at"])
    op.create_index("ix_obd_bundles_failure_kind", "obd_bundles", ["failure_kind"])
    op.create_index("ix_obd_bundles_metadata_trusted", "obd_bundles", ["metadata_trusted"])
    op.create_index("ix_obd_bundles_imported_at", "obd_bundles", ["imported_at"])
    op.create_index(
        "ix_obd_bundle_claim",
        "obd_bundles",
        ["state", "next_attempt_at", "drive_started_at", "id"],
    )

    op.create_table(
        "obd_drives",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "bundle_id",
            sa.Integer(),
            sa.ForeignKey("obd_bundles.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("drive_id", sa.String(length=64), nullable=False, unique=True),
        sa.Column("vehicle_id", sa.String(length=128), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("original_timezone", sa.String(length=128), nullable=True),
        sa.Column("start_reason", sa.String(length=128), nullable=True),
        sa.Column("stop_reason", sa.String(length=128), nullable=True),
        sa.Column("obd_protocol", sa.String(length=256), nullable=True),
        sa.Column("completion_status", sa.String(length=32), nullable=False),
        sa.Column("clean_end", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("duration_s", sa.Float(), nullable=True),
        sa.Column("distance_km", sa.Float(), nullable=True),
        sa.Column("average_speed_kmh", sa.Float(), nullable=True),
        sa.Column("maximum_speed_kmh", sa.Float(), nullable=True),
        sa.Column("average_rpm", sa.Float(), nullable=True),
        sa.Column("maximum_rpm", sa.Float(), nullable=True),
        sa.Column("idle_duration_s", sa.Float(), nullable=True),
        sa.Column("estimated_fuel_used_l", sa.Float(), nullable=True),
        sa.Column("average_fuel_consumption_l_100km", sa.Float(), nullable=True),
        sa.Column("maximum_coolant_temperature_c", sa.Float(), nullable=True),
        sa.Column("maximum_engine_load_pct", sa.Float(), nullable=True),
        sa.Column("missing_data_duration_s", sa.Float(), nullable=True),
        sa.Column("expected_sample_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("received_sample_percentage", sa.Float(), nullable=True),
        sa.Column("sample_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("dtcs_observed", sa.JSON(), nullable=True),
        sa.Column("units", sa.JSON(), nullable=False),
        sa.Column("manifest_json", sa.JSON(), nullable=False),
        sa.Column("summary_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_obd_drives_bundle_id", "obd_drives", ["bundle_id"])
    op.create_index("ix_obd_drives_drive_id", "obd_drives", ["drive_id"])
    op.create_index("ix_obd_drives_vehicle_id", "obd_drives", ["vehicle_id"])
    op.create_index("ix_obd_drives_started_at", "obd_drives", ["started_at"])

    op.create_table(
        "obd_samples",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "drive_db_id",
            sa.Integer(),
            sa.ForeignKey("obd_drives.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sample_id", sa.String(length=96), nullable=False, unique=True),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ecu_data_status", sa.String(length=32), nullable=False),
        sa.Column("engine_rpm", sa.Float(), nullable=True),
        sa.Column("vehicle_speed_kmh", sa.Float(), nullable=True),
        sa.Column("coolant_temperature_c", sa.Float(), nullable=True),
        sa.Column("intake_air_temperature_c", sa.Float(), nullable=True),
        sa.Column("engine_load_pct", sa.Float(), nullable=True),
        sa.Column("throttle_position_pct", sa.Float(), nullable=True),
        sa.Column("timing_advance_deg", sa.Float(), nullable=True),
        sa.Column("mass_air_flow_g_s", sa.Float(), nullable=True),
        sa.Column("short_term_fuel_trim_bank_1_pct", sa.Float(), nullable=True),
        sa.Column("long_term_fuel_trim_bank_1_pct", sa.Float(), nullable=True),
        sa.Column("fuel_system_status", sa.String(length=128), nullable=True),
        sa.Column("oxygen_sensors_present", sa.JSON(), nullable=True),
        sa.Column("obd_standard", sa.String(length=128), nullable=True),
        sa.Column("distance_with_mil_km", sa.Float(), nullable=True),
        sa.Column("oxygen_sensor_1_voltage_v", sa.Float(), nullable=True),
        sa.Column("oxygen_sensor_1_short_term_fuel_trim_pct", sa.Float(), nullable=True),
        sa.Column("oxygen_sensor_2_voltage_v", sa.Float(), nullable=True),
        sa.Column("oxygen_sensor_2_short_term_fuel_trim_pct", sa.Float(), nullable=True),
        sa.Column("adapter_voltage_v", sa.Float(), nullable=True),
        sa.Column("estimated_fuel_rate_l_h", sa.Float(), nullable=True),
        sa.Column("estimated_fuel_consumption_l_100km", sa.Float(), nullable=True),
        sa.Column("quality_json", sa.JSON(), nullable=True),
        sa.Column("raw_json", sa.JSON(), nullable=False),
        sa.UniqueConstraint("drive_db_id", "sequence", name="uq_obd_sample_drive_sequence"),
    )
    op.create_index("ix_obd_samples_drive_db_id", "obd_samples", ["drive_db_id"])
    op.create_index("ix_obd_samples_captured_at", "obd_samples", ["captured_at"])
    op.create_index("ix_obd_samples_drive_time", "obd_samples", ["drive_db_id", "captured_at"])

    op.create_table(
        "obd_diagnostics",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "drive_db_id",
            sa.Integer(),
            sa.ForeignKey("obd_drives.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_hash", sa.String(length=64), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.UniqueConstraint("drive_db_id", "event_hash", name="uq_obd_diagnostic_event"),
    )
    op.create_index("ix_obd_diagnostics_drive_db_id", "obd_diagnostics", ["drive_db_id"])
    op.create_index("ix_obd_diagnostics_observed_at", "obd_diagnostics", ["observed_at"])
    op.create_index("ix_obd_diagnostics_kind", "obd_diagnostics", ["kind"])


def downgrade() -> None:
    op.drop_table("obd_diagnostics")
    op.drop_table("obd_samples")
    op.drop_table("obd_drives")
    op.drop_table("obd_bundles")
