"""OBD copy/import visibility and deliberate manual recovery controls."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select, update

from app.api.deps import PaginationDep, RowId, SessionDep
from app.db.models import OBDBundle, OBDBundleState, utcnow
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
    BundleError,
    bundle_path_for,
    store_validated_bundle,
    validate_bundle,
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
            # Store from the still-recoverable quarantine path first. A drive/hash
            # conflict therefore leaves the bytes and placeholder coherent, while a
            # successful promotion transactionally writes all raw history before READY.
            row = await store_validated_bundle(session, checked)
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
    elif row.state == OBDBundleState.VALIDATING.value:
        row.state = original_state
        row.next_attempt_at = original_next_attempt_at
        row.updated_at = utcnow()
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
