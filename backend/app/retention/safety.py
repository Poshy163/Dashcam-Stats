"""Safety guards for retention.

This is the highest-stakes code in the project: everything else can be recomputed, but a
wrongly deleted recording is gone. Every guard must pass before a single file is removed,
and evaluation **fails closed** — an unexpected exception blocks the run rather than
letting it proceed.

The guard that matters most is the mount check. An unmounted network share looks exactly
like an empty directory, and "the directory is empty" is the one situation where a naive
retention pass would happily conclude there is nothing to keep and, on the next scan,
that every indexed recording has vanished. An empty or unexpected footage directory is
treated as a fault, never as permission to delete.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_config
from app.core.logging import get_logger
from app.core.paths import directory_size, is_mount_point, is_within, is_writable
from app.core.settings_service import get_settings_service
from app.db.models import Recording

log = get_logger(__name__)

#: If the database has indexed many files but the directory now shows far fewer, the
#: mount is probably wrong or partial. Deleting against that view would be catastrophic.
_DISK_VS_INDEX_MIN_RATIO = 0.5


@dataclass(slots=True)
class SafetyCheck:
    name: str
    passed: bool
    reason: str | None = None
    detail: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class SafetyReport:
    ok: bool
    checks: list[SafetyCheck] = field(default_factory=list)
    blocked_reason: str | None = None
    footage_dir: str | None = None
    file_count: int = 0
    total_bytes: int = 0
    indexed_count: int = 0
    writable: bool = False

    @property
    def failed(self) -> list[SafetyCheck]:
        return [c for c in self.checks if not c.passed]

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "blocked_reason": self.blocked_reason,
            "footage_dir": self.footage_dir,
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
            "indexed_count": self.indexed_count,
            "writable": self.writable,
            "checks": [
                {"name": c.name, "passed": c.passed, "reason": c.reason, "detail": c.detail}
                for c in self.checks
            ],
        }


async def evaluate_safety(session: AsyncSession, footage_dir: Path | None = None) -> SafetyReport:
    """Run every guard. The result is authoritative: no deletion may proceed on ``ok=False``."""
    settings = get_settings_service()
    report = SafetyReport(ok=False)

    try:
        root = footage_dir or await settings.footage_dir()
        root = Path(root)
        report.footage_dir = str(root)

        checks = report.checks

        # --- 1. it exists at all ------------------------------------------------------
        exists = root.exists() and root.is_dir()
        checks.append(
            SafetyCheck(
                "directory_exists",
                exists,
                None if exists else f"{root} does not exist or is not a directory",
            )
        )
        if not exists:
            return _blocked(report)

        resolved = root.resolve()

        # --- 2. never anywhere near /data ---------------------------------------------
        data_dir = get_config().data_dir.resolve()
        collides = (
            resolved == data_dir or is_within(data_dir, resolved) or is_within(resolved, data_dir)
        )
        checks.append(
            SafetyCheck(
                "not_data_directory",
                not collides,
                f"footage directory overlaps the application data directory {data_dir}"
                if collides
                else None,
            )
        )
        if collides:
            return _blocked(report)

        # --- 3. not an obviously unsafe root ------------------------------------------
        unsafe_roots = {
            Path("/"),
            Path("/home"),
            Path("/root"),
            Path("/etc"),
            Path("/usr"),
            Path("/var"),
            Path("/tmp"),
        }
        is_unsafe = resolved in unsafe_roots or len(resolved.parts) <= 1
        checks.append(
            SafetyCheck(
                "not_system_root",
                not is_unsafe,
                f"{resolved} is a system directory" if is_unsafe else None,
            )
        )
        if is_unsafe:
            return _blocked(report)

        # --- 4. really a mount, not the empty directory under an absent share ---------
        require_mount = bool(settings.get_nowait("storage.require_mountpoint"))
        mounted = is_mount_point(resolved)
        checks.append(
            SafetyCheck(
                "is_mount_point",
                mounted or not require_mount,
                None
                if mounted or not require_mount
                else (
                    f"{resolved} is not a mount point — the share may not be mounted. "
                    "Retention will not delete against an unmounted path."
                ),
                {"is_mount_point": mounted, "required": require_mount},
            )
        )
        if require_mount and not mounted:
            return _blocked(report)

        # --- 5. contains a plausible number of files ----------------------------------
        extensions = await settings.media_extensions()
        total_bytes, file_count = directory_size(resolved, extensions)
        report.total_bytes = total_bytes
        report.file_count = file_count

        minimum = int(settings.get_nowait("storage.min_expected_files"))
        enough = file_count >= minimum
        checks.append(
            SafetyCheck(
                "minimum_files",
                enough,
                None
                if enough
                else (
                    f"only {file_count} media files found (expected at least {minimum}). "
                    "An empty footage directory is treated as a fault, not as permission to delete."
                ),
                {"found": file_count, "minimum": minimum},
            )
        )
        if not enough:
            return _blocked(report)

        # --- 6. disk view agrees with the index ---------------------------------------
        indexed = int(
            (
                await session.execute(
                    select(func.count(Recording.id)).where(Recording.file_missing.is_(False))
                )
            ).scalar()
            or 0
        )
        report.indexed_count = indexed

        consistent = indexed == 0 or file_count >= indexed * _DISK_VS_INDEX_MIN_RATIO
        checks.append(
            SafetyCheck(
                "consistent_with_index",
                consistent,
                None
                if consistent
                else (
                    f"disk shows {file_count} files but {indexed} are indexed — the mount may "
                    "be wrong or incomplete"
                ),
                {"on_disk": file_count, "indexed": indexed},
            )
        )
        if not consistent:
            return _blocked(report)

        # --- 7. writability, detected rather than assumed -----------------------------
        writable = is_writable(resolved)
        report.writable = writable
        checks.append(
            SafetyCheck(
                "writable",
                writable,
                None
                if writable
                else (
                    f"{resolved} is mounted read-only. Retention will report what it would "
                    "remove but cannot delete anything."
                ),
                {"writable": writable},
            )
        )

        # A read-only mount is a normal, recommended configuration — it does not make the
        # evaluation invalid, it just means deletion is unavailable. Everything else has
        # passed, so the report is usable.
        report.ok = True
        return report

    except Exception as exc:
        # Fail closed. If we cannot establish that deletion is safe, it is not safe.
        log.exception("retention safety evaluation failed", error=str(exc))
        report.checks.append(
            SafetyCheck(
                "evaluation", False, f"safety evaluation raised {type(exc).__name__}: {exc}"
            )
        )
        return _blocked(report)


def _blocked(report: SafetyReport) -> SafetyReport:
    report.ok = False
    failed = report.failed
    report.blocked_reason = failed[0].reason if failed else "safety checks did not pass"
    return report


def can_delete(report: SafetyReport, deletion_enabled: bool) -> tuple[bool, str | None]:
    """Final gate. Deletion needs all of: guards passed, writable mount, explicit opt-in."""
    if not report.ok:
        return False, report.blocked_reason or "safety checks did not pass"
    if not deletion_enabled:
        return False, (
            "deletion is disabled (Settings > Storage > 'Actually delete files'). "
            "Retention is reporting what it would remove."
        )
    if not report.writable:
        return False, "the footage directory is mounted read-only"
    return True, None
