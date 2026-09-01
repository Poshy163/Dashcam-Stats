"""Best-effort OBD bundle discovery/copy alongside the existing footage backup."""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.config import AppConfig, get_config
from app.core.logging import get_logger
from app.core.process_lock import ProcessFileLock, try_acquire
from app.db.models import OBDBundle, OBDBundleState, utcnow
from app.db.session import session_scope
from app.ingest import adb, transport
from app.ingest.ha_import_queue import get_import_worker, redact
from app.ingest.models import DeltaPlan, RemoteFile, UnitInfo, ingest_setting
from app.ingest.obd_bundle import (
    SAFE_DRIVE_ID,
    SHA256_RE,
    BundleConflict,
    BundleError,
    ValidatedBundle,
    file_sha256,
    is_bundle_name,
    store_rejected_bundle,
    store_validated_bundle,
    validate_bundle,
)
from app.ingest.status import IngestStatus

log = get_logger(__name__)

_SAFE_REMOTE_PATH = re.compile(r"^/[A-Za-z0-9._/-]{1,511}$")
MAX_BUNDLES_PER_WINDOW = 64
_REMOTE_RECLAIMABLE_STATES = {
    OBDBundleState.READY_TO_IMPORT.value,
    OBDBundleState.RETRY_WAIT.value,
    OBDBundleState.IMPORTING.value,
    OBDBundleState.IMPORTED.value,
}
_REMOTE_RECLAIMABLE_FAILED_KINDS = {
    "authentication",
    "configuration",
    "payload",
    "permanent",
    "protocol",
}
MAX_REMOTE_STATUS_BYTES = 64 * 1024
MAX_RECEIPT_BYTES = 512


def _sync_lock_path(config: AppConfig) -> Path:
    """A stable per-data-directory fence shared by every server process."""
    return config.obd_dir / ".transfer-sync.lock"


@dataclass(slots=True)
class OBDTransferResult:
    discovered: int = 0
    copied: int = 0
    duplicates: int = 0
    failed: int = 0
    missing: int = 0
    complete: bool = True
    removed_from_unit: int = 0
    bytes: int = 0
    seconds: float = 0.0
    error: str | None = None

    @property
    def throughput_mbs(self) -> float:
        return self.bytes / self.seconds / 1_000_000 if self.seconds > 0 else 0.0


class OBDTransferStatus:
    """Small redacted in-memory snapshot; durable queue state stays in SQLite."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.logger: dict[str, Any] | None = None
        self.logger_checked_at: datetime | None = None
        self.waiting_on_unit = 0
        self.current_bundle: str | None = None
        self.last_copy_at: datetime | None = None
        self.last_error: str | None = None
        self.last_throughput_mbs = 0.0

    def set_inventory(self, count: int) -> None:
        with self._lock:
            self.waiting_on_unit = count

    def set_current(self, name: str | None) -> None:
        with self._lock:
            self.current_bundle = name

    def set_logger(self, value: dict[str, Any] | None) -> None:
        with self._lock:
            self.logger = value
            self.logger_checked_at = datetime.now(UTC)

    def finish(self, result: OBDTransferResult) -> None:
        with self._lock:
            self.current_bundle = None
            self.waiting_on_unit = max(0, self.waiting_on_unit - result.removed_from_unit)
            if isinstance(self.logger, dict):
                logger_pending = self.logger.get("pending_bundle_count")
                if isinstance(logger_pending, int) and not isinstance(logger_pending, bool):
                    self.logger = {
                        **self.logger,
                        "pending_bundle_count": max(0, logger_pending - result.removed_from_unit),
                    }
            self.last_error = result.error
            self.last_throughput_mbs = result.throughput_mbs
            if result.copied or result.duplicates:
                self.last_copy_at = datetime.now(UTC)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            logger = dict(self.logger) if isinstance(self.logger, dict) else self.logger
            if isinstance(logger, dict):
                _reconcile_logger_voltage_freshness(logger)
            logger_pending = (
                logger.get("pending_bundle_count", 0) if isinstance(logger, dict) else 0
            )
            if not isinstance(logger_pending, int) or logger_pending < 0:
                logger_pending = 0
            return {
                "logger": logger,
                "logger_checked_at": (
                    self.logger_checked_at.isoformat() if self.logger_checked_at else None
                ),
                "waiting_on_unit": max(self.waiting_on_unit, logger_pending),
                "current_bundle_copy": self.current_bundle,
                "last_copy_at": self.last_copy_at.isoformat() if self.last_copy_at else None,
                "last_copy_error": self.last_error,
                "copy_throughput_mbs": round(self.last_throughput_mbs, 2),
            }


_status = OBDTransferStatus()


def get_obd_transfer_status() -> OBDTransferStatus:
    return _status


def _safe_remote_path(path: str) -> str:
    if not _SAFE_REMOTE_PATH.fullmatch(path) or ".." in path.split("/") or "'" in path:
        raise BundleError("configured remote OBD path is not a safe absolute Android path")
    return path.rstrip("/")


async def inventory_remote_bundles(address: str, source: str) -> list[RemoteFile]:
    """List only final .obd2.zip files; sibling .partial writes never match."""
    source = _safe_remote_path(source)
    command = (
        f"cd '{source}' 2>/dev/null && set -- *.obd2.zip && [ -e \"$1\" ] && "
        "stat -c '%s|%n|%Y' *.obd2.zip; exit 0"
    )
    files: list[RemoteFile] = []
    for line in (await adb.shell(address, command)).splitlines():
        parts = line.strip().split("|")
        if len(parts) != 3 or not is_bundle_name(parts[1].strip()):
            continue
        try:
            size = int(parts[0])
            mtime = int(parts[2])
        except ValueError:
            continue
        if size <= 0:
            continue
        files.append(RemoteFile(parts[1].strip(), size, mtime, source))
    # Logger drive ids are time-sortable, while mtime provides a conservative fallback
    # for recovered exports.  No newest-first override exists for telemetry: old drives
    # must not be starved by a growing queue.
    files.sort(key=lambda item: (item.name, item.mtime))
    return files


_LOGGER_KEYS = frozenset(
    {
        "schema_version",
        "logger_version",
        "app_version_name",
        "app_version_code",
        "poll_plan_version",
        "build_git_sha",
        "state",
        "ownership_enabled",
        "adapter_state",
        "adapter_reachable",
        "adapter_connected",
        "ecu_connected",
        "engine_running",
        "vehicle_state",
        "battery_voltage",
        "battery_voltage_source",
        "battery_voltage_sample_at_utc",
        "battery_voltage_fresh",
        "battery_voltage_raw_response",
        "battery_voltage_quality",
        "ble_owner",
        "head_unit_state",
        "voltage_only_mode",
        "wifi_connected",
        "acc_state_known",
        "acc_on",
        "ingestion_sleep_hold",
        "ingestion_sleep_hold_known",
        "sleep_window_policy",
        "sleep_window_target_s",
        "sleep_window_observed_s",
        "sleep_window_verified",
        "sleep_window_error",
        "current_drive_id",
        "last_drive_id",
        "last_drive_finished_at_utc",
        "last_sample_at_utc",
        "ingestion_request_id",
        "pending_bundle_count",
        "sample_count",
        "capabilities",
        "metrics",
        "last_error",
        "last_error_at_utc",
        "updated_at_utc",
    }
)

_LOGGER_CAPABILITY_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_LOGGER_METRIC_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
_LOGGER_APP_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
_LOGGER_BUILD_GIT_SHA_RE = re.compile(r"^(?:[0-9a-f]{12}|unknown)$")
_LOGGER_STATE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_LOGGER_SLEEP_WINDOW_POLICIES = frozenset(
    {
        "uninitialized",
        "awaiting_ingestion_state",
        "managed_active",
        "managed_idle",
        "server_owned",
    }
)
_LOGGER_SLEEP_WINDOW_TARGETS = frozenset({300, 900})
_LOGGER_SLEEP_WINDOW_ERRORS = frozenset(
    {
        "sleep countdown update was refused",
        "sleep countdown readback was unavailable",
        "sleep countdown readback did not match the requested value",
    }
)
MAX_LOGGER_CAPABILITIES = 32
MAX_LOGGER_METRICS = 64
LOGGER_VOLTAGE_FRESHNESS_SECONDS = 75
MAX_LOGGER_SLEEP_WINDOW_SECONDS = 3_600
LOGGER_PACKAGE = "com.dashcamstats.obdlogger"
_STATUS_FILE_PREFIX = "__DASHCAM_LOGGER_STATUS_FILE__\n"
_STATUS_NOT_INSTALLED = "__DASHCAM_LOGGER_NOT_INSTALLED__"
_STATUS_INSTALLED_MISSING = "__DASHCAM_LOGGER_INSTALLED_STATUS_MISSING__"
_STATUS_PACKAGE_CHECK_FAILED = "__DASHCAM_LOGGER_PACKAGE_CHECK_FAILED__"


def _clean_logger_capabilities(value: object) -> list[str] | None:
    """Return a small allowlisted capability vector, never attacker-sized input."""
    if not isinstance(value, list) or len(value) > MAX_LOGGER_CAPABILITIES:
        return None
    clean: list[str] = []
    for item in value:
        if not isinstance(item, str) or not _LOGGER_CAPABILITY_RE.fullmatch(item):
            return None
        if item not in clean:
            clean.append(item)
    return clean


def _clean_logger_metrics(value: object) -> dict[str, int | float] | None:
    """Keep only bounded finite scalar counters from the public logger status."""
    if not isinstance(value, dict) or len(value) > MAX_LOGGER_METRICS:
        return None
    clean: dict[str, int | float] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not _LOGGER_METRIC_RE.fullmatch(key):
            return None
        # bool is an int in Python, but it is not a useful counter and accepting it makes
        # schema mistakes look healthy.
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            return None
        if isinstance(item, float) and not math.isfinite(item):
            return None
        if item < 0 or item > 1_000_000_000_000:
            return None
        clean[key] = item
    return clean


def _clean_logger_timestamp(value: object) -> str | None:
    """Accept one bounded timezone-aware ISO timestamp and normalize it to UTC."""
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC).isoformat()


def _reconcile_logger_voltage_freshness(
    status: dict[str, Any],
    *,
    now: datetime | None = None,
) -> None:
    """Derive fresh/reachable from validated evidence, never a persisted app boolean."""
    schema_version = status.get("schema_version")
    if not isinstance(schema_version, int) or schema_version < 4:
        return
    sample_at = _clean_logger_timestamp(status.get("battery_voltage_sample_at_utc"))
    reference = now or utcnow()
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)
    age_seconds = (
        (reference.astimezone(UTC) - datetime.fromisoformat(sample_at)).total_seconds()
        if sample_at is not None
        else None
    )
    fresh = (
        isinstance(status.get("battery_voltage"), float)
        and status.get("battery_voltage_source") == "dashcam_elm_atrv"
        and status.get("battery_voltage_quality") == "valid"
        and age_seconds is not None
        and -5 <= age_seconds <= LOGGER_VOLTAGE_FRESHNESS_SECONDS
    )
    status["battery_voltage_fresh"] = fresh
    status["adapter_reachable"] = fresh
    if not fresh and status.get("battery_voltage_quality") == "valid":
        status["battery_voltage_quality"] = "stale" if sample_at is not None else "unavailable"


async def read_logger_status(address: str, path: str) -> dict[str, Any] | None:
    """Read a bounded status file and prove when no logger package is installed.

    A missing file alone is not authoritative: it also describes an installed app before
    first publication, an unavailable storage mount, or a deleted/corrupt status file.
    Only a successful package-manager lookup that positively reports the package absent
    permits callers to treat Bluetooth as unowned.
    """
    path = _safe_remote_path(path)
    # The marker separates package-manager evidence from an empty/missing status file.
    # Every interpolated value is a fixed constant or has passed the remote-path allowlist.
    command = (
        f"if [ -f '{path}' ] && [ ! -L '{path}' ]; then "
        f"printf '{_STATUS_FILE_PREFIX}'; head -c {MAX_REMOTE_STATUS_BYTES + 1} '{path}'; "
        f"else package_path=\"$(pm path '{LOGGER_PACKAGE}' 2>/dev/null)\"; "
        "package_rc=$?; "
        f"if [ \"$package_rc\" -ne 0 ]; then printf '{_STATUS_PACKAGE_CHECK_FAILED}'; "
        f"elif [ -n \"$package_path\" ]; then printf '{_STATUS_INSTALLED_MISSING}'; "
        f"else printf '{_STATUS_NOT_INSTALLED}'; fi; fi; exit 0"
    )
    try:
        raw = await adb.shell(
            address,
            command,
            timeout=5.0,
        )
    except adb.AdbError:
        # ``None`` is reserved for a positively observed absent file.  Conflating a
        # failed control-channel read with "no logger installed" lets the caller disable
        # Bluetooth while an unobserved logger may still own it.
        return {
            "state": "status_unavailable",
            "last_error": "logger status could not be read",
        }
    if raw == _STATUS_NOT_INSTALLED:
        return None
    if raw == _STATUS_INSTALLED_MISSING:
        return {
            "state": "status_unavailable",
            "last_error": "logger is installed but its status file is missing",
        }
    if raw == _STATUS_PACKAGE_CHECK_FAILED or not raw:
        return {
            "state": "status_unavailable",
            "last_error": "logger installation state could not be verified",
        }
    if raw.startswith(_STATUS_FILE_PREFIX):
        raw = raw[len(_STATUS_FILE_PREFIX) :]
    # Raw JSON remains accepted for unit fakes and older remote helpers. It is still an
    # affirmative file body, never evidence that a missing file means no owner.
    encoded = raw.encode("utf-8")
    if len(encoded) > MAX_REMOTE_STATUS_BYTES:
        return {
            "state": "invalid_status",
            "last_error": "logger status exceeds its size limit",
        }
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {"state": "invalid_status", "last_error": "logger status JSON is invalid"}
    if not isinstance(value, dict):
        return {"state": "invalid_status", "last_error": "logger status is not an object"}
    clean: dict[str, Any] = {}
    boolean_keys = {
        "ownership_enabled",
        "adapter_reachable",
        "adapter_connected",
        "ecu_connected",
        "engine_running",
        "battery_voltage_fresh",
        "voltage_only_mode",
        "wifi_connected",
        "acc_state_known",
        "acc_on",
        "ingestion_sleep_hold",
        "ingestion_sleep_hold_known",
        "sleep_window_verified",
    }
    for key, item in value.items():
        if key not in _LOGGER_KEYS:
            continue
        if key == "capabilities":
            capabilities = _clean_logger_capabilities(item)
            if capabilities is not None:
                clean[key] = capabilities
        elif key == "metrics":
            metrics = _clean_logger_metrics(item)
            if metrics is not None:
                clean[key] = metrics
        elif key == "app_version_name":
            if isinstance(item, str) and _LOGGER_APP_VERSION_RE.fullmatch(item):
                clean[key] = item
        elif key in {"state", "adapter_state"}:
            if isinstance(item, str) and _LOGGER_STATE_RE.fullmatch(item):
                clean[key] = item
        elif key == "schema_version":
            if isinstance(item, int) and not isinstance(item, bool) and 1 <= item <= 100:
                clean[key] = item
        elif key in {"app_version_code", "poll_plan_version"}:
            if isinstance(item, int) and not isinstance(item, bool) and 1 <= item <= 2_147_483_647:
                clean[key] = item
        elif key in {"pending_bundle_count", "sample_count"}:
            if isinstance(item, int) and not isinstance(item, bool) and 0 <= item <= 1_000_000_000:
                clean[key] = item
        elif key == "build_git_sha":
            if isinstance(item, str) and _LOGGER_BUILD_GIT_SHA_RE.fullmatch(item):
                clean[key] = item
        elif key == "battery_voltage":
            if (
                isinstance(item, (int, float))
                and not isinstance(item, bool)
                and math.isfinite(float(item))
                and 9.0 <= float(item) <= 16.5
            ):
                clean[key] = float(item)
        elif key == "battery_voltage_raw_response":
            if isinstance(item, str):
                match = re.fullmatch(r"([0-9]{1,2}(?:\.[0-9]+)?) V", item)
                if match and 9.0 <= float(match.group(1)) <= 16.5:
                    clean[key] = item
        elif key == "battery_voltage_source":
            if item in {"dashcam_elm_atrv"}:
                clean[key] = item
        elif key == "battery_voltage_sample_at_utc":
            if timestamp := _clean_logger_timestamp(item):
                clean[key] = timestamp
        elif key == "battery_voltage_quality":
            if item in {"valid", "stale", "invalid", "failed", "unavailable"}:
                clean[key] = item
        elif key == "ble_owner":
            if item in {
                "unowned",
                "dashcam_voltage_only",
                "dashcam_full_obd",
                "home_assistant_voltage_only",
                "phone_reserved",
                "transitioning",
                "conflict_detected",
            }:
                clean[key] = item
        elif key == "head_unit_state":
            if item in {"awake", "sleep_requested", "asleep", "unknown"}:
                clean[key] = item
        elif key == "sleep_window_policy":
            if isinstance(item, str) and item in _LOGGER_SLEEP_WINDOW_POLICIES:
                clean[key] = item
        elif key == "sleep_window_target_s":
            if item is None or (
                isinstance(item, int)
                and not isinstance(item, bool)
                and item in _LOGGER_SLEEP_WINDOW_TARGETS
            ):
                clean[key] = item
        elif key == "sleep_window_observed_s":
            if item is None or (
                isinstance(item, int)
                and not isinstance(item, bool)
                and 1 <= item <= MAX_LOGGER_SLEEP_WINDOW_SECONDS
            ):
                clean[key] = item
        elif key == "sleep_window_error":
            if item is None or (isinstance(item, str) and item in _LOGGER_SLEEP_WINDOW_ERRORS):
                clean[key] = item
        elif key == "updated_at_utc":
            if timestamp := _clean_logger_timestamp(item):
                clean[key] = timestamp
        elif key in boolean_keys:
            if isinstance(item, bool):
                clean[key] = item
        elif key == "vehicle_state":
            if item in {"parked", "ecu_online", "engine_running", "unknown"}:
                clean[key] = item
        elif isinstance(item, str):
            clean[key] = redact(item)[:256]
        elif item is None:
            clean[key] = item
    _reconcile_logger_voltage_freshness(clean)
    return clean


async def write_verification_receipt(
    address: str,
    receipts_dir: str,
    *,
    drive_id: str,
    bundle_sha256: str,
) -> None:
    """Atomically prove that one immutable bundle is durable on the server.

    The logger may prune its local raw rows only after this exact receipt exists.  It is
    therefore written after the verified file and database registration are durable and
    before the remote bundle is deleted.  A failed shell command deliberately leaves the
    source bundle in ``ready/`` for a later retry.
    """
    directory = _safe_remote_path(receipts_dir)
    if not SAFE_DRIVE_ID.fullmatch(drive_id):
        raise BundleError("receipt drive_id is unsafe")
    if not SHA256_RE.fullmatch(bundle_sha256):
        raise BundleError("receipt bundle SHA-256 is invalid")
    filename = f"{drive_id}.verified.json"
    body = json.dumps(
        {
            "schema_version": 1,
            "drive_id": drive_id,
            "bundle_sha256": bundle_sha256,
        },
        ensure_ascii=True,
        separators=(",", ":"),
    )
    if len(body.encode("utf-8")) > MAX_RECEIPT_BYTES:
        raise BundleError("verification receipt exceeds its byte limit")
    body_size = len(body.encode("utf-8"))
    target = f"{directory}/{filename}"
    partial = f"{target}.partial"
    # Every interpolated identifier/path is allowlisted above. ``sync FILE`` is supported
    # by Android toybox; the plain-sync fallback covers older vendor shells. ``mv`` is a
    # same-directory atomic rename.
    command = (
        f"if [ -e '{directory}' ] || [ -L '{directory}' ]; then "
        f"[ -d '{directory}' ] && [ ! -L '{directory}' ]; "
        f"else mkdir -p '{directory}' && [ -d '{directory}' ] && "
        f"[ ! -L '{directory}' ]; fi && "
        f"if [ -e '{target}' ] || [ -L '{target}' ]; then "
        f"[ -f '{target}' ] && [ ! -L '{target}' ]; fi && "
        f"if [ -e '{partial}' ] || [ -L '{partial}' ]; then "
        f"[ -f '{partial}' ] && [ ! -L '{partial}' ] && rm -f '{partial}'; fi && "
        f"umask 077 && printf '%s' '{body}' > '{partial}' && "
        f"[ -f '{partial}' ] && [ ! -L '{partial}' ] && "
        f"[ \"$(wc -c < '{partial}')\" -eq {body_size} ] && "
        f"[ \"$(cat '{partial}')\" = '{body}' ] && "
        f"(sync '{partial}' 2>/dev/null || sync) && "
        f"mv -f '{partial}' '{target}' && "
        f"[ -f '{target}' ] && [ ! -L '{target}' ] && "
        f"[ \"$(wc -c < '{target}')\" -eq {body_size} ] && "
        f"[ \"$(cat '{target}')\" = '{body}' ] && "
        f"(sync '{directory}' 2>/dev/null || sync)"
    )
    await adb.shell(address, command, timeout=10.0)

    # Do not treat completion of the mutating shell session as proof that the final path is
    # readable. ADB can disappear after the partial write or around the rename, and its
    # client-side result cannot authorise deletion of the only source copy. Re-open the
    # final receipt in a separate authoritative shell round trip and compare its exact
    # canonical bytes before returning success to the deletion path.
    readback_command = (
        f"[ -f '{target}' ] && [ ! -L '{target}' ] && "
        f"[ \"$(wc -c < '{target}')\" -eq {body_size} ] && "
        f"cat '{target}'"
    )
    readback = await adb.shell(address, readback_command, timeout=10.0)
    if readback != body:
        raise adb.AdbError("verification receipt final readback content mismatch")


def _clean_staging(config: AppConfig) -> None:
    root = config.obd_staging_dir.resolve()
    for path in root.iterdir():
        if not path.name.startswith(".transfer-") or not path.name.endswith(".partial"):
            continue
        resolved = path.resolve()
        if resolved.parent != root:
            continue
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)


def _durable_move(source: Path, target: Path) -> None:
    # FlushFileBuffers on Windows requires a handle opened for writing; ``rb`` produces
    # EBADF even though POSIX accepts fsync on a read descriptor.
    with source.open("r+b") as handle:
        os.fsync(handle.fileno())
    os.replace(source, target)
    if os.name != "nt":
        descriptor = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _quarantine(source: Path, bundle_hash: str, config: AppConfig) -> Path:
    root = config.obd_quarantine_dir.resolve()
    target = root / source.name
    if target.exists():
        old_hash, _ = file_sha256(target, maximum=config.obd_max_bundle_bytes)
        if old_hash == bundle_hash:
            # A permanently invalid bundle deliberately remains on the unit.  Retrying the
            # backup must not retain another identical quarantine archive on every visit.
            # Keep the already durable canonical copy and discard only this fresh staging
            # duplicate; its durable rejection row is refreshed by the caller below.
            source.unlink()
            return target
        archived = root / f"{source.name}.{old_hash[:12]}.bad"
        if archived.exists():
            archived = root / (
                f"{source.name}.{old_hash[:12]}.{int(datetime.now(UTC).timestamp())}.bad"
            )
        os.replace(target, archived)
    resolved = target.resolve()
    if resolved.parent != root:
        raise BundleError("quarantine target escapes its root")
    _durable_move(source, target)
    return target


async def _already_verified(item: RemoteFile, config: AppConfig) -> OBDBundle | None:
    async with session_scope() as session:
        row = (
            await session.execute(select(OBDBundle).where(OBDBundle.filename == item.name))
        ).scalar_one_or_none()
    if row is None or row.verified_at is None or row.size_bytes != item.size:
        return None
    path = config.obd_verified_dir / item.name
    if path.is_symlink() or not path.is_file() or path.stat().st_size != item.size:
        return None
    try:
        local_hash, local_size = await asyncio.to_thread(
            file_sha256, path, maximum=config.obd_max_bundle_bytes
        )
    except (BundleError, OSError):
        return None
    if local_size != row.size_bytes or local_hash != row.bundle_hash:
        # Size equality is not identity. Leave the unit's known-good duplicate in place;
        # the normal copy path can repair a corrupt retained file from it.
        return None
    if row.state not in _REMOTE_RECLAIMABLE_STATES:
        repairable = row.state == OBDBundleState.QUARANTINED.value or (
            row.state == OBDBundleState.FAILED.value
            and row.failure_kind in {"integrity", "local_path", "quarantine_io"}
        )
        if not repairable:
            # The local immutable identity is still useful even when delivery state is
            # transitional or terminal. The caller decides whether it is safe to publish
            # a receipt; returning the row prevents a concurrent re-copy over this path.
            return row
        async with session_scope() as session:
            current = await session.get(OBDBundle, row.id)
            if current is None:
                return None
            still_repairable = current.state == OBDBundleState.QUARANTINED.value or (
                current.state == OBDBundleState.FAILED.value
                and current.failure_kind in {"integrity", "local_path", "quarantine_io"}
            )
            if not still_repairable:
                return current if current.state in _REMOTE_RECLAIMABLE_STATES else None
            now = utcnow()
            current.state = OBDBundleState.READY_TO_IMPORT.value
            current.verified_at = now
            current.next_attempt_at = now
            current.import_started_at = None
            current.last_error = None
            current.failure_kind = None
            current.last_http_status = None
            current.updated_at = now
            await session.flush()
            row = current
        await asyncio.to_thread(_clear_stale_quarantine, item.name, config)
    return row


async def _already_rejected(item: RemoteFile, config: AppConfig) -> OBDBundle | None:
    """Return a durable exact local rejection without making it remotely reclaimable."""
    async with session_scope() as session:
        row = (
            await session.execute(select(OBDBundle).where(OBDBundle.filename == item.name))
        ).scalar_one_or_none()
    if (
        row is None
        or row.metadata_trusted
        or row.state != OBDBundleState.QUARANTINED.value
        or row.size_bytes != item.size
    ):
        return None
    path = config.obd_quarantine_dir / item.name
    if path.is_symlink() or not path.is_file() or path.stat().st_size != item.size:
        return None
    try:
        local_hash, local_size = await asyncio.to_thread(
            file_sha256, path, maximum=config.obd_max_bundle_bytes
        )
    except (BundleError, OSError):
        return None
    if local_size != row.size_bytes or local_hash != row.bundle_hash:
        return None
    return row


async def _remote_bundle_sha256(address: str, source: str, item: RemoteFile) -> str:
    """Hash one regular remote bundle before publishing a verification receipt."""
    directory = _safe_remote_path(source)
    if not is_bundle_name(item.name):
        raise BundleError("remote OBD bundle filename is unsafe")
    remote_path = f"{directory}/{item.name}"
    output = await adb.shell(
        address,
        f"[ -f '{remote_path}' ] && [ ! -L '{remote_path}' ] && sha256sum '{remote_path}'",
        timeout=60.0,
    )
    lines = output.strip().splitlines()
    if len(lines) != 1:
        raise BundleError("remote OBD bundle SHA-256 output is invalid")
    digest, separator, reported_path = lines[0].partition(" ")
    reported_path = reported_path.strip().removeprefix("*")
    if not separator or not SHA256_RE.fullmatch(digest) or reported_path != remote_path:
        raise BundleError("remote OBD bundle SHA-256 output is invalid")
    return digest


async def _delete_remote_if_hash(
    address: str,
    source: str,
    *,
    filename: str,
    bundle_sha256: str,
) -> None:
    """Atomically isolate, prove, then unlink only the verified remote inode.

    Hashing a pathname and then unlinking that pathname is racy even inside one shell:
    another atomic producer rename can replace it between the two commands. Move the
    current regular file to a unique same-directory tombstone first. A later replacement
    keeps the public pathname and is never touched; a hash mismatch restores the original
    only when that pathname is still vacant, otherwise both copies are conservatively kept.
    """
    directory = _safe_remote_path(source)
    if not is_bundle_name(filename) or not SHA256_RE.fullmatch(bundle_sha256):
        raise BundleError("remote OBD deletion identity is invalid")
    remote_path = f"{directory}/{filename}"
    tombstone = f"{directory}/.{filename}.{uuid.uuid4().hex}.delete.partial"
    command = (
        f"[ -f '{remote_path}' ] && [ ! -L '{remote_path}' ] && "
        f"[ ! -e '{tombstone}' ] && mv '{remote_path}' '{tombstone}' && "
        f"set -- $(sha256sum '{tombstone}') && "
        f"if [ \"$1\" = '{bundle_sha256}' ]; then "
        f"rm -f '{tombstone}' && [ ! -e '{tombstone}' ] && "
        f"[ ! -e '{remote_path}' ] && printf 'OBD_DELETED'; "
        f"else [ -e '{remote_path}' ] || mv '{tombstone}' '{remote_path}'; "
        f"printf 'OBD_RETAINED'; fi"
    )
    output = await adb.shell(address, command, timeout=60.0)
    if output.strip() != "OBD_DELETED":
        raise adb.AdbError("remote OBD bundle changed before verified deletion")


def _receipt_eligible(row: OBDBundle) -> bool:
    """Whether durable server identity is independent of HA delivery outcome."""
    if not row.metadata_trusted or row.verified_at is None:
        return False
    if row.state in _REMOTE_RECLAIMABLE_STATES:
        return True
    return (
        row.state == OBDBundleState.FAILED.value
        and row.failure_kind in _REMOTE_RECLAIMABLE_FAILED_KINDS
    )


def _clear_stale_quarantine(filename: str, config: AppConfig) -> None:
    """Remove only the canonical corrupt copy after its verified identity is repaired."""
    target = (config.obd_quarantine_dir / filename).resolve()
    if target.parent != config.obd_quarantine_dir.resolve() or not is_bundle_name(filename):
        raise BundleError("stale quarantine path is unsafe")
    if target.is_file():
        target.unlink()


async def _mark_remote_deleted(bundle_id: int) -> None:
    async with session_scope() as session:
        row = await session.get(OBDBundle, bundle_id)
        if row is not None:
            row.remote_deleted_at = utcnow()


async def _register(validated: ValidatedBundle) -> OBDBundle:
    async with session_scope() as session:
        return await store_validated_bundle(session, validated)


async def _registered_winner(validated: ValidatedBundle, config: AppConfig) -> OBDBundle | None:
    """Prove that a uniqueness loser is the same durable immutable bundle.

    An ``IntegrityError`` alone says only that *some* unique key won.  It is not
    authority to discard anything from ``verified/``: the conflict may instead be a
    colliding sample id, drive id, or filename.  Accept the operation as idempotent only
    when both independent durability boundaries agree -- a trusted database identity and
    the exact canonical file bytes.
    """
    async with session_scope() as session:
        row = await session.scalar(
            select(OBDBundle).where(
                OBDBundle.filename == validated.filename,
                OBDBundle.drive_id == validated.drive_id,
                OBDBundle.bundle_hash == validated.bundle_sha256,
                OBDBundle.schema_version == validated.schema_version,
                OBDBundle.size_bytes == validated.size_bytes,
                OBDBundle.metadata_trusted.is_(True),
                OBDBundle.verified_at.is_not(None),
            )
        )
    if row is None:
        return None

    target = config.obd_verified_dir / validated.filename
    if (
        target.is_symlink()
        or not target.is_file()
        or target.resolve().parent != config.obd_verified_dir.resolve()
        or target.stat().st_size != validated.size_bytes
    ):
        return None
    try:
        digest, size = await asyncio.to_thread(
            file_sha256,
            target,
            maximum=config.obd_max_bundle_bytes,
        )
    except (BundleError, OSError):
        return None
    if size != validated.size_bytes or digest != validated.bundle_sha256:
        return None
    return row


async def verified_bundle_matches(filename: str, bundle_sha256: str) -> bool:
    """Whether an acknowledged immutable export has both trusted metadata and bytes."""
    if not is_bundle_name(filename) or not SHA256_RE.fullmatch(bundle_sha256):
        return False
    config = get_config()
    async with session_scope() as session:
        row = await session.scalar(
            select(OBDBundle).where(
                OBDBundle.filename == filename,
                OBDBundle.bundle_hash == bundle_sha256,
                OBDBundle.metadata_trusted.is_(True),
                OBDBundle.verified_at.is_not(None),
            )
        )
    if row is None:
        return False

    target = config.obd_verified_dir / filename
    if (
        target.is_symlink()
        or not target.is_file()
        or target.resolve().parent != config.obd_verified_dir.resolve()
        or target.stat().st_size != row.size_bytes
    ):
        return False
    try:
        digest, size = await asyncio.to_thread(
            file_sha256,
            target,
            maximum=config.obd_max_bundle_bytes,
        )
    except (BundleError, OSError):
        return False
    return size == row.size_bytes and digest == row.bundle_hash


async def sync_remote_bundles(
    info: UnitInfo,
    *,
    ingest_status: IngestStatus | None = None,
    remote: list[RemoteFile] | None = None,
    config: AppConfig | None = None,
) -> OBDTransferResult:
    """Copy, validate and queue bundles.  Never called as a condition of footage success."""
    cfg = config or get_config()
    started = time.monotonic()
    result = OBDTransferResult()
    state = get_obd_transfer_status()
    transfer_dir: Path | None = None
    process_lock: ProcessFileLock | None = None

    try:
        # ``try_acquire`` is a single non-blocking OS call. Keeping it on this task also
        # prevents cancellation from abandoning an acquired descriptor in a worker
        # future whose result nobody will collect.
        process_lock = try_acquire(_sync_lock_path(cfg))
    except OSError as exc:
        result.error = f"could not acquire the OBD transfer lock: {redact(exc)}"
        result.failed = 1
        result.complete = False
        result.seconds = time.monotonic() - started
        log.warning("OBD backup stage could not establish its process fence", error=result.error)
        return result
    if process_lock is None:
        # Do not touch the process-wide status snapshot: an in-process winner may be the
        # run currently represented there.  The caller still receives an explicit
        # incomplete result and therefore cannot treat the OBD durability gate as passed.
        result.error = "another OBD transfer is already active"
        result.failed = 1
        result.complete = False
        result.seconds = time.monotonic() - started
        log.warning("skipped a concurrent OBD backup stage")
        return result

    try:
        source = _safe_remote_path(cfg.obd_remote_ready_dir)
        # A process-lifetime file lock is now held. Any matching directory is therefore
        # known to be an orphan from a dead process rather than another live transfer.
        await asyncio.to_thread(_clean_staging, cfg)
        logger_task = asyncio.create_task(
            read_logger_status(info.address, cfg.obd_remote_status_file)
        )
        if remote is None:
            remote = await inventory_remote_bundles(info.address, source)
        state.set_logger(await logger_task)
        result.discovered = len(remote)
        state.set_inventory(len(remote))
        if not remote:
            return result

        pending: list[RemoteFile] = []
        for item in remote:
            existing = await _already_verified(item, cfg)
            if existing is not None:
                try:
                    remote_hash = await _remote_bundle_sha256(info.address, source, item)
                except (BundleError, adb.AdbError) as exc:
                    # Older vendor shells may lack sha256sum. The normal bounded transfer
                    # path independently hashes and validates the bytes before any receipt
                    # or deletion, so tool unavailability is safe rather than fatal.
                    log.warning(
                        "could not prove remote OBD identity without a full copy",
                        bundle=item.name,
                        error=redact(exc),
                    )
                    pending.append(item)
                    if len(pending) >= MAX_BUNDLES_PER_WINDOW:
                        break
                    continue
                if remote_hash != existing.bundle_hash:
                    log.warning(
                        "remote OBD bytes differ from the durable same-name bundle",
                        bundle=item.name,
                    )
                    pending.append(item)
                    if len(pending) >= MAX_BUNDLES_PER_WINDOW:
                        break
                    continue
                if not _receipt_eligible(existing):
                    log.warning(
                        "retained OBD source while its verified server row is transitional",
                        bundle=item.name,
                        state=existing.state,
                    )
                    continue
                try:
                    await write_verification_receipt(
                        info.address,
                        cfg.obd_remote_receipts_dir,
                        drive_id=existing.drive_id,
                        bundle_sha256=existing.bundle_hash,
                    )
                except (BundleError, adb.AdbError) as exc:
                    result.failed += 1
                    result.error = redact(exc)
                    log.warning(
                        "retained an already verified OBD bundle until its receipt succeeds",
                        bundle=item.name,
                        error=result.error,
                    )
                    continue
                try:
                    await _delete_remote_if_hash(
                        info.address,
                        source,
                        filename=item.name,
                        bundle_sha256=existing.bundle_hash,
                    )
                except (BundleError, adb.AdbError) as exc:
                    result.failed += 1
                    result.error = redact(exc)
                    log.warning(
                        "could not remove an already verified OBD bundle from the unit",
                        error=result.error,
                    )
                else:
                    await _mark_remote_deleted(existing.id)
                    result.duplicates += 1
                    result.removed_from_unit += 1
                continue
            rejected = await _already_rejected(item, cfg)
            if rejected is not None:
                try:
                    remote_hash = await _remote_bundle_sha256(info.address, source, item)
                except (BundleError, adb.AdbError):
                    # Without remote byte identity the regular bounded copy/validation path
                    # remains the only safe choice.
                    pass
                else:
                    if remote_hash == rejected.bundle_hash:
                        log.info(
                            "skipped an unchanged remotely retained rejected OBD bundle",
                            bundle=item.name,
                        )
                        continue
            if item.size > cfg.obd_max_bundle_bytes:
                result.failed += 1
                result.error = f"{item.name} exceeds the configured OBD bundle limit"
                continue
            pending.append(item)
            if len(pending) >= MAX_BUNDLES_PER_WINDOW:
                break
        if not pending:
            return result
        # False until every item in the authoritative pending inventory has crossed the
        # hash/validation/database durability boundary below.
        result.complete = False

        transfer_dir = cfg.obd_staging_dir / f".transfer-{uuid.uuid4().hex}.partial"
        transfer_dir.mkdir()
        if ingest_status is not None:
            ingest_status.extend_plan(
                # OBD files count in the live progress but not in the footage backlog.
                DeltaPlan(files=pending)
            )
        host = info.address.split(":", 1)[0]
        port = int(ingest_setting("data_port", 9000))
        timeout_s = min(
            60,
            int(ingest_setting("listen_timeout_s", 180)),
        )
        await adb.clear_listener(info.address)
        proc = await adb.launch_listener(
            info.address,
            source,
            [item.name for item in pending],
            port=port,
            timeout_s=timeout_s,
        )
        received = await asyncio.to_thread(
            transport.receive,
            host,
            port,
            transfer_dir,
            expected={item.name: item.size for item in pending},
            max_member_bytes=cfg.obd_max_bundle_bytes,
            max_total_bytes=cfg.obd_max_bundle_bytes * len(pending),
            on_file_started=(
                lambda name: (
                    (state.set_current(name), ingest_status.file_started(name))
                    if ingest_status is not None
                    else state.set_current(name)
                )
            ),
            on_file_done=(
                lambda name: ingest_status.file_done(name) if ingest_status is not None else None
            ),
            on_bytes=(ingest_status.add_bytes if ingest_status is not None else None),
            cancel=(ingest_status.cancel_event if ingest_status is not None else None),
        )
        await adb.stop_listener(proc)

        expected = {item.name: item for item in pending}
        durable_names: set[str] = set()
        for name in received.files:
            item = expected.get(name)
            staged = transfer_dir / name
            target: Path | None = None
            target_owned_by_run = False
            validated: ValidatedBundle | None = None
            if item is None or not staged.is_file():
                continue
            try:
                if staged.stat().st_size != item.size:
                    raise BundleError("copied size does not match the remote stat")
                validated = await asyncio.to_thread(validate_bundle, staged, config=cfg)
                target = cfg.obd_verified_dir / name
                if target.exists():
                    if target.is_symlink():
                        raise BundleConflict("a verified bundle path is a symlink")
                    target_hash, target_size = await asyncio.to_thread(
                        file_sha256, target, maximum=cfg.obd_max_bundle_bytes
                    )
                    if (
                        target_hash != validated.bundle_sha256
                        or target_size != validated.size_bytes
                    ):
                        async with session_scope() as session:
                            known = (
                                await session.execute(
                                    select(OBDBundle).where(OBDBundle.filename == name)
                                )
                            ).scalar_one_or_none()
                        if known is None or known.bundle_hash != validated.bundle_sha256:
                            raise BundleConflict(
                                "a verified bundle with the same filename has different bytes"
                            )
                        # The database identity and fresh unit copy agree, proving the
                        # retained same-name bytes are the corrupt side. Quarantine them
                        # before atomically restoring the verified path.
                        await asyncio.to_thread(_quarantine, target, target_hash, cfg)
                        await asyncio.to_thread(_durable_move, staged, target)
                        target_owned_by_run = True
                    else:
                        staged.unlink()
                else:
                    await asyncio.to_thread(_durable_move, staged, target)
                    target_owned_by_run = True
                # The move preserves the inode but the dataclass path is immutable.
                validated = ValidatedBundle(
                    path=target,
                    filename=validated.filename,
                    bundle_sha256=validated.bundle_sha256,
                    size_bytes=validated.size_bytes,
                    manifest=validated.manifest,
                    summary=validated.summary,
                    diagnostics_document=validated.diagnostics_document,
                    latest_sample=validated.latest_sample,
                    latest_values=validated.latest_values,
                    statistics=validated.statistics,
                    summary_source=validated.summary_source,
                    warnings=validated.warnings,
                )
                row = await _register(validated)
                if row.state == OBDBundleState.READY_TO_IMPORT.value:
                    try:
                        await asyncio.to_thread(_clear_stale_quarantine, name, cfg)
                    except (BundleError, OSError) as cleanup_error:
                        # Registration and the new verified bytes are already durable.
                        # A stale quarantine cleanup failure must never demote or move the
                        # good copy; the harmless old file can be retried manually/later.
                        log.warning(
                            "could not remove a stale OBD quarantine copy",
                            bundle=name,
                            error=redact(cleanup_error),
                        )
            except (BundleError, OSError, IntegrityError) as exc:
                winner = (
                    await _registered_winner(validated, cfg)
                    if isinstance(exc, IntegrityError) and validated is not None
                    else None
                )
                if winner is not None:
                    # Another transaction crossed both the trusted-row and canonical-file
                    # durability boundaries first. This run is an idempotent observer, not
                    # the owner of bytes it may quarantine.
                    row = winner
                    log.info(
                        "accepted a concurrently registered OBD bundle",
                        bundle=name,
                    )
                else:
                    result.failed += 1
                    error_text = (
                        "database uniqueness conflict while storing validated OBD history"
                        if isinstance(exc, IntegrityError)
                        else redact(exc)
                    )
                    result.error = error_text
                    quarantine_path: Path | None = None
                    digest: str | None = None
                    rejected_size = 0
                    rejected_source: Path | None = staged if staged.is_file() else None
                    if (
                        rejected_source is None
                        and target_owned_by_run
                        and target is not None
                        and target.is_file()
                        and not target.is_symlink()
                        and target.resolve().parent == cfg.obd_verified_dir.resolve()
                    ):
                        # Only a path installed by this run is eligible. A pre-existing
                        # canonical target may belong to the winning transaction that
                        # produced the IntegrityError and must never be moved on inference.
                        rejected_source = target
                    if rejected_source is not None:
                        with contextlib.suppress(BundleError, OSError):
                            digest, rejected_size = await asyncio.to_thread(
                                file_sha256,
                                rejected_source,
                                maximum=cfg.obd_max_bundle_bytes,
                            )
                            quarantine_path = await asyncio.to_thread(
                                _quarantine, rejected_source, digest, cfg
                            )
                    if digest is not None:
                        try:
                            async with session_scope() as session:
                                await store_rejected_bundle(
                                    session,
                                    filename=name,
                                    bundle_hash=digest,
                                    size_bytes=rejected_size,
                                    error=error_text,
                                    quarantined=quarantine_path is not None,
                                )
                        except Exception as record_error:
                            log.exception(
                                "could not persist an OBD quarantine record",
                                bundle=name,
                                error=redact(record_error),
                            )
                    log.warning(
                        "an OBD bundle failed validation",
                        bundle=name,
                        quarantined=quarantine_path is not None,
                        error=error_text,
                    )
                    continue

            result.copied += 1
            durable_names.add(name)
            result.bytes += item.size
            if not _receipt_eligible(row):
                log.warning(
                    "retained OBD source until its queue state is coherent",
                    bundle=name,
                    state=row.state,
                )
                continue
            try:
                await write_verification_receipt(
                    info.address,
                    cfg.obd_remote_receipts_dir,
                    drive_id=row.drive_id,
                    bundle_sha256=row.bundle_hash,
                )
            except (BundleError, adb.AdbError) as exc:
                result.failed += 1
                result.error = redact(exc)
                log.warning(
                    "verified OBD bundle remains on the unit until its receipt succeeds",
                    bundle=name,
                    error=result.error,
                )
                continue
            try:
                await _delete_remote_if_hash(
                    info.address,
                    source,
                    filename=name,
                    bundle_sha256=row.bundle_hash,
                )
            except (BundleError, adb.AdbError) as exc:
                # The verified server copy and DB transaction are durable.  Leaving the
                # remote alone is safe; the next run recognises it and tries reclamation.
                result.failed += 1
                result.error = redact(exc)
                log.warning(
                    "verified OBD bundle remains on the unit", bundle=name, error=result.error
                )
            else:
                await _mark_remote_deleted(row.id)
                result.removed_from_unit += 1
            get_import_worker().wake()

        missing_names = set(expected) - durable_names
        result.missing = len(missing_names)
        result.complete = not missing_names
        if missing_names:
            # Validation failures were counted in their own branch. Add only the pending
            # files the transport never delivered so each missing bundle is represented.
            result.failed += sum(1 for name in missing_names if name not in received.files)
        if not received.complete and received.error:
            result.error = redact(received.error)
    except (BundleError, adb.AdbError, OSError) as exc:
        result.error = redact(exc)
        result.failed += 1
        result.complete = False
        log.warning("OBD backup stage failed without affecting footage", error=result.error)
    finally:
        try:
            result.seconds = time.monotonic() - started
            state.finish(result)
            # Orphan sweeping happens once at entry under the process fence. At exit,
            # remove only the UUID directory allocated by this invocation; a broad glob
            # here could erase a successor's live transfer after this lock is released.
            with contextlib.suppress(OSError):
                if (
                    transfer_dir is not None
                    and transfer_dir.is_dir()
                    and transfer_dir.resolve().parent == cfg.obd_staging_dir.resolve()
                ):
                    await asyncio.to_thread(shutil.rmtree, transfer_dir)
        finally:
            try:
                process_lock.release()
            except OSError as exc:
                # ``release`` closes the descriptor even when the explicit unlock fails,
                # so the operating system still drops ownership. Do not turn a completed
                # best-effort telemetry stage into a footage failure while reporting it.
                log.warning("could not explicitly release the OBD transfer lock", error=redact(exc))
    return result


__all__ = [
    "OBDTransferResult",
    "get_obd_transfer_status",
    "inventory_remote_bundles",
    "read_logger_status",
    "sync_remote_bundles",
    "verified_bundle_matches",
    "write_verification_receipt",
]
