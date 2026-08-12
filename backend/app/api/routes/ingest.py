"""Footage ingest: status, manual trigger, cancel and history.

``GET /api/ingest/status`` is deliberately plain and cheap. It is read by the Backup page
every second and a half during a transfer *and* it is the Home Assistant REST sensor
source, so it must answer from memory without touching the database or the head unit.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi import status as http_status
from sqlalchemy import func, select

from app.api.deps import PaginationDep, SessionDep
from app.core.logging import get_logger
from app.db.models import IngestRun
from app.ingest.models import RunState
from app.ingest.puller import start_run
from app.ingest.status import get_status

log = get_logger(__name__)

router = APIRouter(prefix="/api", tags=["ingest"])


@router.get("/ingest/status", summary="Live ingest progress")
async def ingest_status() -> dict[str, object]:
    return get_status().snapshot()


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
