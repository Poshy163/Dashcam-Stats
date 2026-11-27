"""Recover compact, positive CarPlay evidence when the existing OBD mirror sees a parked unit."""

import asyncio
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import delete
from sqlalchemy.dialects.sqlite import insert

from app.core.logging import get_logger
from app.core.settings_service import get_settings_service
from app.db.models import OBDCarPlayObservation, utcnow
from app.db.session import session_scope
from app.ingest import adb

if TYPE_CHECKING:
    from app.ingest.obd_events import ValidatedEventSnapshot

_SCRIPT = Path(__file__).with_suffix(".sh").read_text(encoding="utf-8")
_LOCAL_KINDS = {"lease", "decoder", "video_output"}
_LOG_KINDS = {
    "iap",
    "other_iap",
    "phone_handoff",
    "hotspot",
    "early_retry",
    "waiting_connect",
    "waiting_cycle",
}
_task: asyncio.Task[None] | None = None
_attempted: tuple[str, int] | None = None
_attempts = 0
_completed: tuple[str, int] | None = None
log = get_logger(__name__)


def _local_utc(value: str, zone: ZoneInfo | None, parked: datetime) -> datetime | None:
    """Android media.metrics omits the year; reject clock folds and nonexistent civil times."""
    if zone is None:
        return None
    candidates = set()
    if len(value) == 18:
        local_year = parked.astimezone(zone).year
        values = [f"{year}-{value}" for year in (local_year - 1, local_year)]
    else:
        values = [value]
    for full in values:
        if not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}", full, re.ASCII):
            continue
        try:
            local = datetime.fromisoformat(full)
        except ValueError:
            continue
        possible = set()
        for fold in (0, 1):
            at = local.replace(tzinfo=zone, fold=fold).astimezone(UTC)
            if at.astimezone(zone).replace(tzinfo=None) == local:
                possible.add(at)
        if len(possible) == 1:
            at = possible.pop()
            if parked - timedelta(days=7) <= at <= parked + timedelta(seconds=1):
                candidates.add(at)
    return candidates.pop() if len(candidates) == 1 else None


def parse_snapshot(raw: str, received: datetime) -> list[dict[str, Any]] | None:
    lines = raw.splitlines()
    if len(raw) > 65536 or not lines or len(lines) > 773 or lines[-1] != "done":
        return None
    header = re.fullmatch(r"clock ([0-9]{10}) ([a-zA-Z0-9_+./-]{0,80})", lines[0])
    if not header:
        return None
    parked = datetime.fromtimestamp(int(header[1]), UTC)
    if abs(received - parked) > timedelta(minutes=2):
        return None
    try:
        zone = ZoneInfo(header[2])
    except (ZoneInfoNotFoundError, ValueError):
        zone = None
    rows = []
    for line in lines[1:-1]:
        fields = line.split()
        if not 2 <= len(fields) <= 3:
            continue
        kind, stamp = fields[:2]
        duration = None
        if kind in _LOG_KINDS and len(fields) == 2 and re.fullmatch(r"[0-9]{10}\.[0-9]{3}", stamp):
            at = datetime.fromtimestamp(int(stamp[:10]), UTC) + timedelta(
                milliseconds=int(stamp[11:])
            )
        elif kind in _LOCAL_KINDS:
            if kind == "lease":
                if len(fields) != 2 or len(stamp) != 23:
                    continue
            elif len(fields) == 3 and re.fullmatch(r"[0-9]{1,8}", fields[2]) and len(stamp) == 18:
                duration = int(fields[2])
                if not 0 < duration <= 86_400_000:
                    continue
            else:
                continue
            at = _local_utc(stamp, zone, parked)
            if at is not None and duration is not None:
                at -= timedelta(milliseconds=duration)
        else:
            continue
        if at is not None and parked - timedelta(days=7) <= at <= parked + timedelta(seconds=1):
            rows.append({"kind": kind, "occurred_at": at, "duration_ms": duration})
    return rows


async def _recover(address: str, key: tuple[str, int]) -> None:
    global _completed
    try:
        raw = await adb.shell(address, _SCRIPT, timeout=12)
        rows = parse_snapshot(raw, utcnow())
        if rows is None:
            return
        async with session_scope() as session:
            if rows:
                await session.execute(
                    insert(OBDCarPlayObservation).on_conflict_do_nothing(),
                    [{**row, "source_id_hash": key[0]} for row in rows],
                )
            await session.execute(
                delete(OBDCarPlayObservation).where(
                    OBDCarPlayObservation.occurred_at < utcnow() - timedelta(days=90)
                )
            )
        if {"network_complete", "metrics_complete", "log_complete"}.issubset(raw.splitlines()):
            _completed = key
    except Exception as exc:
        # Android and SQL error text may include system details or bound parameters.
        log.warning("parked CarPlay observations unavailable", category=type(exc).__name__)


def on_event_snapshot(address: str, snapshot: "ValidatedEventSnapshot") -> None:
    global _task, _attempted, _attempts
    if _task and not _task.done():
        return
    last_acc = next(
        (
            e
            for e in reversed(snapshot.events)
            if e.kind == "app.service"
            and e.outcome == "observed"
            and e.reason_code in {"acc_on", "acc_off"}
        ),
        None,
    )
    if last_acc is None or last_acc.reason_code != "acc_off":
        return
    if not get_settings_service().get_nowait("ingest.unit_logs"):
        return
    key = (snapshot.source_id_hash, last_acc.sequence)
    if key != _attempted:
        _attempted, _attempts = key, 0
    if key != _completed and _attempts < 2:
        _attempts += 1
        _task = asyncio.create_task(_recover(address, key), name="ingest-carplay-outcome")


async def shutdown() -> None:
    global _task, _attempted, _completed
    if _task is not None:
        _task.cancel()
        await asyncio.gather(_task, return_exceptions=True)
    _task = _attempted = _completed = None
