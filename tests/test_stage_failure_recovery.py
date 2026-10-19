"""Database failures must keep their original cause and leave durable stage outcomes."""

from sqlalchemy import delete, select, text, update

from app.db.models import Recording, RecordingState, StageState, TelemetryPoint
from app.db.session import get_session_factory
from app.pipeline import orchestrator
from app.pipeline.stages import StageError


async def test_failed_flush_recovers_before_reading_expired_recording(db_session, monkeypatch):
    recording = Recording(filename="preserved.ts", rel_path="preserved.ts")
    db_session.add(recording)
    await db_session.flush()
    recording_id = recording.id
    db_session.add(TelemetryPoint(recording_id=recording_id, t_offset_s=12))
    await db_session.commit()

    async def failing_stage(session, row, **kwargs):
        # Exercise a real failed flush: SQLAlchemy expires every persistent object and
        # rejects further queries until rollback. Earlier work in the stage must revert.
        await session.execute(delete(TelemetryPoint).where(TelemetryPoint.recording_id == row.id))
        row.rel_path = None
        await session.flush()

    monkeypatch.setitem(orchestrator.STAGES, "metadata", failing_stage)
    report = await orchestrator.run_stages(db_session, recording, ["metadata"])
    await db_session.commit()

    assert not report.ok
    assert "IntegrityError" in report.error
    assert "PendingRollbackError" not in report.error
    assert "MissingGreenlet" not in report.error
    async with get_session_factory()() as reader:
        stored = await reader.get(Recording, recording_id)
        assert stored.rel_path == "preserved.ts"
        assert stored.state is RecordingState.FAILED
        assert stored.metadata_state is StageState.FAILED
        assert stored.error_count == 1
        assert stored.error_message == report.error
        assert (
            await reader.scalar(
                select(TelemetryPoint.t_offset_s).where(TelemetryPoint.recording_id == recording_id)
            )
            == 12
        )


async def test_real_flush_lock_keeps_its_contention_classification(db_session, monkeypatch):
    recording = Recording(filename="locked.ts", rel_path="locked.ts", vehicle_count=2)
    db_session.add(recording)
    await db_session.commit()
    recording_id = recording.id

    async def locked_stage(session, row, **kwargs):
        await session.execute(text("PRAGMA busy_timeout=1"))
        async with get_session_factory()() as other:
            await other.execute(
                update(Recording).where(Recording.id == recording_id).values(size_bytes=55)
            )
            try:
                row.vehicle_count = 99
                await session.flush()
            finally:
                await other.rollback()

    monkeypatch.setitem(orchestrator.STAGES, "metadata", locked_stage)
    report = await orchestrator.run_stages(db_session, recording, ["metadata"])
    await db_session.commit()

    assert not report.ok
    assert report.transient
    assert "OperationalError" in report.error
    assert "database is locked" in report.error
    assert "PendingRollbackError" not in report.error
    async with get_session_factory()() as reader:
        stored = await reader.get(Recording, recording_id)
        assert stored.state is RecordingState.FAILED
        assert stored.metadata_state is StageState.FAILED
        assert stored.vehicle_count == 2


async def test_unflushed_probe_failure_evidence_survives_recovery(db_session, monkeypatch):
    recording = Recording(
        filename="probe.ts", rel_path="probe.ts", probe_json={"existing": "preserved"}
    )
    db_session.add(recording)
    await db_session.commit()
    recording_id = recording.id
    evidence = {"returncode": 1, "stderr": "invalid file"}

    async def rejected_probe(session, row, **kwargs):
        row.probe_json = {"half_written": True, "probe_failure": evidence}
        raise StageError("probe rejected this source", permanent=True)

    monkeypatch.setitem(orchestrator.STAGES, "metadata", rejected_probe)
    report = await orchestrator.run_stages(db_session, recording, ["metadata"])
    await db_session.commit()

    assert report.permanent
    async with get_session_factory()() as reader:
        stored = await reader.get(Recording, recording_id)
        assert stored.state is RecordingState.INVALID
        assert stored.probe_json == {"existing": "preserved", "probe_failure": evidence}
