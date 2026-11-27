"""Evidence from this pull only. Filenames are used for deduplication and never persisted."""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import Float, bindparam, cast, func, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from app.db.models import IngestRun, UnitLogEntry
from app.ingest import band, temperature_capture, temperature_history, transport
from app.ingest.models import RemoteFile


@dataclass(slots=True)
class RunAccounting:
    first_inventory_at: datetime | None = None
    last_inventory_at: datetime | None = None
    found: dict[str, int] = field(default_factory=dict)
    outstanding: dict[str, int] = field(default_factory=dict)
    saved: dict[str, int] = field(default_factory=dict)
    obd_copied: int = 0
    obd_duplicates: int = 0
    obd_failed: int = 0
    obd_bytes: int = 0
    obd_result_known: bool = True
    stop_reason: str | None = None
    radio_recovery_pending: bool = False
    wifi: band.LinkReading | None = None

    def inventory(self, at: datetime, files: Iterable[RemoteFile], camera: str) -> None:
        # The delta is from an existing full card inventory. Count repeated observations once.
        # If a later scan deliberately excludes a clip, it is no longer outstanding.
        self.outstanding = {
            item.name: item.size
            for item in files
            if camera == "both" or item.name.endswith(f"_{camera}.ts")
        }
        self.found.update(self.outstanding)
        self.first_inventory_at = self.first_inventory_at or at
        self.last_inventory_at = at

    def commit(self, names: Iterable[str], expected: dict[str, int]) -> None:
        for name in names:
            self.saved[name] = expected[name]
            self.found[name] = expected[name]  # Includes rescued power-cut clips.

    def document(self, received: transport.TransferResult | None) -> dict:
        remaining = self.outstanding.keys() - self.saved.keys()
        known = self.last_inventory_at is not None
        return {
            "first_inventory_at": self.first_inventory_at.isoformat() if known else None,
            "last_inventory_at": self.last_inventory_at.isoformat() if known else None,
            "footage_found_files": len(self.found) if known else None,
            "footage_found_bytes": sum(self.found.values()) if known else None,
            "footage_saved_files": len(self.saved),
            "footage_saved_bytes": sum(self.saved.values()),
            "footage_remaining_files": len(remaining) if known else None,
            "footage_remaining_bytes": sum(self.outstanding[n] for n in remaining)
            if known
            else None,
            "obd_copied": self.obd_copied if self.obd_result_known else None,
            "obd_failed": self.obd_failed if self.obd_result_known else None,
            "obd_duplicates": self.obd_duplicates if self.obd_result_known else None,
            "obd_copied_bytes": self.obd_bytes if self.obd_result_known else None,
            "stop_reason": self.stop_reason,
            "radio_recovery_pending": self.radio_recovery_pending,
            "receive_seconds": received.seconds if received and received.seconds > 0 else None,
            "wifi_frequency_mhz": self.wifi.frequency_mhz if self.wifi else None,
            "wifi_signal_dbm": self.wifi.signal_dbm if self.wifi else None,
            "wifi_link_mbps": self.wifi.link_mbps if self.wifi else None,
            "wifi_observed_at": self.wifi.observed_at.isoformat() if self.wifi else None,
            "soc_temperature_c": None,
            "temperature_observed_at": None,
        }


async def observed_temperature(session: AsyncSession, start: datetime, end: datetime) -> dict:
    """Find the hottest verified journal reading in the run, before its source rows age out."""
    message = UnitLogEntry.message
    # The verified journal serializes " soc=<degrees> | event=temperature_capture".
    # Rank on the server and validate the actual records before returning a temperature.
    degrees = cast(func.substr(message, func.instr(message, " soc=") + 5), Float)
    rows = await session.execute(
        select(UnitLogEntry.occurred_at, message)
        .where(
            UnitLogEntry.tag == temperature_capture.TAG,
            UnitLogEntry.occurred_at.between(start, end),
            message.like("% soc=% | event=temperature_capture"),
            ~message.like("% soc=na | event=temperature_capture"),
        )
        .order_by(degrees.desc(), UnitLogEntry.occurred_at, UnitLogEntry.id)
        .limit(32)
    )
    for at, message_text in rows:
        sample = temperature_history.parse_observation(at, message_text)
        if sample and sample.source == temperature_capture.SOURCE and sample.soc_c is not None:
            return {"soc_temperature_c": sample.soc_c, "temperature_observed_at": at.isoformat()}
    return {"soc_temperature_c": None, "temperature_observed_at": None}


async def record_imported_peaks(session: AsyncSession | AsyncConnection, entries) -> None:
    """Attribute a bounded journal chunk to finished runs in the same transaction as its import."""
    points = sorted(
        (entry.occurred_at, sample.soc_c)
        for entry in entries
        if entry.tag == temperature_capture.TAG
        and (sample := temperature_history.parse_observation(entry.occurred_at, entry.message))
        and sample.source == temperature_capture.SOURCE
        and sample.soc_c is not None
    )
    if not points:
        return
    times = [at for at, _degrees in points]
    runs = await session.execute(
        select(
            IngestRun.id, IngestRun.started_at, IngestRun.finished_at, IngestRun.accounting
        ).where(
            IngestRun.started_at <= times[-1],
            IngestRun.finished_at >= times[0],
            IngestRun.accounting.is_not(None),
        )
    )
    patches = []
    for run in runs:
        segment = points[bisect_left(times, run.started_at) : bisect_right(times, run.finished_at)]
        if not segment or not run.accounting:
            continue
        at, degrees = max(segment, key=lambda point: point[1])
        previous = run.accounting.get("soc_temperature_c")
        if (
            previous is None
            or degrees > previous
            or (degrees == previous and at.isoformat() < run.accounting["temperature_observed_at"])
        ):
            patches.append(
                {
                    "pk": run.id,
                    "evidence": {
                        **run.accounting,
                        "soc_temperature_c": degrees,
                        "temperature_observed_at": at.isoformat(),
                    },
                }
            )
    if patches:
        table = IngestRun.__table__
        await session.execute(
            table.update()
            .where(table.c.id == bindparam("pk"))
            .values(accounting=bindparam("evidence")),
            patches,
        )
