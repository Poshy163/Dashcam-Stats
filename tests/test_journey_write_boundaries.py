"""Real WAL writers remain available while journey plans are being derived."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import select, update

from app.db.models import IngestRadioTransition, Journey, Recording, RecordingState, TelemetryPoint
from app.db.session import get_session_factory
from app.ingest import obd_control, poller, puller, radio_coordinator
from app.ingest.models import RunResult, RunState
from app.ingest.status import IngestStatus
from app.journeys.builder import JourneyBuilder, JourneyRefreshConflict
from app.pipeline import stages

BASE = datetime(2026, 10, 22, 7, tzinfo=UTC)


async def seed(session, *, existing=True):
    journey = Journey(started_at=BASE, ended_at=BASE + timedelta(seconds=60))
    if existing:
        session.add(journey)
        await session.flush()
    recording = Recording(
        filename="20261022173100_camera_0.ts",
        rel_path="new.ts",
        state=RecordingState.PROCESSING,
        started_at=BASE + timedelta(seconds=60),
        ended_at=BASE + timedelta(seconds=120),
        duration_s=60,
    )
    session.add(recording)
    await session.flush()
    if existing:
        session.add(
            Recording(
                filename="20261022173000_camera_0.ts",
                rel_path="old.ts",
                state=RecordingState.COMPLETED,
                started_at=BASE,
                ended_at=BASE + timedelta(seconds=60),
                duration_s=60,
                journey_id=journey.id,
            )
        )
    for offset in (0, 20, 40, 59):
        session.add(
            TelemetryPoint(
                recording_id=recording.id,
                t_offset_s=offset,
                captured_at=BASE + timedelta(seconds=60 + offset),
                has_fix=True,
                lat=-34.8 + offset * 0.0001,
                lon=138.6,
                speed_kmh=40,
            )
        )
    await session.commit()
    return recording, journey if existing else None


def pause_derivation(monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    original = JourneyBuilder._find_stale_sighting_positions

    async def derive(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    # This hook also exists in the pre-fix implementation, after autoflush/attachment
    # had taken its writer. The test therefore detects the actual SQLite lock, rather
    # than merely checking whether the new planning helper exists.
    monkeypatch.setattr(JourneyBuilder, "_find_stale_sighting_positions", staticmethod(derive))
    return entered, release


@pytest.mark.parametrize("existing", [False, True])
async def test_summary_derivation_does_not_starve_radio_preparation_or_heartbeat(
    db_session,
    monkeypatch,
    existing,
):
    recording, _ = await seed(db_session, existing=existing)
    controller = SimpleNamespace(report=None, claim=Mock(), release=AsyncMock())
    monkeypatch.setattr(radio_coordinator.radios, "RadioController", lambda *a, **k: controller)
    monkeypatch.setattr(radio_coordinator.radios, "new_quieting_allowed", lambda: True)
    monkeypatch.setattr(
        radio_coordinator.radios, "read_device_boot_id", AsyncMock(return_value="boot")
    )
    monkeypatch.setattr(radio_coordinator, "HEARTBEAT_INTERVAL_S", 60)
    ack = SimpleNamespace(ready=True)
    monkeypatch.setattr(obd_control, "request_quiesce", AsyncMock(return_value=ack))
    entered, release = pause_derivation(monkeypatch)
    summary = asyncio.create_task(stages.stage_summarise(db_session, recording))
    transition = None
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        # Each operation writes through a different pooled SQLite connection while
        # real summarisation retains its pending completion and membership changes.
        transition = await asyncio.wait_for(
            radio_coordinator.begin(
                trigger="test",
                address="unit:5555",
                logger_status=None,
                logger_status_path="/sdcard/obd/status.json",
                watchdog_deadline_s=120,
            ),
            timeout=2,
        )
        await transition._stop_heartbeat()
        assert await asyncio.wait_for(transition.prepare_logger(), timeout=2) is ack
        await asyncio.wait_for(
            transition.checkpoint(last_error="heartbeat while deriving"), timeout=2
        )
        async with get_session_factory()() as observer:
            row = await observer.get(IngestRadioTransition, transition.id)
            assert row.logger_quiesce_requested_at is not None
            assert row.logger_quiesce_acked_at is not None
            assert row.last_error == "heartbeat while deriving"
            assert (await observer.get(Recording, recording.id)).state == RecordingState.PROCESSING
        assert not transition.lease_lost
    finally:
        release.set()
        await asyncio.wait_for(summary, timeout=3)
        if transition is not None:
            await transition.close()
    await db_session.commit()
    await db_session.refresh(recording)
    assert recording.state == RecordingState.COMPLETED
    assert recording.journey_id is not None


@pytest.mark.parametrize("change", ["manual_target", "manual_member_move", "new_member"])
async def test_incremental_publication_rejects_a_concurrent_membership_change(
    db_session,
    monkeypatch,
    change,
):
    recording, journey = await seed(db_session)
    recording_id, journey_id = recording.id, journey.id
    entered, release = pause_derivation(monkeypatch)
    task = asyncio.create_task(stages.stage_summarise(db_session, recording))
    await asyncio.wait_for(entered.wait(), timeout=2)
    try:
        async with get_session_factory()() as editor:
            if change == "manual_target":
                await editor.execute(
                    update(Journey)
                    .where(Journey.id == journey_id)
                    .values(manual=True, title="operator choice")
                )
            elif change == "manual_member_move":
                manual = Journey(started_at=BASE, ended_at=BASE + timedelta(minutes=5), manual=True)
                editor.add(manual)
                await editor.flush()
                await editor.execute(
                    update(Recording)
                    .where(Recording.id == recording_id)
                    .values(journey_id=manual.id)
                )
            else:
                editor.add(
                    Recording(
                        filename="concurrent.ts",
                        rel_path="concurrent.ts",
                        journey_id=journey_id,
                        started_at=BASE,
                        ended_at=BASE + timedelta(seconds=60),
                        state=RecordingState.COMPLETED,
                    )
                )
            await editor.commit()
    finally:
        release.set()
    with pytest.raises(JourneyRefreshConflict, match="changed"):
        await task
    await db_session.rollback()
    async with get_session_factory()() as observer:
        current = await observer.get(Recording, recording_id)
        assert current.state == RecordingState.PROCESSING
        if change == "manual_target":
            target = await observer.get(Journey, journey_id)
            assert target.manual and target.title == "operator choice"
        elif change == "manual_member_move":
            assert current.journey_id != journey_id
            assert (await observer.get(Journey, current.journey_id)).manual
        else:
            assert await observer.scalar(
                select(Recording.id).where(Recording.filename == "concurrent.ts")
            )


async def test_rebuild_derivation_keeps_writer_available_and_manual_plan_is_preserved(
    db_session,
    monkeypatch,
):
    recording, journey = await seed(db_session)
    recording.state = RecordingState.COMPLETED
    await db_session.commit()
    journey_id = journey.id
    entered, release = pause_derivation(monkeypatch)
    task = asyncio.create_task(JourneyBuilder().rebuild(db_session))
    await asyncio.wait_for(entered.wait(), timeout=2)
    try:
        async with get_session_factory()() as editor:
            await asyncio.wait_for(
                editor.execute(
                    update(Journey)
                    .where(Journey.id == journey_id)
                    .values(manual=True, title="during rebuild")
                ),
                timeout=2,
            )
            await editor.commit()
    finally:
        release.set()
    await task
    async with get_session_factory()() as observer:
        target = await observer.get(Journey, journey_id)
        assert target.manual and target.title == "during rebuild"


async def test_manual_target_change_between_candidate_selection_and_snapshot_is_rejected(
    db_session,
    monkeypatch,
):
    recording, journey = await seed(db_session)
    journey_id = journey.id
    prepare = JourneyBuilder._prepare_refresh

    async def edit_before_snapshot(self, session, target, **kwargs):
        async with get_session_factory()() as editor:
            await editor.execute(
                update(Journey)
                .where(Journey.id == journey_id)
                .values(manual=True, title="early operator choice")
            )
            await editor.commit()
        return await prepare(self, session, target, **kwargs)

    monkeypatch.setattr(JourneyBuilder, "_prepare_refresh", edit_before_snapshot)
    with pytest.raises(JourneyRefreshConflict, match="changed before"):
        await stages.stage_summarise(db_session, recording)
    # A retained caller cannot commit the rejected pending completion later.
    await db_session.commit()
    async with get_session_factory()() as observer:
        current = await observer.get(Journey, journey_id)
        assert current.manual and current.title == "early operator choice"


async def test_worker_change_defers_one_cluster_and_finishes_unrelated_clusters(
    db_session,
    monkeypatch,
):
    recording, journey = await seed(db_session)
    recording.state = RecordingState.COMPLETED
    later = Recording(
        filename="later.ts",
        rel_path="later.ts",
        state=RecordingState.COMPLETED,
        started_at=BASE + timedelta(hours=2),
        ended_at=BASE + timedelta(hours=2, seconds=60),
        duration_s=60,
    )
    db_session.add(later)
    await db_session.commit()
    recording_id, journey_id, later_id = recording.id, journey.id, later.id
    entered, release = pause_derivation(monkeypatch)
    task = asyncio.create_task(JourneyBuilder().rebuild(db_session))
    await asyncio.wait_for(entered.wait(), timeout=2)
    try:
        async with get_session_factory()() as worker:
            await worker.execute(
                update(Recording).where(Recording.id == recording_id).values(vehicle_count=999)
            )
            await worker.commit()
    finally:
        release.set()
    assert await task == 1
    async with get_session_factory()() as observer:
        changed = await observer.get(Recording, recording_id)
        assert changed.vehicle_count == 999
        assert changed.journey_id is None
        assert (await observer.get(Journey, journey_id)).id == journey_id
        assert (await observer.get(Recording, later_id)).journey_id is not None


@pytest.mark.parametrize("files,bytes_", [(0, 0), (1, 100)])
def test_terminal_safety_cancellation_keeps_committed_counts(monkeypatch, files, bytes_):
    status = IngestStatus()
    status.try_begin()
    status.cancel(reason="radio_safety_lease_lost", detail="checkpoint database is locked")
    monkeypatch.setattr(puller, "get_status", lambda: status)
    result = puller._classify_cancellation(
        RunResult(
            state=RunState.CANCELLED,
            files=files,
            bytes=bytes_,
            error="the transfer was cancelled",
        )
    )
    assert result.state is (RunState.PARTIAL if files else RunState.ERROR)
    assert (result.files, result.bytes) == (files, bytes_)
    assert "database is locked" in result.error


def test_safety_cancellation_retains_preparation_failure_and_is_retryable(monkeypatch):
    status = IngestStatus()
    status.try_begin()
    status.cancel(reason="radio_safety_lease_lost")
    monkeypatch.setattr(puller, "get_status", lambda: status)
    result = puller._classify_cancellation(
        puller._radio_shutdown_hold("logger preparation failed: database is locked")
    )
    assert result.state is RunState.ERROR
    assert "database is locked" in result.error
    assert "cancelled" not in result.error
    status.finish(result)
    # The existing bounded ERROR policy permits a safe retry, unlike operator Stop.
    current = poller.IngestPoller()
    status._finished_at = 0
    assert current._should_drain_again(status)


@pytest.mark.parametrize("safety_first", [False, True])
def test_operator_stop_is_not_reclassified_or_rearmed_by_later_safety_failure(
    monkeypatch, safety_first
):
    status = IngestStatus()
    status.try_begin()
    if safety_first:
        status.cancel(reason="radio_safety_lease_lost")
    status.cancel()
    status.cancel(reason="radio_safety_lease_lost")
    monkeypatch.setattr(puller, "get_status", lambda: status)
    result = puller._radio_shutdown_hold("database is locked")
    assert result.state is RunState.CANCELLED
    assert not poller.IngestPoller()._should_drain_again(SimpleNamespace(state=RunState.CANCELLED))
    status.finish(RunResult(state=RunState.CANCELLED))
    assert status.try_begin()
    assert status.cancel_reason is None


def test_operator_stop_during_cleanup_prevents_retry_of_preparation_failure(monkeypatch):
    status = IngestStatus()
    status.try_begin()
    status.cancel(reason="radio_safety_lease_lost", detail="database is locked")
    monkeypatch.setattr(puller, "get_status", lambda: status)
    result = puller._radio_shutdown_hold("logger preparation failed: database is locked")
    # This result still awaits radio/staging cleanup before finalisation.
    assert result.state is RunState.CANCELLED
    status.cancel()
    result = puller._classify_cancellation(result)
    assert result.state is RunState.CANCELLED
    assert "database is locked" in result.error
    assert not poller.IngestPoller()._should_drain_again(SimpleNamespace(state=result.state))


@pytest.mark.parametrize("state", [RunState.ERROR, RunState.PARTIAL])
def test_operator_stop_outranks_exception_result_reconstructed_after_cleanup(monkeypatch, state):
    status = IngestStatus()
    status.try_begin()
    status.cancel(reason="radio_safety_lease_lost", detail="database is locked")
    status.cancel()
    monkeypatch.setattr(puller, "get_status", lambda: status)
    result = puller._classify_cancellation(
        RunResult(
            state=state,
            files=2,
            bytes=100,
            error="database is locked; radio recovery remains pending",
        )
    )
    assert result.state is RunState.CANCELLED
    assert (result.files, result.bytes) == (2, 100)
    assert "database is locked" in result.error
    assert "recovery remains pending" in result.error
    assert not poller.IngestPoller()._should_drain_again(SimpleNamespace(state=result.state))
