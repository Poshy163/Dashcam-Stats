"""Bounded historical checks using stored telemetry; no video decoding or deletion."""

from sqlalchemy import func, or_, select

from app.core.logging import get_logger
from app.db.models import Journey, Recording, RecordingState
from app.db.session import session_scope
from app.journeys.builder import JourneyBuilder
from app.journeys.motion import MOTION_REVISION

log = get_logger(__name__)


def assessable_journeys():
    return Journey.id.in_(
        select(Recording.journey_id).where(
            Recording.ignored.is_(False),
            Recording.state == RecordingState.COMPLETED,
            Recording.journey_id.is_not(None),
        )
    )


def outdated_motion():
    revision = Journey.motion_json["revision"].as_string()
    return or_(revision.is_(None), revision != MOTION_REVISION)


async def revalidate_motion(*, limit: int = 10) -> int:
    checked = 0
    async with session_scope() as session:
        ids = list(
            (
                await session.scalars(
                    select(Journey.id)
                    .where(assessable_journeys(), outdated_motion())
                    .order_by(Journey.started_at.desc(), Journey.id.desc())
                    .limit(max(0, limit))
                )
            ).all()
        )
    # Commit each journey independently: do not hold SQLite's writer for a whole library.
    # A restart resumes from the persisted revision, including sessions with no GPS.
    # A failing row remains pending for retry, but cannot starve the rest of this batch.
    for journey_id in ids:
        try:
            async with session_scope() as session:
                journey = await session.scalar(
                    select(Journey).where(Journey.id == journey_id, outdated_motion())
                )
                if journey is None:
                    continue
                await JourneyBuilder().refresh(session, journey)
            checked += 1
        except Exception as exc:
            log.warning(
                "journey movement check failed; will retry",
                journey_id=journey_id,
                error_type=type(exc).__name__,
            )
    if checked:
        log.info(
            "revalidated historical journey movement", journeys=checked, revision=MOTION_REVISION
        )
    return checked


async def motion_coverage(session) -> dict:
    groups = (
        await session.execute(
            select(
                Journey.motion_json["revision"].as_string(),
                Journey.motion_json["status"].as_string(),
                func.count(Journey.id),
            )
            .where(assessable_journeys())
            .group_by(
                Journey.motion_json["revision"].as_string(),
                Journey.motion_json["status"].as_string(),
            )
        )
    ).all()
    result = {
        "revision": MOTION_REVISION,
        "total": 0,
        "pending": 0,
        "moving": 0,
        "unconfirmed": 0,
        "unknown": 0,
    }
    for revision, status, count in groups:
        result["total"] += count
        if revision != MOTION_REVISION:
            result["pending"] += count
        elif status in {"moving", "unconfirmed", "unknown"}:
            result[status] += count
    return result
