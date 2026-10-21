"""Intentional camera omissions are auditable without creating phantom media."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from test_parked_filter import make_policy

from app.db.models import (
    ClipTransferEvaluation,
    ClipTransferExclusion,
    Journey,
    ProcessingJob,
    Recording,
    RecordingState,
)
from app.db.session import session_scope
from app.ingest import clip_exclusions, parked_filter
from app.ingest.clip_exclusions import POLICY_VERSION, REASON, exclusion_summary
from app.journeys.builder import JourneyBuilder
from app.scanner.discovery import Scanner, queue_unprocessed

BASE = datetime(2026, 10, 21, 2, 30, tzinfo=UTC)


@pytest.fixture
def quiet_policy():
    # Reuse the current Android/backend wire contract; ledger tests need only one
    # clip because paired-lens admission is exercised by the transfer filter.
    policy = make_policy()
    policy["decisions"] = policy["decisions"][:1]
    policy["decisions"][0]["clip"]["size_bytes"] = 18800
    result = parked_filter.validate_policy_manifest(json.dumps(policy))
    assert parked_filter.eligible_skip(result["decisions"][0])
    return result


async def record_policy(policy, **overrides):
    return await clip_exclusions.record_evaluation(
        **{
            "manifest": policy,
            "excluded": policy["decisions"],
            "status": "ready",
            "considered_count": len(policy["decisions"]),
            "considered_bytes": sum(entry["clip"]["size_bytes"] for entry in policy["decisions"]),
            "policy_enabled": True,
            **overrides,
        }
    )


async def test_committed_decisions_deduplicate_and_new_empty_plan_preserves_history(
    db_session,
    quiet_policy,
):
    first = await record_policy(quiet_policy)
    second = await record_policy(quiet_policy)
    assert first["generation"] != second["generation"]
    async with session_scope() as session:
        rows = list((await session.scalars(select(ClipTransferExclusion))).all())
        assert len(rows) == 1 and rows[0].observation_count == 2
        assert rows[0].latest_generation == second["generation"]
        assert rows[0].proof_json == {
            "guard": quiet_policy["decisions"][0]["guard"],
            "visual": quiet_policy["decisions"][0]["visual"],
        }
    await record_policy(quiet_policy, excluded=[], status="disabled", policy_enabled=False)
    async with session_scope() as session:
        summary = await exclusion_summary(session)
        assert summary["current_count"] == summary["current_bytes"] == 0
        assert summary["historical_count"] == 1
        assert summary["status"] == "disabled"
        assert await session.scalar(select(func.count()).select_from(Recording)) == 0
        assert await session.scalar(select(func.count()).select_from(ProcessingJob)) == 0


@pytest.mark.parametrize("problem", ["stale", "truncated", "wrong_member", "unsafe_proof"])
async def test_unsafe_or_absent_current_proof_is_not_persisted(db_session, quiet_policy, problem):
    selected = copy.deepcopy(quiet_policy["decisions"])
    if problem == "stale":
        quiet_policy["generated_at_utc"] = (datetime.now(UTC) - timedelta(seconds=181)).isoformat()
    elif problem == "truncated":
        quiet_policy["truncated"] = True
    elif problem == "wrong_member":
        selected[0]["clip"]["clip_id"] = str(uuid.uuid4())
    else:
        quiet_policy["decisions"][0]["visual"]["motion_frames"] = 1
        selected = quiet_policy["decisions"]
    with pytest.raises(ValueError):
        await record_policy(quiet_policy, excluded=selected)
    assert await db_session.scalar(select(func.count()).select_from(ClipTransferExclusion)) == 0
    assert await db_session.get(ClipTransferEvaluation, 1) is None


async def test_temporary_deferral_never_becomes_an_intentional_exclusion(db_session, quiet_policy):
    decision = quiet_policy["decisions"][0]
    decision.update(
        decision="defer",
        reason="analysis_pending",
        defer_until_utc=(BASE + timedelta(seconds=420)).isoformat(),
    )
    # A valid defer wire shape still grants no authority to create a skipped-media row.
    parked_filter.validate_policy_manifest(json.dumps(quiet_policy))
    with pytest.raises(ValueError, match="absent or unsafe"):
        await record_policy(quiet_policy)
    assert await db_session.scalar(select(func.count()).select_from(ClipTransferExclusion)) == 0
    assert await db_session.get(ClipTransferEvaluation, 1) is None


async def test_commit_failure_rolls_back_evidence_before_caller_can_exclude(
    db_session,
    quiet_policy,
    monkeypatch,
):
    original = clip_exclusions.session_scope

    @asynccontextmanager
    async def failed_commit():
        async with original() as session:
            yield session
            raise OSError("injected ledger commit failure")

    monkeypatch.setattr(clip_exclusions, "session_scope", failed_commit)
    with pytest.raises(OSError, match="ledger commit"):
        await record_policy(quiet_policy)
    assert await db_session.scalar(select(func.count()).select_from(ClipTransferExclusion)) == 0
    assert await db_session.get(ClipTransferEvaluation, 1) is None


async def test_cancellation_at_commit_does_not_publish_an_exclusion_generation(
    db_session,
    quiet_policy,
    monkeypatch,
):
    at_commit = asyncio.Event()
    original = clip_exclusions.session_scope

    @asynccontextmanager
    async def blocked_commit():
        async with original() as session:
            yield session
            at_commit.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(clip_exclusions, "session_scope", blocked_commit)
    task = asyncio.create_task(record_policy(quiet_policy))
    await asyncio.wait_for(at_commit.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await db_session.scalar(select(func.count()).select_from(ClipTransferExclusion)) == 0
    assert await db_session.get(ClipTransferEvaluation, 1) is None


async def seed_exclusion(session, *, start=60, end=120, current=True):
    """A prior strictly validated camera decision, never a Recording placeholder."""
    generation = str(uuid.uuid4())
    row = ClipTransferExclusion(
        identity_sha256="a" * 64,
        source_id=str(uuid.uuid4()),
        card_id="b" * 64,
        clip_id=str(uuid.uuid4()),
        relative_path="DCIM/Video/20261021130100_camera_0.ts",
        filename="20261021130100_camera_0.ts",
        camera_key="camera_0",
        size_bytes=18800,
        mtime_ms=1_792_546_260_000,
        fingerprint_json={
            "algorithm": "sha256-sampled-v1",
            "sample_bytes": 16384,
            "digest": "c" * 64,
        },
        started_at=BASE + timedelta(seconds=start),
        ended_at=BASE + timedelta(seconds=end),
        policy_version=POLICY_VERSION,
        reason=REASON,
        proof_json={"guard": {"unknown_ms": 0}, "visual": {"status": "quiet"}},
        first_observed_at=datetime.now(UTC),
        last_observed_at=datetime.now(UTC),
        observation_count=1,
        latest_generation=generation,
    )
    session.add(row)
    if current:
        session.add(
            ClipTransferEvaluation(
                id=1,
                generation=generation,
                evaluated_at=datetime.now(UTC),
                policy_enabled=True,
                policy_version=POLICY_VERSION,
                status="ready",
                source_id=row.source_id,
                card_id=row.card_id,
                manifest_generated_at=datetime.now(UTC),
                manifest_sha256="d" * 64,
                considered_count=3,
                considered_bytes=56400,
                excluded_count=1,
                excluded_bytes=row.size_bytes,
            )
        )
    await session.flush()
    return row


def clip(index, start, end):
    return Recording(
        filename=f"quiet-gap-{index}.ts",
        rel_path=f"quiet-gap-{index}.ts",
        size_bytes=100,
        state=RecordingState.COMPLETED,
        started_at=BASE + timedelta(seconds=start),
        ended_at=BASE + timedelta(seconds=end),
        duration_s=end - start,
    )


async def test_audit_is_separate_from_media_jobs_and_backup_durability(db_session, client):
    await seed_exclusion(db_session)
    await db_session.commit()
    assert await db_session.scalar(select(func.count()).select_from(Recording)) == 0
    assert await db_session.scalar(select(func.count()).select_from(ProcessingJob)) == 0
    status = (await client.get("/api/ingest/movement-status")).json()["intentional_exclusions"]
    assert status["current_count"] == status["historical_count"] == 1
    assert status["current_bytes"] == 18800
    assert status["reason_counts"] == {REASON: 1}
    assert status["locally_indexed_count"] == 0
    assert status["counts_as_backed_up"] is status["files_deleted"] is False
    response = await client.get("/api/ingest/clip-exclusions?page_size=1&current_only=true")
    assert response.status_code == 200
    audit = response.json()
    assert audit["total"] == 1 and len(audit["items"]) == 1
    item = audit["items"][0]
    assert item["reason"] == REASON and item["local_recordings"] == []
    assert item["disposition"] == "observed_exclusion_decision"
    assert item["counts_as_backed_up"] is False
    assert (await client.get("/api/ingest/clip-exclusions?page_size=201")).status_code == 422


async def test_scanner_queues_actual_files_without_marking_intentional_omissions_missing(
    db_session,
    app_config,
    client,
):
    exclusion = await seed_exclusion(db_session)
    await db_session.commit()
    uploaded = app_config.footage_dir / "20261021130000_camera_0.ts"
    uploaded.write_bytes(b"\x47" * 18800)
    old = time.time() - 3600
    os.utime(uploaded, (old, old))
    scanner = Scanner(footage_dir=app_config.footage_dir)
    first = await scanner.scan(trigger="test")
    assert first.new == 1 and first.missing == first.errors == 0
    async with session_scope() as session:
        assert await queue_unprocessed(session) == 1
        recordings = list((await session.scalars(select(Recording))).all())
        assert [row.filename for row in recordings] == [uploaded.name]
        assert not recordings[0].file_missing and not recordings[0].ignored
        assert await session.scalar(select(func.count()).select_from(ProcessingJob)) == 1
    # A later ordinary upload/recovery must be processed normally. The historical
    # decision is an annotation, never an instruction to ignore or hide real media.
    recovered = app_config.footage_dir / exclusion.filename
    recovered.write_bytes(b"\x47" * exclusion.size_bytes)
    os.utime(recovered, (old, old))
    second = await scanner.scan(trigger="test")
    assert second.new == 1 and second.missing == second.errors == 0
    async with session_scope() as session:
        assert await queue_unprocessed(session) == 1
        assert await session.scalar(select(func.count()).select_from(Recording)) == 2
        assert await session.scalar(select(func.count()).select_from(ProcessingJob)) == 2
        status = await exclusion_summary(session)
        assert status["current_count"] == status["locally_indexed_count"] == 1
    item = (await client.get("/api/ingest/clip-exclusions")).json()["items"][0]
    assert len(item["local_recordings"]) == 1
    assert item["local_recordings"][0]["size_matches"] is True


@pytest.mark.parametrize("gap_seconds", [30, 60])
async def test_intentional_quiet_gap_does_not_join_drives_within_normal_tolerance(
    db_session,
    gap_seconds,
):
    await seed_exclusion(db_session, start=60, end=60 + gap_seconds)
    rows = [clip(1, 0, 60), clip(2, 60 + gap_seconds, 120 + gap_seconds)]
    db_session.add_all(rows)
    await db_session.flush()
    builder = JourneyBuilder()
    # The established ordinary one-minute tolerance alone would join these drives.
    assert len(builder._cluster(rows, timedelta(seconds=60), False, 0)) == 1
    await builder.rebuild(db_session)
    journeys = list((await db_session.scalars(select(Journey))).all())
    assert len(journeys) == 2
    assert sum(journey.recording_count for journey in journeys) == 2
    db_session.expunge_all()
    assert not await builder.needs_recluster(db_session)


async def test_overlapping_other_camera_and_unrelated_exclusion_do_not_split(db_session):
    await seed_exclusion(db_session)
    rows = [clip(1, 0, 60), clip(2, 1, 130), clip(3, 120, 180)]
    db_session.add_all(rows)
    await db_session.flush()
    builder = JourneyBuilder()
    await builder.rebuild(db_session)
    journeys = list((await db_session.scalars(select(Journey))).all())
    assert len(journeys) == 1 and journeys[0].recording_count == 3
    db_session.expunge_all()
    assert not await builder.needs_recluster(db_session)


async def test_nested_shorter_recording_cannot_move_available_coverage_backwards(db_session):
    await seed_exclusion(db_session, start=90, end=105)
    rows = [clip(1, 0, 180), clip(2, 0, 60), clip(3, 120, 180)]
    db_session.add_all(rows)
    await db_session.flush()
    await JourneyBuilder().rebuild(db_session)
    journeys = list((await db_session.scalars(select(Journey))).all())
    assert len(journeys) == 1 and journeys[0].recording_count == 3


async def test_summary_never_associates_reused_name_with_a_different_size(db_session):
    exclusion = await seed_exclusion(db_session)
    db_session.add(
        Recording(
            filename=exclusion.filename,
            rel_path=exclusion.filename,
            size_bytes=exclusion.size_bytes + 1,
            state=RecordingState.COMPLETED,
        )
    )
    await db_session.flush()
    summary = await exclusion_summary(db_session)
    assert summary["locally_indexed_count"] == 0
    assert summary["local_match_basis"] == "filename_and_size"


async def test_exclusion_outside_uncovered_gap_does_not_split(db_session):
    await seed_exclusion(db_session, start=0, end=30)
    db_session.add_all([clip(1, 0, 60), clip(2, 90, 150)])
    await db_session.flush()
    await JourneyBuilder().rebuild(db_session)
    assert await db_session.scalar(select(func.count()).select_from(Journey)) == 1


async def test_incremental_assignment_and_rebuild_share_intentional_boundary(db_session):
    await seed_exclusion(db_session)
    before, after = clip(1, 0, 60), clip(2, 120, 180)
    db_session.add_all([before, after])
    await db_session.flush()
    builder = JourneyBuilder()
    first = await builder.assign_recording(db_session, before)
    second = await builder.assign_recording(db_session, after)
    assert first.id != second.id
    db_session.expunge_all()
    assert not await builder.needs_recluster(db_session)


async def test_manual_journey_is_preserved_across_intentional_gap(db_session):
    await seed_exclusion(db_session)
    manual = Journey(started_at=BASE, ended_at=BASE + timedelta(seconds=180), manual=True)
    db_session.add(manual)
    await db_session.flush()
    rows = [clip(1, 0, 60), clip(2, 120, 180)]
    for row in rows:
        row.journey_id = manual.id
    db_session.add_all(rows)
    await db_session.flush()
    await JourneyBuilder().rebuild(db_session)
    assert [row.journey_id for row in rows] == [manual.id, manual.id]
    assert await db_session.scalar(select(func.count()).select_from(Journey)) == 1
    assert not await JourneyBuilder().needs_recluster(db_session)
