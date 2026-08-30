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
from app.pipeline.repair import repair_durations
from app.retention import execute as run_retention
from app.retention import plan as plan_retention
from app.retention import plan_idle
from app.scanner.discovery import Scanner, queue_unprocessed
from app.workers import queue

log = get_logger(__name__)

#: Completed jobs arrive from several workers. They notify this one scheduler instead of
#: running retention themselves, so deletion remains serial with the normal scheduled
#: pass. The short debounce coalesces siblings that finish together; the cooldown bounds
#: expensive share safety walks during a long reprocessing backlog while still shrinking
#: the old six-hour delay to at most a few minutes.
_POST_PROCESS_IDLE_DEBOUNCE_S = 30.0
_POST_PROCESS_IDLE_MIN_INTERVAL_S = 5 * 60.0
_POST_PROCESS_IDLE_BATCH_SIZE = 64
_POST_PROCESS_IDLE_QUEUE_LIMIT = 512


@dataclass(slots=True)
class TaskState:
    name: str
    interval_s: Callable[[], Awaitable[float]]
    run: Callable[[], Awaitable[None]]
    enabled: Callable[[], Awaitable[bool]]
    next_run: float = 0.0
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
        self._pending_idle_recordings: set[int] = set()
        self._post_process_idle_due_at: float | None = None
        self._last_idle_cleanup_at = 0.0
        self._idle_cleanup_overflow_reported = False

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
            # scan, retention and pruning all hitting the database simultaneously.  The
            # exception is retention: run it once immediately on every service restart so
            # already-analysed junk and over-limit old footage free space without waiting
            # as long as the configured (six-hour by default) interval.  The loop is
            # serial, so the later staggered tasks cannot race this startup sweep.
            task.next_run = (
                now if task.name == "retention" else now + 10.0 + index * 5.0 + random.uniform(0, 5)
            )

        if self._pending_idle_recordings:
            self._post_process_idle_due_at = now + _POST_PROCESS_IDLE_DEBOUNCE_S

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

            # Worker completions are coalesced here, after periodic work. There is only
            # one scheduler loop, so this cannot race the scheduled retention pass or
            # turn N workers into N simultaneous deletion runs.
            try:
                await self._run_due_post_process_idle_cleanup()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("post-processing static cleanup failed", error=str(exc))

            await asyncio.sleep(1.0)

    # -- tasks -------------------------------------------------------------------------

    async def _run_scan(self) -> None:
        summary = await self._scanner.scan(trigger="scheduled")
        settings = get_settings_service()
        # Unconditional, because every attempt to predict when there is nothing to queue
        # has been wrong. Gating on `new` alone stranded files the camera rewrote; gating
        # on `new or changed` still strands a file that left the settle window on a scan
        # where its stat did not move, and a recording whose job was cancelled or lost.
        # `queue_unprocessed` is one indexed query that normally matches nothing, so the
        # gate was buying nothing and costing correctness.
        if await settings.auto_process():
            async with session_scope() as session:
                summary.queued = await queue_unprocessed(session)

        await self._post_scan_maintenance(summary)

    async def _post_scan_maintenance(self, summary) -> None:
        """Everything that has to happen after a walk, whoever asked for the walk.

        Factored out because the manual path did not do it. "Scan now" ran an
        *unconditional* full journey rebuild and none of the four self-healing passes, so
        pressing the button did more work than the scheduler and less good: the rebuild
        `needs_recluster` exists to avoid, without the start-position correction, the stale
        rollup repair or the duration repair that make the next one unnecessary. Two paths
        answering the same question differently is the shape of defect this codebase has
        been bitten by most often.
        """
        # New footage almost always extends the most recent journey, so keep boundaries
        # fresh rather than waiting for the next full rebuild.
        if (
            summary.new
            or summary.changed
            or summary.damaged_hidden
            or summary.damaged_deleted
            or summary.damaged_restored
        ):
            async with session_scope() as session:
                await JourneyBuilder().rebuild(session)

        # Correct the derived start positions *before* asking whether anything needs
        # reclustering, because clustering reads them and the staleness check clusters to
        # decide. Left until the rebuild, they are only ever corrected by a rebuild that a
        # correct-looking grouping means never happens -- a library grouped consistently
        # with wrong coordinates is stable, wrong, and has nothing left to disturb it.
        async with session_scope() as session:
            await JourneyBuilder().repair_start_positions(session)

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

        # The same self-healing, one level down: a recording whose stored duration is
        # impossible for its file size was measured by a probe that has since been fixed.
        # Two files were still claiming 26 hours each long after the clamp that catches
        # them started working, because a probe only ever runs once per recording.
        async with session_scope() as session:
            corrected = await repair_durations(session)
        if corrected:
            # Journey rollups are built on these durations, so they are now stale too.
            async with session_scope() as session:
                await JourneyBuilder().repair_stale(session)

    async def _run_retention(self) -> None:
        async with session_scope() as session:
            # dry_run mirrors the user's own setting: with deletion disabled (the default,
            # and the only option on a read-only mount) both passes just refresh the report.
            enabled = await get_settings_service().deletion_enabled()
            plan = await plan_retention(session)
            await run_retention(session, plan, dry_run=not enabled, trigger="scheduled")
            # Static, empty clips are removed independently of the size limit and of the
            # master 'actually delete' switch: the plan authorises its own deletion (it only
            # touches footage proven worthless), so this runs for real -- dry_run=False --
            # while the mount-writable and fraction guards inside still apply. The size-based
            # plan has just evaluated safety, so hand it over rather than walk the tree twice.
            # Snapshot notifications already present: this full-library query subsumes
            # them. Notifications arriving while it runs are left pending because they
            # may have committed after the query took its view of the database.
            covered_notifications = set(self._pending_idle_recordings)
            idle = await plan_idle(session, plan.safety)
            await run_retention(session, idle, dry_run=False, trigger="idle-cleanup")
            self._pending_idle_recordings.difference_update(covered_notifications)
        self._idle_cleanup_finished()

    async def _run_post_process_idle_cleanup(self, recording_ids: tuple[int, ...]) -> None:
        """Delete only the bounded group of recordings whose analysis just settled."""
        async with session_scope() as session:
            idle = await plan_idle(session, recording_ids=recording_ids)
            await run_retention(
                session,
                idle,
                dry_run=False,
                trigger="post-process-idle-cleanup",
            )

    async def _run_due_post_process_idle_cleanup(self, now: float | None = None) -> bool:
        """Run one due batch. Returns whether a cleanup attempt was made."""
        moment = time.monotonic() if now is None else now
        due = self._post_process_idle_due_at
        if due is None or moment < due or not self._pending_idle_recordings:
            return False

        recording_ids = tuple(sorted(self._pending_idle_recordings))[:_POST_PROCESS_IDLE_BATCH_SIZE]
        self._pending_idle_recordings.difference_update(recording_ids)
        self._post_process_idle_due_at = None
        try:
            await self._run_post_process_idle_cleanup(recording_ids)
        except asyncio.CancelledError:
            # A normal service stop can interrupt the safety walk before it has acted.
            # Keep the notification batch for an in-process scheduler restart. A process
            # restart still has the periodic full pass, and a partially completed retry is
            # safe because the database and filesystem guards are re-evaluated.
            self._restore_pending_idle(recording_ids)
            raise
        except Exception:
            # A failed mount or database attempt must not turn into a tight retry loop.
            # Put the bounded batch back; the cooldown below delays it, and the periodic
            # full-library pass remains a second route to the same recordings.
            self._restore_pending_idle(recording_ids)
            raise
        finally:
            self._idle_cleanup_finished()
        return True

    def request_idle_cleanup(self, recording_id: int) -> bool:
        """Coalesce one successful analysis into a scheduler-owned cleanup batch."""
        if (
            not self._running
            or recording_id <= 0
            or not bool(get_settings_service().get_nowait("storage.delete_idle"))
        ):
            return False
        if recording_id in self._pending_idle_recordings:
            return True
        if len(self._pending_idle_recordings) >= _POST_PROCESS_IDLE_QUEUE_LIMIT:
            if not self._idle_cleanup_overflow_reported:
                log.warning(
                    "post-processing static cleanup queue reached its safety limit",
                    limit=_POST_PROCESS_IDLE_QUEUE_LIMIT,
                )
                self._idle_cleanup_overflow_reported = True
            return False

        self._pending_idle_recordings.add(recording_id)
        now = time.monotonic()
        due = max(
            now + _POST_PROCESS_IDLE_DEBOUNCE_S,
            self._last_idle_cleanup_at + _POST_PROCESS_IDLE_MIN_INTERVAL_S,
        )
        # Keep the first deadline. Constant arrivals must coalesce, not postpone cleanup
        # forever by moving the debounce window on every worker completion.
        if self._post_process_idle_due_at is None:
            self._post_process_idle_due_at = due
        else:
            self._post_process_idle_due_at = min(self._post_process_idle_due_at, due)
        return True

    def _restore_pending_idle(self, recording_ids: tuple[int, ...]) -> None:
        room = max(0, _POST_PROCESS_IDLE_QUEUE_LIMIT - len(self._pending_idle_recordings))
        self._pending_idle_recordings.update(recording_ids[:room])

    def _idle_cleanup_finished(self) -> None:
        self._last_idle_cleanup_at = time.monotonic()
        if not self._pending_idle_recordings:
            self._post_process_idle_due_at = None
            self._idle_cleanup_overflow_reported = False
            return
        self._post_process_idle_due_at = max(
            self._last_idle_cleanup_at + _POST_PROCESS_IDLE_MIN_INTERVAL_S,
            time.monotonic() + _POST_PROCESS_IDLE_DEBOUNCE_S,
        )

    async def _run_reclaim(self) -> None:
        async with session_scope() as session:
            await queue.reclaim_stale(session)
            # Both halves of the pairing, not just the job.
            #
            # `reclaim_stale` only looks at RUNNING job rows, so it cannot see a recording
            # whose job ended in a state that is not RUNNING -- and cancelling a job is
            # exactly that. `run_stages` commits `state = PROCESSING` before the first
            # stage, the cancelled run returns without writing an outcome, and nothing
            # else demotes the recording: `queue_unprocessed` looks for DISCOVERED and
            # METADATA_EXTRACTED, "reprocess failed" wants FAILED, and this was the only
            # periodic task in the process. Every cancelled analysis therefore orphaned one
            # recording until the next container restart.
            #
            # Safe to run on a timer: the predicate excludes anything holding a queued or
            # running job, so a healthy run in flight is never touched.
            await queue.release_stranded_recordings(session)

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
        await self._post_scan_maintenance(summary)
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
