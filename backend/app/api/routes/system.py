"""Status, settings, scanning, retention, logs and hardware diagnostics."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select

from app.ai.backend import get_backend
from app.ai.models import describe_models
from app.ai.runtime import describe_runtime
from app.api.deps import PaginationDep, SessionDep
from app.api.schemas import (
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
from app.config import get_config
from app.core.logging import get_logger
from app.core.settings_service import SettingValidationError, get_settings_service
from app.db.models import (
    JobKind,
    Journey,
    LogEntry,
    Plate,
    Recording,
    RecordingState,
    RetentionRun,
    TelemetryPoint,
    TrackedObject,
)
from app.db.session import current_revision
from app.hardware.detect import detect_hardware_async
from app.retention import current_usage, evaluate_safety
from app.retention import execute as run_retention
from app.retention import plan as plan_retention
from app.workers import queue
from app.workers.scheduler import get_scheduler
from app.workers.worker import get_worker_pool

log = get_logger(__name__)

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/status", response_model=StatusOut)
async def get_status(session: SessionDep):
    totals = StatusTotals(
        recordings=int(
            (
                await session.execute(
                    select(func.count(Recording.id)).where(Recording.file_missing.is_(False))
                )
            ).scalar()
            or 0
        ),
        journeys=int((await session.execute(select(func.count(Journey.id)))).scalar() or 0),
        telemetry_points=int(
            (await session.execute(select(func.count(TelemetryPoint.id)))).scalar() or 0
        ),
        tracked_objects=int(
            (await session.execute(select(func.count(TrackedObject.id)))).scalar() or 0
        ),
        plates=int((await session.execute(select(func.count(Plate.id)))).scalar() or 0),
        duration_s=float(
            (
                await session.execute(select(func.coalesce(func.sum(Recording.duration_s), 0.0)))
            ).scalar()
            or 0.0
        ),
    )

    used, files, limit = await current_usage(session)
    totals.footage_bytes = used
    totals.footage_files = files

    state_rows = (
        await session.execute(
            select(Recording.state, func.count(Recording.id)).group_by(Recording.state)
        )
    ).all()
    counts = {state.value: int(count) for state, count in state_rows}

    since = datetime.now(UTC) - timedelta(hours=1)
    processed_last_hour = int(
        (
            await session.execute(
                select(func.count(Recording.id)).where(Recording.processed_at >= since)
            )
        ).scalar()
        or 0
    )

    midnight = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    processing = StatusProcessing(
        completed=counts.get("completed", 0),
        pending=counts.get("discovered", 0)
        + counts.get("metadata_extracted", 0)
        + counts.get("queued", 0),
        processing=counts.get("processing", 0),
        failed=counts.get("failed", 0),
        recordings_today=int(
            (
                await session.execute(
                    select(func.count(Recording.id)).where(Recording.started_at >= midnight)
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
        await session.execute(select(Journey).order_by(Journey.started_at.desc()).limit(1))
    ).scalar_one_or_none()

    hardware = await detect_hardware_async()
    hardware_dict = hardware.as_dict()
    hardware_dict["inference"]["backend_name"] = get_backend().backend_name

    return StatusOut(
        totals=totals,
        processing=processing,
        storage=storage,
        latest_journey=JourneyOut.model_validate(latest) if latest else None,
        hardware=hardware_dict,
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
    stmt = select(Recording.id).where(
        Recording.ignored.is_(False),
        Recording.file_missing.is_(False),
    )
    if body.only_failed:
        stmt = stmt.where(Recording.state == RecordingState.FAILED)

    recording_ids = list((await session.execute(stmt)).scalars())
    for recording_id in recording_ids:
        await queue.enqueue(
            session,
            recording_id,
            kind=JobKind.REPROCESS,
            stages=body.stages,
            priority=200,
            force=True,
        )

    log.info(
        "queued bulk reprocess",
        recordings=len(recording_ids),
        stages=body.stages,
        only_failed=body.only_failed,
    )
    return {"queued": len(recording_ids), "stages": body.stages}


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
    recording_id: int | None = None,
    job_id: int | None = None,
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
    data["inference"]["backend"] = get_backend().describe()
    # What actually executes the detection and OCR graphs, which is not the same thing as
    # the OpenVINO devices probed above: those describe the hardware, this describes where
    # inference is really scheduled.
    data["inference"]["onnx"] = describe_runtime()
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
