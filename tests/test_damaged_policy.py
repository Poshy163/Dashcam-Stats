"""Damaged-footage hiding and deletion must be automatic without being reckless."""

from __future__ import annotations

from app.core.settings_service import get_settings_service
from app.damaged_policy import apply_damaged_policy, apply_known_damaged_policy
from app.db.models import JobState, ProcessingJob, Recording, RecordingState
from app.retention.safety import SafetyReport


async def _recording(session, name: str, *, state=RecordingState.COMPLETED) -> Recording:
    row = Recording(
        rel_path=name,
        filename=name,
        size_bytes=1024,
        state=state,
        probe_json={
            "source_damaged": state is not RecordingState.INVALID,
            "warnings": ["no usable frame could be decoded"],
        },
    )
    session.add(row)
    await session.flush()
    return row


class TestHiding:
    async def test_default_policy_blacklists_without_deleting(self, db_session, temp_dirs):
        _, footage = temp_dirs
        name = "damaged.ts"
        source = footage / name
        source.write_bytes(b"still here")
        row = await _recording(db_session, name)

        outcome = await apply_damaged_policy(db_session, row)

        assert outcome == "hidden"
        assert row.ignored is True
        assert row.state is RecordingState.COMPLETED
        assert source.exists(), "the default policy must never remove footage"
        assert row.probe_json["damaged_policy"]["previous_state"] == "completed"

    async def test_keep_restores_only_a_policy_hidden_recording(self, db_session):
        row = await _recording(db_session, "restore.ts", state=RecordingState.INVALID)
        await apply_damaged_policy(db_session, row, action="hide")

        outcome = await apply_damaged_policy(db_session, row, action="keep")

        assert outcome == "restored"
        assert row.ignored is False
        assert row.state is RecordingState.INVALID
        assert "damaged_policy" not in row.probe_json

    async def test_scan_reconciles_known_damaged_rows(self, db_session):
        await _recording(db_session, "known.ts")

        summary = await apply_known_damaged_policy(db_session)

        assert summary.hidden == 1

    async def test_recoverable_transport_warning_is_not_blacklisted(self, db_session):
        row = await _recording(db_session, "playable.ts")
        row.probe_json = {
            "source_damaged": True,
            "warnings": ["file ends 100 bytes into a 188-byte transport packet"],
        }

        outcome = await apply_damaged_policy(db_session, row, action="hide")

        assert outcome == "kept"
        assert row.ignored is False

    async def test_scan_restores_rows_hidden_for_recoverable_warnings(self, db_session):
        row = await _recording(db_session, "restore-playable.ts")
        await apply_damaged_policy(db_session, row, action="hide")
        probe = dict(row.probe_json)
        probe["warnings"] = ["container reported an implausible frame rate"]
        probe["source_unusable"] = False
        row.probe_json = probe
        await db_session.flush()

        summary = await apply_known_damaged_policy(db_session)

        assert summary.restored == 1
        assert row.ignored is False


class TestDeletion:
    async def test_explicit_delete_removes_only_the_source(self, db_session, temp_dirs):
        _, footage = temp_dirs
        settings = get_settings_service()
        await settings.set("general.footage_dir", str(footage))
        name = "delete-me.ts"
        source = footage / name
        source.write_bytes(b"damaged")
        row = await _recording(db_session, name)

        outcome = await apply_damaged_policy(
            db_session,
            row,
            action="delete",
            safety=SafetyReport(ok=True, writable=True),
        )

        assert outcome == "deleted"
        assert not source.exists()
        assert row.file_missing is True
        assert row.state is RecordingState.DELETED
        assert row.id is not None, "history is retained in the database"

    async def test_failed_safety_check_hides_instead_of_deleting(self, db_session, temp_dirs):
        _, footage = temp_dirs
        name = "keep-me.ts"
        source = footage / name
        source.write_bytes(b"damaged")
        row = await _recording(db_session, name)

        outcome = await apply_damaged_policy(
            db_session,
            row,
            action="delete",
            safety=SafetyReport(ok=False, writable=True, blocked_reason="mount check failed"),
        )

        assert outcome == "blocked"
        assert source.exists()
        assert row.state is RecordingState.COMPLETED
        assert row.probe_json["damaged_policy"]["status"] == "delete_blocked"


class TestRecordingVisibility:
    async def test_hidden_rows_are_absent_normally_but_available_by_filter(
        self, client, db_session
    ):
        visible = await _recording(db_session, "visible.ts")
        visible.probe_json = {"source_damaged": False, "warnings": []}
        hidden = await _recording(db_session, "hidden.ts")
        await apply_damaged_policy(db_session, hidden, action="hide")
        await db_session.commit()

        normal = (await client.get("/api/recordings")).json()
        blacklisted = (await client.get("/api/recordings?state=hidden")).json()
        status = (await client.get("/api/status")).json()
        search = (await client.get("/api/search?q=hidden.ts")).json()

        assert [item["id"] for item in normal["items"]] == [visible.id]
        assert [item["id"] for item in blacklisted["items"]] == [hidden.id]
        assert status["totals"]["recordings"] == 1
        assert search["recordings"] == []

    async def test_blacklisted_failures_leave_queue_but_lock_failures_remain(
        self, client, db_session
    ):
        empty = await _recording(db_session, "empty.ts", state=RecordingState.INVALID)
        await apply_damaged_policy(db_session, empty, action="hide")
        locked = await _recording(db_session, "locked.ts", state=RecordingState.FAILED)
        locked.probe_json = {"source_damaged": False, "warnings": []}
        hidden_job = ProcessingJob(
            recording_id=empty.id,
            state=JobState.FAILED,
            error_message="empty.ts is empty (0 bytes)",
        )
        retryable_job = ProcessingJob(
            recording_id=locked.id,
            state=JobState.FAILED,
            error_message="OperationalError: database is locked",
        )
        db_session.add_all([hidden_job, retryable_job])
        await db_session.commit()

        listed = (await client.get("/api/jobs?state=failed")).json()
        stats = (await client.get("/api/jobs/stats")).json()
        retried = (await client.post("/api/jobs/retry-failed")).json()
        direct_retry = await client.post(f"/api/jobs/{hidden_job.id}/retry")

        assert [item["id"] for item in listed["items"]] == [retryable_job.id]
        assert stats["failed"] == 1
        assert retried["retried"] == 1
        assert direct_retry.status_code == 409
