"""Plate preview I/O must never pin SQLite's writer or destroy committed previews."""

from __future__ import annotations

import asyncio
import sqlite3
import threading

import numpy as np
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError

from app.ai.normalise_au import normalise
from app.ai.plates import PlateReading, vote_track_plate
from app.db.models import Plate, PlateObservation, Recording, TrackedObject
from app.db.session import get_session_factory
from app.pipeline import stages


def placed(track, value="S123ABC", colour=80):
    image = np.full((20, 60, 3), colour, dtype=np.uint8)
    reading = PlateReading(value, 0.99, 0.95, (0.1, 0.2, 0.5, 0.4), crop=image)
    return [(stages._PlateHit(track, normalise(value), vote_track_plate([reading]), image), None)]


async def seed(session, name="crop.ts"):
    recording = Recording(rel_path=name, filename=name, size_bytes=100)
    session.add(recording)
    await session.flush()
    track = TrackedObject(
        recording_id=recording.id,
        track_key=1,
        class_label="car",
        confidence_max=0.9,
        confidence_avg=0.9,
        first_seen_offset_s=0,
        last_seen_offset_s=2,
        duration_s=2,
        frame_count=5,
        best_frame_offset_s=1,
    )
    session.add(track)
    await session.commit()
    await stages._write_observations(session, recording, placed(track), set(), None)
    return recording, track


async def observations(recording_id):
    async with get_session_factory()() as other:
        return (
            await other.execute(
                select(
                    PlateObservation.normalised_text,
                    PlateObservation.plate_crop_path,
                    PlateObservation.vehicle_crop_path,
                ).where(PlateObservation.recording_id == recording_id)
            )
        ).all()


async def concurrent_writer(name):
    async with get_session_factory()() as other:
        await other.execute(text("PRAGMA busy_timeout=50"))
        other.add(Recording(rel_path=name, filename=name, size_bytes=1))
        await asyncio.wait_for(other.commit(), timeout=2)


async def test_crop_io_allows_other_writer_and_keeps_old_rows_visible(db_session, monkeypatch):
    recording, track = await seed(db_session)
    before = await observations(recording.id)
    save = stages._save_jpegs
    calls = 0

    async def inspect_then_save(batch, quality):
        nonlocal calls
        calls += 1
        await concurrent_writer("during-crop-write.ts")
        assert await observations(recording.id) == before
        return await save(batch, quality)

    monkeypatch.setattr(stages, "_save_jpegs", inspect_then_save)
    await stages._write_observations(
        db_session, recording, placed(track, "S456DEF", 150), set(), None
    )
    after = await observations(recording.id)
    assert calls == 1
    assert [row[0] for row in after] == ["S456DEF"]
    assert all(row[1] and row[2] for row in after)


async def test_failed_db_replacement_preserves_previous_rows_and_images(
    db_session, app_config, monkeypatch
):
    recording, track = await seed(db_session)
    recording_id = recording.id
    before = await observations(recording_id)
    old_files = [app_config.media_dir / path for row in before for path in row[1:]]
    insert = stages._insert_observations

    async def fail_after_insert(*args):
        await insert(*args)
        await args[0].flush()
        raise RuntimeError("failed replacement")

    monkeypatch.setattr(stages, "_insert_observations", fail_after_insert)
    with pytest.raises(RuntimeError, match="failed replacement"):
        await stages._write_observations(
            db_session, recording, placed(track, "S456DEF"), set(), None
        )
    assert await observations(recording_id) == before
    assert all(path.is_file() for path in old_files)
    assert set((app_config.media_dir / "plates").rglob("*.jpg")) == set(old_files)
    async with get_session_factory()() as other:
        assert (await other.scalars(select(Plate.normalised_text))).all() == ["S123ABC"]


async def test_commit_ack_failure_preserves_images_referenced_by_committed_rows(
    db_session, app_config, monkeypatch
):
    recording, track = await seed(db_session)
    recording_id = recording.id
    write = stages.write_with_retry

    async def commit_then_fail(*args, **kwargs):
        await write(*args, **kwargs)
        raise RuntimeError("commit acknowledgement lost")

    monkeypatch.setattr(stages, "write_with_retry", commit_then_fail)
    with pytest.raises(RuntimeError, match="acknowledgement"):
        await stages._write_observations(
            db_session, recording, placed(track, "S456DEF"), set(), None
        )
    after = await observations(recording_id)
    assert [row[0] for row in after] == ["S456DEF"]
    assert all((app_config.media_dir / path).is_file() for row in after for path in row[1:])


async def test_cleanup_after_commit_never_removes_new_committed_images(
    db_session, app_config, monkeypatch
):
    recording, track = await seed(db_session)

    async def cleanup_failure(paths):
        raise RuntimeError("cleanup failed")

    monkeypatch.setattr(stages, "_remove_media_dirs", cleanup_failure)
    with pytest.raises(RuntimeError, match="cleanup failed"):
        await stages._write_observations(
            db_session, recording, placed(track, "S456DEF"), set(), None
        )
    after = await observations(recording.id)
    assert [row[0] for row in after] == ["S456DEF"]
    assert all((app_config.media_dir / path).is_file() for row in after for path in row[1:])


async def test_replacement_and_empty_reprocess_clean_only_owned_generations(db_session, app_config):
    recording, track = await seed(db_session)
    other, _ = await seed(db_session, "sibling.ts")
    before = await observations(recording.id)
    sibling = await observations(other.id)
    plate_id = await db_session.scalar(select(Plate.id))
    legacy_sibling = app_config.media_dir / "plates" / f"{plate_id:08d}" / "sibling.jpg"
    legacy_sibling.parent.mkdir(parents=True)
    legacy_sibling.write_bytes(b"unrelated legacy preview")

    await stages._write_observations(db_session, recording, placed(track, "S456DEF"), set(), None)
    assert all(not (app_config.media_dir / path).exists() for row in before for path in row[1:])
    assert all((app_config.media_dir / path).is_file() for row in sibling for path in row[1:])
    assert legacy_sibling.read_bytes() == b"unrelated legacy preview"
    current = await observations(recording.id)
    await stages._clear_plate_observations(db_session, recording)
    assert await observations(recording.id) == []
    assert all(not (app_config.media_dir / path).exists() for row in current for path in row[1:])
    assert all((app_config.media_dir / path).is_file() for row in sibling for path in row[1:])
    assert legacy_sibling.is_file()


async def test_retry_reuses_completed_crops_after_rollback(db_session, app_config, monkeypatch):
    recording, track = await seed(db_session)
    recording_id = recording.id
    insert = stages._insert_observations
    save = stages._save_jpegs
    writes = encodes = 0

    async def retry_once(*args):
        nonlocal writes
        writes += 1
        await insert(*args)
        if writes == 1:
            raise OperationalError("insert", {}, sqlite3.OperationalError("database is locked"))

    async def count_encodes(*args):
        nonlocal encodes
        encodes += 1
        return await save(*args)

    monkeypatch.setattr(stages, "_insert_observations", retry_once)
    monkeypatch.setattr(stages, "_save_jpegs", count_encodes)
    await stages._write_observations(db_session, recording, placed(track, "S456DEF"), set(), None)
    assert (writes, encodes) == (2, 1)
    after = await observations(recording_id)
    assert [row[0] for row in after] == ["S456DEF"]
    assert all((app_config.media_dir / path).is_file() for row in after for path in row[1:])


async def test_repeated_cancel_joins_crop_worker_before_removing_its_files(
    db_session, app_config, monkeypatch
):
    recording, track = await seed(db_session)
    before = await observations(recording.id)
    started = threading.Event()
    release = threading.Event()
    save = stages._save_jpeg
    attempt_dirs = []

    def blocked_save(image, path, quality):
        attempt_dirs.append(path.parent)
        saved = save(image, path, quality)
        started.set()
        assert release.wait(5), "test did not release crop worker"
        return saved

    monkeypatch.setattr(stages, "_save_jpeg", blocked_save)
    task = asyncio.create_task(
        stages._write_observations(db_session, recording, placed(track, "S456DEF"), set(), None)
    )
    try:
        async with asyncio.timeout(3):
            while not started.is_set():
                await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done()
        assert attempt_dirs[0].is_dir()
        await concurrent_writer("during-cancelled-crop.ts")
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=3)
    assert await observations(recording.id) == before
    assert all(not path.exists() for path in attempt_dirs)
    assert all((app_config.media_dir / path).is_file() for row in before for path in row[1:])
