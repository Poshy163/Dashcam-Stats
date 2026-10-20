"""Periodic database housekeeping.

Three things SQLite will not do for us on its own:

* **Job history pruning.** Every analysis leaves a ``processing_jobs`` row behind, and a
  busy library accumulates tens of thousands of them. They are only ever read by the
  queue statistics (which look at the last day) and the job detail view, so anything
  older than ``advanced.job_history_days`` is deleted along with its log lines. Queued and
  running jobs are never touched. ``scan_runs`` gets the same treatment.
* **Planner statistics.** Without ``ANALYZE`` the query planner guesses at index
  selectivity, and on a table that has grown from a thousand rows to a million the guess
  is wrong. A full ``ANALYZE`` runs once, when there are no statistics at all; after that
  ``PRAGMA optimize`` refreshes only what the planner has noticed it needs.
* **WAL checkpoints.** The automatic checkpoint runs on a writer's commit and only when
  no reader holds the WAL open. A long-lived map request can keep deferring it, and the
  WAL file keeps growing. A ``PASSIVE`` checkpoint never blocks anyone; it just does as
  much as it can right now.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.settings_schema import DEFAULTS
from app.db.models import JobState, ProcessingJob, ScanRun

log = get_logger(__name__)

#: Job states that will never change again and are therefore safe to forget.
TERMINAL_STATES: frozenset[JobState] = frozenset(
    {JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED}
)


@dataclass(slots=True)
class MaintenanceReport:
    jobs_pruned: int = 0
    scan_runs_pruned: int = 0
    analyzed: bool = False
    optimized: bool = False
    wal_pages_checkpointed: int | None = None


async def prune_job_history(session: AsyncSession, days: int) -> int:
    """Delete terminal ``processing_jobs`` that finished more than *days* ago.

    Rows that never recorded ``finished_at`` (cancelled while queued, or written by an
    older release) fall back to their ``queued_at``. ``log_entries`` go with them through
    the ``ON DELETE CASCADE`` on ``log_entries.job_id``.
    """
    days = max(1, int(days))
    cutoff = datetime.now(UTC) - timedelta(days=days)
    result = await session.execute(
        delete(ProcessingJob).where(
            ProcessingJob.state.in_(TERMINAL_STATES),
            func.coalesce(ProcessingJob.finished_at, ProcessingJob.queued_at) < cutoff,
        )
    )
    await session.commit()
    return int(result.rowcount or 0)


async def prune_scan_runs(session: AsyncSession, days: int) -> int:
    """Delete ``scan_runs`` that started more than *days* ago."""
    days = max(1, int(days))
    cutoff = datetime.now(UTC) - timedelta(days=days)
    result = await session.execute(delete(ScanRun).where(ScanRun.started_at < cutoff))
    await session.commit()
    return int(result.rowcount or 0)


async def refresh_planner_statistics(session: AsyncSession) -> tuple[bool, bool]:
    """``ANALYZE`` when SQLite has no statistics yet, otherwise ``PRAGMA optimize``.

    Returns ``(analyzed, optimized)``. No-op on other dialects.
    """
    bind = session.get_bind()
    if bind.dialect.name != "sqlite":
        return False, False

    has_stats = (
        await session.execute(
            text("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'sqlite_stat1'")
        )
    ).scalar_one_or_none()
    populated = False
    if has_stats:
        populated = bool(
            (await session.execute(text("SELECT 1 FROM sqlite_stat1 LIMIT 1"))).scalar()
        )

    if not populated:
        await session.execute(text("ANALYZE"))
        await session.commit()
        return True, False

    await session.execute(text("PRAGMA optimize"))
    await session.commit()
    return False, True


async def checkpoint_wal(session: AsyncSession) -> int | None:
    """Run a non-blocking WAL checkpoint. Returns pages checkpointed, or None if N/A."""
    bind = session.get_bind()
    if bind.dialect.name != "sqlite":
        return None
    row = (await session.execute(text("PRAGMA wal_checkpoint(PASSIVE)"))).first()
    await session.commit()
    if row is None:
        return None
    # (busy, log_pages, checkpointed_pages); -1 when not in WAL mode.
    checkpointed = int(row[2])
    return None if checkpointed < 0 else checkpointed


async def run_maintenance(
    session: AsyncSession, *, job_history_days: int | None = None
) -> MaintenanceReport:
    """One housekeeping pass. Safe to run while workers are busy."""
    if job_history_days is None:
        job_history_days = int(DEFAULTS["advanced.job_history_days"])

    report = MaintenanceReport()
    report.jobs_pruned = await prune_job_history(session, job_history_days)
    report.scan_runs_pruned = await prune_scan_runs(session, job_history_days)
    report.analyzed, report.optimized = await refresh_planner_statistics(session)
    report.wal_pages_checkpointed = await checkpoint_wal(session)

    log.info(
        "database maintenance: pruned %d jobs and %d scan runs, %s, checkpointed %s WAL pages",
        report.jobs_pruned,
        report.scan_runs_pruned,
        "analyzed" if report.analyzed else ("optimized" if report.optimized else "no stats work"),
        "n/a" if report.wal_pages_checkpointed is None else report.wal_pages_checkpointed,
    )
    return report


__all__ = [
    "TERMINAL_STATES",
    "MaintenanceReport",
    "checkpoint_wal",
    "prune_job_history",
    "prune_scan_runs",
    "refresh_planner_statistics",
    "run_maintenance",
]
