"""The server lets running analysis finish and gives a footage transfer first use of the share."""

import asyncio

import pytest

from app.core.backup_priority import backup_priority_active, prioritize_footage_transfer
from app.core.settings_service import get_settings_service
from app.db.models import JobState
from app.ingest import puller
from app.ingest.models import RemoteFile, UnitInfo
from app.ingest.status import IngestStatus
from app.workers import queue


async def test_cancelled_footage_pass_releases_pending_jobs(db_session, tmp_path, monkeypatch):
    running = await queue.enqueue(db_session, None)
    assert (await queue.claim_next(db_session, "w1")).id == running.id
    pending = await queue.enqueue(db_session, None)
    preparing_listener = asyncio.Event()

    async def clear_listener(_address):
        preparing_listener.set()
        await asyncio.Future()

    monkeypatch.setattr(puller.adb, "clear_listener", clear_listener)
    monkeypatch.setattr(puller, "get_status", lambda: IngestStatus())
    monkeypatch.setattr(puller, "_get", lambda key, default: default)
    transfer = asyncio.create_task(
        puller._move(
            UnitInfo("unit", source="/card"),
            [RemoteFile("front.ts", 10, 0)],
            staging=tmp_path,
            host="unit",
            port=9000,
            timeout_s=10,
        )
    )
    try:
        await asyncio.wait_for(preparing_listener.wait(), timeout=2)
        assert await queue.claim_next(db_session, "w2") is None
        assert pending.state is JobState.QUEUED and pending.attempts == 0
        assert not queue.is_paused()
        assert (await queue.stats(db_session))["backup_priority_active"]
        await queue.complete(db_session, running)
        assert running.state is JobState.COMPLETED
    finally:
        transfer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await transfer
    assert not backup_priority_active()
    assert (await queue.claim_next(db_session, "w2")).id == pending.id


async def test_operator_pause_and_transfer_priority_are_independent(
    db_session, tmp_path, monkeypatch
):
    marker = tmp_path / "pause-marker"
    monkeypatch.setattr(queue, "_pause_marker", lambda: marker)
    monkeypatch.setattr(queue, "_paused", False)
    pending = await queue.enqueue(db_session, None)
    async with prioritize_footage_transfer():
        async with prioritize_footage_transfer():
            assert not marker.exists()
        assert backup_priority_active()
        queue.pause()
    assert marker.exists()
    assert await queue.claim_next(db_session, "w1") is None
    async with prioritize_footage_transfer():
        queue.resume()
        assert await queue.claim_next(db_session, "w1") is None
        await db_session.commit()
        await get_settings_service().set("processing.prioritize_footage_backup", False)
        assert (await queue.claim_next(db_session, "w1")).id == pending.id


async def test_transfer_start_while_worker_checks_for_work(db_session, monkeypatch):
    pending = await queue.enqueue(db_session, None)
    checked, release = asyncio.Event(), asyncio.Event()
    execute = db_session.execute

    async def delayed_preflight(*args, **kwargs):
        result = await execute(*args, **kwargs)
        if not checked.is_set():
            checked.set()
            await release.wait()
        return result

    monkeypatch.setattr(db_session, "execute", delayed_preflight)
    claiming = asyncio.create_task(queue.claim_next(db_session, "w1"))
    try:
        await asyncio.wait_for(checked.wait(), timeout=2)
        async with prioritize_footage_transfer():
            release.set()
            assert await asyncio.wait_for(claiming, timeout=2) is None
            assert pending.attempts == 0
    finally:
        release.set()
        await claiming
    assert (await queue.claim_next(db_session, "w1")).id == pending.id
