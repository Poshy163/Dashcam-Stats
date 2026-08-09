"""Discard stored positions that cannot belong to the journey they sit in.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-09

On a real 674-recording library, 65 of 45,898 stored fixes were in the wrong place, and
those 65 put an impossible distance on 21 of 45 journeys — one reported 154,701 km in
eighteen minutes. Each corrupt coordinate also plots as a dot on the heat map somewhere in
China or the Gulf of Guinea, and drags the journey's bounds far enough that the map opens
zoomed out to most of the planet.

Three causes, all now fixed at the source:

* Whole-recording sign loss. The rear camera's minus is thin enough that segmentation can
  drop it for an entire clip, so ``N:-34.7612`` becomes ``N:34.7612`` — legal, confident,
  and 7,700 km away, with every fix in the recording agreeing.
* The no-fix placeholder read with one digit wrong. ``00.0000`` became ``00.0001``, which
  cleared an epsilon of 1e-6 and was stored as a position off West Africa.
* Gross digit loss, leaving latitudes of 2.3 or 9.2.

The source fixes only help footage processed from now on. Existing rows would stay wrong
until every recording was reprocessed, which for this library is hours of work to correct
data that can be corrected here in seconds.

Only the position is cleared. Timestamp, speed and raw text are kept, because none of them
were in doubt — the speed printed on a sign-flipped line is perfectly good, and discarding
it would trade a wrong number for a missing one.

The downgrade cannot restore the coordinates; they were wrong, and there is nothing to put
back. It is a no-op rather than a lie.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def _clean() -> None:
    # Imported here rather than at module scope: Alembic imports every migration to build
    # the revision graph, and a module-level app import would make that depend on the
    # application package being importable at that moment.
    from app.osd.outliers import (
        NO_FIX_EPSILON_FALLBACK,
        plausible_radius_m,
        robust_centre,
        spatial_outliers,
    )
    from app.osd.engine import haversine_m

    bind = op.get_bind()

    # Group by journey, because that is the smallest scope at which a wholly sign-flipped
    # recording is visible at all: within one such recording every fix agrees.
    journeys = bind.execute(
        sa.text(
            "SELECT j.id, j.duration_s FROM journeys j "
            "WHERE EXISTS (SELECT 1 FROM recordings r WHERE r.journey_id = j.id)"
        )
    ).all()

    cleared = 0
    for journey_id, duration_s in journeys:
        rows = bind.execute(
            sa.text(
                "SELECT t.id, t.lat, t.lon FROM telemetry_points t "
                "JOIN recordings r ON r.id = t.recording_id "
                "WHERE r.journey_id = :jid AND t.has_fix = 1 "
                "AND t.lat IS NOT NULL AND t.lon IS NOT NULL"
            ),
            {"jid": journey_id},
        ).all()
        if len(rows) < 3:
            continue

        points = [(float(lat), float(lon)) for _, lat, lon in rows]
        bad = spatial_outliers(points, span_s=float(duration_s or 0.0))
        if not bad:
            continue

        centre = robust_centre([p for i, p in enumerate(points) if i not in bad])
        radius = plausible_radius_m(float(duration_s or 0.0))
        if centre is not None:
            # Guard against clearing a real drive that simply moved further than its own
            # recorded duration suggests: if a supposed outlier is barely outside the
            # radius, leave it. Only coordinates that are grossly wrong are touched.
            bad = {
                i
                for i in bad
                if haversine_m(centre[0], centre[1], points[i][0], points[i][1]) > radius * 2.0
            }
        if not bad:
            continue

        ids = [rows[i][0] for i in bad]
        bind.execute(
            sa.text(
                "UPDATE telemetry_points SET has_fix = 0, lat = NULL, lon = NULL, "
                "heading_deg = NULL WHERE id IN ("
                + ",".join(str(int(i)) for i in ids)
                + ")"
            )
        )
        cleared += len(ids)

    # The near-miss placeholder is independent of any journey: it is recognisable on its
    # own, and rows belonging to no journey would otherwise never be examined.
    bind.execute(
        sa.text(
            "UPDATE telemetry_points SET has_fix = 0, lat = NULL, lon = NULL, "
            "heading_deg = NULL "
            "WHERE has_fix = 1 AND lat IS NOT NULL AND lon IS NOT NULL "
            "AND ABS(lat) < :eps AND ABS(lon) < :eps"
        ),
        {"eps": NO_FIX_EPSILON_FALLBACK},
    )

    # Journey rollups were computed from the coordinates just discarded, so every one of
    # them is now stale. Null them rather than recomputing here: the builder owns that
    # logic, it runs on the next scan or process, and a migration reimplementing it would
    # be a second copy free to drift from the first.
    bind.execute(
        sa.text(
            "UPDATE journeys SET distance_m = NULL, min_lat = NULL, max_lat = NULL, "
            "min_lon = NULL, max_lon = NULL, start_lat = NULL, start_lon = NULL, "
            "end_lat = NULL, end_lon = NULL"
        )
    )


def upgrade() -> None:
    try:
        _clean()
    except Exception:  # pragma: no cover - defensive
        # A cleanup pass must never stop a container from starting. The source fixes still
        # apply to everything processed from here, and the stale rows remain merely wrong
        # rather than fatal.
        from app.core.logging import get_logger

        get_logger(__name__).exception("could not clean stored positions; continuing")


def downgrade() -> None:
    """Nothing to restore: the discarded coordinates were wrong."""
