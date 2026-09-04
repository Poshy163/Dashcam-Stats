"""Removing footage of a car that was parked, including the parts that saw traffic.

The static rule next door refuses to touch a clip that detected anything, which is correct
for it and useless here: journey 280 of the live library sat still for nineteen minutes in
sight of a road and logged eighty-six vehicles. This rule judges the drive instead of the
clip, so these tests are mostly about the evidence it demands before it is allowed to --
a journey it knows nothing about, one still being analysed, or one with work queued
against it are all left exactly where they are.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.settings_service import get_settings_service
from app.db.models import (
    JobState,
    Journey,
    ProcessingJob,
    Recording,
    RecordingState,
    StageState,
)
from app.pipeline.revisions import CURRENT_REVISIONS
from app.retention import execute as run_retention
from app.retention.parked import plan_parked


async def _journey(session, *, avg: float | None, top: float | None) -> Journey:
    now = datetime.now(UTC)
    journey = Journey(
        started_at=now - timedelta(minutes=20),
        ended_at=now,
        duration_s=1200.0,
        avg_speed_kmh=avg,
        max_speed_kmh=top,
    )
    session.add(journey)
    await session.flush()
    return journey


async def _rec(
    session,
    name,
    *,
    journey: Journey | None = None,
    vehicles: int = 0,
    plates: int = 0,
    protected: bool = False,
    event: str | None = None,
    size: int = 2048,
    state: RecordingState = RecordingState.COMPLETED,
    telemetry: StageState = StageState.DONE,
    telemetry_revision: str = CURRENT_REVISIONS["telemetry"],
) -> Recording:
    recording = Recording(
        rel_path=name,
        filename=name,
        size_bytes=size,
        started_at=datetime.now(UTC) - timedelta(days=5),
        state=state,
        processed_at=datetime.now(UTC),
        journey_id=journey.id if journey else None,
        metadata_state=StageState.DONE,
        telemetry_state=telemetry,
        detection_state=StageState.DONE,
        plate_state=StageState.DONE,
        metadata_revision=CURRENT_REVISIONS["metadata"],
        telemetry_revision=telemetry_revision,
        detection_revision=CURRENT_REVISIONS["detection"],
        plate_revision=CURRENT_REVISIONS["plates"],
        vehicle_count=vehicles,
        plate_count=plates,
        protected=protected,
        event_type=event,
    )
    session.add(recording)
    await session.flush()
    return recording


@pytest.fixture
async def ready(db_session, temp_dirs):
    """Safety passes and the rule is on, so a plan reflects the data rather than a guard."""
    _, footage = temp_dirs
    for i in range(12):
        (footage / f"2026080{i % 9 + 1}_1200{i:02d}_camera_0.ts").write_bytes(b"\x47" * 4096)
    settings = get_settings_service()
    await settings.set("general.footage_dir", str(footage))
    await settings.set("storage.require_mountpoint", False)
    await settings.set("storage.delete_parked_journeys", True)
    await settings.set("journeys.min_avg_speed_kmh", 5.0)
    await settings.set("journeys.min_top_speed_kmh", 10.0)
    await settings.set("storage.max_delete_fraction", 0.95)
    return db_session


def _names(plan) -> set[str]:
    return {c.filename for c in plan.candidates}


class TestWhatGoes:
    async def test_a_parked_session_goes_even_though_it_saw_cars(self, ready):
        """Journey 280 exactly: nothing moved, and eighty-six vehicles went past."""
        parked = await _journey(ready, avg=0.0, top=1.0)
        await _rec(ready, "parked-a.ts", journey=parked, vehicles=43)
        await _rec(ready, "parked-b.ts", journey=parked, vehicles=43, plates=6)

        plan = await plan_parked(ready)

        assert _names(plan) == {"parked-a.ts", "parked-b.ts"}
        assert plan.candidates[0].reason.startswith("parked session")
        assert plan.exclude_from_stats is True

    async def test_a_crawl_that_never_got_up_to_speed_goes(self, ready):
        """Above the average threshold, under the top-speed one. Both have to be met."""
        crawl = await _journey(ready, avg=6.0, top=6.0)
        await _rec(ready, "crawl.ts", journey=crawl)

        assert _names(await plan_parked(ready)) == {"crawl.ts"}

    async def test_a_post_processing_plan_only_considers_the_notified_batch(self, ready):
        parked = await _journey(ready, avg=0.0, top=0.0)
        first = await _rec(ready, "one.ts", journey=parked)
        await _rec(ready, "two.ts", journey=parked)

        plan = await plan_parked(ready, recording_ids=[first.id])

        assert _names(plan) == {"one.ts"}


class TestWhatStays:
    async def test_a_real_drive_is_kept(self, ready):
        drive = await _journey(ready, avg=42.0, top=80.0)
        await _rec(ready, "drive.ts", journey=drive)

        assert _names(await plan_parked(ready)) == set()

    async def test_a_journey_whose_speed_was_never_read_is_kept(self, ready):
        """The load-bearing asymmetry: hidden from the list, never deleted for it.

        Nulls here mean the overlay was unreadable or telemetry never ran, not that the car
        stood still. Absence of evidence is a reason to keep footage.
        """
        unknown = await _journey(ready, avg=None, top=None)
        await _rec(ready, "unknown.ts", journey=unknown)

        assert _names(await plan_parked(ready)) == set()

    async def test_a_half_read_journey_is_kept(self, ready):
        """One figure present and low is not enough; both have to be known."""
        half = await _journey(ready, avg=0.0, top=None)
        await _rec(ready, "half.ts", journey=half)

        assert _names(await plan_parked(ready)) == set()

    async def test_a_journey_still_being_analysed_is_kept_entirely(self, ready):
        """Rollups are computed from every clip, so a pending sibling makes them provisional."""
        parked = await _journey(ready, avg=0.0, top=0.0)
        await _rec(ready, "settled.ts", journey=parked)
        await _rec(ready, "pending.ts", journey=parked, telemetry=StageState.PENDING)

        assert _names(await plan_parked(ready)) == set()

    async def test_an_outdated_sibling_also_holds_the_whole_journey(self, ready):
        """Null-safety: an old revision must count as unsettled, not fall out of the count."""
        parked = await _journey(ready, avg=0.0, top=0.0)
        await _rec(ready, "current.ts", journey=parked)
        await _rec(ready, "stale.ts", journey=parked, telemetry_revision="telemetry-v0")

        assert _names(await plan_parked(ready)) == set()

    async def test_a_journey_with_work_queued_against_it_is_kept_entirely(self, ready):
        """The rule that would otherwise delete a file out from under its own reprocess."""
        parked = await _journey(ready, avg=0.0, top=0.0)
        busy = await _rec(ready, "busy.ts", journey=parked)
        await _rec(ready, "sibling.ts", journey=parked)
        ready.add(ProcessingJob(recording_id=busy.id, state=JobState.QUEUED))
        await ready.flush()

        assert _names(await plan_parked(ready)) == set()

    async def test_protected_and_event_clips_are_kept_inside_a_parked_session(self, ready):
        parked = await _journey(ready, avg=0.0, top=0.0)
        await _rec(ready, "kept-protected.ts", journey=parked, protected=True)
        await _rec(ready, "kept-event.ts", journey=parked, event="harsh_braking")
        await _rec(ready, "goes.ts", journey=parked)

        assert _names(await plan_parked(ready)) == {"goes.ts"}

    async def test_a_recording_in_no_journey_at_all_is_kept(self, ready):
        await _rec(ready, "orphan.ts", journey=None)

        assert _names(await plan_parked(ready)) == set()


class TestTheSwitchesAndGuards:
    async def test_the_rule_off_proposes_and_authorises_nothing(self, ready):
        await get_settings_service().set("storage.delete_parked_journeys", False)
        parked = await _journey(ready, avg=0.0, top=0.0)
        await _rec(ready, "parked.ts", journey=parked)

        plan = await plan_parked(ready)

        assert plan.candidates == []
        assert plan.deletion_enabled is False

    async def test_it_authorises_its_own_deletion_when_on(self, ready):
        parked = await _journey(ready, avg=0.0, top=0.0)
        await _rec(ready, "parked.ts", journey=parked)

        assert (await plan_parked(ready)).deletion_enabled is True

    async def test_both_thresholds_at_zero_identify_nothing(self, ready):
        """Consistent with the list, which shows everything at zero rather than nothing."""
        settings = get_settings_service()
        await settings.set("journeys.min_avg_speed_kmh", 0.0)
        await settings.set("journeys.min_top_speed_kmh", 0.0)
        parked = await _journey(ready, avg=0.0, top=0.0)
        await _rec(ready, "parked.ts", journey=parked)

        assert _names(await plan_parked(ready)) == set()

    async def test_a_runaway_plan_blocks_rather_than_deletes(self, ready):
        """A telemetry regression calling the whole library stationary must not act."""
        await get_settings_service().set("storage.max_delete_fraction", 0.10)
        parked = await _journey(ready, avg=0.0, top=0.0)
        await _rec(ready, "big.ts", journey=parked, size=10_000_000)

        plan = await plan_parked(ready)

        assert plan.blocked is True
        assert "single-run safety limit" in plan.blocked_reason


class TestItActuallyDeletes:
    async def test_the_file_goes_with_the_master_switch_off(self, ready, temp_dirs):
        """No per-run enable step, same as the static rule: the plan authorised itself."""
        _, footage = temp_dirs
        clip = footage / "parked.ts"
        clip.write_bytes(b"G" * 4096)
        parked = await _journey(ready, avg=0.0, top=1.0)
        recording = await _rec(ready, "parked.ts", journey=parked, vehicles=12, size=4096)
        # A real drive in the index, so the parked clip is a small fraction of the library.
        drive = await _journey(ready, avg=44.0, top=90.0)
        await _rec(ready, "drive.ts", journey=drive, size=500_000)
        assert bool(get_settings_service().get_nowait("storage.enable_deletion")) is False

        plan = await plan_parked(ready)
        run = await run_retention(ready, plan, dry_run=False, trigger="parked-cleanup")

        assert run.deleted_count == 1
        assert not clip.exists()
        assert recording.state is RecordingState.DELETED and recording.file_missing is True
        assert recording.ignored is True, "discarded footage must leave every stats view"

    async def test_an_unsafe_mount_blocks_it(self, ready, monkeypatch):
        """Self-authorising is not unguarded: an unmounted share reads as empty."""
        from app.retention import safety as safety_mod

        async def _measured(*_a, **_kw):
            return (0, 0)

        monkeypatch.setattr(safety_mod, "measure_tree", _measured)
        parked = await _journey(ready, avg=0.0, top=0.0)
        await _rec(ready, "parked.ts", journey=parked)

        plan = await plan_parked(ready)
        run = await run_retention(ready, plan, dry_run=False, trigger="parked-cleanup")

        assert run.deleted_count == 0
        assert run.blocked
