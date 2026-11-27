"""Keep the observed inventory and transfer accounting for each backup.

Revision ID: 0034
Revises: 0033
"""

import sqlalchemy as sa
from alembic import op

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def _applied() -> bool:
    columns = sa.inspect(op.get_bind()).get_columns("ingest_runs")
    column = next((col for col in columns if col["name"] == "accounting"), None)
    if column is None:
        return False
    if (
        not isinstance(column["type"], sa.JSON)
        or not column["nullable"]
        or column["default"] is not None
    ):
        raise RuntimeError("Existing backup accounting column has an incompatible schema")
    return True


def upgrade() -> None:
    if _applied():
        return
    op.add_column("ingest_runs", sa.Column("accounting", sa.JSON(), nullable=True))


def downgrade() -> None:
    if _applied():
        op.drop_column("ingest_runs", "accounting")
