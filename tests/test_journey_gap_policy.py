"""The one-minute policy measures uncovered time, including historical regrouping."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.settings_service import get_settings_service
from app.db.models import Journey, Recording, RecordingState
from app.journeys.builder import JourneyBuilder

BASE = datetime(2026, 9, 8, tzinfo=UTC)


def clip(index, start, end):
    return Recording(
        filename=f"gap-{index}.ts",
        rel_path=f"gap-{index}.ts",
        size_bytes=100,
        state=RecordingState.COMPLETED,
        started_at=BASE + timedelta(seconds=start),
        ended_at=BASE + timedelta(seconds=end),
        duration_s=end - start,
    )


@pytest.mark.parametrize("gap_seconds,expected", [(59.999, 1), (60, 1), (60.001, 2)])
async def test_default_boundary_is_strictly_more_than_one_minute(db_session, gap_seconds, expected):
    settings = get_settings_service()
    assert settings.get_nowait("journeys.gap_minutes") == 1.0
    rows = [clip(1, 0, 60), clip(2, 60 + gap_seconds, 120 + gap_seconds)]
    db_session.add_all(rows)
    await db_session.flush()
    await JourneyBuilder().rebuild(db_session)
    assert len((await db_session.execute(select(Journey))).scalars().all()) == expected


async def test_other_camera_coverage_prevents_a_false_split(db_session):
    # Front stops at 60 and resumes at 181. Rear covers through 150, so the actual
    # uncovered gap is only 31 seconds, despite the front camera's 121-second gap.
    rows = [clip(1, 0, 60), clip(2, 1, 150), clip(3, 181, 241)]
    db_session.add_all(rows)
    await db_session.flush()
    await JourneyBuilder().rebuild(db_session)
    journeys = (await db_session.execute(select(Journey))).scalars().all()
    assert len(journeys) == 1
    assert journeys[0].recording_count == 3


async def test_existing_five_minute_group_is_rebuilt_and_then_stays_stable(db_session):
    old = Journey(started_at=BASE, ended_at=BASE + timedelta(seconds=244))
    db_session.add(old)
    await db_session.flush()
    rows = [clip(1, 0, 60), clip(2, 184, 244)]
    for row in rows:
        row.journey_id = old.id
    db_session.add_all(rows)
    await db_session.flush()
    builder = JourneyBuilder()
    assert await builder.needs_recluster(db_session)
    await builder.rebuild(db_session)
    journeys = (await db_session.execute(select(Journey))).scalars().all()
    assert len(journeys) == 2
    assert sum(j.recording_count for j in journeys) == 2
    # Maintenance checks in a fresh session; rebuild intentionally uses bulk attachment
    # without synchronizing pre-existing ORM recording instances.
    db_session.expunge_all()
    assert not await builder.needs_recluster(db_session)
