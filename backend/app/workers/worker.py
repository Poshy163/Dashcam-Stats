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

from app.core.logging import get_logger
from app.core.settings_service import get_settings_service
from app.db.models import ProcessingJob, Recording
from app.db.session import session_scope
from app.hardware.detect import detect_hardware
from app.pipeline.orchestrator import pending_stages, run_stages
from app.workers import queue

log = get_logger(__name__)

#: How often a running job reports progress. Frequent enough for a live UI, rare enough
#: that the writes are invisible next to the decoding work.
_HEARTBEAT_INTERVAL_S = 3.0

#: Idle poll interval. The queue is not latency-critical — new work arrives from a scan.
_IDLE_SLEEP_S = 2.0


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
        # cleanly; reclaim it before taking new work.
        async with session_scope() as session:
            await queue.reclaim_stale(session)

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
            await queue.reclaim_stale(session)
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
                        stages = list(job.stages) if job.stages else None
                        filename = recording.filename if recording else f"job {job.id}"

                if job is None:
                    await asyncio.sleep(_IDLE_SLEEP_S)
                    continue

                await self._run_job(job_id, recording_id, stages, filename)

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

    async def _run_job(
        self, job_id: int, recording_id: int | None, stages: list[str] | None, filename: str
    ) -> None:
        hardware = detect_hardware()
        active = ActiveJob(
            job_id=job_id,
            recording_id=recording_id,
            filename=filename,
            decoder="vaapi" if hardware.vaapi_available else "software",
        )
        self._active[job_id] = active

        heartbeat_task = asyncio.create_task(self._heartbeat(active))
        try:
            if recording_id is None:
                async with session_scope() as session:
                    job = await session.get(ProcessingJob, job_id)
                    if job is not None:
                        await queue.complete(session, job, {"note": "no recording attached"})
                return

            async with session_scope() as session:
                recording = await session.get(Recording, recording_id)
                if recording is None:
                    job = await session.get(ProcessingJob, job_id)
                    if job is not None:
                        await queue.fail(session, job, "recording no longer exists", permanent=True)
                    return

                selected = stages or list(pending_stages(recording)) or None

                def on_progress(stage: str, fraction: float) -> None:
                    active.stage = stage
                    active.progress = fraction

                report = await run_stages(session, recording, selected, progress=on_progress)
                active.speed_realtime = report.realtime_factor
                # Which device actually ran inference is only known once a stage has used
                # one, and it is worth surfacing: it is the difference between the iGPU
                # doing the work and the CPU quietly doing it instead.
                active.inference_device = self._device_from(report) or active.inference_device

                job = await session.get(ProcessingJob, job_id)
                if job is not None:
                    if report.ok:
                        await queue.complete(
                            session,
                            job,
                            report.as_dict(),
                            speed=active.speed_realtime,
                            decoder=active.decoder,
                            device=active.inference_device,
                        )
                    else:
                        await queue.fail(
                            session,
                            job,
                            report.error or "processing failed",
                            permanent=report.permanent,
                        )

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

    async def _heartbeat(self, active: ActiveJob) -> None:
        """Publish progress so the queue page can show it and the job is not reclaimed."""
        while True:
            await asyncio.sleep(_HEARTBEAT_INTERVAL_S)
            try:
                async with session_scope() as session:
                    await queue.heartbeat(
                        session,
                        active.job_id,
                        progress=active.progress,
                        stage=active.stage,
                        speed=active.speed_realtime,
                        decoder=active.decoder,
                        device=active.inference_device,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.debug("heartbeat failed", job_id=active.job_id, error=str(exc))

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
