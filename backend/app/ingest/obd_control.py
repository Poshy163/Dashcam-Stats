"""Bounded file protocol used to quiesce the on-device OBD logger.

The Android app publishes ``status.json`` in app-specific external storage.  Its control
directory is a sibling of that file so the existing ADB trust boundary is reused: no
exported Android component and no network listener are introduced.  A request is written
atomically, an exact correlated acknowledgement is read with a hard byte/time bound, and
removing both files is the resume signal after the original radio state is restored.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.logging import get_logger
from app.ingest import adb
from app.ingest.ha_import_queue import redact
from app.ingest.obd_bundle import SAFE_DRIVE_ID, SHA256_RE, is_bundle_name

log = get_logger(__name__)

CAPABILITY = "ingestion_quiesce_v1"
REQUEST_ACTION = "prepare_for_ingest"
DEFAULT_TIMEOUT_S = 30.0
POLL_INTERVAL_S = 0.25
MAX_CONTROL_BYTES = 4096
MAX_HOLD_S = 600.0

_SAFE_REMOTE_PATH = re.compile(r"^/[A-Za-z0-9._/-]{1,511}$")
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_REQUEST_KEYS = frozenset(
    {
        "schema_version",
        "request_id",
        "action",
        "requested_at_utc",
        "deadline_at_utc",
    }
)
_ACK_KEYS = frozenset(
    {
        "schema_version",
        "request_id",
        "state",
        "ready_at_utc",
        "drive_id",
        "last_sample_at_utc",
        "bundle_filename",
        "bundle_sha256",
        "error",
    }
)


def _monotonic() -> float:
    return time.monotonic()


class LoggerControlError(RuntimeError):
    """A logger did not prove that it was safe to take Bluetooth away."""


@dataclass(frozen=True, slots=True)
class LoggerAck:
    request_id: str
    state: str
    ready_at_utc: str
    drive_id: str | None
    last_sample_at_utc: str | None
    bundle_filename: str | None
    bundle_sha256: str | None
    error: str | None

    @property
    def ready(self) -> bool:
        return self.state == "ready"


@dataclass(frozen=True, slots=True)
class ControlPaths:
    directory: str
    request: str
    ack: str


def supports_quiesce(status: dict[str, Any] | None) -> bool:
    if not isinstance(status, dict):
        return False
    version = status.get("schema_version")
    capabilities = status.get("capabilities")
    return (
        isinstance(version, int)
        and not isinstance(version, bool)
        and version >= 2
        and isinstance(capabilities, list)
        and CAPABILITY in capabilities
    )


def control_paths(status_path: str) -> ControlPaths:
    path = status_path.rstrip("/")
    if (
        not _SAFE_REMOTE_PATH.fullmatch(path)
        or ".." in path.split("/")
        or "'" in path
        or "/" not in path[1:]
    ):
        raise LoggerControlError("configured logger status path is not a safe Android path")
    parent = path.rsplit("/", 1)[0]
    directory = f"{parent}/control"
    return ControlPaths(
        directory=directory,
        request=f"{directory}/ingestion-request.json",
        ack=f"{directory}/ingestion-ack.json",
    )


def _utc_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_utc(value: object, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or len(value) > 40:
        raise LoggerControlError("logger acknowledgement contains an invalid UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LoggerControlError(
            "logger acknowledgement contains an invalid UTC timestamp"
        ) from exc
    if parsed.tzinfo is None:
        raise LoggerControlError("logger acknowledgement timestamp has no timezone")
    return _utc_text(parsed)


def _nullable_string(value: object, *, maximum: int = 256) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise LoggerControlError("logger acknowledgement contains an invalid string field")
    # Control replies are diagnostic metadata, never a place for multiline/raw payloads.
    if any(ord(char) < 32 for char in value):
        raise LoggerControlError("logger acknowledgement contains control characters")
    return value


def parse_ack(raw: str, request_id: str) -> LoggerAck:
    if not raw or len(raw.encode("utf-8")) > MAX_CONTROL_BYTES:
        raise LoggerControlError("logger acknowledgement is empty or too large")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LoggerControlError("logger acknowledgement is invalid JSON") from exc
    if not isinstance(value, dict) or set(value) != _ACK_KEYS:
        raise LoggerControlError("logger acknowledgement does not match schema v1")
    if value.get("schema_version") != 1 or value.get("request_id") != request_id:
        raise LoggerControlError("logger acknowledgement does not match the active request")
    state = value.get("state")
    if state not in {"ready", "failed"}:
        raise LoggerControlError("logger acknowledgement has an invalid state")

    drive_id = _nullable_string(value.get("drive_id"), maximum=64)
    if drive_id is not None and not SAFE_DRIVE_ID.fullmatch(drive_id):
        raise LoggerControlError("logger acknowledgement has an invalid drive id")
    bundle_filename = _nullable_string(value.get("bundle_filename"), maximum=255)
    if bundle_filename is not None and not is_bundle_name(bundle_filename):
        raise LoggerControlError("logger acknowledgement has an invalid bundle filename")
    bundle_sha256 = _nullable_string(value.get("bundle_sha256"), maximum=64)
    if bundle_sha256 is not None and not SHA256_RE.fullmatch(bundle_sha256):
        raise LoggerControlError("logger acknowledgement has an invalid bundle hash")
    if (bundle_filename is None) != (bundle_sha256 is None):
        raise LoggerControlError(
            "logger acknowledgement bundle filename and hash must appear together"
        )
    error = _nullable_string(value.get("error"), maximum=512)
    if error is not None:
        error = redact(error)[:512]
    if state == "ready" and error is not None:
        raise LoggerControlError("ready logger acknowledgement unexpectedly contains an error")
    if state == "failed" and error is None:
        raise LoggerControlError("failed logger acknowledgement did not include an error")

    return LoggerAck(
        request_id=request_id,
        state=state,
        ready_at_utc=_parse_utc(value.get("ready_at_utc")) or "",
        drive_id=drive_id,
        last_sample_at_utc=_parse_utc(value.get("last_sample_at_utc"), nullable=True),
        bundle_filename=bundle_filename,
        bundle_sha256=bundle_sha256,
        error=error,
    )


def _parse_request_deadline(raw: str, request_id: str) -> datetime:
    if not raw or len(raw.encode("utf-8")) > MAX_CONTROL_BYTES:
        raise LoggerControlError("logger request is empty or too large")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LoggerControlError("logger request is invalid JSON") from exc
    if not isinstance(value, dict) or set(value) != _REQUEST_KEYS:
        raise LoggerControlError("logger request does not match schema v1")
    if (
        value.get("schema_version") != 1
        or value.get("request_id") != request_id
        or value.get("action") != REQUEST_ACTION
    ):
        raise LoggerControlError("logger request does not match the active request")
    requested_text = _parse_utc(value.get("requested_at_utc")) or ""
    deadline_text = _parse_utc(value.get("deadline_at_utc")) or ""
    requested = datetime.fromisoformat(requested_text.replace("Z", "+00:00"))
    deadline = datetime.fromisoformat(deadline_text.replace("Z", "+00:00"))
    duration_s = (deadline - requested).total_seconds()
    if duration_s < 1.0 or duration_s > MAX_HOLD_S:
        raise LoggerControlError("logger request lease duration is outside the bounded range")
    return deadline


async def verify_quiesce(
    address: str,
    status_path: str,
    request_id: str,
    *,
    minimum_remaining_s: float,
) -> LoggerAck:
    """Prove the exact request/ack pair still covers the coming radio-off window."""

    if not _SAFE_REQUEST_ID.fullmatch(request_id):
        raise LoggerControlError("logger request id is unsafe")
    minimum_remaining_s = float(minimum_remaining_s)
    if (
        not math.isfinite(minimum_remaining_s)
        or minimum_remaining_s < 0
        or minimum_remaining_s > MAX_HOLD_S
    ):
        raise LoggerControlError("required logger lease coverage is outside the bounded range")
    paths = control_paths(status_path)

    async def read(path: str) -> str:
        return await adb.shell(
            address,
            f"[ -f '{path}' ] && [ ! -L '{path}' ] && "
            f"head -c {MAX_CONTROL_BYTES + 1} '{path}'; exit 0",
            timeout=6.0,
        )

    request_raw = await read(paths.request)
    deadline = _parse_request_deadline(request_raw, request_id)
    ack = parse_ack(await read(paths.ack), request_id)
    if not ack.ready:
        raise LoggerControlError("logger is no longer ready for ingestion")
    # Re-read the request after the ACK so expiry/removal/replacement during the check
    # cannot leave a stale acknowledgement looking authoritative.
    if await read(paths.request) != request_raw:
        raise LoggerControlError("logger request changed while its lease was verified")
    remaining_s = (deadline - datetime.now(UTC)).total_seconds()
    if remaining_s < minimum_remaining_s:
        raise LoggerControlError("logger quiesce lease does not cover radio recovery")
    return ack


async def _remove_files(address: str, paths: ControlPaths) -> bool:
    await adb.shell(
        address,
        f"rm -f '{paths.request}' '{paths.ack}'; sync '{paths.directory}' 2>/dev/null || sync",
        timeout=6.0,
    )
    reply = await adb.shell(
        address,
        f"[ ! -e '{paths.request}' ] && [ ! -e '{paths.ack}' ] && printf resumed; exit 0",
        timeout=6.0,
    )
    return reply.strip() == "resumed"


async def resume_logger(address: str, status_path: str) -> bool:
    """Remove the active request and ack; Android treats this as permission to resume."""
    try:
        return await _remove_files(address, control_paths(status_path))
    except (adb.AdbError, LoggerControlError) as exc:
        log.warning("could not clear the OBD ingestion control files", error=str(exc))
        return False


async def request_quiesce(
    address: str,
    status_path: str,
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    hold_s: float | None = None,
    request_id: str | None = None,
) -> LoggerAck:
    """Ask the logger to finalise/export and wait for an exact correlated ack.

    ``timeout_s`` bounds this caller's wait. ``hold_s`` is the on-device safety lease:
    if the server dies before removing the files, the logger resumes at that wall-clock
    deadline instead of remaining paused indefinitely.
    """
    timeout_s = max(1.0, min(float(timeout_s), 120.0))
    hold_s = timeout_s if hold_s is None else max(timeout_s, min(float(hold_s), MAX_HOLD_S))
    request_id = request_id or str(uuid.uuid4())
    if not _SAFE_REQUEST_ID.fullmatch(request_id):
        raise LoggerControlError("logger request id is unsafe")
    paths = control_paths(status_path)
    requested = datetime.now(UTC)
    deadline = requested + timedelta(seconds=hold_s)
    body = json.dumps(
        {
            "schema_version": 1,
            "request_id": request_id,
            "action": REQUEST_ACTION,
            "requested_at_utc": _utc_text(requested),
            "deadline_at_utc": _utc_text(deadline),
        },
        ensure_ascii=True,
        separators=(",", ":"),
    )
    # Every field is generated here and validated above; keep this assertion adjacent to
    # the single-quoted shell write so later schema additions cannot make interpolation
    # unsafe by accident.
    if "'" in body or len(body.encode("utf-8")) > MAX_CONTROL_BYTES:
        raise LoggerControlError("logger request body is unsafe or too large")

    # Clear a stale prior handshake before publishing the new request.  The target is a
    # same-directory rename and both path and content are allowlisted.
    await _remove_files(address, paths)
    partial = f"{paths.request}.partial"
    command = (
        f"if [ -e '{paths.directory}' ] || [ -L '{paths.directory}' ]; then "
        f"[ -d '{paths.directory}' ] && [ ! -L '{paths.directory}' ]; "
        f"else mkdir -p '{paths.directory}' && [ -d '{paths.directory}' ] && "
        f"[ ! -L '{paths.directory}' ]; fi && "
        f"rm -f '{partial}' && umask 077 && printf '%s' '{body}' > '{partial}' && "
        f"[ -f '{partial}' ] && [ ! -L '{partial}' ] && "
        f"(sync '{partial}' 2>/dev/null || sync) && mv -f '{partial}' '{paths.request}'"
    )
    await adb.shell(address, command, timeout=8.0)
    readback = await adb.shell(
        address,
        f"[ -f '{paths.request}' ] && [ ! -L '{paths.request}' ] && cat '{paths.request}'",
        timeout=6.0,
    )
    if readback != body:
        raise LoggerControlError("logger request final readback content mismatch")

    monotonic_deadline = _monotonic() + timeout_s
    while True:
        raw = await adb.shell(
            address,
            f"[ -f '{paths.ack}' ] && head -c {MAX_CONTROL_BYTES + 1} '{paths.ack}'; exit 0",
            timeout=5.0,
        )
        if raw:
            ack = parse_ack(raw, request_id)
            if not ack.ready:
                raise LoggerControlError(f"logger refused ingestion quiescence: {ack.error}")
            return ack
        if _monotonic() >= monotonic_deadline:
            raise LoggerControlError("logger did not acknowledge ingestion quiescence in time")
        await asyncio.sleep(POLL_INTERVAL_S)


__all__ = [
    "CAPABILITY",
    "ControlPaths",
    "LoggerAck",
    "LoggerControlError",
    "control_paths",
    "parse_ack",
    "request_quiesce",
    "resume_logger",
    "supports_quiesce",
    "verify_quiesce",
]
