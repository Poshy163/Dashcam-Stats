"""Independent temperature retention/import: bounded and never a power/radio command."""

import asyncio
import base64
import hashlib
import json
import os
import re
import subprocess
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import OperationalError

from app.api.routes.system import unit_temperature_history
from app.db.models import UnitLogEntry
from app.db.session import get_engine
from app.ingest import temperature_capture as capture
from app.ingest import temperature_history, unit_logs
from app.ingest.logger_diagnostics import clean_temperature_capture

AT = datetime(2026, 10, 26, 1, tzinfo=UTC)
SESSION = "a" * 32


def reading(sequence=1, *, at=AT, elapsed=None, value=72_345, session=SESSION):
    return {
        "sequence": sequence,
        "session_id": session,
        "captured_at_utc": at.isoformat(),
        "elapsed_realtime_ms": sequence * 30_000 if elapsed is None else elapsed,
        "soc_millic": value,
        "quality": "valid" if value is not None else "unavailable",
    }


def encoded(value):
    return json.dumps(value, separators=(",", ":")).encode()


def descriptor(rows):
    raw = encoded({"schema_version": 1, "readings": rows})
    return raw, {
        "file": f"samples-{rows[0]['sequence']}.json",
        "first_sequence": rows[0]["sequence"],
        "last_sequence": rows[-1]["sequence"],
        "size_bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def fixture(chunks=None):
    chunks = chunks or [[reading()]]
    content = {}
    descriptors = []
    for rows in chunks:
        raw, item = descriptor(rows)
        content[item["file"]] = raw
        descriptors.append(item)
    manifest = {
        "schema_version": 1,
        "source": capture.SOURCE,
        "sensor": capture.SENSOR,
        "capture_interval_ms": 30_000,
        "generated_at_utc": AT.isoformat(),
        "collector_state": "running",
        "retained_readings": sum(map(len, chunks)),
        "chunks": descriptors,
        "latest": chunks[-1][-1],
    }
    content["manifest.json"] = encoded(manifest)
    return content, manifest


class Remote:
    def __init__(self, content):
        self.content = content
        self.commands = []

    async def shell(self, _address, command, **kwargs):
        assert kwargs["timeout"] == capture.REQUEST_TIMEOUT_S
        assert not any(
            word in command for word in ("am start", "settings put", "kill ", "svc ", "rm ")
        )
        self.commands.append(command)
        path = re.search(r"p='([^']+)'", command)[1].rsplit("/", 1)[-1]
        if path not in self.content:
            return ""
        offset = int(re.search(r"skip=(\d+)", command)[1]) * capture.FRAGMENT_BYTES
        length = int(re.search(r"head -c (\d+)", command)[1])
        part = self.content[path][offset : offset + length]
        return f"TEMP1 {len(self.content[path])}\n{base64.b64encode(part).decode()}\nTEMP_END"


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    capture._last_attempt.clear()
    capture._tasks.clear()
    capture._imported.clear()
    capture._verified_latest.clear()
    capture._backfill_retry.clear()
    monkeypatch.setattr(capture, "_collector", None)
    monkeypatch.setattr(capture, "_shutting_down", False)
    monkeypatch.setattr(capture, "_root", lambda: "/storage/Tfcard/obd/temperature")


@pytest.mark.parametrize(
    "change",
    [
        {"sequence": True},
        {"sequence": 0},
        {"sequence": 2**53},
        {"elapsed_realtime_ms": -1},
        {"session_id": "private-boot-uuid"},
        {"soc_millic": 200_001},
        {"soc_millic": True},
        {"quality": "guessed"},
        {"captured_at_utc": "2026-10-26T01:00:00"},
        {"path": "/private"},
    ],
)
def test_reading_rejects_unbounded_or_ambiguous_values(change):
    with pytest.raises(ValueError):
        capture.validate_reading({**reading(), **change})


def test_sensor_unavailable_and_wall_clock_correction_remain_explicit():
    rows = [reading(1, at=AT + timedelta(hours=1)), reading(2, at=AT)]
    raw, item = descriptor(rows)
    assert capture.validate_chunk(raw, item, now=AT) == rows
    missing = capture.validate_reading(reading(value=None))
    assert missing["soc_millic"] is None and missing["quality"] == "unavailable"
    result = temperature_history.history(
        [capture.parsed_line(item) for item in rows],
        hours=6,
        range_start=AT,
        range_end=AT + timedelta(hours=2),
    )
    assert len({point["segment_id"] for point in result["points"]}) == 2
    assert {point["occurred_at"] for point in result["points"]} == {AT, AT + timedelta(hours=1)}


@pytest.mark.parametrize(
    "change",
    [
        {"file": "../private.json"},
        {"file": "samples-2.json"},
        {"size_bytes": 131_073},
        {"sha256": "x" * 64},
        {"first_sequence": True},
        {"extra": "private"},
    ],
)
def test_manifest_cannot_select_unbounded_or_arbitrary_files(change):
    _, manifest = fixture()
    manifest["chunks"][0].update(change)
    with pytest.raises(ValueError):
        capture.validate_manifest(encoded(manifest), now=AT)


def test_manifest_rejects_duplicate_json_keys_and_oversize_without_poisoning_clock_recovery():
    _, manifest = fixture()
    raw = encoded(manifest)
    with pytest.raises(ValueError):
        capture.validate_manifest(raw[:-1] + b',"sensor":"soc-thmzone"}', now=AT)
    with pytest.raises(ValueError):
        capture.validate_manifest(raw + b" " * capture.MAX_MANIFEST_BYTES, now=AT)
    manifest["generated_at_utc"] = (AT + timedelta(hours=1)).isoformat()
    assert (
        capture.validate_manifest(encoded(manifest), now=AT)["generated_at_utc"]
        == manifest["generated_at_utc"]
    )


def test_chunk_hash_range_monotonicity_and_read_count_are_verified():
    rows = [reading(1), reading(2, elapsed=29_000)]
    raw, item = descriptor(rows)
    with pytest.raises(ValueError):
        capture.validate_chunk(raw, item)
    raw, item = descriptor([reading()])
    with pytest.raises(ValueError):
        capture.validate_chunk(raw + b" ", item)
    item["last_sequence"] = 2
    with pytest.raises(ValueError):
        capture.validate_chunk(raw, item)
    rows = [reading(index + 1) for index in range(257)]
    raw, item = descriptor(rows)
    with pytest.raises(ValueError):
        capture.validate_chunk(raw, item)


async def test_changed_chunk_only_import_and_sqlite_deduplication(monkeypatch, db_session):
    content, _ = fixture()
    remote = Remote(content)
    monkeypatch.setattr(capture.adb, "shell", remote.shell)
    assert await capture.recover("unit") == (1, 0)
    assert await capture.recover("unit") == (0, 0)
    assert sum("samples-" in command for command in remote.commands) == 1
    # A server restart must safely re-import a retained generation once, never duplicate it.
    capture._imported.clear()
    capture._verified_latest.clear()
    assert await capture.recover("unit") == (0, 1)
    assert await db_session.scalar(select(func.count(UnitLogEntry.id))) == 1
    response = await unit_temperature_history(session=db_session, hours=24)
    # Fixture may lie outside the current wall-clock window; directly inspect persisted provenance.
    row = await db_session.scalar(select(UnitLogEntry))
    assert row.tag == capture.TAG and "source=companion_temperature_v1" in row.message
    assert response["collector"]["last_captured_at_utc"] == AT.isoformat()
    assert response["collector"]["checked_at_utc"] != AT.isoformat()


async def test_bad_chunk_and_failed_commit_are_retained_for_retry(monkeypatch):
    content, manifest = fixture()
    remote = Remote(content)
    monkeypatch.setattr(capture.adb, "shell", remote.shell)
    original = content["samples-1.json"]
    content["samples-1.json"] += b"bad"
    # A full bounded prefix still hashes correctly, so alter an actual referenced byte.
    content["samples-1.json"] = b"!" + original[1:]
    with pytest.raises(ValueError):
        await capture.recover("unit")
    assert capture.collector_snapshot() is None
    assert capture._imported["unit"] == {}
    content["samples-1.json"] = original

    async def broken(_entries, **_kwargs):
        raise RuntimeError("database busy")

    monkeypatch.setattr(unit_logs, "store", broken)
    with pytest.raises(RuntimeError):
        await capture.recover("unit")
    assert capture._imported["unit"] == {}
    assert capture.collector_snapshot() is None
    manifest["latest"]["soc_millic"] = 1
    content["manifest.json"] = encoded(manifest)
    with pytest.raises(ValueError):
        await capture.recover("unit")


async def test_backfill_has_a_fixed_budget_and_prioritizes_latest(monkeypatch):
    chunks = [[reading(index)] for index in range(1, 9)]
    content, _ = fixture(chunks)
    remote = Remote(content)
    monkeypatch.setattr(capture.adb, "shell", remote.shell)
    stored = []

    async def store(entries, **kwargs):
        assert kwargs == {"busy_timeout_ms": 200}
        stored.extend(entries)
        return len(entries), 0

    monkeypatch.setattr(unit_logs, "store", store)
    assert await capture.recover("unit") == (4, 0)
    files = [
        re.search(r"p='([^']+)'", command)[1].rsplit("/", 1)[-1]
        for command in remote.commands
        if "samples-" in command
    ]
    assert files == ["samples-8.json", "samples-1.json", "samples-2.json", "samples-3.json"]
    assert capture.collector_snapshot()["last_captured_elapsed_ms"] == 240_000


async def test_unset_clock_and_bad_future_rows_do_not_poison_valid_history(monkeypatch):
    rows = [
        reading(1, at=datetime(1970, 1, 1, tzinfo=UTC)),
        reading(2),
        reading(3, at=datetime(2099, 1, 1, tzinfo=UTC)),
    ]
    content, manifest = fixture([rows])
    remote = Remote(content)
    monkeypatch.setattr(capture.adb, "shell", remote.shell)
    received = []

    async def store(entries, **_kwargs):
        received.extend(entries)
        return len(entries), 0

    monkeypatch.setattr(unit_logs, "store", store)
    assert await capture.recover("unit") == (1, 0)
    assert received[0].occurred_at == AT
    collector = capture.collector_snapshot()
    assert collector["state"] == collector["error"] == "clock_unavailable"
    assert collector["last_captured_at_utc"] is collector["last_captured_elapsed_ms"] is None
    # Invalid rows are quarantined rather than monopolizing every backfill attempt.
    assert await capture.recover("unit") == (0, 0)
    assert capture._imported["unit"]["samples-1.json"] == manifest["chunks"][0]["sha256"]


async def test_old_corrupt_backfill_cannot_block_latest_health_or_good_history(monkeypatch):
    content, manifest = fixture([[reading(index)] for index in range(1, 5)])
    content["samples-1.json"] = b"!" + content["samples-1.json"][1:]
    remote = Remote(content)
    monkeypatch.setattr(capture.adb, "shell", remote.shell)
    received = []

    async def store(entries, **_kwargs):
        received.extend(entries)
        return len(entries), 0

    monkeypatch.setattr(unit_logs, "store", store)
    assert await capture.recover("unit") == (3, 0)
    assert capture.collector_snapshot()["last_captured_elapsed_ms"] == 120_000
    assert capture.collector_snapshot()["checked_at_utc"]
    assert "samples-1.json" not in capture._imported["unit"]
    assert set(capture._imported["unit"]) == {"samples-2.json", "samples-3.json", "samples-4.json"}
    before = len(received)
    assert await capture.recover("unit") == (0, 0)
    assert len(received) == before


async def test_cancel_during_old_backfill_propagates_without_marking_failed_chunk(monkeypatch):
    content, _ = fixture([[reading(1)], [reading(2)]])
    remote = Remote(content)
    monkeypatch.setattr(capture.adb, "shell", remote.shell)

    async def store(entries, **_kwargs):
        if "sequence=1 " in entries[0].message:
            raise asyncio.CancelledError
        return len(entries), 0

    monkeypatch.setattr(unit_logs, "store", store)
    with pytest.raises(asyncio.CancelledError):
        await capture.recover("unit")
    assert set(capture._imported["unit"]) == {"samples-2.json"}
    assert capture.collector_snapshot()["last_captured_elapsed_ms"] == 60_000


async def test_bad_old_chunks_have_fair_cooldown_and_changed_hash_retries_immediately(monkeypatch):
    content, manifest = fixture([[reading(index)] for index in range(1, 7)])
    for index in range(1, 4):
        name = f"samples-{index}.json"
        content[name] = b"!" + content[name][1:]
    remote = Remote(content)
    monkeypatch.setattr(capture.adb, "shell", remote.shell)
    now = [100.0]
    monkeypatch.setattr(capture, "time", SimpleNamespace(monotonic=lambda: now[0]))

    async def store(entries, **_kwargs):
        return len(entries), 0

    monkeypatch.setattr(unit_logs, "store", store)
    assert await capture.recover("unit") == (1, 0)
    assert capture.collector_snapshot()["last_captured_elapsed_ms"] == 180_000
    assert set(capture._backfill_retry["unit"]) == {
        "samples-1.json",
        "samples-2.json",
        "samples-3.json",
    }
    now[0] += 30
    assert await capture.recover("unit") == (2, 0)
    assert set(capture._imported["unit"]) == {"samples-4.json", "samples-5.json", "samples-6.json"}
    repaired, item = descriptor([reading(2, value=72_346)])
    content[item["file"]] = repaired
    manifest["chunks"][1] = item
    content["manifest.json"] = encoded(manifest)
    assert await capture.recover("unit") == (1, 0)
    assert "samples-2.json" in capture._imported["unit"]


async def test_transport_rejects_oversized_actual_file_even_when_valid_prefix_matches(monkeypatch):
    content, manifest = fixture()
    remote = Remote(content)
    monkeypatch.setattr(capture.adb, "shell", remote.shell)
    content["samples-1.json"] += b"ignored trailing garbage"
    with pytest.raises(ValueError, match="size"):
        await capture._read_file(
            "unit", "/safe/samples-1.json", manifest["chunks"][0]["size_bytes"]
        )
    content["manifest.json"] += b" " * capture.MAX_MANIFEST_BYTES
    with pytest.raises(ValueError, match="size"):
        await capture._read_file("unit", "/safe/manifest.json")


@pytest.mark.parametrize(
    "response",
    [
        "TEMP1 5\nYWJj\nTEMP_END",
        "TEMP1 3\nYWJj\nTEMP_END\ntrailing",
        "TEMP1 3\n!\nTEMP_END",
        "TEMP1 true\nYWJj\nTEMP_END",
        "TEMP1 3\nYWJj",
        "TEMP1 3\nYWJj\nTEMP_END\nTEMP_END",
    ],
)
async def test_transport_rejects_incomplete_or_unframed_payloads(monkeypatch, response):
    async def shell(*_args, **_kwargs):
        return response

    monkeypatch.setattr(capture.adb, "shell", shell)
    with pytest.raises(ValueError):
        await capture._read_file("unit", "/safe/manifest.json")


async def test_real_database_lock_is_short_and_not_cached(monkeypatch, db_session):
    content, _ = fixture()
    monkeypatch.setattr(capture.adb, "shell", Remote(content).shell)
    async with get_engine().connect() as locked:
        await locked.exec_driver_sql("BEGIN IMMEDIATE")
        began = time.monotonic()
        try:
            with pytest.raises(OperationalError):
                await capture.recover("unit")
            assert time.monotonic() - began < 2
            assert capture._imported["unit"] == {}
            assert capture.collector_snapshot() is None
        finally:
            await locked.rollback()
    # The connection-local short timeout must not leak into ordinary database users.
    async with get_engine().connect() as connection:
        assert await connection.scalar(text("PRAGMA busy_timeout")) == 30_000
    assert await capture.recover("unit") == (1, 0)


async def test_polling_backup_does_not_skip_independent_temperature_recovery(monkeypatch):
    from unittest.mock import AsyncMock

    from app.ingest import poller as polling
    from app.ingest.status import IngestStatus

    status = IngestStatus()
    status.try_begin()
    owner = polling.IngestPoller()
    owner._running = True
    scheduled = []
    monkeypatch.setattr(polling, "get_status", lambda: status)
    monkeypatch.setattr(owner, "_address", lambda: "unit")
    monkeypatch.setattr(owner, "_observe_unit_runtime", AsyncMock())
    monkeypatch.setattr(polling.band, "on_unit_present", lambda _address: None)
    monkeypatch.setattr(polling.carplay_timing, "recover_on_unit_present", lambda _address: None)
    monkeypatch.setattr(polling.temperature_capture, "on_unit_present", scheduled.append)

    async def one_tick(_delay):
        owner._running = False

    monkeypatch.setattr(polling.asyncio, "sleep", one_tick)
    await owner._loop()
    assert scheduled == ["unit"] and status.running
    assert not status.cancel_event.is_set()


@pytest.mark.skipif(os.name == "nt", reason="Android-compatible POSIX shell runs in Linux CI")
async def test_read_command_reads_pinned_regular_file_and_refuses_symlink(tmp_path, monkeypatch):
    raw = b"bounded bytes" * 3000
    target = tmp_path / "samples-1.json"
    target.write_bytes(raw)

    async def shell(_address, command, **_kwargs):
        return subprocess.run(
            ["sh", "-c", command], check=True, capture_output=True, text=True
        ).stdout

    monkeypatch.setattr(capture.adb, "shell", shell)
    assert await capture._read_file("unit", str(target), len(raw)) == raw
    link = tmp_path / "manifest.json"
    link.symlink_to(target)
    assert await capture._read_file("unit", str(link)) is None


async def test_presence_is_parked_read_only_throttled_and_single_flight(monkeypatch):
    state = SimpleNamespace(ignition_state="on")
    monkeypatch.setattr(capture, "get_status", lambda: state)
    monkeypatch.setattr(capture.adb, "optional_requests_deferred", lambda _address: False)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def recover(address):
        calls.append(address)
        started.set()
        await release.wait()

    monkeypatch.setattr(capture, "recover", recover)
    capture.on_unit_present("unit")
    assert not capture._tasks
    state.ignition_state = "off"
    capture.on_unit_present("unit")
    await started.wait()
    capture.on_unit_present("unit")
    assert calls == ["unit"]
    release.set()
    await asyncio.gather(*capture._tasks.values())
    await asyncio.sleep(0)
    capture.on_unit_present("unit")
    assert calls == ["unit"]
    await capture.shutdown()
    assert not capture._tasks


async def test_missing_old_companion_is_compatible(monkeypatch):
    monkeypatch.setattr(capture.adb, "shell", Remote({}).shell)
    assert await capture.recover("old-unit") == (0, 0)
    assert capture.collector_snapshot() is None


def test_companion_preferred_without_erasing_legacy_only_coverage():
    rows = [capture.parsed_line(reading())]
    for seconds in (5, 120):
        rows.append(
            SimpleNamespace(
                occurred_at=AT + timedelta(seconds=seconds),
                message="session=legacy soc=99 | no phone on hotspot",
            )
        )
    result = temperature_history.history(
        rows, hours=1, range_start=AT, range_end=AT + timedelta(hours=1)
    )
    assert result["source"] == "retained_device_temperature"
    assert result["companion_observation_count"] == result["legacy_observation_count"] == 1
    assert result["unique_observation_count"] == 2
    assert result["points"][0]["source"] == capture.SOURCE
    assert result["maximum_c"] == 99


def test_unavailable_companion_cannot_erase_independent_valid_legacy_temperature():
    rows = [
        capture.parsed_line(reading(value=None)),
        SimpleNamespace(
            occurred_at=AT + timedelta(seconds=5),
            message="session=legacy soc=83 | no phone on hotspot",
        ),
    ]
    result = temperature_history.history(
        rows, hours=1, range_start=AT, range_end=AT + timedelta(hours=1)
    )
    assert result["companion_observation_count"] == result["legacy_observation_count"] == 1
    assert result["minimum_c"] == result["maximum_c"] == 83
    assert result["points"][0]["soc_c"] is None
    assert result["points"][0]["segment_id"] != result["points"][1]["segment_id"]


def test_temperature_status_is_optional_bounded_and_not_power_evidence():
    value = {
        "schema_version": 1,
        "source": capture.SOURCE,
        "sensor": capture.SENSOR,
        "state": "running",
        "last_captured_at_utc": AT.isoformat(),
        "last_captured_elapsed_ms": 30_000,
        "interval_ms": 30_000,
        "retained_readings": 1,
        "error": None,
    }
    assert clean_temperature_capture(value) == value
    for key, invalid in (
        ("retained_readings", True),
        ("retained_readings", 8193),
        ("error", "raw exception"),
        ("interval_ms", 1),
    ):
        assert clean_temperature_capture({**value, key: invalid}) is None
    assert clean_temperature_capture({**value, "native_sleep_allowed": True}) is None
    assert clean_temperature_capture({**value, "last_captured_at_utc": None}) is None
    assert clean_temperature_capture({**value, "state": "sensor_unavailable"}) is None
