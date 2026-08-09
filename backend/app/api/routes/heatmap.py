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

from collections import defaultdict
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import Float, String, distinct, func, select
from sqlalchemy import cast as sa_cast

from app.api.deps import SQLITE_MAX_INT, SessionDep
from app.api.geometry import build_polylines
from app.api.schemas import HeatmapOut, RoutesOut
from app.core.logging import get_logger
from app.db.models import Camera, CameraRole, Journey, Recording, TelemetryPoint

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
    lat_cell = func.round(TelemetryPoint.lat, precision)
    lon_cell = func.round(TelemetryPoint.lon, precision)

    # Distinct *seconds*, not rows. Front and rear record the same drive, both burn the same
    # GPS overlay and both are sampled at 1 Hz, so one real second of driving produces two
    # rows wherever both cameras had a lock. Counting rows made the map claim 12 h 45 m of
    # driving against about 10 h 56 m of actual elapsed journey time — arithmetically
    # impossible — and doubled the heat on exactly the roads with the best coverage.
    #
    # Counting seconds instead of dropping a camera keeps stretches where only the rear
    # had a fix. The coalesce is what makes that safe: a reading can now carry a position
    # without a timestamp, and those rows would otherwise count as nothing at all.
    second = func.coalesce(
        func.strftime("%s", TelemetryPoint.captured_at),
        "row:" + sa_cast(TelemetryPoint.id, String),
    )
    weight = func.count(distinct(second))

    stmt = (
        select(
            sa_cast(lat_cell, Float).label("lat"),
            sa_cast(lon_cell, Float).label("lon"),
            weight.label("weight"),
            # Summed rather than averaged per cell. An average of per-cell averages weights
            # a cell holding one fix the same as a cell holding four thousand, so the
            # reported figure moved from 25.3 to 49.9 km/h on identical data purely by
            # changing the grid resolution. Totals divided at the end are invariant under
            # it, which is the property the number needs to mean anything.
            func.sum(TelemetryPoint.speed_kmh).label("speed_sum"),
            func.count(TelemetryPoint.speed_kmh).label("speed_count"),
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
        .order_by(weight.desc())
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
    overlay, so including each would draw every road twice with a metre or two of OCR
    disagreement between the copies — visible as a doubled line, and twice the payload for
    no extra information.
    """
    stmt = (
        select(
            TelemetryPoint.journey_id,
            TelemetryPoint.lat,
            TelemetryPoint.lon,
            TelemetryPoint.captured_at,
            TelemetryPoint.t_offset_s,
            TelemetryPoint.recording_id,
        )
        .join(Recording, TelemetryPoint.recording_id == Recording.id)
        .outerjoin(Camera, Recording.camera_id == Camera.id)
        .where(
            TelemetryPoint.has_fix.is_(True),
            TelemetryPoint.lat.is_not(None),
            TelemetryPoint.lon.is_not(None),
            # A rear-camera-only stretch is rare and not worth a second copy of every road.
            (Camera.role == CameraRole.FRONT) | (Camera.id.is_(None)),
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

    # Grouped in Python rather than by a query per journey: one ordered pass over the
    # fixes costs a single round trip, where a query per journey is 45 of them on this
    # library and grows with every drive taken.
    by_journey: dict[int | None, list[tuple[float, float, float | None]]] = defaultdict(list)
    for row in (await session.execute(stmt)).all():
        seconds = row.captured_at.timestamp() if row.captured_at is not None else None
        by_journey[row.journey_id].append((float(row.lat), float(row.lon), seconds))

    lines: list[list[list[float]]] = []
    points_sent = 0
    truncated = False
    for fixes in by_journey.values():
        for segment in build_polylines(fixes, tolerance_m=simplify_m):
            if points_sent + len(segment) > max_points:
                truncated = True
                break
            lines.append([[round(lat, 4), round(lon, 4)] for lat, lon in segment])
            points_sent += len(segment)
        if truncated:
            break

    return RoutesOut(
        lines=lines,
        journeys=len(by_journey),
        segments=len(lines),
        points=points_sent,
        simplify_m=simplify_m,
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
