"""Retain source-bound, anonymous CarPlay milestones."""

import sqlalchemy as sa
from alembic import op

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None


_TABLE = "obd_carplay_observations"


def _table_already_applied() -> bool:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(_TABLE):
        return False
    columns = inspector.get_columns(_TABLE)
    expected = [
        ("source_id_hash", sa.String, 64, False),
        ("occurred_at", sa.DateTime, None, False),
        ("kind", sa.String, 32, False),
        ("duration_ms", sa.Integer, None, True),
    ]
    primary = inspector.get_pk_constraint(_TABLE)["constrained_columns"]
    matches = len(columns) == len(expected) and all(
        c["name"] == name
        and isinstance(c["type"], type_)
        and getattr(c["type"], "length", None) == length
        and c["nullable"] == nullable
        for c, (name, type_, length, nullable) in zip(columns, expected, strict=True)
    )
    if not matches or primary != ["source_id_hash", "occurred_at", "kind"]:
        raise RuntimeError("Existing CarPlay observation table has an incompatible schema")
    return True


def upgrade() -> None:
    if _table_already_applied():
        return
    op.create_table(
        _TABLE,
        sa.Column("source_id_hash", sa.String(64), primary_key=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("kind", sa.String(32), primary_key=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    if _table_already_applied():
        op.drop_table(_TABLE)
