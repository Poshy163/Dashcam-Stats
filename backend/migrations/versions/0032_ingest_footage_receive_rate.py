"""Store the measured footage receiver rate for new backup runs.

Revision ID: 0032
Revises: 0031
"""

import sqlalchemy as sa
from alembic import op

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ingest_runs", sa.Column("footage_receive_mbs", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("ingest_runs", "footage_receive_mbs")
