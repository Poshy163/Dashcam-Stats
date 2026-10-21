"""Read-only aggregate view of advisory on-camera movement observations."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter

from app.ingest.movement import get_movement_status, load_status

router = APIRouter(prefix="/api/ingest", tags=["ingest"])


@router.get("/movement-status", summary="Advisory on-camera movement report")
async def movement_status() -> dict[str, Any]:
    status = get_movement_status()
    try:
        await asyncio.wait_for(load_status(status), timeout=0.5)
    except TimeoutError:
        pass
    return status.snapshot()
