"""Read-only aggregate view of advisory on-camera movement observations."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter

from app.api.deps import PaginationDep, SessionDep
from app.ingest.clip_exclusions import exclusion_page, exclusion_summary
from app.ingest.movement import get_movement_status, load_status

router = APIRouter(prefix="/api/ingest", tags=["ingest"])


@router.get("/movement-status", summary="Advisory on-camera movement report")
async def movement_status(session: SessionDep) -> dict[str, Any]:
    status = get_movement_status()
    try:
        await asyncio.wait_for(load_status(status), timeout=0.5)
    except TimeoutError:
        pass
    result = status.snapshot()
    result["intentional_exclusions"] = await exclusion_summary(session)
    return result


@router.get("/clip-exclusions", summary="Observed on-camera quiet-clip exclusion audit")
async def clip_exclusion_audit(
    session: SessionDep,
    pagination: PaginationDep,
    current_only: bool = False,
) -> dict[str, Any]:
    items, total = await exclusion_page(
        session,
        offset=pagination.offset,
        limit=pagination.page_size,
        current_only=current_only,
    )
    return {
        "items": items,
        "total": total,
        "page": pagination.page,
        "page_size": pagination.page_size,
        "pages": pagination.pages(total),
    }
