"""Per-run footage, OBD and passive operating measurements survive a later visit."""

import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from alembic import command
from test_ingest_preparation_lifecycle import isolated_mirror as _isolated_mirror
from test_ingest_preparation_lifecycle import prepared_unit as _prepared_unit

from app.db.models import IngestRun
from app.db.session import alembic_config, session_scope
from app.ingest import band, puller, temperature_capture, unit_logs
from app.ingest.models import RemoteFile, RunResult, RunState
from app.ingest.obd_transfer import OBDTransferResult
from app.ingest.run_accounting import RunAccounting

isolated_mirror = _isolated_mirror
prepared_unit = _prepared_unit
record_completion = puller._record_run_completion


def test_inventory_is_deduplicated_per_lens_and_never_invents_a_backlog():
    at = datetime.now(UTC)
    accounting = RunAccounting()
    assert accounting.document(None)["footage_remaining_files"] is None
    accounting.inventory(at, [], "0")
    assert accounting.document(None)["footage_remaining_files"] == 0
    front = RemoteFile("first_0.ts", 100, 0)
    rear = RemoteFile("first_1.ts", 90, 0)
    second = RemoteFile("second_0.ts", 125, 0)
    third = RemoteFile("third_0.ts", 150, 0)
    accounting.inventory(at, [front, rear, second], "0")
    accounting.commit([front.name], {front.name: front.size})
    later = at + timedelta(seconds=45)
    accounting.inventory(later, [second, third, third], "0")
    accounting.commit([second.name, second.name], {second.name: second.size})
    doc = accounting.document(None)
    assert (doc["footage_found_files"], doc["footage_found_bytes"]) == (3, 375)
    assert (doc["footage_saved_files"], doc["footage_saved_bytes"]) == (2, 225)
    assert (doc["footage_remaining_files"], doc["footage_remaining_bytes"]) == (1, 150)
    assert doc["first_inventory_at"] == at.isoformat()
    assert doc["last_inventory_at"] == later.isoformat()
    assert all(item.name not in str(doc) for item in [front, rear, second, third])


@pytest.mark.parametrize("stop", [False, True])
async def test_actual_run_separates_footage_and_obd_and_keeps_sleep_carryover(
    client, prepared_unit, monkeypatch, stop
):
    from types import SimpleNamespace

    monkeypatch.setattr(puller, "_record_run_completion", record_completion)
    monkeypatch.setattr(
        puller,
        "inventory_remote_bundles",
        AsyncMock(return_value=[SimpleNamespace(name="pending.obd2.zip")]),
    )
    monkeypatch.setattr(
        puller,
        "sync_remote_bundles",
        AsyncMock(return_value=OBDTransferResult(copied=1, duplicates=1, bytes=42)),
    )
    if stop:
        from app.ingest.radio_coordinator import RadioWindowEnding

        monkeypatch.setattr(puller, "_move", AsyncMock(side_effect=RadioWindowEnding()))
    await puller.run_pull(info=prepared_unit.info)
    response = await client.get("/api/ingest/history")
    row = response.json()["items"][0]
    doc = row["accounting"]
    size = len(prepared_unit.payload)
    assert (doc["footage_found_files"], doc["footage_found_bytes"]) == (1, size)
    assert (doc["footage_saved_files"], doc["footage_saved_bytes"]) == (
        (0, 0) if stop else (1, size)
    )
    assert (doc["footage_remaining_files"], doc["footage_remaining_bytes"]) == (
        (1, size) if stop else (0, 0)
    )
    assert (doc["obd_copied"], doc["obd_copied_bytes"], doc["obd_duplicates"]) == (1, 42, 1)
    assert row["files_transferred"] == 2 + doc["footage_saved_files"]
    assert doc["stop_reason"] == ("sleep_deadline" if stop else None)


async def test_zero_copy_deadline_saves_inventory_without_notifying(db_session, monkeypatch):
    lifecycle = puller._RunLifecycle(started_at=datetime.now(UTC))
    lifecycle.accounting.inventory(
        lifecycle.started_at, [RemoteFile("pending_0.ts", 300, 0)], "both"
    )
    lifecycle.accounting.stop_reason = "sleep_deadline"
    token = puller._run_lifecycle.set(lifecycle)
    notify = AsyncMock()
    monkeypatch.setattr(puller, "report_event", notify)
    try:
        await record_completion(RunResult(state=RunState.IDLE), "auto", continuation=False)
    finally:
        puller._run_lifecycle.reset(token)
    row = await db_session.get(IngestRun, 1)
    assert row.accounting["footage_remaining_bytes"] == 300
    notify.assert_not_awaited()


async def test_band_observation_uses_only_this_runs_numeric_link_data(monkeypatch):
    start = datetime.now(UTC)
    read = AsyncMock(
        return_value="WifiInfo: sample, RSSI: -61, Link speed: 433Mbps, Frequency: 5220MHz"
    )
    monkeypatch.setattr(band.adb, "shell", read)
    await band.read_link("unit")
    end = datetime.now(UTC)
    sample = band.observed_link("unit", start, end)
    assert (sample.frequency_mhz, sample.signal_dbm, sample.link_mbps) == (5220, -61, 433)
    assert band.observed_link("elsewhere", start, end) is None
    assert (
        band.observed_link("unit", end + timedelta(seconds=1), end + timedelta(seconds=5)) is None
    )
    read.assert_awaited_once()


async def test_in_run_peak_survives_late_imports_and_ignores_legacy_and_other_windows(client):
    finished = datetime.now(UTC)
    start = finished - timedelta(minutes=1)

    def point(offset, degrees):
        return temperature_capture.parsed_line(
            {
                "session_id": "a" * 32,
                "sequence": offset + 3,
                "elapsed_realtime_ms": 1000,
                "soc_millic": None if degrees is None else degrees * 1000,
                "captured_at_utc": (start + timedelta(seconds=offset)).isoformat(),
            }
        )

    async with session_scope() as session:
        session.add(IngestRun(started_at=start, finished_at=finished, state="ok"))
    # First and last readings differ from the actual maximum.
    await unit_logs.store([point(4, 55), point(10, 72), point(35, 58), point(48, None)])
    lifecycle = puller._RunLifecycle(started_at=start)
    token = puller._run_lifecycle.set(lifecycle)
    try:
        await puller._persist(RunResult(state=RunState.OK), "auto")
    finally:
        puller._run_lifecycle.reset(token)

    async def latest():
        rows = (await client.get("/api/ingest/history")).json()["items"]
        assert next(r for r in rows if r["id"] == 1)["accounting"] is None
        return next(r["accounting"] for r in rows if r["id"] == 2)

    assert (await latest())["soc_temperature_c"] == 72
    # Production collector uses the dedicated Core connection with bounded SQLite wait.
    late = [point(-1, 99), point(12, 81), point(40, 73), point(65, 98)]
    await unit_logs.store(late, busy_timeout_ms=200)
    await unit_logs.store(late, busy_timeout_ms=200)
    doc = await latest()
    assert doc["soc_temperature_c"] == 81
    assert doc["temperature_observed_at"] == (start + timedelta(seconds=12)).isoformat()
    assert doc["footage_found_files"] is None


@pytest.mark.parametrize("incompatible", [False, True])
def test_interrupted_accounting_migration_protects_legacy_rows(app_config, incompatible):
    config = alembic_config()
    command.upgrade(config, "0033")
    with closing(sqlite3.connect(app_config.db_path)) as conn, conn:
        conn.execute("INSERT INTO ingest_runs (id, started_at) VALUES (1, '2026-11-01 12:00:00')")
        kind = "INTEGER" if incompatible else "JSON"
        conn.execute(f"ALTER TABLE ingest_runs ADD COLUMN accounting {kind}")
    if incompatible:
        with pytest.raises(RuntimeError, match="incompatible schema"):
            command.upgrade(config, "0034")
        return
    command.upgrade(config, "0034")
    with closing(sqlite3.connect(app_config.db_path)) as conn:
        assert conn.execute("SELECT accounting FROM ingest_runs WHERE id=1").fetchone() == (None,)
    command.downgrade(config, "0033")
    with closing(sqlite3.connect(app_config.db_path)) as conn:
        assert conn.execute("SELECT id FROM ingest_runs").fetchall() == [(1,)]
