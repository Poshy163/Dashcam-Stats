"""Where this vehicle actually spends its time, aggregated for a map.

The obvious implementation — return every stored fix and let the browser pile them up —
does not survive contact with a real library. One second of footage is one telemetry point,
so a few days of driving is already tens of thousands of coordinates, and a year of it is
millions. That payload grows without bound, and the browser ends up doing the aggregation
anyway, badly.

So the grouping happens in SQL. Coordinates are rounded onto a grid and counted, which
turns "every fix ever recorded" into "how many seconds were spent in each cell" — the
quantity a heat map is actually drawing. The result is bounded by the *area driven* rather
than the *time spent driving*, which is the property that makes this scale: a commute
repeated two hundred times is the same number of cells as one commute, only hotter.

Rounding is honest about the source data. The overlay prints four decimal places, roughly
11 m, so nothing finer than that exists to show.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import aclosing
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query, Response
from sqlalchemy import (
    Select,
    String,
    func,
    or_,
    select,
    text,
    type_coerce,
)
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from app.api import heat_cells, journey_snapshot, map_cache, route_geometry_cache
from app.api.deps import SQLITE_MAX_INT, SessionDep
from app.api.schemas import HeatmapOut, RoutesOut
from app.api.visibility import journey_has_visible_recording, visible_revision
from app.core.backup_priority import backup_priority_active
from app.core.logging import get_logger
from app.db.models import Camera, CameraRole, Journey, Recording, TelemetryPoint
from app.db.session import session_scope

log = get_logger(__name__)

router = APIRouter(prefix="/api", tags=["map"])

#: Grid resolution in decimal places, and what each is good for.
#:
#: 2 (~1.1 km) suits a whole-state view, 3 (~110 m) a city, 4 (~11 m) a single street and
#: the limit of what the overlay actually reports. Anything beyond 4 would invent precision
#: the source does not have.
MIN_PRECISION = 1
MAX_PRECISION = 4
DEFAULT_PRECISION = 3

#: Ceiling on returned cells. Reached only by a library covering a genuinely large area at
#: fine precision; the response says when it bites rather than quietly returning a partial
#: picture that looks complete.
MAX_CELLS = 20_000

#: Default Douglas-Peucker tolerance for the route overlay, in metres.
#:
#: Coarser than the journey view's 5 m because the two are answering different questions.
#: One drive at full zoom deserves every bend; an overlay of every drive ever taken is read
#: at city scale, where 15 m is well under a pixel and only costs payload.
DEFAULT_SIMPLIFY_M = 15.0

#: Ceiling on coordinates returned for the overlay. Past this the browser spends longer
#: drawing than the map is worth, so the response says it was cut rather than quietly
#: handing back a partial road network that looks complete.
MAX_ROUTE_POINTS = 250_000
_DEFAULT_HEAT = ("heatmap", DEFAULT_PRECISION, None, None, None, None, 5.0, MAX_CELLS)
_DEFAULT_ROUTES = ("routes", None, None, None, DEFAULT_SIMPLIFY_M, MAX_ROUTE_POINTS)

#: Rows fetched per round trip while streaming the route query. Large enough that the
#: per-batch overhead is invisible, small enough that the buffer stays a rounding error
#: next to the journey being assembled.
_ROUTE_FETCH_BATCH = 2_000
_JOURNEYS_PER_BATCH = 128
_JOURNEY_REVISIONS = text("""
    SELECT NULL AS journey_id, unassigned_route_revision AS revision
    FROM map_source_state WHERE id = 1
    UNION ALL
    SELECT id AS journey_id, route_revision AS revision FROM journeys
    ORDER BY journey_id
""")


@router.get("/map/heatmap", response_model=HeatmapOut)
async def heatmap(
    session: SessionDep,
    response: Response,
    precision: int = Query(
        DEFAULT_PRECISION,
        ge=MIN_PRECISION,
        le=MAX_PRECISION,
        description="Grid resolution in decimal degrees places: 2≈1.1 km, 3≈110 m, 4≈11 m.",
    ),
    start: datetime | None = Query(None, description="Only fixes at or after this instant."),
    end: datetime | None = Query(None, description="Only fixes at or before this instant."),
    journey_id: Annotated[
        int | None, Query(ge=1, le=SQLITE_MAX_INT, description="Restrict to one journey.")
    ] = None,
    camera: str | None = Query(None, description="Restrict to one camera key."),
    min_speed_kmh: float = Query(
        0.0,
        ge=0.0,
        description=(
            "Ignore fixes slower than this. Raising it above zero turns a dwell map into a "
            "route map: the overlay samples once a second whether or not the car is moving, "
            "so twenty minutes parked contributes a thousand-odd fixes to a single cell."
        ),
    ),
    limit: int = Query(MAX_CELLS, ge=1, le=MAX_CELLS),
):
    """Grid cells with a visit count, ready to be drawn as a heat layer."""
    key = ("heatmap", precision, start, end, journey_id, camera, min_speed_kmh, limit)
    default_view = key == _DEFAULT_HEAT
    return await map_cache.cached(
        session,
        key,
        lambda: _heatmap(session, precision, start, end, journey_id, camera, min_speed_kmh, limit),
        snapshot=HeatmapOut if default_view else None,
        response_headers=response.headers,
    )


async def _heatmap(
    session: AsyncSession,
    precision: int,
    start: datetime | None,
    end: datetime | None,
    journey_id: int | None,
    camera: str | None,
    min_speed_kmh: float,
    limit: int,
) -> HeatmapOut:
    rows = await heat_cells.read_rows(
        session, precision, start, end, journey_id, camera, min_speed_kmh, limit
    )
    return await asyncio.to_thread(_heatmap_response, rows, precision, limit)


def _heatmap_response(rows, precision: int, limit: int) -> HeatmapOut:
    truncated = len(rows) > limit
    rows = rows[:limit]

    points = [
        [
            round(float(row.lat), MAX_PRECISION),
            round(float(row.lon), MAX_PRECISION),
            int(row.weight),
        ]
        for row in rows
    ]
    weights = [p[2] for p in points]
    # Speed is averaged over fixes, not over cells, so the answer does not move when the
    # grid does.
    speed_sum = sum(float(row.speed_sum) for row in rows if row.speed_sum is not None)
    speed_count = sum(int(row.speed_count) for row in rows if row.speed_count)

    return HeatmapOut(
        points=points,
        precision=precision,
        cells=len(points),
        # The heaviest cell sets the scale the client normalises against. Sending it saves
        # the client a pass over the data and keeps the colour ramp stable when the caller
        # pages or filters.
        max_weight=max(weights) if weights else 0,
        total_points=sum(weights),
        average_speed_kmh=round(speed_sum / speed_count, 1) if speed_count else None,
        truncated=truncated,
    )


@router.get("/map/routes", response_model=RoutesOut)
async def routes(
    session: SessionDep,
    response: Response,
    start: datetime | None = Query(None, description="Only fixes at or after this instant."),
    end: datetime | None = Query(None, description="Only fixes at or before this instant."),
    journey_id: Annotated[
        int | None, Query(ge=1, le=SQLITE_MAX_INT, description="Restrict to one journey.")
    ] = None,
    simplify_m: float = Query(
        DEFAULT_SIMPLIFY_M,
        ge=0.0,
        le=200.0,
        description=(
            "Douglas-Peucker tolerance in metres. Larger sends less and looks identical "
            "until you zoom past it."
        ),
    ),
    max_points: int = Query(MAX_ROUTE_POINTS, ge=1_000, le=MAX_ROUTE_POINTS),
):
    """Every drive as drawable lines, for tracing the roads under the heat.

    The heat map answers "how much time here"; it deliberately blurs, so at any useful zoom
    a road and the car park beside it are one warm smudge. These are the paths themselves,
    and the two are complementary rather than alternatives — drawn together, the lines say
    which roads and the heat says how often.

    Front camera only by default. Both cameras record the same drive and both burn the same
    overlay so including each would draw every road twice with a metre or two of OCR
    disagreement between the copies — visible as a doubled line, and twice the payload for
    no extra information.
    """
    key = ("routes", start, end, journey_id, simplify_m, max_points)
    default_view = key == _DEFAULT_ROUTES
    return await map_cache.cached(
        session,
        key,
        lambda: _routes(session, start, end, journey_id, simplify_m, max_points),
        snapshot=RoutesOut if default_view else None,
        response_headers=response.headers,
    )


def _route_query(
    start: datetime | None, end: datetime | None, journey_id: int | None, *, incremental: bool
) -> Select:
    front_recordings = (
        select(Recording.id)
        .outerjoin(Camera, Recording.camera_id == Camera.id)
        .where(
            visible_revision(Recording.telemetry_revision),
            (Camera.role == CameraRole.FRONT) | (Camera.id.is_(None)),
        )
    )
    has_fix = TelemetryPoint.has_fix.is_(True)
    if incremental:
        # Seek by journey_id in ix_telemetry_journey_time, then check the recording
        # by its primary key. Otherwise SQLite may start from every front recording
        # or every has_fix entry in ix_telemetry_map, even for one changed journey.
        has_fix = func.likely(has_fix)
        visible_recording = front_recordings.where(
            Recording.id == TelemetryPoint.recording_id
        ).exists()
    else:
        visible_recording = TelemetryPoint.recording_id.in_(front_recordings)
    stmt = (
        select(
            TelemetryPoint.journey_id,
            TelemetryPoint.lat,
            TelemetryPoint.lon,
            type_coerce(TelemetryPoint.captured_at, String),
            TelemetryPoint.recording_id,
            TelemetryPoint.breaks_segment,
        )
        .where(
            has_fix,
            visible_recording,
            TelemetryPoint.lat.is_not(None),
            TelemetryPoint.lon.is_not(None),
            # Rejected positions carry no coordinate, so the nulls above already exclude
            # them. Naming the quality as well keeps the intent legible and covers a row
            # that was cleared by one route and not the other.
            heat_cells.drawable_quality(),
            or_(
                TelemetryPoint.journey_id.is_(None),
                TelemetryPoint.journey_id.not_in(
                    select(Journey.id).where(
                        Journey.motion_json["status"].as_string().in_(["unconfirmed", "unknown"])
                    )
                ),
            ),
        )
        .order_by(
            TelemetryPoint.journey_id.asc(),
            TelemetryPoint.captured_at.asc(),
            TelemetryPoint.recording_id.asc(),
            TelemetryPoint.t_offset_s.asc(),
        )
    )
    if start is not None:
        stmt = stmt.where(TelemetryPoint.captured_at >= start)
    if end is not None:
        stmt = stmt.where(TelemetryPoint.captured_at <= end)
    if journey_id is not None:
        stmt = stmt.where(TelemetryPoint.journey_id == journey_id)
    return stmt


async def _read_route_groups(
    connection: AsyncConnection, queries: list[Select], tolerance_m: float
) -> AsyncGenerator[tuple[int | None, route_geometry_cache.Lines], None]:
    """Stream the ordered rows and simplify one complete journey at a time."""
    current: int | None = None
    fixes: list[route_geometry_cache.RouteFix] = []
    for stmt in queries:
        result = await connection.stream(stmt.execution_options(yield_per=_ROUTE_FETCH_BATCH))
        try:
            async for batch in result.partitions(_ROUTE_FETCH_BATCH):
                for row_journey, lat, lon, captured_at, recording_id, breaks_segment in batch:
                    if fixes and row_journey != current:
                        yield (
                            current,
                            await asyncio.to_thread(
                                route_geometry_cache.simplify_journey, fixes, tolerance_m
                            ),
                        )
                        fixes = []
                    current = row_journey
                    fixes.append((lat, lon, captured_at, recording_id, breaks_segment))
        finally:
            await result.close()
    if fixes:
        yield (
            current,
            await asyncio.to_thread(route_geometry_cache.simplify_journey, fixes, tolerance_m),
        )


async def _incremental_route_groups(
    connection: AsyncConnection, tolerance_m: float
) -> AsyncGenerator[tuple[int | None, route_geometry_cache.Lines], None]:
    # In SQLite legacy transaction mode consecutive SELECTs can see different commits.
    # A read-only savepoint pins revisions and rows to one snapshot, even if this caller
    # already has a transaction. Workers can continue publishing in WAL mode.
    async with connection.begin_nested():
        revisions = (await connection.execute(_JOURNEY_REVISIONS)).all()
        all_fixes = _route_query(None, None, None, incremental=False)
        if (
            not revisions
            or revisions[0][0] is not None
            or any(not isinstance(token, bytes) or len(token) != 16 for _, token in revisions)
        ):
            # A damaged revision cannot identify saved geometry. Draw directly from the fixes.
            async with aclosing(_read_route_groups(connection, [all_fixes], tolerance_m)) as raw:
                async for group in raw:
                    yield group
            return

        batch_size = _JOURNEYS_PER_BATCH
        first = await asyncio.to_thread(journey_snapshot.load, dict(revisions[:batch_size]))
        cold = not first and not await asyncio.to_thread(
            journey_snapshot.has_any, dict(revisions[batch_size:])
        )
        if cold:
            # One sequential pass with the original covering index is much cheaper than
            # hundreds of journey-index seeks on an empty cache. Seed as the cursor advances.
            batch_size = len(revisions)
        changed_fixes = _route_query(None, None, None, incremental=True)
        for offset in range(0, len(revisions), batch_size):
            window = dict(revisions[offset : offset + batch_size])
            cached = (
                first if offset == 0 else await asyncio.to_thread(journey_snapshot.load, window)
            )
            missing = window.keys() - cached.keys()
            queries = []
            if cold:
                queries.append(all_fixes)
            else:
                # Combining NULL and IN with OR can make SQLite scan every fix twice.
                # NULL sorts first; use the covering recording index for that single group.
                if None in missing:
                    queries.append(all_fixes.where(TelemetryPoint.journey_id.is_(None)))
                assigned = missing - {None}
                if assigned:
                    queries.append(changed_fixes.where(TelemetryPoint.journey_id.in_(assigned)))
            # The raw cursor skips every cache hit. Keep a single lookahead to merge
            # its output with cached geometry in the original order. A missing key
            # might have no visible fixes at all, e.g. a rear-camera-only journey.
            changed = {}
            async with aclosing(_read_route_groups(connection, queries, tolerance_m)) as raw:
                try:
                    ahead = None
                    for group, revision in window.items():
                        if group in cached:
                            lines = cached[group]
                        else:
                            if ahead is None:
                                ahead = await anext(raw, None)
                            lines = None
                            if ahead is not None and ahead[0] == group:
                                lines = ahead[1]
                                ahead = None
                            changed[group] = revision, lines
                            if len(changed) == _JOURNEYS_PER_BATCH:
                                await asyncio.to_thread(journey_snapshot.save, changed)
                                changed.clear()
                        if lines is not None:
                            yield group, lines
                finally:
                    if changed:
                        await asyncio.to_thread(journey_snapshot.save, changed)


async def _routes(
    session: AsyncSession,
    start: datetime | None,
    end: datetime | None,
    journey_id: int | None,
    simplify_m: float,
    max_points: int,
    *,
    background: bool = False,
) -> RoutesOut:
    if session.autoflush:
        await session.flush()
    connection = await session.connection()
    incremental = (
        connection.dialect.name == "sqlite"
        and start is None
        and end is None
        and journey_id is None
        and simplify_m == DEFAULT_SIMPLIFY_M
    )
    groups = (
        _incremental_route_groups(connection, simplify_m)
        if incremental
        else _read_route_groups(
            connection, [_route_query(start, end, journey_id, incremental=False)], simplify_m
        )
    )
    lines: route_geometry_cache.Lines = []
    points_sent = 0
    truncated = False
    journeys = 0
    async with aclosing(groups):
        async for _, segments in groups:
            if background and backup_priority_active():
                raise _WarmupDeferred
            # A journey with even one drawable fix counts, including one whose
            # first line exceeds the cap. Never return a fraction of a segment.
            journeys += 1
            for segment in segments:
                if points_sent + len(segment) > max_points:
                    truncated = True
                    break
                lines.append(segment)
                points_sent += len(segment)
            if truncated:
                break

    return await asyncio.to_thread(
        _routes_response, lines, journeys, points_sent, simplify_m, truncated
    )


def _routes_response(
    lines, journeys: int, points_sent: int, simplify_m: float, truncated: bool
) -> RoutesOut:
    return RoutesOut(
        lines=[[[round(lat, 4), round(lon, 4)] for lat, lon in segment] for segment in lines],
        journeys=journeys,
        segments=len(lines),
        points=points_sent,
        simplify_m=simplify_m,
        truncated=truncated,
    )


class _WarmupDeferred(Exception):
    """The transfer or a newer map revision takes priority over speculative work."""


async def warm_default_layers(revision: bytes) -> bool:
    """Prepare the same authenticated-view data HTTP requests share, heat first."""
    if backup_priority_active():
        return False
    # Publish pending cell changes first. A read-only heat request can overlay
    # them live, but a background pass would then aggregate the same cells twice.
    async with session_scope() as session:
        if await map_cache.source_revision(session) != revision:
            return False
        if not await heat_cells.refresh_heat_cells(session):
            return False
    for key, model in ((_DEFAULT_HEAT, HeatmapOut), (_DEFAULT_ROUTES, RoutesOut)):
        if backup_priority_active():
            return False
        async with session_scope() as session:
            # cached() serializes each layer separately, including foreground reads.
            # Check after acquiring its lock to avoid building for an obsolete revision
            # when another visitor was already working on the same layer.
            async def compute(key=key, session=session):
                if backup_priority_active() or await map_cache.source_revision(session) != revision:
                    raise _WarmupDeferred
                if key == _DEFAULT_HEAT:
                    return await _heatmap(session, *key[1:])
                return await _routes(session, *key[1:], background=True)

            try:
                await map_cache.cached(session, key, compute, snapshot=model)
            except _WarmupDeferred:
                return False
            if await map_cache.source_revision(session) != revision:
                return False
    return True


@router.get("/map/coverage", response_model=list[dict])
async def coverage(
    session: SessionDep,
    limit: int = Query(
        500, ge=1, le=5000, description="Most recent journeys to list; the rest are omitted."
    ),
):
    """Per-journey bounds and distance, for listing routes beside the heat map."""
    stmt = (
        select(
            Journey.id,
            Journey.started_at,
            Journey.ended_at,
            Journey.distance_m,
            Journey.min_lat,
            Journey.min_lon,
            Journey.max_lat,
            Journey.max_lon,
        )
        .where(
            journey_has_visible_recording(),
            or_(
                Journey.motion_json["status"].as_string().is_(None),
                Journey.motion_json["status"].as_string() == "moving",
            ),
            Journey.min_lat.is_not(None),
            Journey.min_lon.is_not(None),
        )
        .order_by(Journey.started_at.desc())
        .limit(limit)
    )
    return [
        {
            "id": row.id,
            "started_at": row.started_at,
            "ended_at": row.ended_at,
            "distance_km": round(row.distance_m / 1000.0, 2) if row.distance_m else None,
            "bounds": [[row.min_lat, row.min_lon], [row.max_lat, row.max_lon]],
        }
        for row in (await session.execute(stmt)).all()
    ]
