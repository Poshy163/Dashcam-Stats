"""Status, settings, scanning, retention, logs and hardware diagnostics."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import FileResponse
from sqlalchemy import func, select

from app.ai.models import describe_models, is_present
from app.ai.runtime import describe_media_policy, describe_runtime
from app.api.deps import PaginationDep, RowIdFilter, SessionDep
from app.api.schemas import (
    FeatureStatus,
    JourneyOut,
    LogEntryOut,
    Paginated,
    ReprocessAllRequest,
    RetentionPlanOut,
    SafetyReportOut,
    SettingCategoryOut,
    SettingsReset,
    SettingsUpdate,
    StatusOut,
    StatusProcessing,
    StatusStorage,
    StatusTotals,
)
from app.api.visibility import visible_journey_ids, visible_revision
from app.config import get_config
from app.core.logging import get_logger
from app.core.settings_service import (
    SettingValidationError,
    get_settings_service,
    local_midnight_utc,
)
from app.db.backup import create_backup, stage_restore
from app.db.models import (
    JobKind,
    Journey,
    LogEntry,
    Plate,
    PlateObservation,
    Recording,
    RecordingState,
    RetentionRun,
    TelemetryPoint,
    TrackedObject,
)
from app.db.session import current_revision
from app.hardware.detect import detect_hardware_async
from app.pipeline.orchestrator import expand_stages, invalidate_recordings
from app.pipeline.revisions import outdated_stages
from app.retention import current_usage, evaluate_safety
from app.retention import execute as run_retention
from app.retention import plan as plan_retention
from app.workers import queue
from app.workers.scheduler import get_scheduler
from app.workers.worker import get_worker_pool

log = get_logger(__name__)

router = APIRouter(prefix="/api", tags=["system"])


def _feature_status(settings, totals: StatusTotals) -> list[FeatureStatus]:
    """Why an analysis count is what it is.

    A zero on the dashboard has two completely different meanings and looked identical
    until now. On a real library, "0 vehicles seen" across 674 processed recordings was not
    a finding about quiet roads — the detection weights were fetched from a URL that did
    not exist, every attempt 404'd, and the stage silently produced nothing while the
    recordings were still marked completed. The number was accurate and told the user the
    opposite of the truth.
    """

    def describe(
        key: str, label: str, setting: str, models: tuple[str, ...], results: int
    ) -> FeatureStatus:
        enabled = bool(settings.get_nowait(setting))
        missing = [name for name in models if not is_present(name)]
        if not enabled:
            reason = "Switched off in settings."
        elif missing:
            reason = (
                f"Model{'s' if len(missing) > 1 else ''} not downloaded yet "
                f"({', '.join(missing)}). They are fetched on first use; a failure here "
                "leaves this feature unavailable rather than failing the recording."
            )
        elif results == 0:
            reason = (
                "Ready, but nothing has been analysed with it yet. Recordings processed "
                "before it became available need reprocessing to be included."
            )
        else:
            reason = None
        return FeatureStatus(
            key=key,
            label=label,
            enabled=enabled,
            ready=enabled and not missing,
            blocked_reason=reason,
            results=results,
        )

    detection_model = str(settings.get_nowait("processing.detection_model"))
    return [
        describe(
            "detection",
            "Vehicle detection",
            "processing.detection_enabled",
            (detection_model,),
            totals.tracked_objects,
        ),
        describe(
            "plates",
            "Plate reading",
            "plates.enabled",
            ("plate-detector", "plate-ocr"),
            totals.plates,
        ),
    ]


@router.get("/status", response_model=StatusOut)
async def get_status(session: SessionDep):
    totals = StatusTotals(
        recordings=int(
            (
                await session.execute(
                    select(func.count(Recording.id)).where(
                        Recording.file_missing.is_(False), Recording.ignored.is_(False)
                    )
                )
            ).scalar()
            or 0
        ),
        journeys=int(
            (
                await session.execute(
                    select(func.count(Journey.id)).where(Journey.id.in_(visible_journey_ids()))
                )
            ).scalar()
            or 0
        ),
        telemetry_points=int(
            (
                await session.execute(
                    select(func.count(TelemetryPoint.id))
                    .join(Recording, Recording.id == TelemetryPoint.recording_id)
                    .where(visible_revision(Recording.telemetry_revision))
                )
            ).scalar()
            or 0
        ),
        tracked_objects=int(
            (
                await session.execute(
                    select(func.count(TrackedObject.id))
                    .join(Recording, Recording.id == TrackedObject.recording_id)
                    .where(visible_revision(Recording.detection_revision))
                )
            ).scalar()
            or 0
        ),
        plates=int(
            (
                await session.execute(
                    select(func.count(func.distinct(Plate.id)))
                    .join(PlateObservation, PlateObservation.plate_id == Plate.id)
                    .join(Recording, Recording.id == PlateObservation.recording_id)
                    .where(visible_revision(Recording.plate_revision))
                )
            ).scalar()
            or 0
        ),
        duration_s=float(
            (
                await session.execute(
                    select(func.coalesce(func.sum(Recording.duration_s), 0.0)).where(
                        Recording.ignored.is_(False)
                    )
                )
            ).scalar()
            or 0.0
        ),
    )

    used, files, limit = await current_usage(session)
    totals.footage_bytes = used
    totals.footage_files = files

    state_rows = (
        await session.execute(
            select(Recording.state, func.count(Recording.id))
            .where(Recording.ignored.is_(False))
            .group_by(Recording.state)
        )
    ).all()
    counts = {state.value: int(count) for state, count in state_rows}

    since = datetime.now(UTC) - timedelta(hours=1)
    processed_last_hour = int(
        (
            await session.execute(
                select(func.count(Recording.id)).where(
                    Recording.processed_at >= since, Recording.ignored.is_(False)
                )
            )
        ).scalar()
        or 0
    )

    # "Today" is the user's day, not UTC's.
    #
    # UTC midnight is 09:30 in Adelaide, so counting from it filed the whole morning's
    # driving as "not today" and pulled in the previous night's late drives instead. On
    # 2026-08-08 the tile read 28 when 50 recordings were made that day. The pipeline
    # already localises every OSD timestamp with this same setting.
    midnight = local_midnight_utc()
    processing = StatusProcessing(
        completed=counts.get("completed", 0),
        pending=counts.get("discovered", 0)
        + counts.get("metadata_extracted", 0)
        + counts.get("queued", 0),
        processing=counts.get("processing", 0),
        failed=counts.get("failed", 0),
        invalid=counts.get("invalid", 0),
        settling=counts.get("settling", 0),
        recordings_today=int(
            (
                await session.execute(
                    select(func.count(Recording.id)).where(
                        Recording.started_at >= midnight, Recording.ignored.is_(False)
                    )
                )
            ).scalar()
            or 0
        ),
        throughput_per_hour=float(processed_last_hour) if processed_last_hour else None,
    )

    settings = get_settings_service()
    safety = await evaluate_safety(session)
    storage = StatusStorage(
        limit_bytes=limit,
        used_bytes=used,
        deletion_enabled=bool(settings.get_nowait("storage.enable_deletion")),
        footage_writable=safety.writable,
    )

    latest = (
        await session.execute(
            select(Journey)
            .where(Journey.id.in_(visible_journey_ids()))
            .order_by(Journey.started_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    hardware = await detect_hardware_async()
    hardware_dict = hardware.as_dict()
    hardware_dict["inference"]["backend_name"] = describe_runtime().get("using")
    # What the two consumers of the iGPU are actually doing, as opposed to what the chip
    # can do. "Decoder: software" beside a working VAAPI device is the intended state
    # whenever inference holds the GPU, and this is what says so.
    hardware_dict["policy"] = describe_media_policy()

    return StatusOut(
        totals=totals,
        processing=processing,
        storage=storage,
        latest_journey=JourneyOut.model_validate(latest) if latest else None,
        hardware=hardware_dict,
        features=_feature_status(settings, totals),
        version=get_config().version,
    )


# --------------------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------------------


@router.get("/settings", response_model=list[SettingCategoryOut])
async def get_settings():
    return get_settings_service().describe_settings()


@router.put("/settings", response_model=list[SettingCategoryOut])
async def update_settings(body: SettingsUpdate):
    try:
        await get_settings_service().set_many(body.values)
    except SettingValidationError as exc:
        # Surface which key was rejected so the UI can mark that field rather than
        # showing a generic failure.
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return get_settings_service().describe_settings()


@router.post("/settings/reset", response_model=list[SettingCategoryOut])
async def reset_settings(body: SettingsReset):
    await get_settings_service().reset_many(body.keys)
    return get_settings_service().describe_settings()


# --------------------------------------------------------------------------------------
# Scanning
# --------------------------------------------------------------------------------------


@router.post("/scan")
async def scan_now():
    summary = await get_scheduler().scan_now()
    return {
        "scan_id": summary.scan_run_id,
        "seen": summary.seen,
        "new": summary.new,
        "changed": summary.changed,
        "unsettled": summary.unsettled,
        "invalid": summary.invalid,
        "damaged_hidden": summary.damaged_hidden,
        "damaged_deleted": summary.damaged_deleted,
        "damaged_delete_blocked": summary.damaged_delete_blocked,
        "damaged_restored": summary.damaged_restored,
        "missing": summary.missing,
        "queued": summary.queued,
        "errors": summary.errors,
        "error_message": summary.error_message,
    }


@router.post("/process")
async def process_new():
    return {"queued": await get_scheduler().process_new()}


@router.post("/reprocess")
async def reprocess_all(body: ReprocessAllRequest, session: SessionDep):
    """Requeue the whole library, or just the failures.

    Needed whenever a processing change invalidates earlier results -- a decoder fix, a
    new model, a corrected overlay region. Per-recording reprocessing does not scale to a
    library of hundreds of files, and re-running only the stages that changed is far
    cheaper than re-running everything.

    Queued at a lower priority than new footage so a bulk rerun never starves the scanner.
    """
    stmt = select(Recording).where(
        Recording.ignored.is_(False),
        Recording.file_missing.is_(False),
        # A file with no bytes in it, or with no video stream, cannot be processed by any
        # number of attempts. Including it here is what let three zero-byte segments
        # reappear in the failure list after every bulk requeue, each one with a fresh
        # four attempts against a file that will never produce a frame. Reprocessing one
        # deliberately is still possible per recording, which is where that belongs.
        Recording.state != RecordingState.INVALID,
    )
    if body.only_failed:
        stmt = stmt.where(Recording.state == RecordingState.FAILED)
    else:
        # This action is explicitly "reprocess existing footage". A newly discovered file
        # has no old analysis to invalidate; including it cancels its priority-100 process
        # job and replaces it with a priority-200 bulk job. On the live queue that left
        # hundreds of new clips (and therefore their thumbnails) waiting behind the whole
        # library reanalysis.
        stmt = stmt.where(Recording.processed_at.is_not(None))

    candidates = list((await session.execute(stmt)).scalars())
    work: list[tuple[Recording, list[str]]] = []
    if body.only_outdated:
        requested = expand_stages(body.stages)
        for recording in candidates:
            stale = [name for name in requested if name in outdated_stages(recording)]
            if stale:
                work.append((recording, stale))
    else:
        work = [(recording, body.stages) for recording in candidates]

    selected: set[str] = set()
    if not body.only_outdated:
        selected.update(
            await invalidate_recordings(
                session, [recording.id for recording, _ in work], body.stages
            )
        )
    else:
        grouped: dict[tuple[str, ...], list[int]] = {}
        for recording, stages in work:
            grouped.setdefault(tuple(stages), []).append(recording.id)
        for stages, recording_ids in grouped.items():
            selected.update(await invalidate_recordings(session, recording_ids, list(stages)))
    for recording, stages in work:
        await queue.enqueue(
            session,
            recording.id,
            kind=JobKind.REPROCESS,
            stages=stages,
            priority=200,
            force=True,
        )

    log.info(
        "queued bulk reprocess",
        recordings=len(work),
        stages=body.stages,
        only_failed=body.only_failed,
        only_outdated=body.only_outdated,
    )
    return {"queued": len(work), "stages": list(expand_stages(list(selected)))}


# --------------------------------------------------------------------------------------
# Retention
# --------------------------------------------------------------------------------------


def _plan_out(plan) -> RetentionPlanOut:
    return RetentionPlanOut(
        bytes_before=plan.bytes_before,
        bytes_limit=plan.bytes_limit,
        bytes_to_free=plan.bytes_to_free,
        would_delete_count=plan.would_delete_count,
        would_free_bytes=plan.would_free_bytes,
        over_limit=plan.over_limit,
        blocked=plan.blocked,
        blocked_reason=plan.blocked_reason,
        deletion_enabled=plan.deletion_enabled,
        candidates=[c.as_dict() for c in plan.candidates],
        skipped=plan.skipped_reasons,
        safety=SafetyReportOut(
            **{
                k: v
                for k, v in (plan.safety.as_dict() if plan.safety else {}).items()
                if k in SafetyReportOut.model_fields
            }
        )
        if plan.safety
        else None,
    )


@router.post("/retention/plan", response_model=RetentionPlanOut)
async def retention_plan(session: SessionDep):
    """Evaluate retention without touching anything."""
    return _plan_out(await plan_retention(session))


@router.post("/retention/run", response_model=RetentionPlanOut)
async def retention_run(session: SessionDep):
    """Run retention.

    Still deletes nothing unless deletion is explicitly enabled *and* the footage mount is
    writable *and* every safety guard passed — the response says which of those blocked it.
    """
    plan = await plan_retention(session)
    enabled = await get_settings_service().deletion_enabled()
    await run_retention(session, plan, dry_run=not enabled, trigger="manual")
    return _plan_out(plan)


@router.get("/retention/safety", response_model=SafetyReportOut)
async def retention_safety(session: SessionDep):
    report = await evaluate_safety(session)
    data = report.as_dict()
    return SafetyReportOut(**{k: v for k, v in data.items() if k in SafetyReportOut.model_fields})


@router.get("/retention/history")
async def retention_history(session: SessionDep, page: PaginationDep):
    total = int((await session.execute(select(func.count(RetentionRun.id)))).scalar() or 0)
    rows = (
        (
            await session.execute(
                select(RetentionRun)
                .order_by(RetentionRun.started_at.desc())
                .offset(page.offset)
                .limit(page.page_size)
            )
        )
        .scalars()
        .all()
    )
    return {
        "items": [
            {
                "id": r.id,
                "started_at": r.started_at,
                "finished_at": r.finished_at,
                "trigger": r.trigger,
                "dry_run": r.dry_run,
                "blocked": r.blocked,
                "blocked_reason": r.blocked_reason,
                "candidate_count": r.candidate_count,
                "candidate_bytes": r.candidate_bytes,
                "deleted_count": r.deleted_count,
                "deleted_bytes": r.deleted_bytes,
            }
            for r in rows
        ],
        "total": total,
        "page": page.page,
        "page_size": page.page_size,
        "pages": page.pages(total),
    }


# --------------------------------------------------------------------------------------
# Logs and diagnostics
# --------------------------------------------------------------------------------------


@router.get("/logs", response_model=Paginated[LogEntryOut])
async def list_logs(
    session: SessionDep,
    page: PaginationDep,
    level: str | None = None,
    recording_id: RowIdFilter = None,
    job_id: RowIdFilter = None,
    search: str | None = Query(None),
):
    stmt = select(LogEntry)
    count_stmt = select(func.count(LogEntry.id))
    for condition in (
        (LogEntry.level == level.upper()) if level else None,
        (LogEntry.recording_id == recording_id) if recording_id is not None else None,
        (LogEntry.job_id == job_id) if job_id is not None else None,
        LogEntry.message.ilike(f"%{search}%") if search else None,
    ):
        if condition is not None:
            stmt = stmt.where(condition)
            count_stmt = count_stmt.where(condition)

    total = int((await session.execute(count_stmt)).scalar() or 0)
    rows = (
        (
            await session.execute(
                stmt.order_by(LogEntry.ts.desc()).offset(page.offset).limit(page.page_size)
            )
        )
        .scalars()
        .all()
    )

    return Paginated[LogEntryOut](
        items=[LogEntryOut.model_validate(r) for r in rows],
        total=total,
        page=page.page,
        page_size=page.page_size,
        pages=page.pages(total),
    )


@router.get("/system/hardware")
async def system_hardware():
    hardware = await detect_hardware_async()
    data = hardware.as_dict()
    data["inference"]["backend"] = describe_runtime()
    # What actually executes the detection and OCR graphs, which is not the same thing as
    # the OpenVINO devices probed above: those describe the hardware, this describes where
    # inference is really scheduled.
    data["inference"]["onnx"] = describe_runtime()
    data["policy"] = describe_media_policy()
    data["models"] = describe_models()
    return data


@router.get("/system/info")
async def system_info(session: SessionDep):
    config = get_config()
    return {
        "version": config.version,
        "data_dir": str(config.data_dir),
        "footage_dir": str(config.footage_dir),
        "scheduler": get_scheduler().describe(),
        "workers": {
            "count": get_worker_pool().worker_count,
            "active": get_worker_pool().current_jobs(),
        },
    }


@router.get("/system/database")
async def system_database(session: SessionDep):
    config = get_config()
    size = config.db_path.stat().st_size if config.db_path.exists() else 0
    counts = {}
    for model in (Recording, Journey, TelemetryPoint, TrackedObject, Plate, LogEntry):
        counts[model.__tablename__] = int(
            (await session.execute(select(func.count()).select_from(model))).scalar() or 0
        )
    return {
        "path": str(config.db_path),
        "size_bytes": size,
        "migration_revision": current_revision(),
        "row_counts": counts,
    }


@router.get("/system/database/backup", response_model=None)
async def download_database_backup():
    if get_config().database_url:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Backups require the built-in SQLite database"
        )
    try:
        path = await asyncio.to_thread(create_backup)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    return FileResponse(path, media_type="application/vnd.sqlite3", filename=path.name)


@router.post("/system/database/restore")
async def upload_database_restore(request: Request):
    """Validate and stage a database restore for the next container restart."""
    if get_config().database_url:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Restore requires the built-in SQLite database"
        )
    length = int(request.headers.get("content-length", "0") or 0)
    if length > 512 * 1024 * 1024:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "Backup exceeds 512 MiB")
    data = await request.body()
    if not data:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Backup is empty")
    if len(data) > 512 * 1024 * 1024:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "Backup exceeds 512 MiB")
    try:
        await asyncio.to_thread(stage_restore, data)
    except (OSError, ValueError) as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from None
    return {
        "validated": True,
        "restart_required": True,
        "message": "Restore validated and staged. Restart the container to apply it.",
    }
