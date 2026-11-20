import asyncio
import threading
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import update

from app.api import map_cache, route_geometry_cache
from app.api.routes import heatmap
from app.core.backup_priority import prioritize_footage_transfer
from app.db.models import Recording, RecordingState, TelemetryPoint
from app.db.session import session_scope
from app.workers import scheduler as scheduler_module
from app.workers.scheduler import Scheduler


async def test_warm_and_foreground_share_builds_and_heat_does_not_wait_for_routes(
    client, db_session, monkeypatch
):
    rec = Recording(filename="warm.ts", rel_path="warm.ts", state=RecordingState.COMPLETED)
    db_session.add(rec)
    await db_session.flush()
    for offset in range(2):
        db_session.add(
            TelemetryPoint(
                recording_id=rec.id,
                t_offset_s=offset,
                captured_at=datetime(2026, 11, 1, 0, 0, offset, tzinfo=UTC),
                has_fix=True,
                lat=-34,
                lon=137 + offset * 0.0001,
                speed_kmh=10,
            )
        )
    await db_session.commit()
    revision = await map_cache.source_revision(db_session)
    entered, resume = asyncio.Event(), asyncio.Event()
    original = heatmap._routes
    heat = AsyncMock(wraps=heatmap._heatmap)
    simplify = route_geometry_cache.simplify_journey
    simplified_on = []
    routes_calls = 0

    def measured(*args):
        simplified_on.append(threading.get_ident())
        return simplify(*args)

    async def paused(*args, **kwargs):
        nonlocal routes_calls
        routes_calls += 1
        entered.set()
        await resume.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(route_geometry_cache, "simplify_journey", measured)
    monkeypatch.setattr(heatmap, "_heatmap", heat)
    monkeypatch.setattr(heatmap, "_routes", paused)
    warm = asyncio.create_task(heatmap.warm_default_layers(revision))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        routes = asyncio.create_task(client.get("/api/map/routes"))
        response = await asyncio.wait_for(client.get("/api/map/heatmap?min_speed_kmh=5"), 3)
        assert response.status_code == 200 and response.json()["cells"] == 1
        assert not routes.done()
    finally:
        resume.set()
    assert await warm
    assert (await routes).json()["points"] == 2
    assert heat.await_count == routes_calls == 1
    assert simplified_on and threading.get_ident() not in simplified_on


async def test_warmup_coalesces_churn_and_gives_transfers_priority(db_session, monkeypatch):
    revision = AsyncMock(return_value=b"a" * 16)
    warm = AsyncMock(return_value=True)
    now = 100
    monkeypatch.setattr(map_cache, "source_revision", revision)
    monkeypatch.setattr(heatmap, "warm_default_layers", warm)
    monkeypatch.setattr(scheduler_module, "time", SimpleNamespace(monotonic=lambda: now))
    worker = Scheduler()
    async with prioritize_footage_transfer():
        await worker._run_map_warmup()
        revision.assert_not_awaited()
    await worker._run_map_warmup()
    revision.return_value = b"b" * 16
    await worker._run_map_warmup()
    warm.assert_not_awaited()
    now += 5
    await worker._run_map_warmup()
    await worker._run_map_warmup()
    warm.assert_awaited_once_with(b"b" * 16)
    revision.return_value = b"c" * 16
    now += 5
    await worker._run_map_warmup()
    await worker._run_map_warmup()
    warm.assert_awaited_once()
    now += 30
    await worker._run_map_warmup()
    assert warm.await_count == 2
    revision.return_value = b"d" * 16
    now += 35
    warm.side_effect = TimeoutError
    await worker._run_map_warmup()
    await worker._run_map_warmup()
    now += 35
    await worker._run_map_warmup()
    assert warm.await_count == 3


async def test_newer_visibility_change_aborts_route_prewarm(client, db_session, monkeypatch):
    rec = Recording(filename="hidden.ts", rel_path="hidden.ts", state=RecordingState.COMPLETED)
    db_session.add(rec)
    await db_session.flush()
    db_session.add(
        TelemetryPoint(recording_id=rec.id, t_offset_s=0, has_fix=True, lat=-34, lon=137)
    )
    await db_session.commit()
    revision = await map_cache.source_revision(db_session)
    original = heatmap._heatmap

    async def compute_then_hide(*args):
        value = await original(*args)
        async with session_scope() as writer:
            await writer.execute(
                update(Recording).where(Recording.id == rec.id).values(ignored=True)
            )
        return value

    routes = AsyncMock(wraps=heatmap._routes)
    monkeypatch.setattr(heatmap, "_heatmap", compute_then_hide)
    monkeypatch.setattr(heatmap, "_routes", routes)
    assert not await heatmap.warm_default_layers(revision)
    routes.assert_not_awaited()
    monkeypatch.setattr(heatmap, "_heatmap", original)
    response = await client.get("/api/map/heatmap?min_speed_kmh=5")
    assert response.json()["cells"] == 0
    assert response.headers["X-Map-Revision"] != revision.hex()
