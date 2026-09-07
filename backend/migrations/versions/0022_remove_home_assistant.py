"""Retire Home Assistant delivery while preserving stored OBD history.

Revision ID: 0022
Revises: 0021
Create Date: 2026-09-07
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None

_SETTING_RENAMES = (
    ("ingest.ha_webhook_url", "ingest.webhook_url"),
    ("ingest.ha_mqtt_enabled", "ingest.mqtt_enabled"),
    ("ingest.ha_mqtt_host", "ingest.mqtt_host"),
    ("ingest.ha_mqtt_port", "ingest.mqtt_port"),
    ("ingest.ha_mqtt_user", "ingest.mqtt_user"),
    ("ingest.ha_mqtt_pass", "ingest.mqtt_pass"),
    ("ingest.ha_mqtt_base_topic", "ingest.mqtt_base_topic"),
)


def upgrade() -> None:
    connection = op.get_bind()
    for old, new in _SETTING_RENAMES:
        connection.execute(
            sa.text(
                "INSERT INTO app_settings (key, value, updated_at) "
                "SELECT :new, value, updated_at FROM app_settings WHERE key = :old "
                "ON CONFLICT(key) DO NOTHING"
            ),
            {"old": old, "new": new},
        )
        connection.execute(sa.text("DELETE FROM app_settings WHERE key = :old"), {"old": old})

    # A verified server copy is the durable terminal state. Previous delivery outcomes
    # do not alter or remove its raw samples, diagnostics, summary, or immutable bundle.
    # An interrupted delivery may have moved corrupt bytes to quarantine before it died.
    # Let standalone startup recovery inspect the filesystem instead of assuming success.
    connection.execute(
        sa.text("UPDATE obd_bundles SET state = 'validating' WHERE state = 'importing'")
    )
    connection.execute(
        sa.text(
            "UPDATE obd_bundles SET state = 'stored', last_error = NULL, "
            "failure_kind = NULL WHERE verified_at IS NOT NULL AND state IN "
            "('ready_to_import', 'imported', 'retry_wait')"
        )
    )
    connection.execute(
        sa.text(
            "UPDATE obd_bundles SET state = 'stored', last_error = NULL, "
            "failure_kind = NULL WHERE verified_at IS NOT NULL AND state = 'failed' "
            "AND failure_kind IN ('authentication', 'configuration', 'payload', "
            "'permanent', 'protocol', 'temporary', 'interrupted', 'projection')"
        )
    )

    op.drop_index("ix_obd_bundle_claim", table_name="obd_bundles")
    op.drop_index("ix_obd_bundles_imported_at", table_name="obd_bundles")
    op.drop_index("ix_obd_bundles_next_attempt_at", table_name="obd_bundles")
    with op.batch_alter_table("obd_bundles") as batch:
        batch.drop_column("ha_result")
        batch.drop_column("imported_at")
        batch.drop_column("import_started_at")
        batch.drop_column("last_http_status")
        batch.drop_column("next_attempt_at")
        batch.drop_column("attempts")


def downgrade() -> None:
    with op.batch_alter_table("obd_bundles") as batch:
        batch.add_column(sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("last_http_status", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("import_started_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("imported_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("ha_result", sa.JSON(), nullable=True))
    op.create_index("ix_obd_bundles_next_attempt_at", "obd_bundles", ["next_attempt_at"])
    op.create_index("ix_obd_bundles_imported_at", "obd_bundles", ["imported_at"])
    op.create_index(
        "ix_obd_bundle_claim",
        "obd_bundles",
        ["state", "next_attempt_at", "drive_started_at", "id"],
    )
    # A bundle created by a standalone version has never proved delivery to the retired
    # integration. Queue it for an idempotent attempt instead of inventing old success.
    op.execute("UPDATE obd_bundles SET state = 'ready_to_import' WHERE state = 'stored'")
    connection = op.get_bind()
    for old, new in reversed(_SETTING_RENAMES):
        connection.execute(
            sa.text(
                "INSERT INTO app_settings (key, value, updated_at) "
                "SELECT :old, value, updated_at FROM app_settings WHERE key = :new "
                "ON CONFLICT(key) DO NOTHING"
            ),
            {"old": old, "new": new},
        )
        connection.execute(sa.text("DELETE FROM app_settings WHERE key = :new"), {"new": new})
