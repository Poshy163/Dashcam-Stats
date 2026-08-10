"""Clear sighting coordinates that the telemetry behind them no longer claims.

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-10

Migration 0004 cleaned ``telemetry_points`` and stopped there, which turned out to be half
the job. ``tracked_objects`` and ``plate_observations`` each keep their own copy of the
position, stamped at the moment the recording was processed, and nothing has ever gone
back for them. So a coordinate recognised as impossible and cleared at the source went on
being drawn on the map as a detection -- one sighting reported a longitude of -8.6845 on a
drive whose every other reading agreed on 138.68, and it survived the pass that was written
to remove exactly that.

Two rules, both the ones the running application now applies, so this cleans the history to
the same standard the pipeline holds new work to:

* A position that is not a place at all -- out of range, non-finite, or the camera's
  ``00.0000/00.0000`` no-fix marker -- goes regardless of context.
* A position outside the plausible radius of its own journey's centre goes, judged by the
  same median-and-physics test the journey builder uses. Nothing here knows or cares which
  part of the world the footage is from: the reference is the drive's own centre.

Only ``lat``/``lon`` are cleared. The sighting itself, its crop, its plate reading, its
confidence and its offset into the recording were never in doubt -- it is only the claim
about *where* that was wrong, and a sighting with no location is something the UI already
knows how to show.

The downgrade is a no-op: the coordinates removed were wrong, and there is nothing to put
back.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

_TABLES = ("tracked_objects", "plate_observations")


def _clear(bind, table: str, ids: list[int]) -> int:
    if not ids:
        return 0
    bind.execute(
        sa.text(
            f"UPDATE {table} SET lat = NULL, lon = NULL WHERE id IN ("  # noqa: S608 - fixed names
            + ",".join(str(int(i)) for i in ids)
            + ")"
        )
    )
    return len(ids)


def _clean() -> None:
    # Imported here rather than at module scope: Alembic imports every migration to build
    # the revision graph, and a module-level app import would make that depend on the
    # application package being importable at that moment.
    from app.osd.geo import haversine_m
    from app.osd.outliers import plausible_radius_m, robust_centre
    from app.osd.validate import is_valid_coordinate

    bind = op.get_bind()
    cleared = 0

    # -- coordinates that are not places, whatever journey they belong to ----------------
    for table in _TABLES:
        rows = bind.execute(
            sa.text(
                f"SELECT id, lat, lon FROM {table} "  # noqa: S608 - fixed names
                "WHERE lat IS NOT NULL AND lon IS NOT NULL"
            )
        ).all()
        cleared += _clear(
            bind, table, [rid for rid, lat, lon in rows if not is_valid_coordinate(lat, lon)]
        )

    # -- coordinates their own journey disagrees with -------------------------------------
    journeys = bind.execute(
        sa.text(
            "SELECT j.id, j.duration_s FROM journeys j "
            "WHERE EXISTS (SELECT 1 FROM recordings r WHERE r.journey_id = j.id)"
        )
    ).all()

    for journey_id, duration_s in journeys:
        # The centre comes from the telemetry rather than from the sightings, because 0004
        # has already cleaned the telemetry and the sightings are what is under suspicion.
        fixes = bind.execute(
            sa.text(
                "SELECT t.lat, t.lon FROM telemetry_points t "
                "JOIN recordings r ON r.id = t.recording_id "
                "WHERE r.journey_id = :jid AND t.has_fix = 1 "
                "AND t.lat IS NOT NULL AND t.lon IS NOT NULL"
            ),
            {"jid": journey_id},
        ).all()
        if len(fixes) < 3:
            continue

        centre = robust_centre([(float(lat), float(lon)) for lat, lon in fixes])
        if centre is None:
            continue
        # Doubled, matching 0004's guard: only grossly wrong coordinates are touched, so a
        # drive that genuinely covered more ground than its recorded duration suggests is
        # left alone for a human to look at.
        radius = plausible_radius_m(float(duration_s or 0.0)) * 2.0

        for table in _TABLES:
            rows = bind.execute(
                sa.text(
                    f"SELECT s.id, s.lat, s.lon FROM {table} s "  # noqa: S608 - fixed names
                    "JOIN recordings r ON r.id = s.recording_id "
                    "WHERE r.journey_id = :jid AND s.lat IS NOT NULL AND s.lon IS NOT NULL"
                ),
                {"jid": journey_id},
            ).all()
            cleared += _clear(
                bind,
                table,
                [
                    rid
                    for rid, lat, lon in rows
                    if haversine_m(centre[0], centre[1], float(lat), float(lon)) > radius
                ],
            )

    if cleared:
        from app.core.logging import get_logger

        get_logger(__name__).info("cleared impossible sighting positions", count=cleared)


def upgrade() -> None:
    try:
        _clean()
    except Exception:  # pragma: no cover - defensive
        # A cleanup pass must never stop a container from starting. The source fixes still
        # apply to everything processed from here, and the stale rows remain merely wrong
        # rather than fatal.
        from app.core.logging import get_logger

        get_logger(__name__).exception("could not clean stored sighting positions; continuing")


def downgrade() -> None:
    """Nothing to restore: the discarded coordinates were wrong."""
