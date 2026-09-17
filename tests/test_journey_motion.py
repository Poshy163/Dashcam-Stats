"""Sensor speed spikes must not turn parked recordings into drives."""

from datetime import UTC, datetime, timedelta
from math import cos, pi, sin

import pytest
from sqlalchemy import select

from app.db.models import Journey, Recording, RecordingState, TelemetryPoint
from app.journeys.builder import JourneyBuilder
from app.journeys.motion import MOTION_REVISION, assess_motion
from app.journeys.revalidation import motion_coverage, revalidate_motion
from app.journeys.track import TrackPoint
from app.retention.parked import parked_journey_ids

BASE = datetime(2026, 1, 1, tzinfo=UTC)


def point(t, speed, metres=0, *, east=0, broken=False):
    return TrackPoint(
        1, float(t), metres / 111_195, east / 111_195, speed, BASE + timedelta(seconds=t), broken
    )


def test_source_speed_spike_is_rejected_despite_perfect_ocr():
    speeds = [0] * 20 + [2, 35, 35, 111, 111, 88, 88, 47, 47, 23, 23, 6, 6] + [0] * 20
    original = [point(i, s, (i % 3) * 11) for i, s in enumerate(speeds)]
    result = assess_motion(original)
    assert result.evidence["status"] == "unconfirmed"
    assert result.evidence["rejected_speed_samples"] > 0
    assert all(p.speed_kmh != 111 for p in result.track)
    assert original[23].speed_kmh == 111, "raw observations must remain available"


def test_stationary_gps_wander_with_sustained_false_speed_is_not_a_drive():
    result = assess_motion(
        [point(t, 12, sin(t / 5) * 12, east=cos(t / 5) * 12) for t in range(300)]
    )
    assert result.evidence["status"] == "unconfirmed"


@pytest.mark.parametrize("cadence", [1, 5, 10])
def test_short_real_drive_survives_at_different_sample_rates(cadence):
    result = assess_motion([point(t, 36, t * 10) for t in range(0, 31, cadence)])
    assert result.evidence["status"] == "moving"


def test_long_stop_does_not_dilute_a_real_drive():
    points = [point(t, 0) for t in range(300)]
    points += [point(t, 36, (t - 300) * 10) for t in range(300, 320)]
    points += [point(t, 0, 200) for t in range(320, 900)]
    assert assess_motion(points).evidence["status"] == "moving"


def test_loop_and_ordinary_acceleration_are_retained():
    loop = [point(t, 36, 100 * sin(t / 30 * pi), east=100 * cos(t / 30 * pi)) for t in range(61)]
    assert assess_motion(loop).evidence["status"] == "moving"
    ramp = [point(t, min(100, t * 4), t * t / 2) for t in range(40)]
    assert not assess_motion(ramp).rejected


def test_gaps_and_route_breaks_cannot_glue_together_short_bursts():
    gaps = [point(t, 40, t * 10) for t in [0, 1, 30, 31, 60, 61]]
    assert assess_motion(gaps).evidence["status"] == "unconfirmed"
    breaks = [point(t, 40, t * 10, broken=t % 5 == 0) for t in range(60)]
    assert assess_motion(breaks).evidence["status"] == "unconfirmed"


def test_no_data_is_unknown_not_stationary():
    assert assess_motion([]).evidence["status"] == "unknown"


def test_sparse_travel_requires_substantial_plausible_progress_and_matching_speeds():
    tunnel = [point(0, 60), point(240, 60, 4000)]
    assert assess_motion(tunnel).evidence["reason"] == "plausible_sparse_leg"
    for uncertain in (
        [point(0, 60), point(240, 60, 40)],  # receiver wander
        [point(0, 5), point(240, 5, 4000)],  # speeds contradict the jump
        [point(0, 60), point(11, 60, 4000)],  # physically impossible
        [point(0, 60), point(240, 60, 4000, broken=True)],
        [point(0, 60), point(600, 60, 10000)],  # too much missing evidence
    ):
        assert assess_motion(uncertain).evidence["status"] == "unconfirmed"


async def session_fixture(session, *, name="parked", moving=False):
    journey = Journey(
        started_at=BASE,
        ended_at=BASE + timedelta(seconds=60),
        has_gps=True,
        avg_speed_kmh=40,
        max_speed_kmh=111,
    )
    session.add(journey)
    await session.flush()
    recording = Recording(
        filename=name + ".ts",
        rel_path=name + ".ts",
        size_bytes=2048,
        state=RecordingState.COMPLETED,
        started_at=BASE,
        ended_at=BASE + timedelta(seconds=60),
        duration_s=60,
        journey_id=journey.id,
    )
    session.add(recording)
    await session.flush()
    for t in range(60):
        session.add(
            TelemetryPoint(
                recording_id=recording.id,
                journey_id=journey.id,
                t_offset_s=t,
                captured_at=BASE + timedelta(seconds=t),
                has_fix=True,
                lat=1 + (t * 10 if moving else t % 3 * 11) / 111_195,
                lon=1,
                speed_kmh=36 if moving else 111 if t in [29, 30] else 0,
            )
        )
    await session.commit()
    return journey.id, recording.id


async def test_historical_check_is_bounded_resumable_and_preserves_raw_data(db_session, client):
    parked, rid = await session_fixture(db_session)
    await session_fixture(db_session, name="real", moving=True)
    assert (await motion_coverage(db_session))["pending"] == 2
    assert await revalidate_motion(limit=1) == 1
    assert (await motion_coverage(db_session))["pending"] == 1
    assert await revalidate_motion(limit=1) == 1
    assert await revalidate_motion(limit=1) == 0
    db_session.expire_all()
    j = await db_session.get(Journey, parked)
    assert j.motion_json["revision"] == MOTION_REVISION
    assert j.motion_json["status"] == "unconfirmed"
    assert j.distance_m is None and j.max_speed_kmh is None
    r = await db_session.get(Recording, rid)
    assert not r.ignored and not r.file_missing
    speeds = list(
        (
            await db_session.scalars(
                select(TelemetryPoint.speed_kmh).where(TelemetryPoint.recording_id == rid)
            )
        ).all()
    )
    assert 111 in speeds
    # Missing validated speed is not affirmative stationary evidence for deletion.
    assert parked not in list((await db_session.scalars(parked_journey_ids(5, 10))).all())
    await db_session.commit()
    response = await client.get(f"/api/journeys/{parked}")
    assert response.status_code == 200
    assert response.json()["route"] == []
    routes = (await client.get(f"/api/map/routes?journey_id={parked}")).json()
    assert routes["lines"] == []
    assert parked not in {j["id"] for j in (await client.get("/api/journeys")).json()["items"]}
    coverage = (await client.get("/api/journeys/motion-quality")).json()
    assert coverage["pending"] == 0 and coverage["moving"] == 1
    assert coverage["unconfirmed"] == 1
    assert await JourneyBuilder().repair_stale(db_session) == 0


async def test_new_telemetry_reassesses_previously_unconfirmed_journey(db_session):
    jid, rid = await session_fixture(db_session)
    await revalidate_motion()
    db_session.expire_all()
    for t in range(60, 81):
        db_session.add(
            TelemetryPoint(
                recording_id=rid,
                journey_id=jid,
                captured_at=BASE + timedelta(seconds=t),
                t_offset_s=t,
                lat=1 + (t - 60) * 10 / 111_195,
                lon=1,
                speed_kmh=36,
                has_fix=True,
            )
        )
    await db_session.flush()
    j = await db_session.get(Journey, jid)
    await JourneyBuilder().refresh(db_session, j)
    assert j.motion_json["status"] == "moving"
    assert j.distance_m is not None


async def test_failed_historical_check_remains_pending_without_blocking_other_rows(
    db_session, monkeypatch
):
    await session_fixture(db_session, name="healthy", moving=True)
    bad_id, _ = await session_fixture(db_session, name="temporary-error")
    original = JourneyBuilder.refresh

    async def fail_one(self, session, journey):
        if journey.id == bad_id:
            raise RuntimeError("temporary test failure")
        return await original(self, session, journey)

    monkeypatch.setattr(JourneyBuilder, "refresh", fail_one)
    assert await revalidate_motion() == 1
    coverage = await motion_coverage(db_session)
    assert coverage["pending"] == 1 and coverage["moving"] == 1
    monkeypatch.setattr(JourneyBuilder, "refresh", original)
    assert await revalidate_motion() == 1
    assert (await motion_coverage(db_session))["pending"] == 0
