"""Operator-authorized maintenance; never accepts client assertions of native safety."""

from typing import Annotated

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import SettingsDep
from app.ingest import adb, maintenance

router = APIRouter(prefix="/api/ingest", tags=["ingest"])


class MaintenanceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    duration_s: Annotated[int, Field(strict=True, ge=60, le=1800)] = 900


@router.get("/maintenance", summary="Verified maintenance hold")
async def maintenance_status() -> dict[str, object]:
    return maintenance.get_controller().snapshot()


@router.post("/maintenance", summary="Cancel the visible native countdown for maintenance")
async def maintenance_start(
    payload: MaintenanceRequest, settings: SettingsDep
) -> dict[str, object]:
    address = adb.normalised_address(str(settings.get_nowait("ingest.unit_adb_address") or ""))
    if not address:
        raise HTTPException(409, "No dashcam ADB address is configured.")
    try:
        return await maintenance.get_controller().start(address, payload.duration_s)
    except maintenance.MaintenanceRefused as exc:
        raise HTTPException(409, str(exc)) from exc
    except adb.AdbError as exc:
        raise HTTPException(
            502, "The dashcam did not verify maintenance; backup admission remains closed."
        ) from exc


@router.delete("/maintenance", summary="End maintenance and restore radios")
async def maintenance_stop() -> dict[str, object]:
    return await maintenance.get_controller().stop()
