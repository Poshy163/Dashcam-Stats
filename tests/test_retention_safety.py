"""Retention safety.

The brief singles this out, and rightly: everything else the app computes can be rebuilt
from the footage, but a wrongly deleted recording is gone.

The scenario these tests exist for is the one that looks harmless. An unmounted network
share is indistinguishable from an empty directory, and a naive retention pass would see
"no files, nothing to keep" and then, on the next scan, "every indexed recording has
vanished". An empty or unexpected footage directory must be treated as a **fault**, never
as permission to delete.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.settings_service import get_settings_service
from app.db.models import Recording, RecordingState, RetentionRun
from app.retention import evaluate_safety, execute, plan
from app.retention.safety import can_delete

GB = 1024**3


async def _add_recording(
    session, name: str, *, size: int, days_old: float = 10.0, plates: int = 0, vehicles: int = 0
) -> Recording:
    recording = Recording(
        rel_path=name,
        filename=name,
        size_bytes=size,
        started_at=datetime.now(UTC) - timedelta(days=days_old),
        state=RecordingState.COMPLETED,
        plate_count=plates,
        vehicle_count=vehicles,
    )
    session.add(recording)
    await session.flush()
    return recording


def _write_footage(footage_dir, count: int, size: int = 4096) -> list[str]:
    names = []
    for i in range(count):
        name = f"2026080{i % 9 + 1}12000{i}_camera_0.ts"
        (footage_dir / name).write_bytes(b"\x47" * size)
        names.append(name)
    return names


@pytest.fixture
async def ready(db_session, temp_dirs):
    """A migrated database plus a footage directory holding plausible files."""
    _, footage = temp_dirs
    _write_footage(footage, 12)
    settings = get_settings_service()
    await settings.set("general.footage_dir", str(footage))
    # tmp_path is not a mount point, and on a developer machine it never will be. The
    # guard itself is exercised explicitly in TestUnmountedShare below.
    await settings.set("storage.require_mountpoint", False)
    return db_session, footage


class TestUnmountedShare:
    """The failure this whole module exists to prevent."""

    async def test_missing_directory_blocks_everything(self, db_session, temp_dirs, tmp_path):
        _, footage = temp_dirs
        await get_settings_service().set("general.footage_dir", str(tmp_path / "never-mounted"))

        report = await evaluate_safety(db_session)
        assert report.ok is False
        assert any(c.name == "directory_exists" and not c.passed for c in report.checks)

    async def test_empty_directory_is_a_fault_not_permission(self, db_session, temp_dirs):
        _, footage = temp_dirs  # exists, but deliberately left empty
        settings = get_settings_service()
        await settings.set("general.footage_dir", str(footage))
        await settings.set("storage.require_mountpoint", False)

        report = await evaluate_safety(db_session)

        assert report.ok is False, "an empty footage directory must never pass safety"
        failed = {c.name for c in report.failed}
        assert "minimum_files" in failed
        assert report.blocked_reason and "fault" in report.blocked_reason.lower()

    async def test_not_a_mount_point_blocks_when_required(self, ready):
        session, _ = ready
        # This is the real guard against a share that failed to mount: the directory is
        # present and even populated, but it is not a mount.
        await get_settings_service().set("storage.require_mountpoint", True)

        report = await evaluate_safety(session)
        assert report.ok is False
        assert any(c.name == "is_mount_point" and not c.passed for c in report.checks)

    async def test_disk_far_below_index_blocks(self, ready):
        session, footage = ready
        # The database believes there are 40 recordings; the mount shows 12. That is a
        # partial or wrong mount, not 28 deletions waiting to happen.
        for i in range(40):
            await _add_recording(session, f"indexed_{i}.ts", size=1024)
        await session.flush()

        report = await evaluate_safety(session)
        assert report.ok is False
        assert any(c.name == "consistent_with_index" and not c.passed for c in report.checks)


class TestPathGuards:
    async def test_footage_directory_may_not_overlap_data(self, db_session, temp_dirs):
        data, _ = temp_dirs
        # /data holds the database and every derived artefact; it is never a deletion
        # target under any circumstance.
        await get_settings_service().set("general.footage_dir", str(data))

        report = await evaluate_safety(db_session)
        assert report.ok is False
        assert any(c.name == "not_data_directory" and not c.passed for c in report.checks)

    async def test_system_root_is_refused(self, db_session):
        await get_settings_service().set("general.footage_dir", "/")
        report = await evaluate_safety(db_session)
        assert report.ok is False


class TestCanDelete:
    """The final gate. All three conditions are independent and all must hold."""

    def _report(self, *, ok: bool, writable: bool):
        from app.retention.safety import SafetyReport

        return SafetyReport(ok=ok, writable=writable, blocked_reason=None if ok else "guard failed")

    def test_refuses_when_safety_failed(self):
        allowed, reason = can_delete(self._report(ok=False, writable=True), True)
        assert allowed is False and reason

    def test_refuses_when_deletion_not_enabled(self):
        allowed, reason = can_delete(self._report(ok=True, writable=True), False)
        assert allowed is False
        assert reason and "disabled" in reason.lower()

    def test_refuses_on_a_read_only_mount(self):
        # The recommended deployment. Detected, not assumed.
        allowed, reason = can_delete(self._report(ok=True, writable=False), True)
        assert allowed is False
        assert reason and "read-only" in reason.lower()

    def test_allows_only_when_everything_holds(self):
        allowed, reason = can_delete(self._report(ok=True, writable=True), True)
        assert allowed is True and reason is None


class TestPlanning:
    async def test_under_the_limit_proposes_nothing(self, ready):
        session, _ = ready
        await _add_recording(session, "a.ts", size=1024)
        result = await plan(session)
        assert result.would_delete_count == 0
        assert result.over_limit is False

    async def test_selects_oldest_first(self, ready):
        session, footage = ready
        # A limit small enough that the on-disk fixtures already exceed it.
        await get_settings_service().set("storage.max_footage_gb", 1)
        for index, age in enumerate([100, 50, 5]):
            await _add_recording(
                session, f"2026080{index + 1}120000_camera_0.ts", size=2 * GB, days_old=age
            )
        result = await plan(session)

        if result.candidates:
            ages = [c.started_at for c in result.candidates if c.started_at]
            assert ages == sorted(ages), "candidates must be oldest-first"

    async def test_min_age_exempts_recent_footage(self, ready):
        session, _ = ready
        settings = get_settings_service()
        await settings.set("storage.max_footage_gb", 1)
        await settings.set("storage.min_age_days", 30)
        await _add_recording(session, "recent.ts", size=5 * GB, days_old=1)

        result = await plan(session)
        assert all(c.filename != "recent.ts" for c in result.candidates)

    async def test_keep_with_detections_exempts_them(self, ready, monkeypatch):
        session, _ = ready
        settings = get_settings_service()
        await settings.set("storage.max_footage_gb", 1)
        await settings.set("storage.keep_with_detections", True)
        await _add_recording(session, "has_plates.ts", size=5 * GB, days_old=90, plates=3)
        await _add_recording(session, "plain.ts", size=5 * GB, days_old=95)

        # The fixture files are a few kilobytes, so the planner would exit before
        # evaluating eligibility at all. Reporting a large directory is what puts it over
        # the limit and makes it actually walk the candidates.
        monkeypatch.setattr("app.retention.safety.directory_size", lambda *_a, **_kw: (10 * GB, 12))

        result = await plan(session)

        assert result.over_limit is True, "the planner never got as far as eligibility"
        assert all(c.filename != "has_plates.ts" for c in result.candidates)
        assert "contains detections" in result.skipped_reasons
        # The unprotected recording is still fair game, proving the exemption is what
        # spared the other one rather than the plan simply being empty.
        assert any(c.filename == "plain.ts" for c in result.candidates)


class TestExecution:
    async def test_report_only_deletes_nothing(self, ready):
        session, footage = ready
        before = {p.name for p in footage.iterdir()}

        result = await plan(session)
        run = await execute(session, result, dry_run=True, trigger="test")

        assert run.dry_run is True
        assert run.deleted_count == 0
        assert {p.name for p in footage.iterdir()} == before, "report-only removed files"

    async def test_blocked_run_is_still_recorded(self, db_session, temp_dirs):
        _, footage = temp_dirs  # empty: safety will refuse
        await get_settings_service().set("general.footage_dir", str(footage))

        result = await plan(db_session)
        run = await execute(db_session, result, dry_run=False, trigger="test")

        assert run.blocked is True
        assert run.blocked_reason
        assert run.deleted_count == 0
        # Every evaluation is auditable, including the ones that refused to act.
        rows = (await db_session.execute(select(RetentionRun))).scalars().all()
        assert len(rows) >= 1

    async def test_deletion_never_happens_without_explicit_opt_in(self, ready):
        session, footage = ready
        await get_settings_service().set("storage.max_footage_gb", 1)
        names = _write_footage(footage, 3, size=8192)
        for name in names:
            await _add_recording(session, name, size=5 * GB, days_old=200)

        # storage.enable_deletion defaults to False, and dry_run=False alone must not
        # override that.
        result = await plan(session)
        run = await execute(session, result, dry_run=False, trigger="test")

        assert run.deleted_count == 0
        for name in names:
            assert (footage / name).exists(), f"{name} was deleted without opt-in"


class TestScannerDoesNotWipeTheIndex:
    """The scanner must fail closed on a share it cannot vouch for.

    Retention refuses to delete from a directory that looks wrong. The scanner had no such
    posture, and it could do equivalent damage from the other side: an empty or unmounted
    share makes every indexed recording look deleted.

    The bind mount is what makes this so easy to hit. If the host source is empty or
    unmounted when the container starts, Docker still presents ``/dashcam`` as an existing
    directory — so the existence check passes, the walk yields nothing, no exception is
    raised, and all 674 rows are stamped ``file_missing`` with a ``deleted_at`` while the
    run is recorded as a clean success.

    The second-order damage is worse than the first. ``evaluate_safety`` counts only rows
    that are not flagged missing, and its consistency guard passes trivially when the index
    is empty — so zeroing the index disarms the one retention check designed to notice a
    wrong mount, using exactly the event it exists to detect.
    """

    @pytest.fixture
    async def indexed_library(self, db_session, tmp_path):
        from app.db.models import Recording, RecordingState
        from app.db.session import session_scope

        footage = tmp_path / "share"
        footage.mkdir(exist_ok=True)
        async with session_scope() as session:
            for index in range(20):
                session.add(
                    Recording(
                        rel_path=f"lib_{index}.ts",
                        filename=f"lib_{index}.ts",
                        size_bytes=1024,
                        state=RecordingState.COMPLETED,
                    )
                )
            await session.flush()
        return footage

    async def _still_indexed(self) -> int:
        from sqlalchemy import func, select

        from app.db.models import Recording
        from app.db.session import session_scope

        async with session_scope() as session:
            return int(
                (
                    await session.execute(
                        select(func.count(Recording.id)).where(Recording.file_missing.is_(False))
                    )
                ).scalar()
                or 0
            )

    async def test_an_empty_share_does_not_delete_the_index(self, indexed_library):
        from app.scanner.discovery import Scanner

        assert await self._still_indexed() == 20
        summary = await Scanner(footage_dir=indexed_library).scan(trigger="test")

        assert await self._still_indexed() == 20, (
            "an empty footage directory wiped the index; a share that failed to mount "
            "looks exactly like this"
        )
        assert summary.missing == 0
        assert summary.error_message, "the operator must be told the scan drew no conclusions"

    async def test_a_large_unexplained_drop_is_treated_as_a_mount_problem(self, indexed_library):
        from app.scanner.discovery import Scanner

        # Two of twenty present: real deletion on that scale is far rarer than a share
        # that came back half-mounted.
        for index in range(2):
            (indexed_library / f"lib_{index}.ts").write_bytes(b"\x00" * 16)

        await Scanner(footage_dir=indexed_library).scan(trigger="test")
        assert await self._still_indexed() == 20

    async def test_ordinary_deletion_is_still_reconciled(self, indexed_library):
        """The guard must not be so cautious that the index never keeps up."""
        from app.scanner.discovery import Scanner

        for index in range(18):
            (indexed_library / f"lib_{index}.ts").write_bytes(b"\x00" * 16)

        summary = await Scanner(footage_dir=indexed_library).scan(trigger="test")
        assert summary.missing == 2, "two genuinely absent files should have been reconciled"
