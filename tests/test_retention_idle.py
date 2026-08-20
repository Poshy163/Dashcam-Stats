"""Removing clips that recorded nothing: static, empty, and proven so.

The rule deletes on its own authority, so these edges are where the caution lives: an
analysed desk clip goes, a clip that moved or saw something stays, a clip not yet analysed
is never touched, and a false detection in a sibling clip does not save an empty one.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.settings_service import get_settings_service
from app.db.models import Journey, Recording, RecordingState, StageState
from app.retention import execute as run_retention
from app.retention.idle import plan_idle


async def _journey(session) -> Journey:
    now = datetime.now(UTC)
    journey = Journey(started_at=now - timedelta(minutes=10), ended_at=now)
    session.add(journey)
    await session.flush()
    return journey


async def _rec(
    session,
    name,
    *,
    max_speed: float | None = 0.0,
    distance_m: float | None = None,
    journey: Journey | None = None,
    telemetry=StageState.DONE,
    detection=StageState.DONE,
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
        detection_state=detection,
        telemetry_point_count=60 if telemetry == StageState.DONE else 0,
        max_speed_kmh=max_speed,
        avg_speed_kmh=max_speed,
        distance_m=distance_m,
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
    """Footage safety passes and the rule is on, so plans reflect the data not a guard."""
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


def _names(plan) -> set[str]:
    return {c.filename for c in plan.candidates}


class TestWhatGoes:
    async def test_a_static_empty_clip_is_a_candidate(self, ready):
        """The desk case exactly: analysed, no GPS, speed 0, nothing detected."""
        await _rec(ready, "desk.ts", max_speed=0.0, distance_m=None)
        plan = await plan_idle(ready)
        assert _names(plan) == {"desk.ts"}
        assert plan.candidates[0].reason.startswith("static")

    async def test_it_is_judged_per_clip_not_per_journey(self, ready):
        """A false detection in one clip must not save the empty ones beside it — the whole
        point of moving off the whole-journey rule."""
        session_journey = await _journey(ready)
        await _rec(ready, "empty_a.ts", journey=session_journey)
        await _rec(ready, "empty_b.ts", journey=session_journey)
        await _rec(ready, "caught_something.ts", journey=session_journey, vehicles=1)
        plan = await plan_idle(ready)
        assert _names(plan) == {"empty_a.ts", "empty_b.ts"}, "the empty siblings should still go"


class TestWhatStays:
    async def test_a_clip_that_moved_is_kept(self, ready):
        await _rec(ready, "moving.ts", max_speed=42.0, distance_m=800.0)
        plan = await plan_idle(ready)
        assert plan.candidates == []

    async def test_a_clip_that_covered_ground_is_kept_even_if_speed_misread_low(self, ready):
        """The second witness: GPS says it travelled, so a low speed reading is not trusted
        to delete it."""
        await _rec(ready, "travelled.ts", max_speed=1.0, distance_m=600.0)
        plan = await plan_idle(ready)
        assert plan.candidates == []

    async def test_a_clip_with_a_detection_is_kept(self, ready):
        await _rec(ready, "saw_a_car.ts", vehicles=1)
        await _rec(ready, "saw_a_plate.ts", plates=1)
        plan = await plan_idle(ready)
        assert plan.candidates == []

    async def test_unanalysed_footage_is_never_touched(self, ready):
        """Silence is not evidence: unknown speed or unknown object count means leave it."""
        await _rec(ready, "no_telemetry.ts", telemetry=StageState.PENDING, max_speed=None)
        await _rec(ready, "no_detection.ts", detection=StageState.PENDING)
        plan = await plan_idle(ready)
        assert plan.candidates == []

    async def test_a_protected_or_event_clip_is_kept(self, ready):
        await _rec(ready, "protected.ts", protected=True)
        await _rec(ready, "event.ts", event="harsh_braking")
        plan = await plan_idle(ready)
        assert plan.candidates == []


class TestTheSwitchesAndGuards:
    async def test_the_rule_off_proposes_and_authorises_nothing(self, ready):
        await get_settings_service().set("storage.delete_idle", False)
        await _rec(ready, "desk.ts")
        plan = await plan_idle(ready)
        assert plan.candidates == []
        assert plan.deletion_enabled is False, "off means it authorises no deletion"

    async def test_it_authorises_its_own_deletion_when_on(self, ready):
        await _rec(ready, "desk.ts")
        plan = await plan_idle(ready)
        assert plan.deletion_enabled is True, "the rule does not wait on the master switch"

    async def test_a_runaway_plan_blocks_rather_than_deletes(self, ready):
        await get_settings_service().set("storage.max_delete_fraction", 0.25)
        for i in range(5):
            await _rec(ready, f"desk_{i}.ts", size=10_000)
        plan = await plan_idle(ready)
        assert plan.blocked
        assert plan.blocked_reason and "single-run limit" in plan.blocked_reason


class TestItActuallyDeletesWithoutTheMasterSwitch:
    async def test_it_removes_the_file_with_enable_deletion_off(self, ready, temp_dirs):
        """The operator's ask: no per-run enable step. enable_deletion stays off (default)
        and the static clip is still removed, because the plan authorised itself."""
        _, footage = temp_dirs
        (footage / "desk.ts").write_bytes(b"\x47" * 4096)
        desk = await _rec(ready, "desk.ts", size=4096)
        # A real drive in the index so the desk clip is a small fraction.
        await _rec(ready, "drive.ts", max_speed=55.0, distance_m=900.0, size=500_000)
        assert bool(get_settings_service().get_nowait("storage.enable_deletion")) is False

        plan = await plan_idle(ready)
        run = await run_retention(ready, plan, dry_run=False, trigger="idle-cleanup")

        assert run.deleted_count == 1, "the static clip must be removed without the switch"
        assert not (footage / "desk.ts").exists()
        assert desk.state is RecordingState.DELETED and desk.file_missing is True

    async def test_an_unsafe_mount_still_blocks_deletion(self, ready, temp_dirs, monkeypatch):
        """Self-authorising does not mean unguarded: a footage dir that is not safe to write
        must still stop it, or an unmounted share would look empty and get wiped."""
        from app.retention import safety as safety_mod

        (await get_settings_service().footage_dir())  # ensure configured
        monkeypatch.setattr(
            safety_mod, "directory_size", lambda *a, **k: (0, 0)
        )  # empty dir -> safety fails
        await _rec(ready, "desk.ts")
        plan = await plan_idle(ready)
        run = await run_retention(ready, plan, dry_run=False, trigger="idle-cleanup")
        assert run.deleted_count == 0
        assert run.blocked
