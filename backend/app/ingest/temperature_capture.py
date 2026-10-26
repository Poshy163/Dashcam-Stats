"""Read-only, bounded recovery of the companion's independent temperature journal.

Capture is owned by Android's existing foreground service. This importer cannot start
an app, change power/radios, or grant a backup/sleep lease. A stored manifest describes
historical capture; its import time is deliberately separate from capture time.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from typing import Any

from app.config import get_config
from app.core.logging import get_logger
from app.ingest import adb, unit_logs
from app.ingest.obd_transfer import _safe_remote_path
from app.ingest.status import get_status

log = get_logger(__name__)
TAG = "DashcamTemperature"
SOURCE = "companion_temperature_v1"
SENSOR = "soc-thmzone"
CAPABILITY = "temperature_history_v1"
INTERVAL_MS = 30_000
MAX_READINGS = 8192
MAX_CHUNKS = 32
MAX_CHUNK_READINGS = 256
MAX_CHUNK_BYTES = 128 * 1024
MAX_MANIFEST_BYTES = 16 * 1024
FRAGMENT_BYTES = 16 * 1024
MAX_CHANGED_CHUNKS = 4
REFRESH_S = 30.0
REQUEST_TIMEOUT_S = 3.0
TOTAL_TIMEOUT_S = 20.0
CHUNK_TIMEOUT_S = 4.0
BACKFILL_RETRY_S = 90.0
MAX_INTEGER = 2**53 - 1
_FILE = re.compile(r"samples-([1-9][0-9]{0,15})\.json\Z")
_SESSION = re.compile(r"[0-9a-f]{32}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_last_attempt: dict[str, float] = {}
_tasks: dict[str, asyncio.Task[None]] = {}
_imported: dict[str, dict[str, str]] = {}
_verified_latest: dict[str, dict[str, Any] | None] = {}
_backfill_retry: dict[str, dict[str, tuple[str, float]]] = {}
_collector: dict[str, Any] | None = None
_shutting_down = False


def _object(value: Any, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("invalid temperature object")
    return value


def _integer(value: Any, *, minimum: int = 0, maximum: int = MAX_INTEGER) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("invalid temperature count")
    return value


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not 20 <= len(value) <= 40:
        raise ValueError("invalid temperature timestamp")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("invalid temperature timestamp")
    return result.astimezone(UTC)


def attributable(row: dict[str, Any], *, now: datetime | None = None) -> bool:
    """Raw clock failures remain on-camera, never relabelled with server receipt time."""
    at = _timestamp(row["captured_at_utc"])
    return 2020 <= at.year <= 2100 and at <= (now or datetime.now(UTC)) + timedelta(minutes=5)


def _json(raw: bytes, maximum: int) -> Any:
    if len(raw) > maximum:
        raise ValueError("temperature document exceeds limit")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate temperature key")
            result[key] = value
        return result

    def invalid(_value):
        raise ValueError("invalid temperature number")

    return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=invalid)


def validate_reading(value: Any, *, now: datetime | None = None) -> dict[str, Any]:
    row = _object(
        value,
        {
            "sequence",
            "session_id",
            "captured_at_utc",
            "elapsed_realtime_ms",
            "soc_millic",
            "quality",
        },
    )
    _integer(row["sequence"], minimum=1)
    _integer(row["elapsed_realtime_ms"])
    if not isinstance(row["session_id"], str) or not _SESSION.fullmatch(row["session_id"]):
        raise ValueError("invalid temperature session")
    # Retained captures can precede a wall-clock correction. Preserve their stated UTC;
    # the graph's requested range excludes future rows without inventing replacement time.
    _timestamp(row["captured_at_utc"])
    if row["quality"] == "valid":
        _integer(row["soc_millic"], minimum=-40_000, maximum=200_000)
    elif row["quality"] != "unavailable" or row["soc_millic"] is not None:
        raise ValueError("invalid temperature quality")
    return row


def validate_manifest(raw: bytes, *, now: datetime | None = None) -> dict[str, Any]:
    row = _object(
        _json(raw, MAX_MANIFEST_BYTES),
        {
            "schema_version",
            "source",
            "sensor",
            "capture_interval_ms",
            "generated_at_utc",
            "collector_state",
            "retained_readings",
            "chunks",
            "latest",
        },
    )
    if (
        type(row["schema_version"]) is not int
        or row["schema_version"] != 1
        or row["source"] != SOURCE
        or row["sensor"] != SENSOR
        or type(row["capture_interval_ms"]) is not int
        or row["capture_interval_ms"] != INTERVAL_MS
        or row["collector_state"]
        not in {"running", "sensor_unavailable", "storage_unavailable", "clock_unavailable"}
    ):
        raise ValueError("invalid temperature manifest identity")
    _timestamp(row["generated_at_utc"])
    retained = _integer(row["retained_readings"], maximum=MAX_READINGS)
    if not isinstance(row["chunks"], list) or len(row["chunks"]) > MAX_CHUNKS:
        raise ValueError("invalid temperature chunks")
    previous = 0
    for chunk in row["chunks"]:
        _object(chunk, {"file", "first_sequence", "last_sequence", "size_bytes", "sha256"})
        first = _integer(chunk["first_sequence"], minimum=1)
        last = _integer(chunk["last_sequence"], minimum=first)
        _integer(chunk["size_bytes"], minimum=1, maximum=MAX_CHUNK_BYTES)
        match = _FILE.fullmatch(chunk["file"]) if isinstance(chunk["file"], str) else None
        if not match or int(match[1]) != first or first <= previous:
            raise ValueError("invalid temperature chunk range")
        if not isinstance(chunk["sha256"], str) or not _HASH.fullmatch(chunk["sha256"]):
            raise ValueError("invalid temperature chunk digest")
        previous = last
    if row["latest"] is None:
        if row["chunks"] or retained:
            raise ValueError("temperature latest is missing")
    else:
        latest = validate_reading(row["latest"], now=now)
        if not row["chunks"] or latest["sequence"] != previous or not retained:
            raise ValueError("inconsistent temperature latest")
    if not len(row["chunks"]) <= retained <= len(row["chunks"]) * MAX_CHUNK_READINGS:
        raise ValueError("inconsistent temperature retention count")
    return row


def validate_chunk(raw: bytes, descriptor: dict[str, Any], *, now=None) -> list[dict[str, Any]]:
    if (
        len(raw) != descriptor["size_bytes"]
        or hashlib.sha256(raw).hexdigest() != descriptor["sha256"]
    ):
        raise ValueError("temperature chunk changed")
    body = _object(_json(raw, MAX_CHUNK_BYTES), {"schema_version", "readings"})
    if type(body["schema_version"]) is not int or body["schema_version"] != 1:
        raise ValueError("invalid temperature chunk version")
    rows = body["readings"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_CHUNK_READINGS:
        raise ValueError("invalid temperature reading count")
    previous = 0
    elapsed_by_session: dict[str, int] = {}
    for item in rows:
        validate_reading(item, now=now)
        if item["sequence"] <= previous:
            raise ValueError("temperature sequence regressed")
        previous = item["sequence"]
        elapsed = elapsed_by_session.get(item["session_id"])
        if elapsed is not None and item["elapsed_realtime_ms"] <= elapsed:
            raise ValueError("temperature elapsed time regressed")
        elapsed_by_session[item["session_id"]] = item["elapsed_realtime_ms"]
    if (
        rows[0]["sequence"] != descriptor["first_sequence"]
        or previous != descriptor["last_sequence"]
    ):
        raise ValueError("temperature chunk range changed")
    return rows


def parsed_line(row: dict[str, Any]) -> unit_logs.ParsedLine:
    value = "na" if row["soc_millic"] is None else f"{row['soc_millic'] / 1000:.3f}"
    message = (
        f"sample={row['session_id']}-{row['sequence']} session={row['session_id']} "
        f"source={SOURCE} sensor={SENSOR} sequence={row['sequence']} "
        f"elapsed_ms={row['elapsed_realtime_ms']} soc={value} | event=temperature_capture"
    )
    return unit_logs.ParsedLine(_timestamp(row["captured_at_utc"]), 0, 0, "I", TAG, message)


def _root() -> str:
    return _safe_remote_path(
        str(PurePosixPath(get_config().obd_remote_status_file).parent / "temperature")
    )


def _read_command(path: str, offset: int, length: int) -> str:
    path = _safe_remote_path(path)
    _integer(offset, maximum=MAX_CHUNK_BYTES)
    _integer(length, minimum=1, maximum=FRAGMENT_BYTES)
    if offset % FRAGMENT_BYTES:
        raise ValueError("unaligned temperature read")
    # A fixed capped fragment never exceeds the unit's small shell response ceiling.
    return (
        f'p=\'{path}\'; [ ! -L "$p" ] && [ -f "$p" ] && [ -r "$p" ] || exit 0; '
        'exec 3<"$p" || exit 1; n=$(stat -L -c "%s" /proc/self/fd/0 <&3) || exit 1; '
        f"printf 'TEMP1 %s\\n' \"$n\"; dd bs={FRAGMENT_BYTES} skip={offset // FRAGMENT_BYTES} "
        f"count=1 <&3 2>/dev/null | head -c {length} | base64; printf '\\nTEMP_END\\n'"
    )


async def _read_file(address: str, path: str, size: int | None = None) -> bytes | None:
    limit = size if size is not None else MAX_MANIFEST_BYTES + 1
    result = bytearray()
    actual_size = None
    for offset in range(0, limit, FRAGMENT_BYTES):
        length = min(FRAGMENT_BYTES, limit - offset)
        raw = await adb.shell(
            address, _read_command(path, offset, length), timeout=REQUEST_TIMEOUT_S
        )
        if isinstance(raw, adb.AdbResult):
            raw = raw.stdout
        if not raw.strip() and offset == 0:
            return None
        if len(raw) > FRAGMENT_BYTES * 2 + 100:
            raise ValueError("temperature response exceeds limit")
        lines = raw.strip().splitlines()
        header = re.fullmatch(r"TEMP1 ([0-9]{1,10})", lines[0]) if lines else None
        if header is None or lines[-1] != "TEMP_END":
            raise ValueError("incomplete temperature response")
        reported_size = int(header[1])
        maximum = size if size is not None else MAX_MANIFEST_BYTES
        if reported_size > maximum or (size is not None and reported_size != size):
            raise ValueError("temperature file size exceeds declared bound")
        if actual_size is not None and reported_size != actual_size:
            raise ValueError("temperature file changed during read")
        actual_size = reported_size
        payload = base64.b64decode("".join(lines[1:-1]), validate=True)
        if len(payload) != min(length, max(0, actual_size - offset)):
            raise ValueError("temperature response size changed")
        result.extend(payload)
        if size is None and len(payload) < length:
            break
    return bytes(result)


def _publish_collector(manifest: dict[str, Any]) -> None:
    global _collector
    latest = manifest["latest"]
    clock_valid = latest is not None and attributable(latest)
    state = manifest["collector_state"]
    if (
        state == "clock_unavailable"
        or not attributable({"captured_at_utc": manifest["generated_at_utc"]})
        or (latest is not None and not clock_valid)
    ):
        state = "clock_unavailable"
        clock_valid = False
    _collector = {
        "schema_version": 1,
        "source": SOURCE,
        "sensor": SENSOR,
        "state": state,
        "last_captured_at_utc": latest["captured_at_utc"] if clock_valid else None,
        "last_captured_elapsed_ms": latest["elapsed_realtime_ms"] if clock_valid else None,
        "interval_ms": INTERVAL_MS,
        "retained_readings": manifest["retained_readings"],
        "error": "clock_unavailable"
        if state == "clock_unavailable"
        else "sensor_unavailable"
        if state == "sensor_unavailable"
        else "mirror_unavailable"
        if state == "storage_unavailable"
        else None,
        "checked_at_utc": datetime.now(UTC).isoformat(),
    }


async def _import_chunk(
    address: str,
    item: dict[str, Any],
    latest: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], tuple[int, int]]:
    async with asyncio.timeout(CHUNK_TIMEOUT_S):
        data = await _read_file(address, f"{_root()}/{item['file']}", item["size_bytes"])
        if data is None:
            raise ValueError("temperature chunk disappeared")
        readings = validate_chunk(data, item)
        if latest is not None and readings[-1] != latest:
            raise ValueError("temperature latest disagrees with verified chunk")
        entries = [parsed_line(row) for row in readings if attributable(row)]
        result = await unit_logs.store(entries, busy_timeout_ms=200) if entries else (0, 0)
        return readings, result


async def recover(address: str) -> tuple[int, int]:
    """Import at most four changed chunks, newest first, then backfill older history."""
    raw = await _read_file(address, f"{_root()}/manifest.json")
    if raw is None:
        return 0, 0  # Older companions do not have this journal.
    manifest = validate_manifest(raw)
    cache = _imported.setdefault(address, {})
    retry = _backfill_retry.setdefault(address, {})
    current = {item["file"] for item in manifest["chunks"]}
    for name in set(cache) - current:
        cache.pop(name, None)
    for name in set(retry) - current:
        retry.pop(name, None)
    chunks = manifest["chunks"]
    if not chunks:
        _verified_latest[address] = None

    def latest_verified():
        return manifest["latest"] == _verified_latest.get(address) and (
            not chunks or cache.get(chunks[-1]["file"]) == chunks[-1]["sha256"]
        )

    if latest_verified():
        _publish_collector(manifest)
    ordered = [*chunks[-1:], *chunks[:-1]]
    changed = [
        item
        for item in ordered
        if cache.get(item["file"]) != item["sha256"]
        and (
            item is chunks[-1]
            or item["file"] not in retry
            or retry[item["file"]][0] != item["sha256"]
            or time.monotonic() >= retry[item["file"]][1]
        )
    ]
    accepted = duplicates = 0
    backfill_failures = 0
    latest_error = None
    for item in changed[:MAX_CHANGED_CHUNKS]:
        try:
            readings, (new, duplicate) = await _import_chunk(
                address,
                item,
                manifest["latest"] if item is chunks[-1] else None,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if item is chunks[-1]:
                # Current capture needs its own verified bytes and successful commit.
                # Keep its old check timestamp while allowing independent backfill.
                latest_error = exc
                continue
            backfill_failures += 1
            retry[item["file"]] = (item["sha256"], time.monotonic() + BACKFILL_RETRY_S)
            continue
        accepted += new
        duplicates += duplicate
        # Only a successfully committed database import can suppress the next read.
        cache[item["file"]] = item["sha256"]
        retry.pop(item["file"], None)
        if item is chunks[-1]:
            _verified_latest[address] = readings[-1]
            # Publish before unrelated old history can time out or be cancelled.
            _publish_collector(manifest)
    if not latest_verified():
        if latest_error is not None:
            raise latest_error
        raise ValueError("temperature latest was not verified")
    if backfill_failures:
        log.debug("older temperature chunks remain on camera for retry", chunks=backfill_failures)
    return accepted, duplicates


def collector_snapshot() -> dict[str, Any] | None:
    return dict(_collector) if _collector else None


def on_unit_present(address: str) -> None:
    if (
        _shutting_down
        or get_status().ignition_state != "off"
        or adb.optional_requests_deferred(address)
        or address in _tasks
    ):
        return
    now = time.monotonic()
    if now - _last_attempt.get(address, -REFRESH_S) < REFRESH_S:
        return
    _last_attempt[address] = now
    task = asyncio.create_task(_refresh(address), name="ingest-temperature-journal")
    _tasks[address] = task

    def completed(done: asyncio.Task[None]) -> None:
        if _tasks.get(address) is done:
            _tasks.pop(address, None)
        if not done.cancelled():
            done.exception()  # _refresh handles bounded errors without payload logging.

    task.add_done_callback(completed)


async def _refresh(address: str) -> None:
    try:
        async with asyncio.timeout(TOTAL_TIMEOUT_S):
            await recover(address)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.debug("temperature journal import deferred; retained on camera for retry")
    finally:
        _last_attempt[address] = time.monotonic()


async def shutdown() -> None:
    global _shutting_down
    _shutting_down = True
    try:
        tasks = list(_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        _tasks.clear()
        _last_attempt.clear()
        _shutting_down = False
