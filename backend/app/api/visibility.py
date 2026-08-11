"""Shared visibility rules for derived API resources.

Reanalysis keeps old rows until their replacement succeeds.  Public queries must therefore
look at the recording revision rather than at row existence; otherwise retained data looks
live while the queue is rebuilding it.
"""

from sqlalchemy import or_, select

from app.db.models import Recording
from app.pipeline.revisions import INVALIDATED_REVISION


def visible_journey_ids():
    """Journey ids backed by at least one non-invalidated recording.

    ``NULL`` remains visible for databases created before analysis revisions existed.
    Missing footage is intentionally included because retention preserves its history.
    """
    return (
        select(Recording.journey_id)
        .where(
            Recording.journey_id.is_not(None),
            Recording.ignored.is_(False),
            or_(
                Recording.telemetry_revision.is_(None),
                Recording.telemetry_revision != INVALIDATED_REVISION,
            ),
        )
        .distinct()
    )
