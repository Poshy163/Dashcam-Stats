"""Store the check-engine lamp and stored-DTC count per OBD sample.

Revision ID: 0019
Revises: 0018
Create Date: 2026-09-02

Poll-plan v4 (logger 0.2.8) polls mode-01 PID 0x01, the one supported PID the car has
that earlier plans never asked for. Byte A carries the MIL in bit 7 and the count of
stored codes in bits 0-6; the logger decodes them as ``mil_on`` and ``dtc_count``. Both
are nullable because every drive before v4 never captured them, and nothing about those
rows should change.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("obd_samples", sa.Column("mil_on", sa.Boolean(), nullable=True))
    op.add_column("obd_samples", sa.Column("dtc_count", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("obd_samples", "dtc_count")
    op.drop_column("obd_samples", "mil_on")
