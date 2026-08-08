"""Tighten the default OSD region to the measured overlay band.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-08

The seeded default cropped y=0.94..1.00 (1015..1080 on a 1080-line frame), but the overlay
text actually occupies y=1040..1072. Those extra 25 lines of scene above the text are the
problem: at night, headlights and lit signage inside the crop produce bright blobs that
glyph segmentation counts as characters, so the glyph indices shift and both template
learning and decoding fail. On a real library that showed up as GPS on 7 recordings out of
452, with the rest reporting success and no telemetry.

Seeding is idempotent by design, so an existing profile keeps its old region unless it is
moved here. Only rows still sitting on the old default are updated -- a region the user has
tuned is theirs.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

OLD_Y, OLD_H = 0.94, 0.06
NEW_Y, NEW_H = 0.9537, 0.0463

#: Tolerance for matching the previous default. Floats round-trip through SQLite exactly
#: here, but comparing with a window avoids a migration that silently does nothing.
EPS = 1e-6


def _move_region(from_y: float, from_h: float, to_y: float, to_h: float) -> None:
    profiles = sa.table(
        "osd_profiles",
        sa.column("region_y", sa.Float),
        sa.column("region_h", sa.Float),
    )
    op.execute(
        profiles.update()
        .where(
            sa.and_(
                profiles.c.region_y.between(from_y - EPS, from_y + EPS),
                profiles.c.region_h.between(from_h - EPS, from_h + EPS),
            )
        )
        .values(region_y=to_y, region_h=to_h)
    )


def _discard_learned_templates() -> None:
    """Templates are tied to the crop they were learned from.

    Glyph bitmaps harvested through the old, taller region do not match what the new crop
    produces, and a stale cache would survive this migration and keep decoding badly. The
    file is rebuilt automatically on the next telemetry run.
    """
    try:
        from app.config import get_config

        cached = get_config().data_dir / "osd_templates.npz"
        if cached.exists():
            cached.unlink()
    except Exception:
        # Never fail a migration over a cache file; a stale one only costs accuracy until
        # it is relearned, and the app can always be told to recalibrate.
        pass


def upgrade() -> None:
    _move_region(OLD_Y, OLD_H, NEW_Y, NEW_H)
    _discard_learned_templates()


def downgrade() -> None:
    _move_region(NEW_Y, NEW_H, OLD_Y, OLD_H)
    _discard_learned_templates()
