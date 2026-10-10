"""A rebuild publishes complete groups without taking the old library offline."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.api.visibility import visible_journey_ids
from app.db.models import (
    Journey,
    Plate,
    PlateObservation,
    Recording,
    RecordingState,
    TelemetryPoint,
    TrackedObject,
)
from app.db.session import get_session_factory
from app.journeys.builder import JourneyBuilder
from app.pipeline.revisions import INVALIDATED_REVISION

BASE = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)


async def _journey(session, *, index=0, manual=False, title=None):
    row = Journey(
        started_at=BASE + timedelta(hours=index),
        ended_at=BASE + timedelta(hours=index, minutes=1),
        duration_s=60,
        recording_count=1,
        avg_speed_kmh=40,
        max_speed_kmh=40,
        manual=manual,
        title=title,
    )
    session.add(row)
    await session.flush()
    return row


async def _recording(session, app_config, name, start, journey, **overrides):
    (app_config.footage_dir / name).write_bytes(b"preserved footage")
    row = Recording(
        filename=name,
        rel_path=name,
        size_bytes=17,
        started_at=start,
        ended_at=start + timedelta(seconds=60),
        duration_s=60,
        state=RecordingState.COMPLETED,
        journey_id=journey.id,
        start_lat=-34.8,
        start_lon=138.6,
        **overrides,
    )
    session.add(row)
    await session.flush()
    for sample in range(5):
        session.add(
            TelemetryPoint(
                recording_id=row.id,
                journey_id=journey.id,
                t_offset_s=sample * 5.0,
                captured_at=start + timedelta(seconds=sample * 5),
                lat=-34.8 + sample * 0.0003,
                lon=138.6,
                speed_kmh=40,
                has_fix=True,
                raw_text=f"original overlay {name} {sample}",
            )
        )
    track = TrackedObject(
        recording_id=row.id,
        journey_id=journey.id,
        track_key=1,
        class_label="car",
        first_seen_offset_s=1,
        last_seen_offset_s=2,
        crop_path=f"original-{name}.jpg",
    )
    plate = Plate(normalised_text=name, display_text=name)
    session.add_all([track, plate])
    await session.flush()
    session.add(
        PlateObservation(
            recording_id=row.id,
            journey_id=journey.id,
            tracked_object_id=track.id,
            plate_id=plate.id,
            t_offset_s=1,
            raw_text=f"original plate {name}",
            normalised_text=name,
            ocr_confidence=0.9,
            detection_confidence=0.9,
        )
    )
    await session.flush()
    return row


async def _payload(session):
    """Source data must survive, independently of the derived grouping pointers."""
    selections = (
        (Recording.id, Recording.filename, Recording.state, Recording.file_missing),
        (
            TelemetryPoint.id,
            TelemetryPoint.recording_id,
            TelemetryPoint.raw_text,
            TelemetryPoint.lat,
            TelemetryPoint.lon,
            TelemetryPoint.speed_kmh,
            TelemetryPoint.has_fix,
        ),
        (TrackedObject.id, TrackedObject.recording_id, TrackedObject.crop_path),
        (Plate.id, Plate.normalised_text, Plate.display_text),
        (PlateObservation.id, PlateObservation.recording_id, PlateObservation.raw_text),
    )
    return [
        list((await session.execute(select(*columns).order_by(columns[0]))).all())
        for columns in selections
    ]


async def _memberships(session):
    return dict((await session.execute(select(Recording.id, Recording.journey_id))).all())


async def _assert_attached(session, expected_recordings):
    members = await _memberships(session)
    assert set(members) == expected_recordings
    existing = set((await session.scalars(select(Journey.id))).all())
    assert set(members.values()) <= existing
    for model in (TelemetryPoint, TrackedObject, PlateObservation):
        rows = (await session.execute(select(model.recording_id, model.journey_id))).all()
        assert all(journey_id == members[recording_id] for recording_id, journey_id in rows)


async def test_an_independent_reader_keeps_the_library_at_each_commit(
    db_session, app_config, monkeypatch
):
    from app.journeys import builder as module

    journeys = [await _journey(db_session, index=i, title=f"drive {i}") for i in range(3)]
    records = [
        await _recording(db_session, app_config, f"drive-{i}.ts", BASE + timedelta(hours=i), j)
        for i, j in enumerate(journeys)
    ]
    await db_session.commit()
    before = await _payload(db_session)
    original_ids = [j.id for j in journeys]
    recording_ids = {r.id for r in records}
    seen = []
    real_commit = module.commit_with_retry

    async def inspect_commit(session, *, what):
        await real_commit(session, what=what)
        if what == "rebuild journey":
            async with get_session_factory()() as reader:
                await _assert_attached(reader, recording_ids)
                assert await _payload(reader) == before
                visible = set((await reader.scalars(visible_journey_ids())).all())
                assert visible == set(original_ids)
                seen.append(visible)

    monkeypatch.setattr(module, "commit_with_retry", inspect_commit)
    builder = JourneyBuilder()
    assert await builder.rebuild(db_session) == 3
    assert len(seen) == 3
    assert await builder.needs_recluster(db_session) is False
    assert [(await db_session.get(Journey, journey_id)).title for journey_id in original_ids] == [
        "drive 0",
        "drive 1",
        "drive 2",
    ]
    for record in records:
        assert (app_config.footage_dir / record.filename).read_bytes() == b"preserved footage"


async def test_an_interrupted_cluster_rolls_back_without_orphaning_the_rest(
    db_session, app_config, monkeypatch
):
    journeys = [await _journey(db_session, index=i // 2) for i in range(6)]
    records = [
        await _recording(
            db_session,
            app_config,
            f"crash-{i}.ts",
            BASE + timedelta(hours=i // 2, seconds=30 * (i % 2)),
            j,
        )
        for i, j in enumerate(journeys)
    ]
    await db_session.commit()
    before = await _payload(db_session)
    memberships = await _memberships(db_session)
    # Keep the first committed merge, but roll back membership changes in the next.
    after_first = dict(memberships)
    after_first[records[1].id] = journeys[0].id
    builder = JourneyBuilder()
    real_refresh = builder.refresh
    calls = 0

    async def fail_second(session, journey):
        nonlocal calls
        calls += 1
        await real_refresh(session, journey)
        if calls == 2:
            raise RuntimeError("interrupted after attaching the second group")
        return journey

    monkeypatch.setattr(builder, "refresh", fail_second)
    with pytest.raises(RuntimeError, match="interrupted"):
        await builder.rebuild(db_session)
    async with get_session_factory()() as reader:
        await _assert_attached(reader, set(memberships))
        assert await _payload(reader) == before
        assert await _memberships(reader) == after_first
    # A session retained by the caller cannot accidentally publish the failed cluster.
    await db_session.commit()
    assert await _payload(db_session) == before
    assert await _memberships(db_session) == after_first
    assert len(list(app_config.footage_dir.iterdir())) == len(records)


async def test_split_and_merge_converge_without_refreshing_a_temporary_mixture(
    db_session, app_config, monkeypatch
):
    source = await _journey(db_session, title="original school run")
    fragment = await _journey(db_session, index=2, title="additional title")
    early = await _recording(db_session, app_config, "early.ts", BASE, source)
    late = await _recording(db_session, app_config, "late.ts", BASE + timedelta(hours=2), source)
    extra = await _recording(
        db_session, app_config, "extra.ts", BASE + timedelta(hours=2, seconds=30), fragment
    )
    await db_session.commit()
    before = await _payload(db_session)
    source_id, fragment_id = source.id, fragment.id
    early_id, late_id, extra_id = early.id, late.id, extra.id
    builder = JourneyBuilder()
    real_refresh = builder.refresh
    refreshed = []

    async def inspect_refresh(session, journey):
        members = set(
            (
                await session.scalars(
                    select(Recording.id).where(Recording.journey_id == journey.id)
                )
            ).all()
        )
        assert members in ({early_id}, {late_id, extra_id})
        refreshed.append(members)
        return await real_refresh(session, journey)

    monkeypatch.setattr(builder, "refresh", inspect_refresh)
    assert await builder.rebuild(db_session) == 2
    assert refreshed == [{early_id}, {late_id, extra_id}]
    assert await _payload(db_session) == before
    members = await _memberships(db_session)
    assert members[early_id] != members[late_id] == members[extra_id] == source_id
    assert (await db_session.get(Journey, source_id)).title == "original school run"
    assert (await db_session.get(Journey, fragment_id)).title == "additional title"
    assert await builder.needs_recluster(db_session) is False
    assert await builder.rebuild(db_session) == 2
    assert await _memberships(db_session) == members


async def test_partial_rebuild_preserves_manual_hidden_invalidated_and_retired_members(
    db_session, app_config
):
    source = await _journey(db_session, title="historical source")
    manual = await _journey(db_session, index=3, manual=True, title="hand edited")
    old = await _recording(db_session, app_config, "outside.ts", BASE, source)
    recent = await _recording(
        db_session, app_config, "recent.ts", BASE + timedelta(hours=2), source
    )
    hidden = await _recording(
        db_session, app_config, "hidden.ts", BASE + timedelta(hours=2), source, ignored=True
    )
    pending = await _recording(
        db_session,
        app_config,
        "invalidated.ts",
        BASE + timedelta(hours=2),
        source,
        telemetry_revision=INVALIDATED_REVISION,
    )
    fixed = await _recording(db_session, app_config, "manual.ts", BASE + timedelta(hours=3), manual)
    retired = await _recording(
        db_session, app_config, "retired.ts", BASE + timedelta(hours=4), source
    )
    retired.state = RecordingState.DELETED
    retired.file_missing = True
    await db_session.commit()
    before = await _payload(db_session)
    source_id, manual_id = source.id, manual.id
    old_id, recent_id, hidden_id, pending_id, fixed_id, retired_id = (
        row.id for row in (old, recent, hidden, pending, fixed, retired)
    )
    builder = JourneyBuilder()
    assert await builder.rebuild(db_session, since=BASE + timedelta(hours=1)) == 2
    members = await _memberships(db_session)
    assert members[old_id] == members[hidden_id] == members[pending_id] == source_id
    assert members[fixed_id] == manual_id
    assert members[recent_id] not in {source_id, manual_id}
    assert members[retired_id] not in {source_id, manual_id, members[recent_id]}
    assert (await db_session.get(Journey, manual_id)).title == "hand edited"
    assert (await db_session.get(Journey, manual_id)).manual is True
    assert (await db_session.get(Journey, source_id)).title == "historical source"
    assert await _payload(db_session) == before
    await _assert_attached(db_session, set(members))


async def test_a_partial_cutoff_does_not_detach_a_valid_journeys_short_tail(db_session, app_config):
    from app.core.settings_service import get_settings_service

    await get_settings_service().set("journeys.min_recordings", 2)
    source = await _journey(db_session, title="drive crossing the cutoff")
    await _recording(db_session, app_config, "before-cutoff.ts", BASE, source)
    await _recording(
        db_session, app_config, "after-cutoff.ts", BASE + timedelta(seconds=30), source
    )
    await db_session.commit()
    before = await _memberships(db_session)
    payload = await _payload(db_session)
    assert await JourneyBuilder().rebuild(db_session, since=BASE + timedelta(seconds=20)) == 0
    assert await _memberships(db_session) == before
    assert await _payload(db_session) == payload
    await _assert_attached(db_session, set(before))


async def test_a_manual_edit_after_planning_is_not_overwritten(db_session, app_config, monkeypatch):
    from app.journeys import builder as module

    source = await _journey(db_session)
    record = await _recording(db_session, app_config, "manual-race.ts", BASE, source)
    await db_session.commit()
    source_id, recording_id = source.id, record.id
    real_commit = module.commit_with_retry
    edited = False

    async def edit_after_preparation(session, *, what):
        nonlocal edited
        await real_commit(session, what=what)
        if what == "prepare journey positions" and not edited:
            async with get_session_factory()() as editor:
                journey = await editor.get(Journey, source_id)
                journey.manual = True
                journey.title = "concurrent manual edit"
                await editor.commit()
            edited = True

    monkeypatch.setattr(module, "commit_with_retry", edit_after_preparation)
    assert await JourneyBuilder().rebuild(db_session) == 0
    async with get_session_factory()() as reader:
        journey = await reader.get(Journey, source_id)
        assert journey.manual is True
        assert journey.title == "concurrent manual edit"
        assert await _memberships(reader) == {recording_id: source_id}


async def test_a_new_worker_member_keeps_its_source_and_cannot_mix_two_drives(
    db_session, app_config, monkeypatch
):
    from app.journeys import builder as module

    source = await _journey(db_session, title="preserved source title")
    old = await _recording(db_session, app_config, "planned.ts", BASE, source)
    await db_session.commit()
    source_id, old_id = source.id, old.id
    real_commit = module.commit_with_retry
    new_id = None

    async def add_after_preparation(session, *, what):
        nonlocal new_id
        await real_commit(session, what=what)
        if what == "prepare journey positions" and new_id is None:
            async with get_session_factory()() as worker:
                journey = await worker.get(Journey, source_id)
                new = await _recording(
                    worker, app_config, "new-worker.ts", BASE + timedelta(hours=2), journey
                )
                new_id = new.id
                await worker.commit()

    monkeypatch.setattr(module, "commit_with_retry", add_after_preparation)
    builder = JourneyBuilder()
    assert await builder.rebuild(db_session) == 1
    members = await _memberships(db_session)
    assert members[new_id] == source_id
    assert members[old_id] != source_id
    assert (await db_session.get(Journey, source_id)).title == "preserved source title"
    await _assert_attached(db_session, {old_id, new_id})
    assert await builder.needs_recluster(db_session) is False


@pytest.mark.parametrize("interrupt", [False, True])
async def test_overlapping_rebuilds_wait_and_release_the_lock_on_cancellation(
    db_session, monkeypatch, interrupt
):
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def held_rebuild(self, session, *, since=None):
        calls.append(session)
        if len(calls) == 1:
            entered.set()
            await release.wait()
        return len(calls)

    monkeypatch.setattr(JourneyBuilder, "_rebuild", held_rebuild)
    first = asyncio.create_task(JourneyBuilder().rebuild(db_session))
    await entered.wait()
    async with get_session_factory()() as other:
        second = asyncio.create_task(JourneyBuilder().rebuild(other))
        await asyncio.sleep(0)
        assert calls == [db_session]
        if interrupt:
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
        else:
            release.set()
            assert await first == 1
        assert await asyncio.wait_for(second, timeout=2) == 2
        assert calls == [db_session, other]
