"""Hold the UniFi console credential outside app_settings.

Revision ID: 0020
Revises: 0019
Create Date: 2026-09-03

Nothing on the head unit can change its Wi-Fi band -- the firmware owns BSSID selection and
this build's non-privileged shell has no connect-network, disconnect or roam verb -- so the
only way to move it onto 5 GHz is to ask the access point to disassociate it. That needs a
credential for the console, and a credential may not live in ``app_settings``: every value
there is echoed by ``GET /api/settings``. Same reasoning, and same shape, as
``auth_credentials``.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "unifi_credentials",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("api_key", sa.String(length=512), nullable=True),
        sa.Column("username", sa.String(length=255), nullable=True),
        sa.Column("password", sa.String(length=1024), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("unifi_credentials")
