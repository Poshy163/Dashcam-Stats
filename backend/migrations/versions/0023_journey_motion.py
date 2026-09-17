"""Persist non-destructive motion validation and recheck existing journeys.

Revision ID: 0023
Revises: 0022
"""

import sqlalchemy as sa
from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("journeys", sa.Column("motion_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("journeys", "motion_json")
