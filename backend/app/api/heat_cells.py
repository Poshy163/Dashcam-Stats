"""The default moving heat grid and the raw aggregation shared by all filter combinations."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Float,
    Select,
    cast,
    column,
    desc,
    distinct,
    func,
    literal_column,
    or_,
    select,
    table,
    text,
    tuple_,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.api.visibility import visible_revision
from app.db.models import Camera, Journey, Recording, TelemetryPoint
from app.osd.reasons import DRAWABLE

_STATE = text("""
    SELECT revision, heat_cells_ready, EXISTS(SELECT 1 FROM map_heat_dirty) AS dirty
    FROM map_source_state WHERE id = 1
""")
_cells = table(
    "map_heat_cells", *(column(k) for k in ("lat", "lon", "weight", "speed_sum", "speed_count"))
)
_dirty = table("map_heat_dirty", column("lat"), column("lon"))


def _dirty_points():
    # CROSS JOIN fixes the loop order: read changed keys and seek both rounded
    # coordinates in ix_telemetry_heat_cells. A tuple IN can scan every fix.
    source = (
        text("""
        SELECT p.* FROM map_heat_dirty AS d
        CROSS JOIN telemetry_points AS p INDEXED BY ix_telemetry_heat_cells
        WHERE round(p.lat, 3) = d.lat AND round(p.lon, 3) = d.lon
    """)
        .columns(*TelemetryPoint.__table__.columns)
        .subquery()
    )
    return aliased(TelemetryPoint, source)


def drawable_quality(points=TelemetryPoint):
    """Positions a map may draw: read directly, or conservatively reconstructed.

    ``gps_quality`` is null on rows written before it existed. Those are treated as
    drawable on the strength of ``has_fix``, which is what every consumer relied on before
    this column, rather than vanishing from the map until the library is reprocessed.
    """
    return points.gps_quality.is_(None) | points.gps_quality.in_(
        [str(quality) for quality in DRAWABLE]
    )


def _aggregate(
    precision: int = 3,
    start: datetime | None = None,
    end: datetime | None = None,
    journey_id: int | None = None,
    camera: str | None = None,
    min_speed_kmh: float = 5.0,
    *,
    dirty_only: bool = False,
) -> Select:
    # The initial page requests this whole-library view. SQLite matches expressions and
    # partial-index predicates structurally, so its grid and cutoff must be SQL literals
    # for ix_telemetry_heat_cells to serve the aggregation directly in cell order.
    indexed_grid = (
        precision == 3
        and min_speed_kmh == 5
        and start is None
        and end is None
        and camera is None
        and journey_id is None
    )
    points = _dirty_points() if dirty_only else TelemetryPoint
    grid = literal_column("3") if indexed_grid else precision
    lat_cell = func.round(points.lat, grid)
    lon_cell = func.round(points.lon, grid)

    # Distinct *seconds*, not rows. Front and rear record the same drive, both burn the same
    # GPS overlay and both are sampled at 1 Hz, so one real second of driving produces two
    # rows wherever both cameras had a lock. Counting rows made the map claim 12 h 45 m of
    # driving against about 10 h 56 m of actual elapsed journey time — arithmetically
    # impossible — and doubled the heat on exactly the roads with the best coverage.
    #
    # Counting seconds instead of dropping a camera keeps stretches where only the rear
    # had a fix. The coalesce is what makes that safe: a reading can now carry a position
    # without a timestamp, and those rows would otherwise count as nothing at all.
    # strftime returns text. A time-less fix uses its integer row ID, which SQLite
    # counts separately even if an epoch second happens to have the same digits.
    second = func.coalesce(func.strftime(literal_column("'%s'"), points.captured_at), points.id)
    weight = func.count(distinct(second))
    recordings = select(Recording.id).where(visible_revision(Recording.telemetry_revision))
    if camera is not None:
        recordings = recordings.join(Camera, Recording.camera_id == Camera.id).where(
            Camera.key == camera
        )
    visible_recording = points.recording_id.in_(recordings)
    if dirty_only:
        # A handful of cell seeks needs only the matching recording primary keys.
        visible_recording = recordings.where(Recording.id == points.recording_id).exists()
    elif indexed_grid:
        # Most recordings are visible. Without this selectivity hint ANALYZE can lead
        # SQLite to seek one recording at a time and sort the whole library by cell.
        visible_recording = func.likely(visible_recording)

    stmt = (
        select(
            cast(lat_cell, Float).label("lat"),
            cast(lon_cell, Float).label("lon"),
            weight.label("weight"),
            # Summed rather than averaged per cell. An average of per-cell averages weights
            # a cell holding one fix the same as a cell holding four thousand, so the
            # reported figure moved from 25.3 to 49.9 km/h on identical data purely by
            # changing the grid resolution. Totals divided at the end are invariant under
            # it, which is the property the number needs to mean anything.
            func.sum(points.speed_kmh).label("speed_sum"),
            func.count(points.speed_kmh).label("speed_count"),
        )
        # has_fix alone is not enough: a row can carry the flag with null coordinates if a
        # reading was rejected after the flag was set, and null coordinates would round to
        # a cell at Null Island.
        .where(
            points.has_fix.is_(True),
            # Seek the eligible recordings in the map index. One hidden clip must never
            # contribute fixes merely because another clip has a visible revision.
            visible_recording,
            points.lat.is_not(None),
            points.lon.is_not(None),
            drawable_quality(points),
            # Retain raw positions for inspection, but do not draw sensor wander as a
            # travelled route after the journey's motion check failed to establish one.
            or_(
                points.journey_id.is_(None),
                points.journey_id.not_in(
                    select(Journey.id).where(
                        Journey.motion_json["status"].as_string().in_(["unconfirmed", "unknown"])
                    )
                ),
            ),
        )
        .group_by(lat_cell, lon_cell)
    )

    if min_speed_kmh > 0:
        # Null speeds are kept: an unreadable speed field says nothing about whether the
        # car was moving, and discarding those fixes would punch holes in real routes.
        cutoff = literal_column("5") if indexed_grid else min_speed_kmh
        stmt = stmt.where((points.speed_kmh.is_(None)) | (points.speed_kmh >= cutoff))
    if start is not None:
        stmt = stmt.where(points.captured_at >= start)
    if end is not None:
        stmt = stmt.where(points.captured_at <= end)
    if journey_id is not None:
        stmt = stmt.where(points.journey_id == journey_id)
    return stmt


async def read_rows(
    session: AsyncSession,
    precision: int,
    start: datetime | None,
    end: datetime | None,
    journey_id: int | None,
    camera: str | None,
    min_speed_kmh: float,
    limit: int,
):
    args = (precision, start, end, journey_id, camera, min_speed_kmh)
    if args != (3, None, None, None, None, 5.0) or session.get_bind().dialect.name != "sqlite":
        stmt = _aggregate(*args).order_by(desc("weight"), "lat", "lon").limit(limit + 1)
        return (await session.execute(stmt)).all()

    if session.autoflush:
        await session.flush()
    connection = await session.connection()
    # Pin readiness, dirty keys and the rows themselves to the same SQLite snapshot.
    async with connection.begin_nested():
        _, ready, dirty = (await connection.execute(_STATE)).one()
        if not ready:
            stmt = _aggregate()
        else:
            stmt = select(_cells)
            if dirty:
                # Pending background work must never leak hidden or moved positions.
                # Substitute exact live totals for every changed cell, including cells
                # that now have no contributing fix at all.
                clean = stmt.where(tuple_(_cells.c.lat, _cells.c.lon).not_in(select(_dirty)))
                stmt = clean.union_all(_aggregate(dirty_only=True))
        return (
            await connection.execute(stmt.order_by(desc("weight"), "lat", "lon").limit(limit + 1))
        ).all()


async def refresh_heat_cells(session: AsyncSession) -> bool:
    """Refresh from a dedicated session. The caller commits; False means retry a newer revision."""
    if session.get_bind().dialect.name != "sqlite":
        return True
    connection = await session.connection()
    async with connection.begin_nested():
        revision, ready, dirty = (await connection.execute(_STATE)).one()
        if ready and not dirty:
            return True
        rows = (await connection.execute(_aggregate(dirty_only=bool(ready)))).mappings().all()

    # The potentially expensive aggregation ran in a read transaction. Acquire
    # the write lock and check its source revision together before publishing.
    # A competing edit can now either precede this write (retry), or follow its
    # commit (its triggers will dirty the affected cells again).
    claimed = await connection.execute(
        text("UPDATE map_source_state SET heat_cells_ready = 1 WHERE id = 1 AND revision = :rev"),
        {"rev": revision},
    )
    if not claimed.rowcount:
        return False
    if ready:
        await connection.execute(
            text(
                "DELETE FROM map_heat_cells WHERE (lat, lon) IN (SELECT lat, lon FROM map_heat_dirty)"
            )
        )
    else:
        await connection.execute(text("DELETE FROM map_heat_cells"))
    if rows:
        await connection.execute(
            text("""INSERT OR REPLACE INTO map_heat_cells (lat, lon, weight, speed_sum, speed_count)
                    VALUES (:lat, :lon, :weight, :speed_sum, :speed_count)"""),
            [dict(row) for row in rows],
        )
    await connection.execute(text("DELETE FROM map_heat_dirty"))
    return True
