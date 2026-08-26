"""Re-derive the journey tiles that were measured against rows the map does not draw.

Revision ID: 0013
Revises: 0012
Create Date: 2026-08-26

Two independent wrongs in ``journeys``, both of them derived, both fixable from rows that
are already stored. Neither needs a frame decoded.

**Distance measured straight through a break.** ``JourneyBuilder._measure`` guarded its
distance accumulation with ``getattr(row, "breaks_segment", False)`` against a ``TrackPoint``
that carried no such attribute, and the column was not in the query behind it — so the guard
returned False on every row and the walk added the straight line across every dropout. The
route layer has always declined to draw those legs, so ``JourneyDetail`` served a distance
measured one way beside a route drawn the other. Measured against the live library: for
journeys whose route is drawn in one piece the stored figure and the drawn route agree to
within 1% (that residue is polyline simplification), while for journeys cut at a break the
stored figure runs up to 52% long.

The fix for the code shipped already. This clears ``distance_m`` on exactly the journeys
that hold a break, which is the state ``repair_stale`` already looks for — ``has_gps`` with
no distance is a shape no correctly refreshed journey can be in — so the existing
maintenance pass recomputes distance, bounds, endpoints and counts from
``telemetry_points`` on its next run. Routed through ``refresh`` rather than recomputed
here because the breaks-aware walk is Python, and duplicating it in SQL is how the two
drift apart again. Self-clearing: once refreshed the distance is not null and this cannot
fire twice.

**Counts taken over members the journey will not show.** ``refresh`` selected members on the
revision columns alone while ``get_journey`` also requires ``ignored is False``, so a journey
holding a hidden recording counted a member it then declined to list. Hiding damaged footage
is routine, so the two disagreed in ordinary use. That query is fixed too, and the count is
re-derived directly below — a count needs no Python.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.pipeline.revisions import INVALIDATED_REVISION

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

#: ``Enum`` persists the member *name*; a revision column holds a plain string, so this one
#: really is the literal. Taken from the module rather than written out, for the same reason
#: 0011 takes its state name from the enum.
_INVALIDATED = INVALIDATED_REVISION

#: The membership rule, matching ``_journey_ready_recording`` plus the ``ignored`` test that
#: the read side has always applied. NULL revisions are legacy rows and remain valid.
_MEMBER = " AND ".join(
    f"(r.{col} IS NULL OR r.{col} <> :invalidated)"
    for col in ("metadata_revision", "telemetry_revision", "detection_revision", "plate_revision")
)

_CLEAR_DISTANCE = f"""
UPDATE journeys
   SET distance_m = NULL
 WHERE has_gps = 1
   AND distance_m IS NOT NULL
   AND EXISTS (
       SELECT 1
         FROM recordings r
         JOIN telemetry_points t ON t.recording_id = r.id
        WHERE r.journey_id = journeys.id
          AND r.ignored = 0
          AND {_MEMBER}
          AND t.breaks_segment = 1
   )
"""

_RECOUNT_MEMBERS = f"""
UPDATE journeys
   SET recording_count = (
       SELECT COUNT(*)
         FROM recordings r
        WHERE r.journey_id = journeys.id
          AND r.ignored = 0
          AND {_MEMBER}
   )
"""


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text(_RECOUNT_MEMBERS).bindparams(invalidated=_INVALIDATED))
    bind.execute(sa.text(_CLEAR_DISTANCE).bindparams(invalidated=_INVALIDATED))


def downgrade() -> None:
    # Nothing to restore. Both values are derived, the ones this replaced were measured
    # against rows their own page excludes, and `repair_stale` will have recomputed the
    # distances by the time anyone reads this.
    pass
