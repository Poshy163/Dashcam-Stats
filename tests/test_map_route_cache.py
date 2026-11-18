from datetime import UTC, datetime, timedelta

from sqlalchemy import update

from app.api import route_geometry_cache
from app.api.geometry import simplify_indices, split_indices
from app.api.routes import heatmap
from app.db.models import Journey, Recording, RecordingState, TelemetryPoint
from app.db.session import session_scope


async def test_routes_reuse_only_identical_journeys_across_library_changes(client, monkeypatch):
    base = datetime(2026, 8, 8, tzinfo=UTC)
    async with session_scope() as session:
        for drive in range(2):
            start = base + timedelta(hours=drive)
            journey = Journey(started_at=start, ended_at=start + timedelta(seconds=3))
            session.add(journey)
            await session.flush()
            recording = Recording(
                rel_path=f"{drive}.ts",
                filename=f"{drive}.ts",
                state=RecordingState.COMPLETED,
                journey_id=journey.id,
            )
            session.add(recording)
            await session.flush()
            for step in range(4):
                session.add(
                    TelemetryPoint(
                        recording_id=recording.id,
                        journey_id=journey.id,
                        t_offset_s=step,
                        captured_at=start + timedelta(seconds=step),
                        has_fix=True,
                        lat=-34 + drive + step * 0.0001,
                        lon=138,
                    )
                )
        changed_recording = recording.id

    # Journey boundaries can land inside a cursor batch or on its first row.
    monkeypatch.setattr(heatmap, "_ROUTE_FETCH_BATCH", 3)
    simplify = route_geometry_cache.simplify_journey
    calls = []

    def measured(rows, tolerance_m):
        calls.append(rows[0][3])
        return simplify(rows, tolerance_m)

    monkeypatch.setattr(route_geometry_cache, "simplify_journey", measured)
    before = (await client.get("/api/map/routes")).json()
    assert len(calls) == before["journeys"] == before["segments"] == 2

    async with session_scope() as session:
        # Reprocessing retains IDs, counts and time bounds but changes an endpoint.
        await session.execute(
            update(TelemetryPoint)
            .where(TelemetryPoint.recording_id == changed_recording, TelemetryPoint.t_offset_s == 3)
            .values(lon=138.0005)
        )
        await session.execute(
            update(Recording).where(Recording.id == changed_recording).values(processed_at=base)
        )
    after = (await client.get("/api/map/routes")).json()
    assert calls[2:] == [changed_recording]
    assert after["lines"][0] == before["lines"][0]
    assert after["lines"][1][-1] == [-32.9997, 138.0005]

    async with session_scope() as session:
        capped = await heatmap._routes(session, None, None, None, heatmap.DEFAULT_SIMPLIFY_M, 2)
    assert capped.truncated and capped.lines == after["lines"][:1]
    assert len(calls) == 3


def test_geometry_honours_utc_offsets_and_subsecond_gaps():
    rows = [
        (0.0, 0.0000, "2026-08-08 00:00:00", 1, False),
        (0.0, 0.0001, "2026-08-08 09:30:10+09:30", 1, False),
        (0.0, 0.0002, "2026-08-08 00:00:20.000001", 1, False),
        (0.0, 0.0003, None, 1, False),
    ]
    assert route_geometry_cache.simplify_journey(rows, 0) == [
        [(0.0, 0.0000), (0.0, 0.0001)],
        [(0.0, 0.0002), (0.0, 0.0003)],
    ]


def test_route_simplification_keeps_first_tied_vertex_and_original_endpoints():
    points = [(0.0, 0.0)] * 3 + [(0.001, 0.001)] * 3 + [(0.002, 0.0)] * 3
    assert simplify_indices(points, 15) == [0, 3, 8]
    assert simplify_indices(points, 0) == list(range(9))
    assert simplify_indices(points[:3], 15) == [0, 2]


def test_route_jump_threshold_uses_physical_distance_across_dateline_and_near_pole():
    # 0.0017 degrees at the equator is ~189 m. The next step spans ~211 m.
    equator = [(0, 0.0, None), (0, 0.0017, None), (0, 0.0036, None), (0, 0.0037, None)]
    assert split_indices(equator) == [[0, 1], [2, 3]]
    # These longitudes cross the date line then advance 2 degrees at 89.99 N.
    pole = [(89.99, 179.9, None), (89.99, -179.9, None), (89.99, -177.9, None)]
    assert split_indices(pole) == [[0, 1, 2]]
