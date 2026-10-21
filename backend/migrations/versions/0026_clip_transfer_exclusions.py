"""Audit observed on-camera quiet-clip exclusions without inventing media rows.

Revision ID: 0026
Revises: 0025
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "clip_transfer_evaluations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("generation", sa.String(36), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("policy_enabled", sa.Boolean(), nullable=False),
        sa.Column("policy_version", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("source_id", sa.String(36), nullable=True),
        sa.Column("card_id", sa.String(64), nullable=True),
        sa.Column("manifest_generated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("manifest_sha256", sa.String(64), nullable=True),
        sa.Column("considered_count", sa.Integer(), nullable=False),
        sa.Column("considered_bytes", sa.BigInteger(), nullable=False),
        sa.Column("excluded_count", sa.Integer(), nullable=False),
        sa.Column("excluded_bytes", sa.BigInteger(), nullable=False),
    )
    op.create_table(
        "clip_transfer_exclusions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("identity_sha256", sa.String(64), nullable=False, unique=True),
        sa.Column("source_id", sa.String(36), nullable=False),
        sa.Column("card_id", sa.String(64), nullable=False),
        sa.Column("clip_id", sa.String(36), nullable=False),
        sa.Column("relative_path", sa.String(128), nullable=False),
        sa.Column("filename", sa.String(64), nullable=False),
        sa.Column("camera_key", sa.String(16), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("mtime_ms", sa.BigInteger(), nullable=False),
        sa.Column("fingerprint_json", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("policy_version", sa.String(64), nullable=False),
        sa.Column("reason", sa.String(64), nullable=False),
        sa.Column("proof_json", sa.JSON(), nullable=False),
        sa.Column("first_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observation_count", sa.Integer(), nullable=False),
        sa.Column("latest_generation", sa.String(36), nullable=False),
    )
    op.create_index(
        "ix_clip_transfer_exclusions_filename", "clip_transfer_exclusions", ["filename"]
    )
    op.create_index(
        "ix_clip_transfer_exclusions_latest_generation",
        "clip_transfer_exclusions",
        ["latest_generation"],
    )
    op.create_index(
        "ix_clip_exclusions_span", "clip_transfer_exclusions", ["started_at", "ended_at"]
    )
    op.create_index(
        "ix_clip_exclusions_observed", "clip_transfer_exclusions", ["last_observed_at", "id"]
    )


def downgrade() -> None:
    op.drop_table("clip_transfer_exclusions")
    op.drop_table("clip_transfer_evaluations")
