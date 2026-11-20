import asyncio
from datetime import UTC, datetime

from sqlalchemy import delete, desc, select, text, update
from sqlalchemy.ext.asyncio import AsyncConnection

from app.api import heat_cells
from app.db.models import Journey, Recording, RecordingState, TelemetryPoint
from app.db.session import session_scope

START = datetime(2026, 11, 1, tzinfo=UTC)


async def _clip(session, name, journey, lat, *, published=True):
    rec = Recording(
        filename=name,
        rel_path=name,
        state=RecordingState.COMPLETED if published else RecordingState.PROCESSING,
    )
    session.add(rec)
    await session.flush()
    session.add_all(
        TelemetryPoint(
            recording_id=rec.id,
            journey_id=journey,
            t_offset_s=step,
            captured_at=timestamp,
            has_fix=True,
            lat=lat,
            lon=135.0,
            speed_kmh=speed,
            gps_quality=quality,
        )
        for step, (timestamp, speed, quality) in enumerate(
            [
                (START, 5, None),
                (START, 15, "interpolated"),
                (None, None, "valid"),
                (None, 4.5, None),
                (None, 10, "rejected"),
            ]
        )
    )
    return rec.id


async def _check(limited=100):
    async with session_scope() as session:
        raw = (
            await session.execute(
                heat_cells._aggregate().order_by(desc("weight"), "lat", "lon").limit(limited + 1)
            )
        ).all()
        actual = await heat_cells.read_rows(session, 3, None, None, None, None, 5, limited)
    assert actual == raw
    return raw


async def _refresh():
    async with session_scope() as session:
        assert await heat_cells.refresh_heat_cells(session)


async def _dirty():
    async with session_scope() as session:
        return set((await session.execute(text("SELECT lat FROM map_heat_dirty"))).scalars())


async def test_exact_weights_and_visibility_before_and_after_cell_refresh(db_session):
    async with session_scope() as session:
        trip = Journey(started_at=START, ended_at=START)
        session.add(trip)
        await session.flush()
        jid = trip.id
        front = await _clip(session, "front.ts", jid, -34.0)
        rear = await _clip(session, "rear.ts", None, -34.0)
        await _clip(session, "elsewhere.ts", jid, -35.0)
        pending = await _clip(session, "pending.ts", None, -36.0, published=False)
    assert await _dirty() == {-34, -35}
    assert (await _check())[0] == (-34, 135, 3, 40, 4)
    await _refresh()
    assert not await _dirty()
    await _check()
    async with session_scope() as session:
        await session.execute(
            update(Recording).where(Recording.id == pending).values(state=RecordingState.COMPLETED)
        )
        await session.execute(
            update(TelemetryPoint).where(TelemetryPoint.recording_id == front).values(lat=-37)
        )
    assert await _dirty() == {-34, -36, -37}
    await _check(1)
    assert len(await _check()) == 4
    await _refresh()
    await _check()
    async with session_scope() as session:
        await session.execute(
            update(Journey).where(Journey.id == jid).values(motion_json={"status": "unknown"})
        )
        await session.execute(update(Recording).where(Recording.id == rear).values(ignored=True))
        await session.execute(delete(Recording).where(Recording.id == pending))
    assert await _check() == []
    await _refresh()
    assert await _check() == []
    async with session_scope() as session:
        await session.execute(
            update(Journey).where(Journey.id == jid).values(motion_json={"status": "moving"})
        )
        await session.execute(
            update(Recording)
            .where(Recording.id == rear)
            .values(ignored=False, state=RecordingState.DELETED, processed_at=START)
        )
        await session.execute(
            update(Recording).where(Recording.id == front).values(telemetry_revision="invalidated")
        )
    assert len(await _check()) == 2
    await _refresh()
    await _check()
    async with session_scope() as session:
        point = (
            await session.scalars(
                select(TelemetryPoint).where(
                    TelemetryPoint.recording_id == rear, TelemetryPoint.t_offset_s == 1
                )
            )
        ).one()
        point.captured_at = None
        point.speed_kmh = 120
    assert await _dirty() == {-34}
    assert (await _check())[0] == (-34, 135, 3, 125, 2)
    await _refresh()
    await _check()


async def test_dirty_refresh_uses_both_grid_index_coordinates(db_session):
    connection = await db_session.connection()
    sql = heat_cells._aggregate(dirty_only=True).compile(
        dialect=connection.dialect,
        compile_kwargs={"literal_binds": True},
    )
    plan = (await connection.exec_driver_sql(f"EXPLAIN QUERY PLAN {sql}")).all()
    assert any(
        "ix_telemetry_heat_cells (has_fix=? AND <expr>=? AND <expr>=?)" in row[-1] for row in plan
    ), plan


async def test_worker_does_not_publish_when_source_changes_after_aggregation(
    db_session, monkeypatch
):
    async with session_scope() as session:
        clip = await _clip(session, "moving.ts", None, -34)
    ready, resume = asyncio.Event(), asyncio.Event()
    original = AsyncConnection.execute

    async def paused(conn, stmt, *args, **kwargs):
        if (
            str(stmt).startswith("UPDATE map_source_state SET heat_cells_ready = 1")
            and not ready.is_set()
        ):
            ready.set()
            await resume.wait()
        return await original(conn, stmt, *args, **kwargs)

    async def worker():
        async with session_scope() as session:
            return await heat_cells.refresh_heat_cells(session)

    monkeypatch.setattr(AsyncConnection, "execute", paused)
    task = asyncio.create_task(worker())
    try:
        await asyncio.wait_for(ready.wait(), timeout=5)
        async with session_scope() as session:
            await session.execute(
                update(Recording).where(Recording.id == clip).values(ignored=True)
            )
    finally:
        resume.set()
    assert not await task
    assert await _dirty() == {-34}
    assert await _check() == []
    await _refresh()
    assert not await _dirty()
