"""A failed commit flush must keep its original cause when the session cannot retry."""

import sqlite3

import pytest
from sqlalchemy import text, update
from sqlalchemy.exc import OperationalError

from app.db import retry
from app.db.models import Recording, StageState
from app.db.session import get_session_factory


async def test_commit_flush_lock_is_not_retried_on_an_inactive_session(db_session, monkeypatch):
    recording = Recording(filename="commit-lock.ts", rel_path="commit-lock.ts", vehicle_count=2)
    db_session.add(recording)
    await db_session.commit()
    recording_id = recording.id
    await db_session.execute(text("PRAGMA busy_timeout=1"))
    real_commit, real_retry = db_session.commit, retry.retry_on_locked
    failures = []

    async def observed_commit():
        try:
            await real_commit()
        except Exception as exc:
            failures.append(exc)
            raise

    async def fast_retry(*args, **kwargs):
        return await real_retry(*args, **kwargs, base_delay_s=0.001)

    monkeypatch.setattr(db_session, "commit", observed_commit)
    monkeypatch.setattr(retry, "retry_on_locked", fast_retry)
    async with get_session_factory()() as other:
        await other.execute(
            update(Recording).where(Recording.id == recording_id).values(vehicle_count=55)
        )
        recording.metadata_state = StageState.RUNNING
        try:
            with pytest.raises(OperationalError, match="database is locked") as failure:
                await retry.commit_with_retry(db_session, what="stage running marker")

            assert len(failures) == 1
            assert failure.value is failures[0]
            assert isinstance(failure.value.orig, sqlite3.OperationalError)
            assert not db_session.is_active
            assert retry.is_locked_error(failure.value)
        finally:
            # Cleanup belongs to the outer unit of work; the helper must not roll back
            # arbitrary pending changes and then report an empty commit as success.
            await db_session.rollback()
            await other.rollback()

    async with get_session_factory()() as reader:
        stored = await reader.get(Recording, recording_id)
        assert stored.metadata_state is StageState.PENDING
        assert stored.vehicle_count == 2
