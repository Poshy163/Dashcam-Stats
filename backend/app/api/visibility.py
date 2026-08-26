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
        # Hidden means hidden, on the maps too.
        #
        # `ignored` is the application's one "take this out of every view" flag -- the
        # damaged-footage policy sets it, the queue skips those recordings, and every
        # recording, journey and status query filters on it. The three map queries did not,
        # and `_hide` deliberately preserves the lifecycle state, so a recording that
        # completed and was *then* hidden kept `state == COMPLETED` and a current revision
        # and went on contributing heat, route lines and journey-detail path. Folding the
        # flag into the shared rule fixes all of them at once and stops the next query
        # forgetting it.
        Recording.ignored.is_(False),
        or_(column.is_(None), column != INVALIDATED_REVISION),
    )


def telemetry_quality_view(row: object) -> dict:
    """The quality block for one ``telemetry_points`` row, columns winning over the JSON.

    Two routes render this -- the recording's telemetry list and the overlay-reader debug
    panel -- and they had two copies of it that had already diverged. The debug panel's was
    missing three fields and, more importantly, did not apply the column override, so a row
    repaired by migration 0009 was shown there with the verdict the *old* pipeline had
    reached: the one screen built to answer "what did the reader actually see" disagreed
    with the recording page beside it about the very row a person had opened it to inspect.

    The override is the point. A repaired row carries its verdict in ``gps_quality`` /
    ``gps_reason`` only; the blob still describes what was believed at the time, which is
    precisely what was wrong.
    """
    quality = getattr(row, "quality_json", None) or {
        "source": "overlay_ocr",
        "ocr_status": "legacy",
        "time_status": "valid" if row.captured_at is not None else "unknown",
        "time_source": "overlay" if row.captured_at is not None else "unknown",
        "gps_status": "valid" if row.has_fix else "unknown",
        "gps_source": "direct" if row.has_fix else "none",
        "interpolated": False,
        "candidate_count": 1,
        "problems": ["quality unavailable until telemetry is reprocessed"],
    }
    return {
        **quality,
        "gps_quality": row.gps_quality,
        "gps_reason": row.gps_reason or quality.get("gps_reason"),
        "breaks_segment": bool(row.breaks_segment),
    }


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
            # `visible_revision` carries the ignored check now.
            visible_revision(Recording.telemetry_revision),
        )
        .distinct()
    )
