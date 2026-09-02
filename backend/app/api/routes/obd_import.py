"""OBD copy/import visibility and deliberate manual recovery controls."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import FileResponse
from sqlalchemy import func, select, update
from sqlalchemy.orm import joinedload

from app.api.deps import PaginationDep, RowId, SessionDep
from app.db.models import (
    Journey,
    OBDBundle,
    OBDBundleState,
    OBDDiagnostic,
    OBDDrive,
    OBDLoggerEvent,
    OBDSample,
    utcnow,
)
from app.ingest.ha_import_queue import (
    configuration_status,
    get_import_worker,
    move_to_quarantine,
    queue_claim_lock,
    rebuild_queue,
    redact,
    restore_from_quarantine,
)
from app.ingest.obd_bundle import (
    SAFE_DRIVE_ID,
    BundleError,
    bundle_path_for,
    file_sha256,
    store_validated_bundle,
    validate_bundle,
)
from app.ingest.obd_events import (
    EVENT_KINDS,
    EVENT_LEVELS,
    get_logger_event_status,
)
from app.ingest.obd_reconciliation import (
    SIGNALS,
    reconcile_drive_projection,
    specs_for_poll_plan,
)
from app.ingest.obd_transfer import get_obd_transfer_status

router = APIRouter(prefix="/api/obd", tags=["obd-import"])


async def _quarantine_row(
    row: OBDBundle, path, error: Exception, *, was_quarantined: bool = False
) -> None:
    if was_quarantined:
        # Revalidation of an existing quarantine must not rotate the only recoverable
        # copy to a .bad archive and then try to move the now-missing source again.
        row.state = OBDBundleState.QUARANTINED.value
        row.failure_kind = "integrity"
        row.last_error = redact(error)
    else:
        try:
            await asyncio.to_thread(move_to_quarantine, path)
        except (BundleError, OSError) as move_error:
            row.state = OBDBundleState.FAILED.value
            row.failure_kind = "quarantine_io"
            row.last_error = redact(move_error)
        else:
            row.state = OBDBundleState.QUARANTINED.value
            row.failure_kind = "integrity"
            row.last_error = redact(error)
    row.next_attempt_at = None
    row.updated_at = utcnow()


def _bundle(row: OBDBundle) -> dict[str, object]:
    return {
        "id": row.id,
        "drive_id": row.drive_id,
        "schema_version": row.schema_version,
        "bundle_sha256": row.bundle_hash,
        "filename": row.filename,
        "size_bytes": row.size_bytes,
        "vehicle_id": row.vehicle_id,
        "drive_started_at": row.drive_started_at.isoformat(),
        "drive_finished_at": row.drive_finished_at.isoformat(),
        "sample_count": row.sample_count,
        "diagnostic_count": row.diagnostic_count,
        "metadata_trusted": row.metadata_trusted,
        "state": row.state,
        "attempts": row.attempts,
        "next_attempt_at": row.next_attempt_at.isoformat() if row.next_attempt_at else None,
        "last_error": row.last_error,
        "failure_kind": row.failure_kind,
        "last_http_status": row.last_http_status,
        "verified_at": row.verified_at.isoformat() if row.verified_at else None,
        "imported_at": row.imported_at.isoformat() if row.imported_at else None,
        "duplicate": row.duplicate,
        "warnings": row.validation_warnings or [],
    }


def _logger_event(row: OBDLoggerEvent) -> dict[str, object]:
    """Public app evidence; hashed producer/session identities stay server-side."""
    return {
        "sequence": row.sequence,
        "occurred_at": row.occurred_at.isoformat(),
        "received_at": row.received_at.isoformat(),
        "kind": row.kind,
        "level": row.level,
        "outcome": row.outcome,
        "reason_code": row.reason_code,
        "drive_id": row.drive_id,
        "metrics": row.metrics_json or {},
        "app_version_name": row.app_version_name,
        "app_version_code": row.app_version_code,
        "build_git_sha": row.build_git_sha,
    }


def _drive(row: OBDDrive, *, include_quality: bool = False) -> dict[str, object]:
    bundle = row.bundle
    result: dict[str, object] = {
        "drive_id": row.drive_id,
        "vehicle_id": row.vehicle_id,
        "started_at": row.started_at.isoformat(),
        "finished_at": row.finished_at.isoformat(),
        "original_timezone": row.original_timezone,
        "start_reason": row.start_reason,
        "stop_reason": row.stop_reason,
        "obd_protocol": row.obd_protocol,
        # Keep the existing field truthful for API clients and expose the literal legacy
        # producer value separately. manifest_json itself is never rewritten.
        "completion_status": row.lifecycle_status,
        "producer_completion_status": row.completion_status,
        "lifecycle_status": row.lifecycle_status,
        "clean_end": row.clean_end,
        "interruption_reason": row.interruption_reason,
        "first_sample_at": row.first_sample_at.isoformat() if row.first_sample_at else None,
        "last_sample_at": row.last_sample_at.isoformat() if row.last_sample_at else None,
        "last_successful_response_at": (
            row.last_successful_response_at.isoformat() if row.last_successful_response_at else None
        ),
        "finalization_observed_at": (
            row.finalization_observed_at.isoformat() if row.finalization_observed_at else None
        ),
        "connection_loss_count": row.connection_loss_count,
        "gap_count": row.gap_count,
        "longest_gap_s": row.longest_gap_s,
        "data_completeness_percentage": row.data_completeness_percentage,
        "processing_status": row.processing_status,
        "last_processing_error": row.last_processing_error,
        "summary_source": row.summary_source,
        "summary_generated_at": (
            row.summary_generated_at.isoformat() if row.summary_generated_at else None
        ),
        "duration_s": row.duration_s,
        "distance_km": row.distance_km,
        "average_speed_kmh": row.average_speed_kmh,
        "maximum_speed_kmh": row.maximum_speed_kmh,
        "average_rpm": row.average_rpm,
        "maximum_rpm": row.maximum_rpm,
        "idle_duration_s": row.idle_duration_s,
        "estimated_fuel_used_l": row.estimated_fuel_used_l,
        "average_fuel_consumption_l_100km": row.average_fuel_consumption_l_100km,
        "maximum_coolant_temperature_c": row.maximum_coolant_temperature_c,
        "maximum_engine_load_pct": row.maximum_engine_load_pct,
        "missing_data_duration_s": row.missing_data_duration_s,
        "expected_sample_count": row.expected_sample_count,
        "received_sample_percentage": row.received_sample_percentage,
        "sample_count": row.sample_count,
        "error_count": row.error_count,
        "dtcs_observed": row.dtcs_observed or [],
        # The queue row's state says how far along the HA hand-off is; the drive row
        # itself only exists once validation and registration have already succeeded.
        "bundle_id": bundle.id,
        "bundle_filename": bundle.filename,
        "bundle_sha256": bundle.bundle_hash,
        "bundle_available": bool(bundle.metadata_trusted and bundle.verified_at),
        "bundle_download_url": (
            f"/api/obd/drives/{row.drive_id}/bundle"
            if bundle.metadata_trusted and bundle.verified_at
            else None
        ),
        "export_status": "available" if bundle.metadata_trusted else "incomplete",
        "backup_status": "verified" if bundle.verified_at else "pending",
        "copied_at": bundle.copied_at.isoformat() if bundle.copied_at else None,
        "verified_at": bundle.verified_at.isoformat() if bundle.verified_at else None,
        "imported_at": bundle.imported_at.isoformat() if bundle.imported_at else None,
        "import_state": bundle.state,
        "bundle_error": bundle.last_error,
        "validation_warnings": bundle.validation_warnings or [],
    }
    if include_quality:
        result["gap_analysis"] = row.gap_analysis_json
    return result


def _series_sample(row: OBDSample, specs=SIGNALS) -> dict[str, object]:
    result: dict[str, object] = {
        "sample_id": row.sample_id,
        "t": row.captured_at.isoformat(),
        "sequence": row.sequence,
        "ecu_data_status": row.ecu_data_status,
        "quality": row.quality_json,
        "engine_rpm": row.engine_rpm,
        "vehicle_speed_kmh": row.vehicle_speed_kmh,
        "coolant_temperature_c": row.coolant_temperature_c,
        "intake_air_temperature_c": row.intake_air_temperature_c,
        "engine_load_pct": row.engine_load_pct,
        "throttle_position_pct": row.throttle_position_pct,
        "timing_advance_deg": row.timing_advance_deg,
        "mass_air_flow_g_s": row.mass_air_flow_g_s,
        "short_term_fuel_trim_pct": row.short_term_fuel_trim_bank_1_pct,
        "long_term_fuel_trim_pct": row.long_term_fuel_trim_bank_1_pct,
        "oxygen_sensor_1_voltage_v": row.oxygen_sensor_1_voltage_v,
        "oxygen_sensor_2_voltage_v": row.oxygen_sensor_2_voltage_v,
        "adapter_voltage_v": row.adapter_voltage_v,
        "estimated_fuel_rate_l_h": row.estimated_fuel_rate_l_h,
        "estimated_fuel_consumption_l_100km": row.estimated_fuel_consumption_l_100km,
        "mil_on": row.mil_on,
        "dtc_count": row.dtc_count,
    }
    result["provenance"] = {
        spec.name: spec.provenance for spec in specs if getattr(row, spec.attribute) is not None
    }
    return result


@router.get("/drives", summary="List imported drives with their rollups")
async def list_drives(session: SessionDep, page: PaginationDep) -> dict[str, object]:
    total = int((await session.execute(select(func.count(OBDDrive.id)))).scalar() or 0)
    rows = (
        (
            await session.execute(
                select(OBDDrive)
                .options(joinedload(OBDDrive.bundle))
                .order_by(OBDDrive.started_at.desc(), OBDDrive.id.desc())
                .offset(page.offset)
                .limit(page.page_size)
            )
        )
        .scalars()
        .all()
    )
    return {
        "items": [_drive(row) for row in rows],
        "total": total,
        "page": page.page,
        "page_size": page.page_size,
        "pages": page.pages(total),
    }


@router.get("/drives/summary", summary="Aggregate rollups across every imported drive")
async def drives_summary(session: SessionDep) -> dict[str, object]:
    (
        count,
        distance,
        duration,
        idle,
        fuel,
        max_speed,
        max_rpm,
        max_coolant,
        samples,
    ) = (
        await session.execute(
            select(
                func.count(OBDDrive.id),
                func.sum(OBDDrive.distance_km),
                func.sum(OBDDrive.duration_s),
                func.sum(OBDDrive.idle_duration_s),
                func.sum(OBDDrive.estimated_fuel_used_l),
                func.max(OBDDrive.maximum_speed_kmh),
                func.max(OBDDrive.maximum_rpm),
                func.max(OBDDrive.maximum_coolant_temperature_c),
                func.sum(OBDDrive.sample_count),
            )
        )
    ).one()
    # Ordered single-row selects rather than min()/max() aggregates: SQLite hands an
    # aggregate over a typed datetime column back through the driver as its raw stored
    # string, sidestepping the UtcDateTime decoder.
    first = (
        await session.execute(select(OBDDrive.started_at).order_by(OBDDrive.started_at.asc()))
    ).scalar()
    last = (
        await session.execute(select(OBDDrive.finished_at).order_by(OBDDrive.finished_at.desc()))
    ).scalar()
    total_distance = float(distance or 0.0)
    total_fuel = float(fuel or 0.0)
    return {
        "drive_count": int(count or 0),
        "total_distance_km": total_distance,
        "total_duration_s": float(duration or 0.0),
        "total_idle_duration_s": float(idle or 0.0),
        "total_fuel_used_l": total_fuel,
        "average_fuel_consumption_l_100km": (
            total_fuel / total_distance * 100.0 if total_distance > 0 else None
        ),
        "maximum_speed_kmh": float(max_speed) if max_speed is not None else None,
        "maximum_rpm": float(max_rpm) if max_rpm is not None else None,
        "maximum_coolant_temperature_c": (float(max_coolant) if max_coolant is not None else None),
        "total_sample_count": int(samples or 0),
        "first_drive_at": first.isoformat() if first else None,
        "last_drive_at": last.isoformat() if last else None,
    }


@router.get(
    "/drives/for-journey/{journey_id}",
    summary="The OBD drive that overlaps one footage journey, if any",
)
async def drive_for_journey(journey_id: RowId, session: SessionDep) -> dict[str, object]:
    journey = await session.get(Journey, journey_id)
    if journey is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Journey was not found")
    # Candidates by span overlap, best match by overlap length. Both sides are UTC, so a
    # dashcam journey and an OBD drive of the same trip overlap for essentially its whole
    # duration; anything touching the window at all is still offered, because the logger
    # starts on voltage and the camera on power — their edges rarely agree to the second.
    candidates = (
        (
            await session.execute(
                select(OBDDrive)
                .options(joinedload(OBDDrive.bundle))
                .where(
                    OBDDrive.started_at < journey.ended_at,
                    OBDDrive.finished_at > journey.started_at,
                )
            )
        )
        .scalars()
        .all()
    )
    best = None
    best_overlap = 0.0
    for row in candidates:
        overlap = (
            min(row.finished_at, journey.ended_at) - max(row.started_at, journey.started_at)
        ).total_seconds()
        if overlap > best_overlap:
            best, best_overlap = row, overlap
    return {
        "drive": _drive(best) if best else None,
        "overlap_s": best_overlap if best else None,
    }


async def _journey_for_drive(session, drive: OBDDrive) -> dict[str, object] | None:
    candidates = (
        (
            await session.execute(
                select(Journey).where(
                    Journey.started_at < drive.finished_at,
                    Journey.ended_at > drive.started_at,
                )
            )
        )
        .scalars()
        .all()
    )
    best = None
    best_overlap = 0.0
    for row in candidates:
        overlap = (
            min(row.ended_at, drive.finished_at) - max(row.started_at, drive.started_at)
        ).total_seconds()
        if overlap > best_overlap:
            best, best_overlap = row, overlap
    if best is None:
        return None
    return {"id": best.id, "title": best.title, "overlap_s": best_overlap}


@router.get(
    "/drives/{drive_id}/series",
    summary="Full-resolution samples and diagnostic events for one drive",
)
async def drive_series(drive_id: str, session: SessionDep) -> dict[str, object]:
    drive = (
        (
            await session.execute(
                select(OBDDrive)
                .options(joinedload(OBDDrive.bundle))
                .where(OBDDrive.drive_id == drive_id)
            )
        )
        .scalars()
        .first()
    )
    if drive is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "OBD drive was not found")
    raw_poll_plan = (
        drive.manifest_json.get("poll_plan_version")
        if isinstance(drive.manifest_json, dict)
        else None
    )
    _poll_plan_version, specs = specs_for_poll_plan(raw_poll_plan)
    samples = (
        (
            await session.execute(
                select(OBDSample)
                .where(OBDSample.drive_db_id == drive.id)
                .order_by(OBDSample.sequence.asc())
            )
        )
        .scalars()
        .all()
    )
    diagnostics = (
        (
            await session.execute(
                select(OBDDiagnostic)
                .where(OBDDiagnostic.drive_db_id == drive.id)
                .order_by(OBDDiagnostic.observed_at.asc(), OBDDiagnostic.id.asc())
            )
        )
        .scalars()
        .all()
    )
    return {
        "drive": _drive(drive, include_quality=True),
        "journey": await _journey_for_drive(session, drive),
        "units": drive.units,
        "signal_metadata": [
            {
                "name": spec.name,
                "label": spec.label,
                "pid": f"{spec.pid:02X}" if spec.pid is not None else None,
                "tier": spec.tier,
                "expected_cadence_s": spec.cadence_s,
                "provenance": spec.provenance,
                "discrete": spec.discrete,
            }
            for spec in specs
        ],
        "samples": [_series_sample(row, specs) for row in samples],
        "diagnostics": [
            {
                "observed_at": row.observed_at.isoformat() if row.observed_at else None,
                "kind": row.kind,
                "payload": row.payload_json,
            }
            for row in diagnostics
        ],
    }


@router.post(
    "/drives/{drive_id}/reprocess",
    summary="Idempotently rebuild one drive's lifecycle and completeness projection",
)
async def reprocess_drive(drive_id: str, session: SessionDep) -> dict[str, object]:
    drive = (
        (
            await session.execute(
                select(OBDDrive)
                .options(joinedload(OBDDrive.bundle))
                .where(OBDDrive.drive_id == drive_id)
            )
        )
        .scalars()
        .first()
    )
    if drive is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "OBD drive was not found")
    result = await reconcile_drive_projection(session, drive)
    ha_refresh_queued = False
    if result.get("status") == "ready" and drive.bundle.state == OBDBundleState.IMPORTED.value:
        drive.bundle.state = OBDBundleState.READY_TO_IMPORT.value
        drive.bundle.next_attempt_at = utcnow()
        drive.bundle.import_started_at = None
        drive.bundle.last_error = None
        drive.bundle.failure_kind = None
        drive.bundle.updated_at = utcnow()
        ha_refresh_queued = True
        get_import_worker().wake()
    return {
        "result": result,
        "drive": _drive(drive, include_quality=True),
        "ha_refresh_queued": ha_refresh_queued,
    }


@router.get(
    "/drives/{drive_id}/bundle",
    summary="Download the immutable verified OBD bundle",
    response_class=FileResponse,
)
async def download_drive_bundle(drive_id: str, session: SessionDep) -> FileResponse:
    drive = (
        (
            await session.execute(
                select(OBDDrive)
                .options(joinedload(OBDDrive.bundle))
                .where(OBDDrive.drive_id == drive_id)
            )
        )
        .scalars()
        .first()
    )
    if drive is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "OBD drive was not found")
    row = drive.bundle
    if not row.metadata_trusted or row.verified_at is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Bundle has no verified server copy")
    try:
        path = bundle_path_for(row)
        digest, size = await asyncio.to_thread(file_sha256, path)
    except (BundleError, OSError) as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The verified bundle is not currently readable",
        ) from exc
    if digest != row.bundle_hash or size != row.size_bytes:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The server copy no longer matches its verified identity",
        )
    return FileResponse(
        path,
        media_type="application/zip",
        filename=row.filename,
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/status", summary="OBD logger, backup and Home Assistant queue status")
async def obd_status(session: SessionDep) -> dict[str, object]:
    grouped = (
        await session.execute(select(OBDBundle.state, func.count()).group_by(OBDBundle.state))
    ).all()
    counts = {state_name: int(count) for state_name, count in grouped}
    latest = (
        (
            await session.execute(
                select(OBDBundle)
                .where(OBDBundle.verified_at.is_not(None))
                .order_by(OBDBundle.drive_finished_at.desc(), OBDBundle.id.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    importing = (
        (
            await session.execute(
                select(OBDBundle)
                .where(OBDBundle.state == OBDBundleState.IMPORTING.value)
                .order_by(OBDBundle.drive_started_at.asc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    last_imported = (
        (
            await session.execute(
                select(OBDBundle)
                .where(OBDBundle.state == OBDBundleState.IMPORTED.value)
                .order_by(OBDBundle.imported_at.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    last_error = (
        (
            await session.execute(
                select(OBDBundle)
                .where(OBDBundle.last_error.is_not(None))
                .order_by(OBDBundle.updated_at.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    since = datetime.now(UTC) - timedelta(hours=1)
    imported_last_hour = int(
        (
            await session.execute(
                select(func.count(OBDBundle.id)).where(OBDBundle.imported_at >= since)
            )
        ).scalar()
        or 0
    )
    auth, auth_error = await asyncio.to_thread(configuration_status)
    waiting = sum(
        counts.get(item.value, 0)
        for item in (
            OBDBundleState.READY_TO_IMPORT,
            OBDBundleState.RETRY_WAIT,
            OBDBundleState.IMPORTING,
        )
    )
    transfer = get_obd_transfer_status().snapshot()
    return {
        **transfer,
        "event_stream": get_logger_event_status().snapshot(),
        "home_assistant_authentication": auth,
        "home_assistant_configuration_error": auth_error,
        "counts": counts,
        "waiting_for_home_assistant": waiting,
        "current_import": importing.filename if importing else None,
        "last_completed_drive": _bundle(latest) if latest else None,
        "imported_drive_count": counts.get(OBDBundleState.IMPORTED.value, 0),
        "duplicate_count": int(
            (
                await session.execute(
                    select(func.count(OBDBundle.id)).where(OBDBundle.duplicate.is_(True))
                )
            ).scalar()
            or 0
        ),
        "failed_count": counts.get(OBDBundleState.FAILED.value, 0)
        + counts.get(OBDBundleState.QUARANTINED.value, 0),
        "last_successful_home_assistant_sync": (
            last_imported.imported_at.isoformat()
            if last_imported and last_imported.imported_at
            else None
        ),
        "last_import_error": last_error.last_error if last_error else None,
        "imports_last_hour": imported_last_hour,
        "worker_running": get_import_worker().running,
    }


@router.get("/events", summary="List privacy-safe lifecycle events mirrored from the OBD app")
async def list_logger_events(
    session: SessionDep,
    page: PaginationDep,
    drive_id: str | None = Query(None, min_length=1, max_length=64),
    kind: str | None = Query(None, min_length=1, max_length=32),
    level: str | None = Query(None, min_length=1, max_length=8),
    since: datetime | None = Query(None),
) -> dict[str, object]:
    if drive_id is not None and not SAFE_DRIVE_ID.fullmatch(drive_id):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Invalid OBD drive id")
    if kind is not None and kind not in EVENT_KINDS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Unknown app event kind")
    if level is not None and level not in EVENT_LEVELS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Unknown app event level")
    if since is not None:
        if since.tzinfo is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "Event time filter must include a timezone",
            )
        try:
            since = since.astimezone(UTC)
        except (OverflowError, ValueError) as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "Event time filter is outside the supported range",
            ) from exc
        if since < datetime(2020, 1, 1, tzinfo=UTC) or since > utcnow() + timedelta(days=1):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "Event time filter is outside the supported range",
            )

    filters = [
        OBDLoggerEvent.drive_id == drive_id if drive_id is not None else None,
        OBDLoggerEvent.kind == kind if kind is not None else None,
        OBDLoggerEvent.level == level if level is not None else None,
        OBDLoggerEvent.occurred_at >= since if since is not None else None,
    ]
    predicates = [item for item in filters if item is not None]
    query = select(OBDLoggerEvent)
    count_query = select(func.count(OBDLoggerEvent.id))
    if predicates:
        query = query.where(*predicates)
        count_query = count_query.where(*predicates)
    total = int((await session.execute(count_query)).scalar() or 0)
    rows = (
        (
            await session.execute(
                query.order_by(OBDLoggerEvent.occurred_at.desc(), OBDLoggerEvent.id.desc())
                .offset(page.offset)
                .limit(page.page_size)
            )
        )
        .scalars()
        .all()
    )
    return {
        "items": [_logger_event(row) for row in rows],
        "total": total,
        "page": page.page,
        "page_size": page.page_size,
        "pages": page.pages(total),
    }


@router.get("/bundles", summary="List durable OBD import queue rows")
async def list_bundles(
    session: SessionDep,
    page: PaginationDep,
    state_filter: str | None = Query(None, alias="state"),
) -> dict[str, object]:
    query = select(OBDBundle)
    count_query = select(func.count(OBDBundle.id))
    if state_filter is not None:
        allowed = {item.value for item in OBDBundleState}
        if state_filter not in allowed:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Unknown OBD queue state")
        query = query.where(OBDBundle.state == state_filter)
        count_query = count_query.where(OBDBundle.state == state_filter)
    total = int((await session.execute(count_query)).scalar() or 0)
    rows = (
        (
            await session.execute(
                query.order_by(OBDBundle.drive_started_at.desc(), OBDBundle.id.desc())
                .offset(page.offset)
                .limit(page.page_size)
            )
        )
        .scalars()
        .all()
    )
    return {
        "items": [_bundle(row) for row in rows],
        "total": total,
        "page": page.page,
        "page_size": page.page_size,
        "pages": page.pages(total),
    }


@router.post("/bundles/{bundle_id}/validate", summary="Revalidate one server copy")
async def validate_one(bundle_id: RowId, session: SessionDep) -> dict[str, object]:
    row = await session.get(OBDBundle, bundle_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "OBD bundle was not found")
    if row.state in {
        OBDBundleState.WAITING_FOR_BACKUP.value,
        OBDBundleState.COPYING.value,
        OBDBundleState.VALIDATING.value,
        OBDBundleState.IMPORTING.value,
    }:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The bundle is currently being copied, validated, or imported",
        )
    original_state = row.state
    original_next_attempt_at = row.next_attempt_at
    try:
        path = bundle_path_for(row)
    except (BundleError, OSError) as exc:
        path_error: Exception | None = exc
        path = None
    else:
        path_error = None
    # Claim with a committed compare-and-swap before touching the filesystem. A queue
    # worker racing this endpoint can now either claim READY first or observe VALIDATING,
    # never move/post the same bytes concurrently.
    async with queue_claim_lock():
        claimed = await session.execute(
            update(OBDBundle)
            .where(OBDBundle.id == bundle_id, OBDBundle.state == original_state)
            .values(state=OBDBundleState.VALIDATING.value, updated_at=utcnow())
        )
        if not claimed.rowcount:
            await session.rollback()
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "The bundle changed state before validation could claim it",
            )
        await session.commit()
    row = await session.get(OBDBundle, bundle_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "OBD bundle was not found")
    if path_error is not None or path is None:
        row.state = OBDBundleState.FAILED.value
        row.failure_kind = "local_path"
        row.last_error = redact(path_error)
        row.next_attempt_at = None
        row.updated_at = utcnow()
        return {"valid": False, "bundle": _bundle(row)}
    was_quarantined = original_state == OBDBundleState.QUARANTINED.value
    try:
        checked = await asyncio.to_thread(validate_bundle, path)
    except (BundleError, OSError) as exc:
        await _quarantine_row(row, path, exc, was_quarantined=was_quarantined)
        return {"valid": False, "bundle": _bundle(row)}
    if row.metadata_trusted and checked.bundle_sha256 != row.bundle_hash:
        await _quarantine_row(
            row,
            path,
            BundleError("Verified bundle SHA-256 changed on disk"),
            was_quarantined=was_quarantined,
        )
        return {"valid": False, "bundle": _bundle(row)}
    was_untrusted = not row.metadata_trusted
    if was_untrusted:
        try:
            # Store from the still-recoverable path first. Keep the durable queue claim
            # in VALIDATING while committing the trusted identity and raw history: the
            # filesystem promotion below cannot share this database transaction, so a
            # crash must leave recovery with either quarantine+trusted or verified+trusted.
            row = await store_validated_bundle(session, checked)
            row.state = OBDBundleState.VALIDATING.value
            row.next_attempt_at = None
            row.updated_at = utcnow()
            await session.commit()
        except BundleError as exc:
            row.state = (
                OBDBundleState.QUARANTINED.value if was_quarantined else OBDBundleState.FAILED.value
            )
            row.failure_kind = "registration"
            row.last_error = redact(exc)
            return {"valid": False, "bundle": _bundle(row)}
    else:
        row.verified_at = utcnow()
        row.validation_warnings = list(checked.warnings) or None
    if was_quarantined:
        try:
            await asyncio.to_thread(restore_from_quarantine, path)
        except (BundleError, OSError) as exc:
            row.state = OBDBundleState.QUARANTINED.value
            row.next_attempt_at = None
            row.failure_kind = "quarantine_io"
            row.last_error = redact(exc)
            return {"valid": False, "bundle": _bundle(row)}
        row.state = OBDBundleState.READY_TO_IMPORT.value
        row.next_attempt_at = utcnow()
        row.failure_kind = None
        row.last_error = None
    elif was_untrusted:
        # The trusted history was committed above while the row remained claimed. A
        # non-quarantined rejected copy is already in the verified directory, so only
        # the final durable queue transition remains.
        row.state = OBDBundleState.READY_TO_IMPORT.value
        row.next_attempt_at = utcnow()
        row.failure_kind = None
        row.last_error = None
    elif row.state == OBDBundleState.VALIDATING.value:
        row.state = original_state
        row.next_attempt_at = original_next_attempt_at
        row.updated_at = utcnow()
    drive = (
        await session.execute(select(OBDDrive).where(OBDDrive.bundle_id == row.id))
    ).scalar_one_or_none()
    if drive is not None:
        await reconcile_drive_projection(
            session,
            drive,
            summary_source=checked.summary_source,
        )
    # Publish the final state before waking the worker. Besides avoiding a missed wake,
    # this is the second half of the filesystem/database promotion protocol: if this
    # commit fails after a quarantine move, startup recovery sees verified+trusted while
    # the last durable state is VALIDATING and safely requeues it.
    await session.commit()
    get_import_worker().wake()
    return {"valid": True, "bundle": _bundle(row)}


@router.post("/bundles/{bundle_id}/retry", summary="Retry one failed HA import")
async def retry_one(bundle_id: RowId, session: SessionDep) -> dict[str, object]:
    row = await session.get(OBDBundle, bundle_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "OBD bundle was not found")
    if row.state == OBDBundleState.IMPORTED.value:
        return {"queued": False, "already_imported": True, "bundle": _bundle(row)}
    if row.state in {
        OBDBundleState.WAITING_FOR_BACKUP.value,
        OBDBundleState.COPYING.value,
        OBDBundleState.VALIDATING.value,
        OBDBundleState.IMPORTING.value,
    }:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The bundle is currently being copied, validated, or imported",
        )
    if row.state == OBDBundleState.QUARANTINED.value:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Validate the quarantined copy successfully before retrying it",
        )
    if row.verified_at is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Bundle has no verified server copy")
    original_state = row.state
    now = utcnow()
    async with queue_claim_lock():
        claimed = await session.execute(
            update(OBDBundle)
            .where(OBDBundle.id == bundle_id, OBDBundle.state == original_state)
            .values(
                state=OBDBundleState.READY_TO_IMPORT.value,
                next_attempt_at=now,
                import_started_at=None,
                last_error=None,
                failure_kind=None,
                last_http_status=None,
                updated_at=now,
            )
        )
        if not claimed.rowcount:
            await session.rollback()
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "The bundle changed state before retry could claim it",
            )
        await session.commit()
    row = await session.get(OBDBundle, bundle_id, populate_existing=True)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "OBD bundle was not found")
    get_import_worker().wake()
    return {"queued": True, "bundle": _bundle(row)}


@router.post("/queue/rebuild", summary="Rebuild missing OBD queue rows safely")
async def rebuild() -> dict[str, int]:
    result = await rebuild_queue()
    get_import_worker().wake()
    return result
