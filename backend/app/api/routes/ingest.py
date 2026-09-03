"""Footage ingest: status, manual trigger, cancel and history.

``GET /api/ingest/status`` is deliberately plain and cheap. It is read by the Backup page
every second and a half during a transfer *and* it is the Home Assistant REST sensor
source, so it must answer from memory without touching the database or the head unit.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response
from fastapi import status as http_status
from sqlalchemy import func, select

from app.api.deps import PaginationDep, SessionDep, SettingsDep
from app.api.schemas import IngestRadioStatusOut, UnifiCredentialRequest
from app.core.logging import get_logger
from app.db.models import IngestRadioTransition, IngestRun, UnifiCredential, utcnow
from app.ingest import unifi
from app.ingest.models import RunState
from app.ingest.puller import start_run
from app.ingest.status import get_status

log = get_logger(__name__)

router = APIRouter(prefix="/api", tags=["ingest"])


@router.get("/ingest/status", summary="Live ingest progress")
async def ingest_status() -> dict[str, object]:
    return get_status().snapshot()


def _radio_baseline(value: str) -> str:
    """Keep the public contract bounded even if an old/corrupt row has another value."""
    return value if value in {"on", "off", "transport"} else "unknown"


def _radio_state(row: IngestRadioTransition, name: str) -> dict[str, object]:
    return {
        "baseline": _radio_baseline(str(getattr(row, f"{name}_before", "unknown"))),
        "disable_attempted": bool(getattr(row, f"{name}_disable_attempted")),
        "disable_verified": bool(getattr(row, f"{name}_disable_verified")),
        "restore_attempted": bool(getattr(row, f"{name}_restore_attempted")),
        "restore_verified": bool(getattr(row, f"{name}_restore_verified")),
    }


@router.get(
    "/ingest/radio-status",
    response_model=IngestRadioStatusOut,
    summary="Latest radio transition safety state",
)
async def ingest_radio_status(
    session: SessionDep,
    settings: SettingsDep,
) -> dict[str, object]:
    """Expose radio safety evidence without leaking device or hotspot credentials.

    Live byte progress stays on the memory-only ``/ingest/status`` endpoint. Radio
    transitions are deliberately durable, so this separate, slower endpoint reads the
    current transition (when one exists) or the most recently finished transition.

    Device addresses, lease tokens, logger paths, hotspot capsule references, capabilities
    and raw error text are intentionally omitted. The latter may include command output;
    the Backup page only needs to know whether recovery is still required.
    """
    row = await session.scalar(
        select(IngestRadioTransition)
        .order_by(
            IngestRadioTransition.active.desc(),
            IngestRadioTransition.recovery_required.desc(),
            IngestRadioTransition.created_at.desc(),
        )
        .limit(1)
    )
    result: dict[str, object] = {
        "quieting_enabled": bool(settings.get_nowait("ingest.quiet_radios")),
        "transition": None,
    }
    if row is None:
        return result

    result["transition"] = {
        "phase": row.phase,
        "active": bool(row.active),
        "recovery_required": bool(row.recovery_required),
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
        "completed_at": row.completed_at.isoformat() if row.completed_at else None,
        "bluetooth": _radio_state(row, "bluetooth"),
        "hotspot": _radio_state(row, "hotspot"),
        "obd_logger": {
            "quiesce_capable": bool(row.logger_quiesce_capable),
            "quiesce_attempted": row.logger_quiesce_requested_at is not None,
            "quiesce_verified": row.logger_quiesce_acked_at is not None,
            "resume_attempted": bool(row.logger_resume_attempted),
            "resume_verified": bool(row.logger_resume_verified),
        },
    }
    return result


@router.post("/ingest/run", summary="Pull from the head unit now")
async def ingest_run() -> dict[str, object]:
    state = get_status()
    if state.running:
        raise HTTPException(http_status.HTTP_409_CONFLICT, "A transfer is already running")

    # Backgrounded, because a transfer outlives any sensible request timeout. `start_run`
    # owns the task, so it cannot be garbage collected mid-flight and shutdown can stop it.
    import asyncio

    task = start_run(trigger="manual")
    await asyncio.sleep(0)
    if task.done() and task.exception() is not None:
        raise HTTPException(
            http_status.HTTP_500_INTERNAL_SERVER_ERROR, str(task.exception())
        ) from task.exception()
    return {"started": True, "state": get_status().snapshot()["state"]}


@router.post("/ingest/show-test", summary="Open the backup page on the dashcam now")
async def ingest_show_test() -> dict[str, object]:
    """Fire the head unit's screen by hand, and say what actually happened.

    This exists because the real thing is almost impossible to observe on purpose. It only
    fires when a transfer has files to copy, and a card with nothing new on it is the
    steady state -- so "did it work?" normally means waiting for the car to arrive carrying
    footage, catching a sixty-second window, and watching the dashboard at the same time.
    Every failure below was found that way once and should not have to be again.

    Unlike the transfer path this reports failure, and reports it in the terms the operator
    can act on: no address learned yet, no unit on the network, no browser on the unit.
    """
    from app.ingest import adb, puller
    from app.ingest.origin import redacted

    url = puller.display_url()
    if not url:
        raise HTTPException(
            http_status.HTTP_409_CONFLICT,
            "This app does not know its own address yet. Open the dashboard once, or set "
            "the address in Settings → Backup / Ingest.",
        )

    address = adb.normalised_address(str(puller._get("unit_adb_address", "") or ""))
    if not address:
        raise HTTPException(
            http_status.HTTP_409_CONFLICT,
            "No head unit address is configured in Settings → Backup / Ingest.",
        )
    if not await adb.is_listening(address):
        raise HTTPException(
            http_status.HTTP_409_CONFLICT,
            f"Nothing is answering at {address}. The unit is only on the network while the "
            "engine is running.",
        )

    # Awaited, unlike the transfer path — there is no window being spent here, and the
    # whole point is to find out how it went.
    reason = await adb.show_url(address, url)
    if reason:
        raise HTTPException(http_status.HTTP_502_BAD_GATEWAY, reason)
    log.info("opened the backup page on the head unit by hand", url=redacted(url))
    # Redacted: this is echoed into the UI and straight into any screenshot of it.
    return {"shown": True, "url": redacted(url)}


@router.put("/ingest/unifi/credential", status_code=http_status.HTTP_204_NO_CONTENT)
async def set_unifi_credential(body: UnifiCredentialRequest, session: SessionDep) -> Response:
    """Save how this app signs in to the UniFi console.

    Write-only, and deliberately not a setting: everything in ``app_settings`` is echoed by
    ``GET /api/settings`` to any authenticated browser. There is no matching GET here for the
    same reason -- the value can be replaced, never read back.
    """
    api_key = (body.api_key or "").strip()
    username = (body.username or "").strip()
    password = body.password or ""
    if api_key and (username or password):
        raise HTTPException(
            http_status.HTTP_400_BAD_REQUEST,
            "Give either an API key or a username and password, not both.",
        )
    if not api_key and not (username and password):
        raise HTTPException(
            http_status.HTTP_400_BAD_REQUEST,
            "Give an API key, or a username and password.",
        )
    row = await session.get(UnifiCredential, unifi.CREDENTIAL_ID)
    if row is None:
        row = UnifiCredential(id=unifi.CREDENTIAL_ID)
        session.add(row)
    row.api_key = api_key or None
    row.username = username or None
    row.password = password or None
    row.updated_at = utcnow()
    await session.commit()
    return Response(status_code=http_status.HTTP_204_NO_CONTENT)


@router.delete("/ingest/unifi/credential", status_code=http_status.HTTP_204_NO_CONTENT)
async def clear_unifi_credential(session: SessionDep) -> Response:
    """Forget the console credential. The bounce then simply stops happening."""
    row = await session.get(UnifiCredential, unifi.CREDENTIAL_ID)
    if row is not None:
        await session.delete(row)
        await session.commit()
    return Response(status_code=http_status.HTTP_204_NO_CONTENT)


@router.post("/ingest/unifi/test", summary="Check the UniFi console answers")
async def test_unifi() -> dict[str, object]:
    """Prove the address and credential work, without touching the unit's connection."""
    ok, detail = await unifi.probe()
    return {"ok": ok, "detail": detail}


@router.post("/ingest/cancel", summary="Stop the running transfer")
async def ingest_cancel() -> dict[str, object]:
    if not get_status().cancel():
        raise HTTPException(http_status.HTTP_409_CONFLICT, "No transfer is running")
    return {"cancelled": True}


@router.get("/ingest/history", summary="Past transfers")
async def ingest_history(session: SessionDep, page: PaginationDep) -> dict[str, object]:
    total = int((await session.execute(select(func.count(IngestRun.id)))).scalar() or 0)
    rows = (
        (
            await session.execute(
                select(IngestRun)
                .order_by(IngestRun.started_at.desc())
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
                "id": row.id,
                "started_at": row.started_at.isoformat() if row.started_at else None,
                "finished_at": row.finished_at.isoformat() if row.finished_at else None,
                "trigger": row.trigger,
                "state": row.state,
                "files_transferred": row.files_transferred,
                "bytes_transferred": row.bytes_transferred,
                "throughput_mbs_avg": row.throughput_mbs_avg,
                "error": row.error,
            }
            for row in rows
        ],
        "total": total,
        "page": page.page,
        "page_size": page.page_size,
        "pages": page.pages(total),
        "states": [item.value for item in RunState],
    }
