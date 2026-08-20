"""Removing footage from drives where the car never moved.

The rule is cautious on purpose, and these are the edges that caution is made of: a desk
session goes, a real drive with a red light in it stays, a stationary clip with a plate in
it stays, and footage whose telemetry has not been read is never touched.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.settings_service import get_settings_service
from app.db.models import Journey, Recording, RecordingState, StageState
from app.retention import execute as run_retention
from app.retention.idle import plan_idle


async def _journey(session, *, max_speed: float | None) -> Journey:
    now = datetime.now(UTC)
    journey = Journey(
        started_at=now - timedelta(minutes=10),
        ended_at=now,
        max_speed_kmh=max_speed,
    )
    session.add(journey)
    await session.flush()
    return journey


async def _rec(
    session,
    name,
    *,
    max_speed: float | None,
    journey: Journey | None = None,
    telemetry=StageState.DONE,
    plates: int = 0,
    vehicles: int = 0,
    protected: bool = False,
    event: str | None = None,
    size: int = 2048,
) -> Recording:
    recording = Recording(
        rel_path=name,
        filename=name,
        size_bytes=size,
        started_at=datetime.now(UTC) - timedelta(days=5),
        state=RecordingState.COMPLETED,
        journey_id=journey.id if journey else None,
        telemetry_state=telemetry,
        telemetry_point_count=60 if telemetry == StageState.DONE else 0,
        max_speed_kmh=max_speed,
        avg_speed_kmh=max_speed,
        plate_count=plates,
        vehicle_count=vehicles,
        protected=protected,
        event_type=event,
    )
    session.add(recording)
    await session.flush()
    return recording


@pytest.fixture
async def ready(db_session, temp_dirs):
    """Footage safety passes, and the idle rule is on, so plans reflect the data not a guard."""
    _, footage = temp_dirs
    for i in range(12):
        (footage / f"2026080{i % 9 + 1}_1200{i:02d}_camera_0.ts").write_bytes(b"\x47" * 4096)
    settings = get_settings_service()
    await settings.set("general.footage_dir", str(footage))
    await settings.set("storage.require_mountpoint", False)
    await settings.set("storage.delete_idle", True)
    await settings.set("storage.idle_speed_kmh", 3.0)
    await settings.set("storage.max_delete_fraction", 0.95)
    return db_session


def _ids(plan) -> set[int]:
    return {c.recording_id for c in plan.candidates}


class TestWhatGoes:
    async def test_a_desk_session_with_no_journey_is_removed(self, ready):
        """No GPS, no journey, overlay reads 0 km/h — the exact desk case."""
        desk = await _rec(ready, "desk.ts", max_speed=0.0, journey=None)
        plan = await plan_idle(ready)
        assert _ids(plan) == {desk.id}
        assert plan.candidates[0].reason.startswith("stationary")

    async def test_a_whole_idle_journey_is_removed(self, ready):
        idle = await _journey(ready, max_speed=1.0)
        a = await _rec(ready, "idle_a.ts", max_speed=0.0, journey=idle)
        b = await _rec(ready, "idle_b.ts", max_speed=2.0, journey=idle)
        plan = await plan_idle(ready)
        assert _ids(plan) == {a.id, b.id}


class TestWhatStays:
    async def test_a_real_drive_with_a_still_segment_is_kept_whole(self, ready):
        """The red-light case: one segment reads 0, but the journey moved, so none go."""
        drive = await _journey(ready, max_speed=60.0)
        await _rec(ready, "moving.ts", max_speed=58.0, journey=drive)
        await _rec(ready, "at_a_light.ts", max_speed=0.0, journey=drive)
        plan = await plan_idle(ready)
        assert plan.candidates == []

    async def test_footage_without_telemetry_is_never_touched(self, ready):
        await _rec(ready, "unread.ts", max_speed=None, telemetry=StageState.PENDING, journey=None)
        plan = await plan_idle(ready)
        assert plan.candidates == []

    async def test_a_stationary_clip_with_a_detection_is_kept(self, ready):
        await _rec(ready, "parked_with_plate.ts", max_speed=0.0, journey=None, plates=1)
        await _rec(ready, "parked_with_car.ts", max_speed=0.0, journey=None, vehicles=2)
        plan = await plan_idle(ready)
        assert plan.candidates == []

    async def test_a_protected_or_event_clip_is_kept(self, ready):
        await _rec(ready, "protected.ts", max_speed=0.0, journey=None, protected=True)
        await _rec(ready, "event.ts", max_speed=0.0, journey=None, event="harsh_braking")
        plan = await plan_idle(ready)
        assert plan.candidates == []

    async def test_a_clip_above_the_speed_threshold_is_kept(self, ready):
        await _rec(ready, "crawling.ts", max_speed=9.0, journey=None)  # slow, but moving
        plan = await plan_idle(ready)
        assert plan.candidates == []


class TestTheSwitchesAndGuards:
    async def test_the_rule_off_proposes_nothing(self, ready):
        await get_settings_service().set("storage.delete_idle", False)
        await _rec(ready, "desk.ts", max_speed=0.0, journey=None)
        plan = await plan_idle(ready)
        assert plan.candidates == []

    async def test_a_runaway_plan_blocks_rather_than_deletes(self, ready):
        await get_settings_service().set("storage.max_delete_fraction", 0.25)
        # Everything indexed is idle -> the plan would wipe ~all of it -> blocked.
        for i in range(5):
            await _rec(ready, f"desk_{i}.ts", max_speed=0.0, journey=None, size=10_000)
        plan = await plan_idle(ready)
        assert plan.blocked
        assert plan.blocked_reason and "single-run limit" in plan.blocked_reason


class TestItActuallyDeletes:
    async def test_an_enabled_run_removes_the_file_and_keeps_the_record(self, ready, temp_dirs):
        """The whole point, through the shared executor: file gone, row kept as DELETED so
        anything learned from it survives."""
        _, footage = temp_dirs
        (footage / "desk.ts").write_bytes(b"\x47" * 4096)
        desk = await _rec(ready, "desk.ts", max_speed=0.0, journey=None, size=4096)
        # A real drive in the index too, so removing the one desk clip is a small fraction of
        # the library and the runaway guard stays out of the way.
        await _rec(ready, "real_drive.ts", max_speed=55.0, journey=None, size=500_000)

        plan = await plan_idle(ready)
        # Grant deletion on the plan itself rather than through a settings write, which would
        # contend with this test's open transaction for the SQLite lock. execute() reads the
        # permission from here, so this exercises the real deletion path.
        plan.deletion_enabled = True
        run = await run_retention(ready, plan, dry_run=False, trigger="idle-cleanup")

        assert run.deleted_count == 1
        assert not (footage / "desk.ts").exists()
        # execute() marked the same row through this session; the file is gone but the row
        # (and everything learned from it) is kept, flagged DELETED.
        assert desk.state is RecordingState.DELETED
        assert desk.file_missing is True

    async def test_report_only_touches_nothing(self, ready, temp_dirs):
        _, footage = temp_dirs
        (footage / "desk.ts").write_bytes(b"\x47" * 4096)
        await _rec(ready, "desk.ts", max_speed=0.0, journey=None, size=4096)
        # enable_deletion left off (the default) -> report only.

        plan = await plan_idle(ready)
        run = await run_retention(ready, plan, dry_run=True, trigger="idle-cleanup")

        assert run.deleted_count == 0
        assert (footage / "desk.ts").exists(), "nothing may be deleted while reporting only"
        assert run.candidate_count == 1, "but the report still names what it would remove"
