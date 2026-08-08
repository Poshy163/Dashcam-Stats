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

from datetime import datetime

from fastapi import APIRouter, Query
from sqlalchemy import Float, func, select
from sqlalchemy import cast as sa_cast

from app.api.deps import SessionDep
from app.api.schemas import HeatmapOut
from app.core.logging import get_logger
from app.db.models import Camera, Journey, Recording, TelemetryPoint

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


@router.get("/map/heatmap", response_model=HeatmapOut)
async def heatmap(
    session: SessionDep,
    precision: int = Query(
        DEFAULT_PRECISION,
        ge=MIN_PRECISION,
        le=MAX_PRECISION,
        description="Grid resolution in decimal degrees places: 2≈1.1 km, 3≈110 m, 4≈11 m.",
    ),
    start: datetime | None = Query(None, description="Only fixes at or after this instant."),
    end: datetime | None = Query(None, description="Only fixes at or before this instant."),
    journey_id: int | None = Query(None, description="Restrict to one journey."),
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
    lat_cell = func.round(TelemetryPoint.lat, precision)
    lon_cell = func.round(TelemetryPoint.lon, precision)

    stmt = (
        select(
            sa_cast(lat_cell, Float).label("lat"),
            sa_cast(lon_cell, Float).label("lon"),
            func.count().label("weight"),
            func.avg(TelemetryPoint.speed_kmh).label("speed"),
        )
        # has_fix alone is not enough: a row can carry the flag with null coordinates if a
        # reading was rejected after the flag was set, and null coordinates would round to
        # a cell at Null Island.
        .where(
            TelemetryPoint.has_fix.is_(True),
            TelemetryPoint.lat.is_not(None),
            TelemetryPoint.lon.is_not(None),
        )
        .group_by(lat_cell, lon_cell)
        # Densest first, so a truncated response still shows the places most driven rather
        # than an arbitrary slice.
        .order_by(func.count().desc())
        .limit(limit + 1)
    )

    if min_speed_kmh > 0:
        # Null speeds are kept: an unreadable speed field says nothing about whether the
        # car was moving, and discarding those fixes would punch holes in real routes.
        stmt = stmt.where(
            (TelemetryPoint.speed_kmh.is_(None)) | (TelemetryPoint.speed_kmh >= min_speed_kmh)
        )
    if start is not None:
        stmt = stmt.where(TelemetryPoint.captured_at >= start)
    if end is not None:
        stmt = stmt.where(TelemetryPoint.captured_at <= end)
    if journey_id is not None:
        stmt = stmt.where(TelemetryPoint.journey_id == journey_id)
    if camera is not None:
        stmt = (
            stmt.join(Recording, TelemetryPoint.recording_id == Recording.id)
            .join(Camera, Recording.camera_id == Camera.id)
            .where(Camera.key == camera)
        )

    rows = (await session.execute(stmt)).all()
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
    speeds = [float(row.speed) for row in rows if row.speed is not None]

    return HeatmapOut(
        points=points,
        precision=precision,
        cells=len(points),
        # The heaviest cell sets the scale the client normalises against. Sending it saves
        # the client a pass over the data and keeps the colour ramp stable when the caller
        # pages or filters.
        max_weight=max(weights) if weights else 0,
        total_points=sum(weights),
        average_speed_kmh=round(sum(speeds) / len(speeds), 1) if speeds else None,
        truncated=truncated,
    )


@router.get("/map/coverage", response_model=list[dict])
async def coverage(session: SessionDep):
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
        .where(Journey.min_lat.is_not(None), Journey.min_lon.is_not(None))
        .order_by(Journey.started_at.desc())
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
