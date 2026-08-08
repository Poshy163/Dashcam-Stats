"""Point the detection model setting at a model that exists.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-09

The registry used to name ``yolov8n`` and ``yolov8s``, fetched from release assets on this
repository that were never published. Every download 404'd, so object detection and plate
reading were unavailable on every run while the logs filled with fetch failures for a
feature that could not have worked.

Weights now come from upstream projects that publish them, and the model names changed with
them. Settings are seeded idempotently, so an existing deployment keeps whatever is already
in the row -- which after this change is a name the registry no longer knows, leaving
detection quietly switched off with only a warning to show for it. Hence this migration:
the stale value is rewritten rather than left to fail at load.

Only values belonging to the retired registry are touched. A user who has chosen a model
that still exists keeps their choice.
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

SETTING_KEY = "processing.detection_model"

#: Retired name -> closest replacement. Nano and small map to the corresponding RF-DETR
#: size so a user's speed/accuracy preference survives the move.
FORWARD = {"yolov8n": "rfdetr-nano", "yolov8s": "rfdetr-small"}
BACKWARD = {new: old for old, new in FORWARD.items()}


def _settings_table() -> sa.Table:
    """The settings table with ``value`` treated as raw text, deliberately.

    ``value`` really is a JSON column, but declaring it as :class:`sa.JSON` here does *not*
    work: SQLAlchemy applies the JSON bind processor to the assignment and not to an
    equality comparison, so the ``WHERE`` clause tests the stored text ``"yolov8n"``
    — quotes included — against the bare string ``yolov8n`` and matches nothing at all.
    The migration then completes successfully having changed no rows, which is the worst
    possible outcome: silent, and indistinguishable from having worked.

    Treating the column as text and JSON-encoding both sides makes the comparison exact.
    """
    return sa.table(
        "app_settings",
        sa.column("key", sa.String),
        sa.column("value", sa.String),
    )


def _remap(mapping: dict[str, str]) -> None:
    settings = _settings_table()
    for old, new in mapping.items():
        op.execute(
            settings.update()
            .where(
                sa.and_(
                    settings.c.key == SETTING_KEY,
                    settings.c.value == json.dumps(old),
                )
            )
            .values(value=json.dumps(new))
        )


def upgrade() -> None:
    _remap(FORWARD)


def downgrade() -> None:
    _remap(BACKWARD)
