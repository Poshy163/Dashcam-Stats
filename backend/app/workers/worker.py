"""The worker pool.

Concurrency follows ``processing.max_workers`` and resizes **live** — changing it in the
UI must take effect without a restart, which is an explicit product requirement. Each
worker owns its own database session; SQLite in WAL mode handles one writer and many
readers, and jobs are short enough that write contention stays negligible.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
import uuid
from dataclasses import dataclass, field

from app.core.logging import get_logger, log_context
from app.core.settings_service import get_settings_service
from app.damaged_policy import apply_damaged_policy
from app.db.models import JobKind, JobState, ProcessingJob, Recording
from app.db.session import session_scope
from app.hardware.detect import detect_hardware
from app.pipeline.orchestrator import RunReport, pending_stages, run_stages
from app.pipeline.stages import StageError, StageResult, ensure_thumbnail
from app.workers import queue

log = get_logger(__name__)

#: How often a running job reports progress. Frequent enough for a live UI, rare enough
#: that the writes are invisible next to the decoding work.
_HEARTBEAT_INTERVAL_S = 3.0

#: Idle poll interval. The queue is not latency-critical — new work arrives from a scan.
_IDLE_SLEEP_S = 2.0

#: Attempts at recording a finished job's outcome, and the pause between them.
#:
#: Losing this write throws away the entire run: the row stays RUNNING with no worker, is
#: reclaimed five minutes later, and the recording is decoded again from the first frame.
#: Retrying is cheap and the work it protects is not.
_FINISH_ATTEMPTS = 4
_FINISH_RETRY_S = 3.0

#: Consecutive heartbeat failures before saying so at a level anyone will see. One is a
#: blip; a run of them means the job is on its way to being reclaimed.
_HEARTBEAT_WARN_AFTER = 3

#: How long an abort waits for the runs it interrupted to actually stop.
#:
#: Cancelling an asyncio task only takes effect at its next await, and a stage inside a
#: model or an ffmpeg read reaches one within a frame or two -- not instantly, but quickly.
#: The wait matters because the caller is about to queue replacement work: returning early
#: would let a new job for a recording start while the old run was still writing that same
#: recording's rows, which is the one thing the claim's ``NOT EXISTS`` guard exists to
#: prevent and which it cannot see once the old job has been cancelled out from under it.
_ABORT_GRACE_S = 20.0


@dataclass(slots=True)
class ActiveJob:
    job_id: int
    recording_id: int | None
    filename: str
    stage: str = "starting"
    progress: float = 0.0
    started_at: float = field(default_factory=time.monotonic)
    speed_realtime: float | None = None
    decoder: str | None = None
    inference_device: str | None = None
    #: Set once the job has been cancelled in the database, so the run can be stopped.
    cancelled: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "job_id": self.job_id,
            "recording_id": self.recording_id,
            "filename": self.filename,
            "stage": self.stage,
            "progress": round(self.progress, 3),
            "elapsed_s": round(time.monotonic() - self.started_at, 1),
            "speed_realtime": self.speed_realtime,
            "decoder": self.decoder,
            "inference_device": self.inference_device,
        }


class WorkerPool:
    """Runs queued jobs, resizing itself when the concurrency setting changes."""

    def __init__(self) -> None:
        self._workers: dict[int, asyncio.Task[None]] = {}
        self._active: dict[int, ActiveJob] = {}
        #: The in-flight run for each job, so a cancellation can interrupt it.
        self._work: dict[int, asyncio.Task] = {}
        self._running = False
        self._supervisor: asyncio.Task[None] | None = None
        self._pool_id = uuid.uuid4().hex[:8]
        self._unsubscribe = None

    # -- lifecycle ---------------------------------------------------------------------

    async def start(self) -> None:
        if self._running:
            return
        self._running = True

        # Anything left RUNNING belongs to a previous process that did not shut down
        # cleanly; reclaim it before taking new work. Both halves: the job, and the
        # recording that the job was halfway through, which nothing else puts right.
        async with session_scope() as session:
            await queue.reclaim_interrupted(session)
            await queue.release_stranded_recordings(session)
            await queue.reconcile_legacy_contention_failures(session)
            await queue.reconcile_misclassified_probe_crashes(session)
            await queue.reconcile_media_failure_hides(session)

        self._supervisor = asyncio.create_task(self._supervise(), name="worker-supervisor")
        settings = get_settings_service()
        self._unsubscribe = (
            settings.subscribe(lambda _change: None, keys=("processing.max_workers",))
            if hasattr(settings, "subscribe")
            else None
        )
        log.info("worker pool started", pool=self._pool_id)

    async def stop(self) -> None:
        self._running = False
        if self._supervisor is not None:
            self._supervisor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._supervisor
            self._supervisor = None

        for task in list(self._workers.values()):
            task.cancel()
        for task in list(self._workers.values()):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._workers.clear()

        if callable(self._unsubscribe):
            with contextlib.suppress(Exception):
                self._unsubscribe()

        # Hand back anything still in flight so the next start picks it up immediately
        # rather than waiting for the heartbeat timeout.
        async with session_scope() as session:
            await queue.reclaim_interrupted(session)
        log.info("worker pool stopped", pool=self._pool_id)

    async def _supervise(self) -> None:
        """Keep the number of workers matching the setting."""
        while self._running:
            try:
                desired = await get_settings_service().max_workers()
                desired = max(1, min(16, desired))

                for index in list(self._workers):
                    if self._workers[index].done():
                        self._workers.pop(index, None)

                while len(self._workers) < desired:
                    index = next(i for i in range(desired + 1) if i not in self._workers)
                    self._workers[index] = asyncio.create_task(
                        self._worker(index), name=f"worker-{index}"
                    )

                while len(self._workers) > desired:
                    index = max(self._workers)
                    task = self._workers.pop(index)
                    task.cancel()

            except Exception as exc:
                log.warning("worker supervisor error", error=str(exc))

            await asyncio.sleep(2.0)

    # -- the work ----------------------------------------------------------------------

    async def _worker(self, index: int) -> None:
        worker_id = f"{self._pool_id}-{os.getpid()}-{index}"
        while self._running:
            try:
                job = None
                async with session_scope() as session:
                    job = await queue.claim_next(session, worker_id)
                    if job is not None:
                        # Materialise what we need before the session closes.
                        recording = (
                            await session.get(Recording, job.recording_id)
                            if job.recording_id
                            else None
                        )
                        job_id = job.id
                        recording_id = job.recording_id
                        kind = job.kind
                        stages = list(job.stages) if job.stages else None
                        filename = recording.filename if recording else f"job {job.id}"

                if job is None:
                    await asyncio.sleep(_IDLE_SLEEP_S)
                    continue

                await self._run_job(job_id, recording_id, kind, stages, filename)

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # A worker must never die on an unexpected error, or the pool silently
                # shrinks and processing stops with no visible cause.
                log.exception("worker loop error", worker=worker_id, error=str(exc))
                await asyncio.sleep(_IDLE_SLEEP_S)

    @staticmethod
    def _device_from(report) -> str | None:
        """The inference device a stage reported using, if any ran.

        Stages record it in their own stats rather than on the report, because only the
        stages that actually run a model know one. Telemetry-only work legitimately has
        no answer.
        """
        for stage in reversed(report.stages):
            device = (stage.stats or {}).get("device")
            if device:
                return str(device)
        return None

    @staticmethod
    def _decoder_from(report) -> str | None:
        """Summarise the decoder actually used, preferring any software fallback."""
        found: str | None = None
        for stage in report.stages:
            decoder = (stage.stats or {}).get("decoder")
            if decoder == "software":
                return "software"
            if decoder:
                found = str(decoder)
        return found

    async def _run_job(
        self,
        job_id: int,
        recording_id: int | None,
        kind: JobKind,
        stages: list[str] | None,
        filename: str,
    ) -> None:
        # Everything logged from here down carries the job and the recording, including
        # lines raised from inside a thread executor. This is what makes a sighting
        # traceable: the coordinate a stage chose, the job that chose it and the file it
        # came from end up on the same rows of `log_entries`.
        with log_context(recording_id=recording_id, job_id=job_id, file=filename):
            await self._run_job_inner(job_id, recording_id, kind, stages, filename)

    async def _run_job_inner(
        self,
        job_id: int,
        recording_id: int | None,
        kind: JobKind,
        stages: list[str] | None,
        filename: str,
    ) -> None:
        hardware = detect_hardware()
        active = ActiveJob(
            job_id=job_id,
            recording_id=recording_id,
            filename=filename,
            # Availability is not usage. The stage callback fills this with the decoder
            # that actually produced frames, including a VAAPI-to-software fallback.
            decoder=None if hardware.vaapi_available else "software",
        )
        self._active[job_id] = active

        heartbeat_task = asyncio.create_task(self._heartbeat(active))
        try:
            if recording_id is None:
                await self._finish(job_id, active, note="no recording attached")
                return

            # The run happens in its own task so the heartbeat can stop it. Cancelling is
            # the only way to interrupt work that is inside a model or an ffmpeg read;
            # without it the Cancel button changed a badge and nothing else, and the run
            # then wrote its own result over the top and un-cancelled itself.
            work = asyncio.create_task(
                self._make_thumbnail(recording_id, active)
                if kind is JobKind.THUMBNAIL
                else self._process(recording_id, stages, active)
            )
            self._work[job_id] = work
            try:
                report = await work
            except asyncio.CancelledError:
                if not active.cancelled:
                    raise
                log.info("job cancelled", job_id=job_id, file=filename)
                return

            if report is None:
                await self._finish(
                    job_id, active, fail="recording no longer exists", permanent=True
                )
                return

            await self._finish(job_id, active, report=report)
            log.info(
                "processed recording",
                file=filename,
                ok=report.ok,
                elapsed_s=round(report.elapsed_s, 1),
                realtime=report.realtime_factor,
            )
        finally:
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat_task
            self._active.pop(job_id, None)
            self._work.pop(job_id, None)

    async def _make_thumbnail(self, recording_id: int, active: ActiveJob):
        """Run the thumbnail-first pass over one recording. Returns a report, or None.

        Shaped like ``_process`` so the outcome is recorded by the same code: a thumbnail
        job succeeds, retries and fails exactly as an analysis job does, and needs no second
        opinion about what any of those mean.

        Nothing here touches ``recordings.state``. The recording is still queued -- for its
        analysis, which this pass has deliberately not done -- and marking it ``processing``
        for the few seconds a thumbnail takes would report a run that is not happening and
        leave the row stranded if the process died mid-decode.
        """
        started = time.monotonic()
        async with session_scope() as session:
            recording = await session.get(Recording, recording_id)
            if recording is None:
                return None

            active.stage = "thumbnail"
            report = RunReport(recording_id=recording_id)
            try:
                outcome = await ensure_thumbnail(recording)
            except StageError as exc:
                report.ok = False
                report.error = str(exc)
                report.permanent = exc.permanent
                report.transient = exc.transient
            else:
                detail = (
                    "written"
                    if outcome.written
                    else ("already present" if outcome.present else "no usable frame")
                )
                report.stages.append(StageResult("thumbnail", True, detail=detail))
            report.elapsed_s = time.monotonic() - started
            active.progress = 1.0
            await session.flush()
            return report

    async def _process(self, recording_id: int, stages: list[str] | None, active: ActiveJob):
        """Run the stages. Returns the report, or None if the recording has gone."""
        async with session_scope() as session:
            recording = await session.get(Recording, recording_id)
            if recording is None:
                return None

            selected = stages or list(pending_stages(recording)) or None

            def on_progress(stage: str, fraction: float) -> None:
                active.stage = stage
                active.progress = fraction

            def on_stage_complete(result) -> None:
                stats = result.stats or {}
                decoder = stats.get("decoder")
                if decoder == "software" or (decoder and active.decoder is None):
                    active.decoder = str(decoder)
                device = stats.get("device")
                if device:
                    active.inference_device = str(device)

            report = await run_stages(
                session,
                recording,
                selected,
                progress=on_progress,
                stage_complete=on_stage_complete,
            )
            active.speed_realtime = report.realtime_factor
            # Which device actually ran inference is only known once a stage has used
            # one, and it is worth surfacing: it is the difference between the iGPU
            # doing the work and the CPU quietly doing it instead.
            active.inference_device = self._device_from(report) or active.inference_device
            active.decoder = self._decoder_from(report) or active.decoder

            # Metadata inspection can discover playable-but-damaged footage, while a
            # permanent stage failure identifies an unusable source. Apply the user's
            # policy only after the run has stopped reading the file.
            await apply_damaged_policy(session, recording)

            # Flush the recording's own outcome here, in the session that owns it. Left
            # dirty, it was flushed later by the first query of the bookkeeping below --
            # an autoflush that took the write lock at the worst possible moment and
            # failed, taking the finished run down with it.
            await session.flush()
            return report

    async def _finish(
        self,
        job_id: int,
        active: ActiveJob,
        *,
        report=None,
        fail: str | None = None,
        permanent: bool = False,
        note: str | None = None,
    ) -> None:
        """Record how a job ended, in a session of its own, retrying if the write fails.

        Separate from the run on purpose. These few writes are the only durable trace that
        minutes of decoding and inference happened at all, and they used to share both the
        session and the error handling with the work itself -- so a lock held elsewhere
        turned a completed job into a stranded RUNNING row, five idle minutes, a burned
        attempt, and a full reprocess from the first frame.
        """
        last: Exception | None = None
        for attempt in range(_FINISH_ATTEMPTS):
            try:
                async with session_scope() as session:
                    job = await session.get(ProcessingJob, job_id)
                    if job is None:
                        return
                    if job.state == JobState.CANCELLED:
                        # Someone asked for this to stop. Writing the result now would
                        # resurrect it -- as COMPLETED, or as QUEUED with a retry pending.
                        log.info("job was cancelled; not recording its outcome", job_id=job_id)
                        return
                    if fail is not None:
                        await queue.fail(session, job, fail, permanent=permanent)
                    elif report is not None and not report.ok:
                        await queue.fail(
                            session,
                            job,
                            report.error or "processing failed",
                            permanent=report.permanent,
                            transient=report.transient,
                        )
                    else:
                        await queue.complete(
                            session,
                            job,
                            report.as_dict() if report is not None else {"note": note},
                            speed=active.speed_realtime,
                            decoder=active.decoder,
                            device=active.inference_device,
                        )
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last = exc
                if attempt < _FINISH_ATTEMPTS - 1:
                    await asyncio.sleep(_FINISH_RETRY_S * (attempt + 1))

        # Out of attempts. Say so plainly: the row is now stranded RUNNING and will be
        # reclaimed, and this line is the only explanation of why the work was repeated.
        log.error(
            "could not record job outcome; it will be reclaimed and run again",
            job_id=job_id,
            attempts=_FINISH_ATTEMPTS,
            error=str(last),
        )

    async def _heartbeat(self, active: ActiveJob) -> None:
        """Publish progress, and notice if the job has been cancelled underneath us.

        This is also where a cancellation request is picked up. The pool has no other
        regular tick, and the alternative — polling from inside the stages — would put a
        database read in the middle of every decode loop.
        """
        consecutive_failures = 0
        while True:
            await asyncio.sleep(_HEARTBEAT_INTERVAL_S)
            try:
                async with session_scope() as session:
                    state = await queue.heartbeat(
                        session,
                        active.job_id,
                        progress=active.progress,
                        stage=active.stage,
                        speed=active.speed_realtime,
                        decoder=active.decoder,
                        device=active.inference_device,
                        resource=(
                            "waiting_for_vaapi" if active.decoder == "vaapi-waiting" else "running"
                        ),
                    )
                consecutive_failures = 0
                if state == JobState.CANCELLED and not active.cancelled:
                    active.cancelled = True
                    task = self._work.get(active.job_id)
                    if task is not None:
                        task.cancel()
                    return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                consecutive_failures += 1
                # A heartbeat that keeps failing is the only warning before the job is
                # reclaimed and its work thrown away. At debug level nobody ever saw it.
                report = log.warning if consecutive_failures >= _HEARTBEAT_WARN_AFTER else log.debug
                report(
                    "heartbeat failed",
                    job_id=active.job_id,
                    consecutive=consecutive_failures,
                    error=str(exc),
                )

    # -- interruption ------------------------------------------------------------------

    async def abort_active(self) -> int:
        """Stop every run in flight, and wait until they have actually stopped.

        For rebuilding the queue from scratch. Cancelling the job rows alone is not enough
        and is in fact the more dangerous half on its own: the claim refuses to start a
        second job for a recording that is ``RUNNING``, so cancelling the row removes the
        very guard that keeps the replacement job from starting on top of a run still
        decoding, deleting and rewriting that recording's tables.

        The pool itself keeps running. Workers whose job disappears simply find the queue
        empty and idle until the rebuild fills it, which is what should happen -- stopping
        and restarting the pool would reclaim jobs, restart the supervisor and run every
        reconciliation, none of which a queue reset wants.
        """
        interrupted = list(self._active.values())
        if not interrupted:
            return 0

        tasks: list[asyncio.Task] = []
        for job in interrupted:
            # Set before cancelling: this is what tells the run its cancellation was asked
            # for rather than propagating out as an unexplained failure, and what stops it
            # writing an outcome over a queue that has moved on without it.
            job.cancelled = True
            task = self._work.get(job.job_id)
            if task is not None and not task.done():
                task.cancel()
                tasks.append(task)

        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=_ABORT_GRACE_S)
            if pending:
                log.warning(
                    "some runs did not stop within the abort grace period",
                    still_running=len(pending),
                    grace_s=_ABORT_GRACE_S,
                )
        log.info("stopped in-flight processing", jobs=len(interrupted))
        return len(interrupted)

    # -- introspection -----------------------------------------------------------------

    def current_jobs(self) -> list[dict[str, object]]:
        return [job.as_dict() for job in self._active.values()]

    @property
    def worker_count(self) -> int:
        return len(self._workers)

    @property
    def healthy(self) -> bool:
        return self._running and bool(self._workers)


_pool: WorkerPool | None = None


def get_worker_pool() -> WorkerPool:
    global _pool
    if _pool is None:
        _pool = WorkerPool()
    return _pool
