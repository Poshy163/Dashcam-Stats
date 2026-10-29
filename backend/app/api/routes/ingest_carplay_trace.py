"""Explicit, expiring codec diagnostics; passive observations stay independent."""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import SettingsDep
from app.core.settings_service import SettingsService
from app.ingest import adb, carplay_timing
from app.ingest.status import get_status

router = APIRouter(prefix="/api/ingest", tags=["ingest"])


class CodecTraceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    duration_s: Annotated[int, Field(strict=True, ge=60, le=1800)] = 900


def _snapshot(settings: SettingsService) -> dict[str, object]:
    until = carplay_timing.codec_trace_until_unix_s()
    now = int(datetime.now(UTC).timestamp())
    passive = bool(settings.get_nowait(carplay_timing.ENABLED_KEY)) and bool(
        settings.get_nowait("ingest.unit_logs")
    )
    # A saved request remains stoppable even if passive logging is switched off.
    # Device eligibility is separate from the operator's unexpired intent.
    requested = until > now
    return {
        "requested": requested,
        "expires_at": datetime.fromtimestamp(until, UTC).isoformat() if requested else None,
        "remaining_s": max(0, until - now) if requested else 0,
        "passive_enabled": passive,
        "unit_online": get_status().unit_online,
    }


def _reconcile(settings: SettingsService, *, force_off: bool = False) -> None:
    address = adb.normalised_address(str(settings.get_nowait("ingest.unit_adb_address") or ""))
    if address and get_status().unit_online:
        # A request is not proof of a capture. The ordinary tracked deployment path
        # checks its own fingerprint and retries after transport failure.
        if force_off:
            carplay_timing.reconcile_on_unit_present(address, config_only=True, force_off=True)
        else:
            carplay_timing.reconcile_on_unit_present(address, config_only=True)


@router.get("/carplay-trace", summary="Requested codec diagnostic window")
async def codec_trace_status(settings: SettingsDep) -> dict[str, object]:
    return _snapshot(settings)


@router.post("/carplay-trace", summary="Request a bounded codec diagnostic window")
async def codec_trace_start(payload: CodecTraceRequest, settings: SettingsDep) -> dict[str, object]:
    if not settings.get_nowait(carplay_timing.ENABLED_KEY) or not settings.get_nowait(
        "ingest.unit_logs"
    ):
        raise HTTPException(409, "Enable passive CarPlay timing and unit logs first.")
    if not adb.normalised_address(str(settings.get_nowait("ingest.unit_adb_address") or "")):
        raise HTTPException(409, "No dashcam ADB address is configured.")
    until = int(datetime.now(UTC).timestamp()) + payload.duration_s
    await settings.set(carplay_timing.CODEC_TRACE_UNTIL_KEY, until, internal=True)
    _reconcile(settings)
    return _snapshot(settings)


@router.delete("/carplay-trace", summary="Stop requesting heavy codec diagnostics")
async def codec_trace_stop(settings: SettingsDep) -> dict[str, object]:
    await settings.set(carplay_timing.CODEC_TRACE_UNTIL_KEY, 0, internal=True)
    _reconcile(settings, force_off=True)
    return _snapshot(settings)
