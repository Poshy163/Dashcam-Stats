import asyncio
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta

from sqlalchemy import update

from app.api import journey_snapshot, map_cache
from app.api.routes import heatmap
from app.db.models import Journey, Recording, RecordingState, TelemetryPoint
from app.db.session import session_scope


async def _clip(session, name, group, start, lon, *, fixes=2, published=True):
    recording = Recording(
        filename=name,
        rel_path=name,
        journey_id=group,
        state=RecordingState.COMPLETED if published else RecordingState.PROCESSING,
    )
    session.add(recording)
    await session.flush()
    session.add_all(
        TelemetryPoint(
            recording_id=recording.id,
            journey_id=group,
            has_fix=True,
            t_offset_s=step,
            captured_at=start + timedelta(seconds=step),
            lat=-34.0,
            lon=lon + step * 0.0001,
        )
        for step in range(fixes)
    )
    return recording.id


async def test_default_reads_only_changed_journeys_across_restart(client, monkeypatch):
    start = datetime(2026, 9, 5, tzinfo=UTC)
    async with session_scope() as session:
        journeys = [Journey(started_at=start, ended_at=start) for _ in range(3)]
        session.add_all(journeys)
        await session.flush()
        first, second, singleton = [j.id for j in journeys]
        unassigned_clip = await _clip(session, "unassigned.ts", None, start, 136)
        first_clip = await _clip(session, "first.ts", first, start, 137)
        await _clip(session, "second.ts", second, start, 138)
        await _clip(session, "singleton.ts", singleton, start, 139, fixes=1)
        new = await _clip(
            session, "new.ts", second, start + timedelta(minutes=1), 139, published=False
        )

    read = heatmap._read_route_groups
    consumed = []

    async def measured(*args, **kwargs):
        async for group, geometry in read(*args, **kwargs):
            consumed.append(group)
            yield group, geometry

    monkeypatch.setattr(heatmap, "_read_route_groups", measured)
    monkeypatch.setattr(heatmap, "_JOURNEYS_PER_BATCH", 2)
    before = await client.get("/api/map/routes")
    assert before.is_success and before.json()["journeys"] == 4
    assert consumed == [None, first, second, singleton]

    map_cache.clear()  # A restarted process must be able to use the persisted groups.
    async with session_scope() as session:
        await session.execute(
            update(Recording).where(Recording.id == new).values(state=RecordingState.COMPLETED)
        )
    consumed.clear()
    after = await client.get("/api/map/routes")
    assert after.is_success and after.content != before.content
    assert consumed == [second]
    assert after.json()["segments"] == before.json()["segments"] + 1
    # Same data through the independent, unpartitioned date-filtered query.
    reference = await client.get("/api/map/routes?start=2026-09-04")
    assert reference.is_success and after.content == reference.content

    # The entire first cache window is now stale. Later windows can still be
    # reused, including the one-fix journey with no drawable line.
    async with session_scope() as session:
        await session.execute(
            update(TelemetryPoint)
            .where(TelemetryPoint.recording_id.in_([unassigned_clip, first_clip]))
            .values(lat=-35.0)
        )
    consumed.clear()
    edited = await client.get("/api/map/routes")
    assert edited.is_success and edited.content != after.content
    assert consumed == [None, first]
    assert edited.content == (await client.get("/api/map/routes?start=2026-09-04")).content


async def test_capped_first_request_leaves_later_groups_for_the_next_request(client):
    start = datetime(2026, 9, 5, tzinfo=UTC)
    async with session_scope() as session:
        for index in range(3):
            journey = Journey(started_at=start, ended_at=start)
            session.add(journey)
            await session.flush()
            await _clip(session, f"{index}.ts", journey.id, start, 136 + index)
    async with session_scope() as session:
        capped = await heatmap._routes(session, None, None, None, heatmap.DEFAULT_SIMPLIFY_M, 2)
    assert capped.truncated and capped.points == 2 and capped.journeys == 2
    full = await client.get("/api/map/routes")
    assert full.is_success
    assert full.json()["journeys"] == full.json()["segments"] == 3
    assert full.content == (await client.get("/api/map/routes?start=2026-09-04")).content


async def test_route_group_matches_the_snapshot_that_supplied_its_revision(client, monkeypatch):
    start = datetime(2026, 9, 5, tzinfo=UTC)
    async with session_scope() as session:
        recording = await _clip(session, "unassigned.ts", None, start, 138)

    started, resume = asyncio.Event(), asyncio.Event()
    read = heatmap._read_route_groups

    async def paused_read(*args, **kwargs):
        if not started.is_set():
            started.set()
            await resume.wait()
        async for item in read(*args, **kwargs):
            yield item

    monkeypatch.setattr(heatmap, "_read_route_groups", paused_read)
    request = asyncio.create_task(client.get("/api/map/routes"))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        async with session_scope() as session:
            await session.execute(
                update(TelemetryPoint)
                .where(TelemetryPoint.recording_id == recording)
                .values(lat=-35.0)
            )
    finally:
        resume.set()
    older = await request
    newer = await client.get("/api/map/routes")
    assert older.is_success and newer.is_success
    assert older.json()["lines"] == [[[-34.0, 138.0], [-34.0, 138.0001]]]
    assert newer.json()["lines"] == [[[-35.0, 138.0], [-35.0, 138.0001]]]


def test_journey_cache_keeps_empty_groups_distinct_and_recovers_damaged_rows(app_config):
    coords = [[(1.0, 2.0), (1.0, 2.0001)]]
    revisions = {None: b"x" * 16, 0: b"y" * 16, -1: b"z" * 16}
    values = {None: None, 0: [], -1: coords}
    assert not journey_snapshot.has_any(revisions)
    journey_snapshot.save({group: (token, values[group]) for group, token in revisions.items()})
    assert journey_snapshot.has_any(dict.fromkeys(range(510), b"w" * 16) | {-1: revisions[-1]})
    assert journey_snapshot.load(revisions) == values
    assert journey_snapshot.load({-1: b"a" * 16}) == {}
    path = app_config.cache_dir / "route-journeys.sqlite3"
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("UPDATE geometry SET payload=? WHERE group_key='-1'", (b"null",))
    assert journey_snapshot.load(revisions) == {None: None, 0: []}
    journey_snapshot.save({-1: (revisions[-1], coords)})
    assert journey_snapshot.load(revisions) == values


def test_unwritable_journey_cache_falls_back_with_bounded_memory(app_config, monkeypatch):
    (app_config.cache_dir / "route-journeys.sqlite3").mkdir()
    monkeypatch.setattr(journey_snapshot, "_MEMORY_BUDGET", 180)
    try:
        journey_snapshot.save({0: (b"a" * 16, []), 1: (b"b" * 16, [])})
        assert journey_snapshot.load({0: b"a" * 16, 1: b"b" * 16}) == {1: []}
        assert journey_snapshot.has_any({1: b"b" * 16})
    finally:
        journey_snapshot.clear()
