"""Mirror the Android logger's bounded lifecycle event stream.

The app owns this stream.  It publishes a rolling, atomically replaced ``events.json``
beside ``status.json``; the server only validates, deduplicates and retains a longer
operator-facing history.  Event content is intentionally code-and-number only.  Free-form
messages, Bluetooth addresses, SSIDs, VINs, adapter replies and exception strings are not
part of the contract and therefore cannot leak into the API or Logs UI.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.core.logging import get_logger
from app.db.models import OBDLoggerEvent, utcnow
from app.db.session import session_scope
from app.ingest import adb
from app.ingest.obd_bundle import SAFE_DRIVE_ID

log = get_logger(__name__)

CAPABILITY = "app_event_stream_v1"
MAX_REMOTE_EVENT_BYTES = 512 * 1024
MAX_EVENTS_PER_SNAPSHOT = 512
MAX_SERVER_EVENTS = 50_000
SERVER_RETENTION_DAYS = 90
# The ADB file read already has a five-second ceiling. Leave one additional second
# for validation, the insert and retention pruning, while keeping observability off
# the backup critical path when any one of those stages becomes stuck.
EVENT_SYNC_TIMEOUT_SECONDS = 6.0

EVENT_KINDS = frozenset(
    {
        "app.boot",
        "app.service",
        "network.wifi",
        "power.sleep_window",
        "obd.ble_connection",
        "obd.elm_session",
        "obd.ecu_session",
        "obd.poll_health",
        "drive.lifecycle",
        "ingest.handoff",
        "radio.observation",
        "bundle.export",
        "receipt.verification",
    }
)
EVENT_LEVELS = frozenset({"info", "warning", "error"})
EVENT_OUTCOMES = frozenset(
    {
        "started",
        "succeeded",
        "failed",
        "retrying",
        "connected",
        "disconnected",
        "available",
        "lost",
        "requested",
        "acknowledged",
        "resumed",
        "verified",
        "changed",
        "skipped",
        "completed",
        "interrupted",
        "recovered",
        "observed",
        "pruned",
    }
)
EVENT_REASON_CODES = frozenset(
    {
        "boot_completed",
        "package_replaced",
        "service_started",
        "service_stopped",
        "start_command",
        "uncaught_restart",
        "wifi_available",
        "wifi_lost",
        "default_network_changed",
        "backup_active",
        "wifi_connected",
        "wifi_disconnected",
        "server_owned",
        "ingestion_state_unknown",
        "property_refused",
        "readback_unavailable",
        "readback_mismatch",
        "scheduled_connect",
        "adapter_discovered",
        "adapter_not_found",
        "gatt_connected",
        "gatt_disconnected",
        "gatt_error",
        "gatt_timeout",
        "services_ready",
        "notifications_ready",
        "elm_ready",
        "elm_timeout",
        "protocol_search_failed",
        "adapter_voltage_valid",
        "ecu_proof_valid",
        "ecu_offline",
        "engine_running",
        "engine_stopped",
        "voltage_below_start",
        "voltage_below_stop",
        "connection_lost",
        "connection_failed",
        "backoff_scheduled",
        "retry_woken",
        "ble_callback",
        "sleep_wake",
        "engine_detected",
        "device_restart",
        "ingestion_requested",
        "request_observed",
        "quiesce_entered",
        "quiesce_acknowledged",
        "resume_observed",
        "resume_completed",
        "request_expired",
        "bluetooth_on",
        "bluetooth_off",
        "screen_on",
        "user_present",
        "power_connected",
        "acc_on",
        "acc_off",
        "hotspot_on",
        "hotspot_off",
        "state_unknown",
        "export_started",
        "export_completed",
        "export_failed",
        "receipt_verified",
        "receipt_invalid",
        "retention_pruned",
        "first_sample_persisted",
        "drive_summary",
        "cadence_gap",
        "poll_timeout",
        "manual_request",
        "configuration_disabled",
        "storage_unavailable",
        "permission_denied",
        "unknown",
    }
)

# Numeric-only evidence.  Per-key ceilings reject both accidental unit mistakes and
# attacker-sized values while keeping ordinary millisecond counters comfortably inside.
EVENT_METRIC_LIMITS: dict[str, tuple[float, float]] = {
    "elapsed_ms": (0, 7 * 24 * 60 * 60 * 1000),
    "attempt": (0, 10_000),
    "retry_delay_ms": (0, 24 * 60 * 60 * 1000),
    "scan_ms": (0, 60 * 60 * 1000),
    "connect_ms": (0, 60 * 60 * 1000),
    "discovery_ms": (0, 60 * 60 * 1000),
    "subscribe_ms": (0, 60 * 60 * 1000),
    "elm_init_ms": (0, 60 * 60 * 1000),
    "ecu_probe_ms": (0, 60 * 60 * 1000),
    "first_sample_ms": (0, 60 * 60 * 1000),
    "poll_cycle_ms": (0, 60 * 60 * 1000),
    "polling_duty_cycle_percent": (0, 100),
    "sleep_target_s": (0, 3_600),
    "sleep_observed_s": (0, 3_600),
    "wifi_frequency_mhz": (0, 10_000),
    "sample_count": (0, 1_000_000_000),
    "pending_bundle_count": (0, 1_000_000),
    "bundle_bytes": (0, 512 * 1024 * 1024),
    "receipt_count": (0, 1_000_000),
    "gap_count": (0, 1_000_000_000),
    "timeout_count": (0, 1_000_000_000),
    "command_count": (0, 1_000_000_000),
    "consecutive_failures": (0, 1_000_000),
    "queue_depth": (0, 1_000_000),
}

_TOP_LEVEL_KEYS = frozenset(
    {
        "schema_version",
        "source_id",
        "generated_at_utc",
        "first_sequence",
        "last_sequence",
        "producer",
        "events",
    }
)
_PRODUCER_KEYS = frozenset({"app_version_name", "app_version_code", "build_git_sha"})
_EVENT_KEYS = frozenset(
    {
        "sequence",
        "occurred_at_utc",
        "session_id",
        "kind",
        "level",
        "outcome",
        "reason_code",
        "drive_id",
        "metrics",
    }
)
_EVENT_FILE_PREFIX = "__DASHCAM_LOGGER_EVENT_FILE__\n"
_EVENT_FILE_MISSING = "__DASHCAM_LOGGER_EVENT_FILE_MISSING__"


class EventStreamError(ValueError):
    """A public event snapshot did not match the bounded v1 contract."""


@dataclass(frozen=True, slots=True)
class ValidatedLoggerEvent:
    sequence: int
    occurred_at: datetime
    session_id_hash: str
    kind: str
    level: str
    outcome: str
    reason_code: str | None
    drive_id: str | None
    metrics: dict[str, int | float]


@dataclass(frozen=True, slots=True)
class ValidatedEventSnapshot:
    source_id_hash: str
    generated_at: datetime
    app_version_name: str
    app_version_code: int
    build_git_sha: str
    events: tuple[ValidatedLoggerEvent, ...]


@dataclass(slots=True)
class EventSyncResult:
    available: bool = False
    accepted: int = 0
    duplicates: int = 0
    sequence_gap: int = 0
    error: str | None = None
    seconds: float = 0.0


class LoggerEventStatus:
    """Small in-memory read/sync health snapshot for ``/api/obd/status``."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.checked_at: datetime | None = None
        self.last_received_at: datetime | None = None
        self.available = False
        self.accepted = 0
        self.duplicates = 0
        self.sequence_gap = 0
        self.last_error: str | None = None

    def finish(self, result: EventSyncResult) -> None:
        now = utcnow()
        with self._lock:
            self.checked_at = now
            self.available = result.available
            self.accepted = result.accepted
            self.duplicates = result.duplicates
            # A later duplicate snapshot reports no *new* gap. It is not evidence
            # that rows already missed by the rolling on-unit ring have reappeared.
            # Accumulate newly observed gaps until an explicit process/test reset.
            self.sequence_gap += max(0, result.sequence_gap)
            self.last_error = result.error
            if result.accepted or result.duplicates:
                self.last_received_at = now

    def reset(self) -> None:
        """Clear process-local health, primarily for isolated lifecycle/test setup."""
        with self._lock:
            self.checked_at = None
            self.last_received_at = None
            self.available = False
            self.accepted = 0
            self.duplicates = 0
            self.sequence_gap = 0
            self.last_error = None

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "available": self.available,
                "checked_at": self.checked_at.isoformat() if self.checked_at else None,
                "last_received_at": (
                    self.last_received_at.isoformat() if self.last_received_at else None
                ),
                "accepted": self.accepted,
                "duplicates": self.duplicates,
                "sequence_gap": self.sequence_gap,
                "last_error": self.last_error,
            }


_status = LoggerEventStatus()


def get_logger_event_status() -> LoggerEventStatus:
    return _status


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise EventStreamError("event stream JSON contains a duplicate key")
        result[key] = value
    return result


def _exact_keys(value: dict[str, object], expected: frozenset[str], label: str) -> None:
    if value.keys() != expected:
        raise EventStreamError(f"{label} fields do not match schema v1")


def _timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        raise EventStreamError(f"{label} must be a bounded ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EventStreamError(f"{label} is not a valid ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise EventStreamError(f"{label} must include a timezone")
    parsed = parsed.astimezone(UTC)
    if parsed < datetime(2020, 1, 1, tzinfo=UTC) or parsed > utcnow() + timedelta(days=1):
        raise EventStreamError(f"{label} is outside the accepted clock range")
    return parsed


def _uuid_hash(value: object, label: str) -> str:
    if not isinstance(value, str) or len(value) != 36:
        raise EventStreamError(f"{label} must be a canonical random UUID")
    try:
        parsed = uuid.UUID(value)
    except ValueError as exc:
        raise EventStreamError(f"{label} must be a canonical random UUID") from exc
    if str(parsed) != value or parsed.version not in {4, 7}:
        raise EventStreamError(f"{label} must be a lowercase random UUID")
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _positive_int(value: object, label: str, *, allow_zero: bool = False) -> int:
    minimum = 0 if allow_zero else 1
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= 2**63 - 1:
        raise EventStreamError(f"{label} is outside the supported integer range")
    return value


def _metrics(value: object) -> dict[str, int | float]:
    if not isinstance(value, dict) or len(value) > len(EVENT_METRIC_LIMITS):
        raise EventStreamError("event metrics must be a bounded object")
    clean: dict[str, int | float] = {}
    for key, metric in value.items():
        bounds = EVENT_METRIC_LIMITS.get(key)
        if bounds is None:
            raise EventStreamError("event metrics contain an unsupported field")
        if isinstance(metric, bool) or not isinstance(metric, (int, float)):
            raise EventStreamError("event metrics must contain numeric scalars")
        try:
            numeric = float(metric)
        except (OverflowError, ValueError) as exc:
            raise EventStreamError("event metric is outside its supported range") from exc
        if not math.isfinite(numeric) or not bounds[0] <= numeric <= bounds[1]:
            raise EventStreamError("event metric is outside its supported range")
        clean[key] = metric
    return clean


def validate_event_snapshot(raw: str) -> ValidatedEventSnapshot:
    """Parse one exact schema-v1 snapshot without retaining identifiers or free text."""
    try:
        body = json.loads(raw, object_pairs_hook=_strict_object)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise EventStreamError("event stream JSON is invalid") from exc
    if not isinstance(body, dict):
        raise EventStreamError("event stream must be an object")
    _exact_keys(body, _TOP_LEVEL_KEYS, "event stream")
    schema_version = body["schema_version"]
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version != 1
    ):
        raise EventStreamError("event stream schema version is unsupported")

    source_id_hash = _uuid_hash(body["source_id"], "source_id")
    generated_at = _timestamp(body["generated_at_utc"], "generated_at_utc")
    first_sequence = _positive_int(body["first_sequence"], "first_sequence", allow_zero=True)
    last_sequence = _positive_int(body["last_sequence"], "last_sequence", allow_zero=True)

    producer = body["producer"]
    if not isinstance(producer, dict):
        raise EventStreamError("producer must be an object")
    _exact_keys(producer, _PRODUCER_KEYS, "producer")
    app_version_name = producer["app_version_name"]
    if (
        not isinstance(app_version_name, str)
        or not 1 <= len(app_version_name) <= 64
        or any(
            char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._+-"
            for char in app_version_name
        )
    ):
        raise EventStreamError("producer app version is invalid")
    app_version_code = _positive_int(producer["app_version_code"], "app_version_code")
    if app_version_code > 2_147_483_647:
        raise EventStreamError("producer app version code is invalid")
    build_git_sha = producer["build_git_sha"]
    if not isinstance(build_git_sha, str) or not (
        build_git_sha == "unknown"
        or (len(build_git_sha) == 12 and all(char in "0123456789abcdef" for char in build_git_sha))
    ):
        raise EventStreamError("producer build SHA is invalid")

    raw_events = body["events"]
    if not isinstance(raw_events, list) or len(raw_events) > MAX_EVENTS_PER_SNAPSHOT:
        raise EventStreamError("events must be a bounded array")
    events: list[ValidatedLoggerEvent] = []
    previous_sequence = 0
    for raw_event in raw_events:
        if not isinstance(raw_event, dict):
            raise EventStreamError("each event must be an object")
        _exact_keys(raw_event, _EVENT_KEYS, "event")
        sequence = _positive_int(raw_event["sequence"], "event sequence")
        if sequence <= previous_sequence:
            raise EventStreamError("event sequences must be strictly increasing")
        previous_sequence = sequence
        kind = raw_event["kind"]
        level = raw_event["level"]
        outcome = raw_event["outcome"]
        reason_code = raw_event["reason_code"]
        drive_id = raw_event["drive_id"]
        if not isinstance(kind, str) or kind not in EVENT_KINDS:
            raise EventStreamError("event kind is unsupported")
        if not isinstance(level, str) or level not in EVENT_LEVELS:
            raise EventStreamError("event level is unsupported")
        if not isinstance(outcome, str) or outcome not in EVENT_OUTCOMES:
            raise EventStreamError("event outcome is unsupported")
        if reason_code is not None and (
            not isinstance(reason_code, str) or reason_code not in EVENT_REASON_CODES
        ):
            raise EventStreamError("event reason code is unsupported")
        if drive_id is not None and (
            not isinstance(drive_id, str) or not SAFE_DRIVE_ID.fullmatch(drive_id)
        ):
            raise EventStreamError("event drive id is invalid")
        events.append(
            ValidatedLoggerEvent(
                sequence=sequence,
                occurred_at=_timestamp(raw_event["occurred_at_utc"], "event occurred_at_utc"),
                session_id_hash=_uuid_hash(raw_event["session_id"], "event session_id"),
                kind=kind,
                level=level,
                outcome=outcome,
                reason_code=reason_code,
                drive_id=drive_id,
                metrics=_metrics(raw_event["metrics"]),
            )
        )

    if events:
        if first_sequence != events[0].sequence or last_sequence != events[-1].sequence:
            raise EventStreamError("event sequence bounds do not match the snapshot")
    elif first_sequence != 0 or last_sequence != 0:
        raise EventStreamError("an empty event stream must use zero sequence bounds")

    return ValidatedEventSnapshot(
        source_id_hash=source_id_hash,
        generated_at=generated_at,
        app_version_name=app_version_name,
        app_version_code=app_version_code,
        build_git_sha=build_git_sha,
        events=tuple(events),
    )


async def read_remote_event_snapshot(address: str, path: str) -> str | None:
    """Read one optional regular file with a hard byte ceiling."""
    from app.ingest.obd_transfer import _safe_remote_path

    safe_path = _safe_remote_path(path)
    command = (
        f"if [ -f '{safe_path}' ] && [ ! -L '{safe_path}' ]; then "
        f"printf '{_EVENT_FILE_PREFIX}'; head -c {MAX_REMOTE_EVENT_BYTES + 1} '{safe_path}'; "
        f"else printf '{_EVENT_FILE_MISSING}'; fi; exit 0"
    )
    raw = await adb.shell(address, command, timeout=5.0)
    if raw == _EVENT_FILE_MISSING:
        return None
    if raw.startswith(_EVENT_FILE_PREFIX):
        raw = raw[len(_EVENT_FILE_PREFIX) :]
    if len(raw.encode("utf-8")) > MAX_REMOTE_EVENT_BYTES:
        raise EventStreamError("event stream exceeds its size limit")
    return raw


async def _prune_server_events() -> None:
    cutoff = utcnow() - timedelta(days=SERVER_RETENTION_DAYS)
    async with session_scope() as session:
        await session.execute(delete(OBDLoggerEvent).where(OBDLoggerEvent.occurred_at < cutoff))
        total = int((await session.execute(select(func.count(OBDLoggerEvent.id)))).scalar() or 0)
        excess = total - MAX_SERVER_EVENTS
        if excess <= 0:
            return
        victim_ids = (
            (
                await session.execute(
                    select(OBDLoggerEvent.id)
                    .order_by(OBDLoggerEvent.occurred_at.asc(), OBDLoggerEvent.id.asc())
                    .limit(excess)
                )
            )
            .scalars()
            .all()
        )
        if victim_ids:
            await session.execute(delete(OBDLoggerEvent).where(OBDLoggerEvent.id.in_(victim_ids)))


async def store_event_snapshot(snapshot: ValidatedEventSnapshot) -> tuple[int, int, int]:
    """Store unseen sequence rows and return accepted, duplicate and missing counts."""
    if not snapshot.events:
        await _prune_server_events()
        return 0, 0, 0

    sequences = [event.sequence for event in snapshot.events]
    async with session_scope() as session:
        latest = (
            await session.execute(
                select(func.max(OBDLoggerEvent.sequence)).where(
                    OBDLoggerEvent.source_id_hash == snapshot.source_id_hash
                )
            )
        ).scalar()
        statement = (
            sqlite_insert(OBDLoggerEvent)
            .values(
                [
                    {
                        "source_id_hash": snapshot.source_id_hash,
                        "sequence": event.sequence,
                        "occurred_at": event.occurred_at,
                        "session_id_hash": event.session_id_hash,
                        "kind": event.kind,
                        "level": event.level,
                        "outcome": event.outcome,
                        "reason_code": event.reason_code,
                        "drive_id": event.drive_id,
                        "metrics_json": event.metrics or None,
                        "app_version_name": snapshot.app_version_name,
                        "app_version_code": snapshot.app_version_code,
                        "build_git_sha": snapshot.build_git_sha,
                    }
                    for event in snapshot.events
                ]
            )
            .on_conflict_do_nothing(
                index_elements=[OBDLoggerEvent.source_id_hash, OBDLoggerEvent.sequence]
            )
        )
        result = await session.execute(statement)
        accepted = max(0, int(result.rowcount or 0))

    expected = 1 if latest is None else int(latest) + 1
    sequence_gap = 0
    for sequence in sequences:
        if sequence < expected:
            continue
        if sequence > expected:
            sequence_gap += sequence - expected
        expected = sequence + 1
    await _prune_server_events()
    return accepted, len(snapshot.events) - accepted, sequence_gap


async def sync_remote_events(
    address: str,
    path: str,
    *,
    timeout_seconds: float = EVENT_SYNC_TIMEOUT_SECONDS,
) -> EventSyncResult:
    """Fail-soft read/validate/store used alongside every normal head-unit visit."""
    started = time.monotonic()
    result = EventSyncResult()
    try:
        # This is deliberately one deadline around the *whole* mirror. Per-command
        # ADB timeouts do not cover a wedged validator, database write or prune.
        async with asyncio.timeout(timeout_seconds):
            raw = await read_remote_event_snapshot(address, path)
            if raw is None:
                return result
            result.available = True
            snapshot = validate_event_snapshot(raw)
            result.accepted, result.duplicates, result.sequence_gap = await store_event_snapshot(
                snapshot
            )
            if result.sequence_gap:
                log.warning(
                    "the app event snapshot starts after unseen sequence rows",
                    missing=result.sequence_gap,
                )
    except TimeoutError:
        result.error = "timeout"
        log.warning("could not mirror the app event stream", category=result.error)
    except adb.AdbError:
        result.error = "device_unreachable"
        log.warning("could not mirror the app event stream", category=result.error)
    except EventStreamError:
        result.error = "invalid_snapshot"
        log.warning("could not mirror the app event stream", category=result.error)
    except OSError:
        result.error = "storage_error"
        log.warning("could not mirror the app event stream", category=result.error)
    except Exception:
        # Observability is never a durability gate. Keep unexpected database/runtime
        # failures fail-soft too, but never surface their text through status or logs.
        result.error = "internal_error"
        log.warning("could not mirror the app event stream", category=result.error)
    finally:
        result.seconds = time.monotonic() - started
        _status.finish(result)
    return result


__all__ = [
    "CAPABILITY",
    "EVENT_KINDS",
    "EVENT_LEVELS",
    "EVENT_METRIC_LIMITS",
    "EVENT_OUTCOMES",
    "EVENT_REASON_CODES",
    "EVENT_SYNC_TIMEOUT_SECONDS",
    "MAX_EVENTS_PER_SNAPSHOT",
    "MAX_REMOTE_EVENT_BYTES",
    "EventStreamError",
    "EventSyncResult",
    "LoggerEventStatus",
    "get_logger_event_status",
    "read_remote_event_snapshot",
    "store_event_snapshot",
    "sync_remote_events",
    "validate_event_snapshot",
]
