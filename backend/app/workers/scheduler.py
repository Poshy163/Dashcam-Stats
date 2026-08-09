"""Periodic background tasks.

Every interval is re-read from settings on each cycle rather than captured at start-up,
so changing the scan interval in the UI takes effect on the next tick without a restart.
One failing task never stops the loop — a share that is briefly unavailable must not
disable log pruning or job reclamation.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.core.logging import get_logger, prune_logs
from app.core.settings_service import get_settings_service
from app.db.session import session_scope
from app.journeys.builder import JourneyBuilder
from app.retention import execute as run_retention
from app.retention import plan as plan_retention
from app.scanner.discovery import Scanner, queue_unprocessed
from app.workers import queue

log = get_logger(__name__)


@dataclass(slots=True)
class TaskState:
    name: str
    interval_s: Callable[[], Awaitable[float]]
    run: Callable[[], Awaitable[None]]
    enabled: Callable[[], Awaitable[bool]]
    next_run: float = 0.0
    last_run: float | None = None
    last_error: str | None = None
    runs: int = 0
    failures: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "runs": self.runs,
            "failures": self.failures,
            "last_error": self.last_error,
            "seconds_until_next": max(0.0, round(self.next_run - time.monotonic(), 1)),
        }


class Scheduler:
    """Drives the scan, retention, job reclamation and log pruning."""

    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._tasks: list[TaskState] = []
        self._scanner = Scanner()

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        settings = get_settings_service()

        self._tasks = [
            TaskState(
                name="scan",
                interval_s=settings.scan_interval_s,
                enabled=settings.scan_enabled,
                run=self._run_scan,
            ),
            TaskState(
                name="retention",
                interval_s=settings.cleanup_interval_s,
                enabled=lambda: _flag("storage.cleanup_enabled"),
                run=self._run_retention,
            ),
            TaskState(
                name="reclaim",
                interval_s=lambda: _fixed(60.0),
                enabled=lambda: _fixed(True),
                run=self._run_reclaim,
            ),
            TaskState(
                name="prune-logs",
                interval_s=lambda: _fixed(6 * 3600.0),
                enabled=lambda: _fixed(True),
                run=self._run_prune,
            ),
        ]

        now = time.monotonic()
        for index, task in enumerate(self._tasks):
            # Stagger the first run. Firing every task at once on boot would have the
            # scan, retention and pruning all hitting the database simultaneously.
            task.next_run = now + 10.0 + index * 5.0 + random.uniform(0, 5)

        self._task = asyncio.create_task(self._loop(), name="scheduler")
        log.info("scheduler started", tasks=[t.name for t in self._tasks])

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        log.info("scheduler stopped")

    async def _loop(self) -> None:
        while self._running:
            now = time.monotonic()
            for task in self._tasks:
                if now < task.next_run:
                    continue
                try:
                    if await task.enabled():
                        await task.run()
                        task.runs += 1
                        task.last_run = now
                        task.last_error = None
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    task.failures += 1
                    task.last_error = f"{type(exc).__name__}: {exc}"
                    log.warning("scheduled task failed", task=task.name, error=str(exc))
                finally:
                    try:
                        interval = max(30.0, await task.interval_s())
                    except Exception:
                        interval = 300.0
                    task.next_run = time.monotonic() + interval

            await asyncio.sleep(1.0)

    # -- tasks -------------------------------------------------------------------------

    async def _run_scan(self) -> None:
        summary = await self._scanner.scan(trigger="scheduled")
        settings = get_settings_service()
        # `changed` matters as much as `new`. A file the camera rewrote, or one the settle
        # window held back last time, is reset to DISCOVERED with its stage states cleared
        # but is never enqueued by the scan itself. Gating on `new` alone left it demoted
        # and unprocessed -- showing stale telemetry as if current -- until some unrelated
        # new file happened to arrive. On a library that has finished importing, that is
        # never: this one reported `new: 0` on all twenty scans since its initial import.
        if (summary.new or summary.changed) and await settings.auto_process():
            async with session_scope() as session:
                summary.queued = await queue_unprocessed(session)

        # New footage almost always extends the most recent journey, so keep boundaries
        # fresh rather than waiting for the next full rebuild.
        if summary.new or summary.changed:
            async with session_scope() as session:
                await JourneyBuilder().rebuild(session)

        # Recluster when the journey boundaries have drifted. Recordings finishing out of
        # chronological order each create their own journey, and on a library that has
        # finished importing the scanner never reports a new file, so nothing above would
        # ever put them back together.
        async with session_scope() as session:
            builder = JourneyBuilder()
            if await builder.needs_recluster(session):
                log.info("journey boundaries look stale; reclustering")
                await builder.rebuild(session)

        # Self-heal journeys whose rollups were invalidated and never recomputed -- by a
        # migration, a decoder fix, or telemetry that arrived after the journey did. Cheap
        # when there is nothing to do, which is the normal case.
        async with session_scope() as session:
            await JourneyBuilder().repair_stale(session)

    async def _run_retention(self) -> None:
        async with session_scope() as session:
            plan = await plan_retention(session)
            # dry_run mirrors the user's own setting: with deletion disabled (the default,
            # and the only option on a read-only mount) this just refreshes the report.
            enabled = await get_settings_service().deletion_enabled()
            await run_retention(session, plan, dry_run=not enabled, trigger="scheduled")

    async def _run_reclaim(self) -> None:
        async with session_scope() as session:
            await queue.reclaim_stale(session)

    async def _run_prune(self) -> None:
        days = int(get_settings_service().get_nowait("advanced.log_retention_days"))
        async with session_scope() as session:
            await prune_logs(session, days)

    # -- manual triggers ---------------------------------------------------------------

    async def scan_now(self) -> object:
        summary = await self._scanner.scan(trigger="manual")
        if await get_settings_service().auto_process():
            async with session_scope() as session:
                summary.queued = await queue_unprocessed(session)
        async with session_scope() as session:
            await JourneyBuilder().rebuild(session)
        return summary

    async def process_new(self) -> int:
        async with session_scope() as session:
            return await queue_unprocessed(session)

    @property
    def healthy(self) -> bool:
        return self._running and self._task is not None and not self._task.done()

    def describe(self) -> list[dict[str, object]]:
        return [task.as_dict() for task in self._tasks]


async def _flag(key: str) -> bool:
    return bool(get_settings_service().get_nowait(key))


async def _fixed(value):
    return value


_scheduler: Scheduler | None = None


def get_scheduler() -> Scheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = Scheduler()
    return _scheduler
