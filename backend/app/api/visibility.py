"""Shared visibility rules for derived API resources.

Reanalysis keeps old rows until their replacement succeeds.  Public queries must therefore
look at the recording revision rather than at row existence; otherwise retained data looks
live while the queue is rebuilding it.
"""

from sqlalchemy import and_, or_, select

from app.db.models import Recording, RecordingState
from app.pipeline.revisions import INVALIDATED_REVISION


def visible_revision(column):
    """A finalised result that is not hidden by an active reanalysis.

    Stage revisions are committed independently so workers can release SQLite's write
    lock between expensive stages.  A revision becoming current therefore does *not* mean
    the recording's derived views are internally consistent yet: the summarise stage still
    has to rebuild its journey and rollups.  Publishing results before the recording is
    completed is what revived retained journey rows with their old membership mid-run.
    """
    return and_(
        Recording.state == RecordingState.COMPLETED,
        or_(column.is_(None), column != INVALIDATED_REVISION),
    )


def visible_journey_ids():
    """Journey ids backed by at least one fully rebuilt recording.

    ``NULL`` remains visible for databases created before analysis revisions existed.
    Missing footage is intentionally included because retention leaves an already analysed
    recording in the completed state while marking the file separately as missing.
    """
    return (
        select(Recording.journey_id)
        .where(
            Recording.journey_id.is_not(None),
            Recording.ignored.is_(False),
            visible_revision(Recording.telemetry_revision),
        )
        .distinct()
    )
