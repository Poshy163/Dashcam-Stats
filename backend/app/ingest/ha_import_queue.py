"""Independent, restart-safe delivery of validated OBD drives to Home Assistant."""

from __future__ import annotations

import asyncio
import contextlib
import email.utils
import gzip
import ipaddress
import json
import math
import os
import re
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx
from sqlalchemy import func, or_, select

from app.config import AppConfig, get_config
from app.core.logging import get_logger
from app.db.models import OBDBundle, OBDBundleState, OBDDrive, utcnow
from app.db.session import session_scope
from app.ingest.obd_bundle import (
    SAFE_REASON_RE,
    BundleError,
    HAPayloadError,
    bundle_path_for,
    file_sha256,
    is_bundle_name,
    store_rejected_bundle,
    store_validated_bundle,
    validate_bundle,
)
from app.ingest.obd_reconciliation import reconcile_drive_projection

log = get_logger(__name__)

MAX_HA_BODY_BYTES = 8 * 1024 * 1024
MAX_ERROR_TEXT = 1000
MAX_RESPONSE_BYTES = 256 * 1024
HA_PROJECTION_VERSION = 3
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{8,}")
_SUCCESS_FIELDS = frozenset(
    {
        "status",
        "drive_id",
        "accepted_samples",
        "duplicate_samples",
        "rejected_samples",
        "statistics_imported",
        "drive_sample_count",
        "raw_samples_stored",
        "warnings",
        "errors",
        "drive_lifecycle",
    }
)
_SUCCESS_LIFECYCLE_FIELDS = frozenset(
    {
        "status",
        "interruption_reason",
        "clean_end",
        "sample_count",
        "expected_sample_count",
        "missing_data_duration_s",
        "received_sample_percentage",
        "gap_count",
        "longest_gap_s",
    }
)
_SUCCESS_COUNTERS = frozenset(
    {
        "accepted_samples",
        "duplicate_samples",
        "rejected_samples",
        "statistics_imported",
        "drive_sample_count",
        "raw_samples_stored",
    }
)


class HAConfigurationError(RuntimeError):
    """The URL/token cannot be used until the operator changes deployment config."""


class TemporaryImportError(RuntimeError):
    def __init__(
        self, message: str, *, status: int | None = None, retry_after: float | None = None
    ):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class PermanentImportError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, kind: str = "permanent"):
        super().__init__(message)
        self.status = status
        self.kind = kind


def redact(value: object, *, token: str | None = None) -> str:
    """Bounded error text with bearer credentials removed before logs/database/UI."""
    text = str(value).replace("\r", " ").replace("\n", " ")
    text = _BEARER_RE.sub("Bearer [REDACTED]", text)
    if token:
        text = text.replace(token, "[REDACTED]")
    return text[:MAX_ERROR_TEXT]


def _trusted_http_host(host: str) -> bool:
    # ``urlsplit`` exposes the Unicode spelling, while HTTP clients IDNA-normalize the
    # actual destination.  Normalize before deciding whether cleartext is LAN-only so
    # Unicode full stops cannot turn a public FQDN into an apparent single-label host.
    try:
        lowered = host.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError:
        return False
    if lowered in {"localhost", "homeassistant", "home-assistant"} or lowered.endswith(".local"):
        return True
    try:
        address = ipaddress.ip_address(lowered.strip("[]"))
    except ValueError:
        # Single-label container/DNS names are private deployment names.  Public FQDNs
        # must use TLS; accepting arbitrary dotted names here would silently send the
        # bearer token over the internet in clear text.
        return "." not in lowered
    return address.is_private or address.is_loopback or address.is_link_local


def import_url(config: AppConfig | None = None) -> str:
    cfg = config or get_config()
    base = cfg.ha_url.strip().rstrip("/")
    if not base:
        raise HAConfigurationError("HA_URL is not configured")
    parsed = urlsplit(base)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise HAConfigurationError("HA_URL must be an absolute http(s) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise HAConfigurationError("HA_URL must not contain credentials, a query, or a fragment")
    if parsed.scheme == "http" and not _trusted_http_host(parsed.hostname):
        raise HAConfigurationError("HTTP is allowed only for trusted LAN hosts; use TLS externally")
    endpoint = cfg.ha_obd_import_path.strip()
    if (
        not endpoint.startswith("/api/")
        or "?" in endpoint
        or "#" in endpoint
        or "\r" in endpoint
        or "\n" in endpoint
        or ".." in endpoint.split("/")
    ):
        raise HAConfigurationError("HA_OBD_IMPORT_PATH must be a safe absolute /api path")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/") + endpoint, "", ""))


def load_token(config: AppConfig | None = None) -> str:
    cfg = config or get_config()
    path = cfg.ha_token_file
    if path.is_symlink():
        raise HAConfigurationError("HA token file must not be a symlink")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_BINARY", 0)
    if os.name != "nt":
        flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        raise HAConfigurationError("HA_TOKEN_FILE does not exist") from None
    except OSError as exc:
        raise HAConfigurationError(
            f"HA_TOKEN_FILE cannot be opened safely: {type(exc).__name__}"
        ) from None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size <= 0 or info.st_size > 16 * 1024:
            raise HAConfigurationError(
                "HA_TOKEN_FILE must be a non-empty regular file under 16 KiB"
            )
        if os.name != "nt" and info.st_mode & 0o022:
            raise HAConfigurationError("HA_TOKEN_FILE must not be writable by group or other users")
        chunks: list[bytes] = []
        remaining = 16 * 1024 + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
    except OSError as exc:
        raise HAConfigurationError(f"HA_TOKEN_FILE cannot be read: {type(exc).__name__}") from None
    finally:
        os.close(descriptor)
    if len(raw) > 16 * 1024:
        raise HAConfigurationError("HA_TOKEN_FILE grew beyond 16 KiB while being read")
    try:
        token = raw.decode("utf-8").strip()
    except UnicodeError:
        raise HAConfigurationError("HA_TOKEN_FILE is not valid UTF-8") from None
    if not token or len(token) > 8192 or any(char.isspace() for char in token):
        raise HAConfigurationError("HA_TOKEN_FILE does not contain one valid bearer token")
    return token


def configuration_status(config: AppConfig | None = None) -> tuple[str, str | None]:
    """Return configured/invalid/not_configured without ever returning the secret."""
    cfg = config or get_config()
    if not cfg.ha_url.strip():
        return "not_configured", None
    try:
        import_url(cfg)
        load_token(cfg)
    except HAConfigurationError as exc:
        return "invalid", redact(exc)
    return "configured", None


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After", "").strip()
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            at = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if at.tzinfo is None:
            at = at.replace(tzinfo=UTC)
        return max(0.0, (at.astimezone(UTC) - datetime.now(UTC)).total_seconds())


async def _read_bounded_response(response: httpx.Response) -> tuple[bytes, bool]:
    """Read at most the decoded response limit plus one sentinel byte."""
    body = bytearray()
    limit = MAX_RESPONSE_BYTES + 1
    async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
        remaining = limit - len(body)
        if remaining <= 0:
            break
        body.extend(chunk[:remaining])
        if len(body) >= limit:
            break
    return bytes(body[:MAX_RESPONSE_BYTES]), len(body) > MAX_RESPONSE_BYTES


def _response_message(response: httpx.Response, raw: bytes, *, truncated: bool, token: str) -> str:
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        detail = raw.decode("utf-8", "replace")
    else:
        if isinstance(data, dict):
            detail = data.get("detail") or data.get("errors") or data.get("status") or data
        else:
            detail = data
    if truncated:
        detail = f"{detail} [response truncated at 256 KiB]"
    return redact(f"Home Assistant returned HTTP {response.status_code}: {detail}", token=token)


def _success_result(value: object, *, drive_id: str, token: str) -> dict:
    """Validate and copy only the bounded HA acknowledgement fields persisted in SQLite."""
    if not isinstance(value, dict) or value.get("status") not in {
        "ok",
        "already_imported",
    }:
        raise PermanentImportError(
            "Home Assistant returned an unsupported success response", kind="protocol"
        )
    if value.get("drive_id") != drive_id:
        raise PermanentImportError(
            "Home Assistant success response is missing the requested drive_id",
            kind="protocol",
        )
    extras = set(value) - _SUCCESS_FIELDS
    if extras:
        raise PermanentImportError(
            "Home Assistant success response contains unsupported fields", kind="protocol"
        )
    clean: dict[str, object] = {"status": value["status"], "drive_id": drive_id}
    for field_name in _SUCCESS_COUNTERS:
        if field_name not in value:
            continue
        item = value[field_name]
        if isinstance(item, bool) or not isinstance(item, int) or not 0 <= item <= 2**63 - 1:
            raise PermanentImportError(
                f"Home Assistant success field {field_name} is invalid", kind="protocol"
            )
        clean[field_name] = item
    for field_name in ("warnings", "errors"):
        if field_name not in value:
            continue
        items = value[field_name]
        if (
            not isinstance(items, list)
            or len(items) > 128
            or any(not isinstance(item, str) or len(item) > 1024 for item in items)
        ):
            raise PermanentImportError(
                f"Home Assistant success field {field_name} is invalid", kind="protocol"
            )
        clean[field_name] = [redact(item, token=token) for item in items]
    if "drive_lifecycle" in value:
        lifecycle = value["drive_lifecycle"]
        if not isinstance(lifecycle, dict) or set(lifecycle) != _SUCCESS_LIFECYCLE_FIELDS:
            raise PermanentImportError(
                "Home Assistant drive_lifecycle response is invalid", kind="protocol"
            )
        lifecycle_status = lifecycle.get("status")
        reason = lifecycle.get("interruption_reason")
        clean_end = lifecycle.get("clean_end")
        if lifecycle_status not in {"complete", "interrupted", "recovered"}:
            raise PermanentImportError(
                "Home Assistant drive_lifecycle status is invalid", kind="protocol"
            )
        if reason is not None and (
            not isinstance(reason, str) or not SAFE_REASON_RE.fullmatch(reason)
        ):
            raise PermanentImportError(
                "Home Assistant drive_lifecycle reason is invalid", kind="protocol"
            )
        if not isinstance(clean_end, bool) or (lifecycle_status == "complete") != clean_end:
            raise PermanentImportError(
                "Home Assistant drive_lifecycle clean-end state is invalid", kind="protocol"
            )
        if lifecycle_status == "complete" and reason is not None:
            raise PermanentImportError(
                "Home Assistant drive_lifecycle reason is invalid", kind="protocol"
            )
        if lifecycle_status != "complete" and reason is None:
            raise PermanentImportError(
                "Home Assistant interrupted lifecycle is missing its reason", kind="protocol"
            )
        counters: dict[str, int] = {}
        for field_name in ("sample_count", "expected_sample_count"):
            item = lifecycle.get(field_name)
            if isinstance(item, bool) or not isinstance(item, int) or not 0 <= item <= 2**63 - 1:
                raise PermanentImportError(
                    f"Home Assistant drive_lifecycle {field_name} is invalid", kind="protocol"
                )
            counters[field_name] = item
        if counters["expected_sample_count"] < counters["sample_count"]:
            raise PermanentImportError(
                "Home Assistant drive_lifecycle sample counts are invalid", kind="protocol"
            )
        numbers: dict[str, float] = {}
        for field_name, maximum in (
            ("missing_data_duration_s", 31 * 24 * 3600.0),
            ("received_sample_percentage", 100.0),
        ):
            item = lifecycle.get(field_name)
            if (
                isinstance(item, bool)
                or not isinstance(item, (int, float))
                or not math.isfinite(float(item))
                or not 0 <= float(item) <= maximum
            ):
                raise PermanentImportError(
                    f"Home Assistant drive_lifecycle {field_name} is invalid", kind="protocol"
                )
            numbers[field_name] = float(item)
        gap_count = lifecycle.get("gap_count")
        if gap_count is not None and (
            isinstance(gap_count, bool)
            or not isinstance(gap_count, int)
            or not 0 <= gap_count <= 10_000_000
        ):
            raise PermanentImportError(
                "Home Assistant drive_lifecycle gap_count is invalid", kind="protocol"
            )
        longest_gap = lifecycle.get("longest_gap_s")
        if longest_gap is not None and (
            isinstance(longest_gap, bool)
            or not isinstance(longest_gap, (int, float))
            or not math.isfinite(float(longest_gap))
            or not 0 <= float(longest_gap) <= 31 * 24 * 3600
        ):
            raise PermanentImportError(
                "Home Assistant drive_lifecycle longest_gap_s is invalid", kind="protocol"
            )
        if (gap_count is None) != (longest_gap is None) or (
            gap_count is not None
            and longest_gap is not None
            and ((gap_count == 0) != (float(longest_gap) == 0.0))
        ):
            raise PermanentImportError(
                "Home Assistant drive_lifecycle gap fields disagree", kind="protocol"
            )
        clean["drive_lifecycle"] = {
            "status": lifecycle_status,
            "interruption_reason": reason,
            "clean_end": clean_end,
            **counters,
            **numbers,
            "gap_count": gap_count,
            "longest_gap_s": float(longest_gap) if longest_gap is not None else None,
        }
    return clean


async def post_bundle(
    bundle,
    *,
    lifecycle: dict[str, object] | None = None,
    canonical_summary: dict[str, object] | None = None,
    config: AppConfig | None = None,
) -> dict:
    """Send one bounded, gzip-compressed idempotent batch and classify every failure."""
    cfg = config or get_config()
    url = import_url(cfg)
    try:
        payload = bundle.ha_payload(
            lifecycle=lifecycle,
            canonical_summary=canonical_summary,
        )
        payload["projection_version"] = HA_PROJECTION_VERSION
        if canonical_summary is not None:
            # HA accepts a one-way projection amendment only after proving that the body it
            # already stored is exactly one of the two predecessor contracts.  A lost HA
            # acknowledgement can leave the server unable to know whether v1 or v2 landed,
            # so both bounded candidates are self-contained. V1 used the producer summary,
            # whose own strict window also bounds its diagnostics; v2 used this canonical
            # summary and its narrower aggregate. Any v1 body HA actually accepted already
            # satisfied that window, while later finalisation events remain only in the
            # server's immutable raw history.
            producer_payload = bundle.ha_payload()
            payload["supersedes_projections"] = {
                "1": {
                    "summary": producer_payload["summary"],
                    "diagnostics": producer_payload["diagnostics"],
                },
                "2": {
                    "summary": payload["summary"],
                    "diagnostics": payload["diagnostics"],
                },
            }
        raw = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (HAPayloadError, TypeError, ValueError) as exc:
        raise PermanentImportError(
            f"canonical Home Assistant projection is invalid: {exc}", kind="projection"
        ) from None
    if len(raw) > MAX_HA_BODY_BYTES:
        raise PermanentImportError(
            f"HA import body is {len(raw)} bytes, above the 8 MiB v1 limit", kind="payload"
        )
    token = await asyncio.to_thread(load_token, cfg)
    body = await asyncio.to_thread(gzip.compress, raw, 6)
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Content-Encoding": "gzip",
        "Idempotency-Key": (
            f"{bundle.drive_id}:{bundle.bundle_sha256}:{bundle.schema_version}:"
            f"projection-v{HA_PROJECTION_VERSION}"
        ),
    }
    try:
        async with (
            httpx.AsyncClient(
                timeout=httpx.Timeout(cfg.ha_request_timeout_s),
                follow_redirects=False,
                trust_env=False,
            ) as client,
            client.stream("POST", url, content=body, headers=headers) as response,
        ):
            response_body, response_truncated = await _read_bounded_response(response)
    except httpx.RequestError as exc:
        raise TemporaryImportError(
            redact(f"Home Assistant request failed: {type(exc).__name__}: {exc}", token=token)
        ) from None

    if response.status_code == 200:
        if response_truncated:
            raise PermanentImportError("Home Assistant response exceeds 256 KiB", kind="protocol")
        try:
            result = json.loads(response_body)
        except (ValueError, UnicodeDecodeError):
            raise PermanentImportError(
                "Home Assistant returned HTTP 200 with invalid JSON", kind="protocol"
            ) from None
        return _success_result(result, drive_id=bundle.drive_id, token=token)

    message = _response_message(
        response,
        response_body,
        truncated=response_truncated,
        token=token,
    )
    if response.status_code in {404, 408, 425, 429} or response.status_code >= 500:
        raise TemporaryImportError(
            message,
            status=response.status_code,
            retry_after=_retry_after(response),
        )
    kind = "authentication" if response.status_code in {401, 403} else "permanent"
    raise PermanentImportError(message, status=response.status_code, kind=kind)


def retry_delay(
    attempts: int, *, config: AppConfig | None = None, retry_after: float | None = None
) -> float:
    cfg = config or get_config()
    exponential = cfg.obd_retry_base_s * (2 ** min(max(0, attempts - 1), 20))
    requested = retry_after or 0.0
    return min(cfg.obd_retry_max_s, max(exponential, requested))


_claim_lock = asyncio.Lock()


def queue_claim_lock() -> asyncio.Lock:
    """Shared state-transition lock for worker and manual queue controls."""
    return _claim_lock


async def recover_interrupted_imports(
    *,
    message: str = "Home Assistant import was interrupted by a server restart",
    config: AppConfig | None = None,
) -> int:
    """Recover claims without stranding bytes moved to quarantine before a crash."""
    cfg = config or get_config()
    now = utcnow()
    recovered = 0
    async with session_scope() as session:
        rows = (
            (
                await session.execute(
                    select(OBDBundle).where(OBDBundle.state == OBDBundleState.IMPORTING.value)
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            quarantined = cfg.obd_quarantine_dir / row.filename
            verified = cfg.obd_verified_dir / row.filename
            safe_quarantine = (
                is_bundle_name(row.filename)
                and quarantined.is_file()
                and not quarantined.is_symlink()
                and quarantined.resolve().parent == cfg.obd_quarantine_dir.resolve()
            )
            safe_verified = (
                is_bundle_name(row.filename)
                and verified.is_file()
                and not verified.is_symlink()
                and verified.resolve().parent == cfg.obd_verified_dir.resolve()
            )
            if safe_quarantine and not safe_verified:
                try:
                    observed_hash, observed_size = await asyncio.to_thread(
                        file_sha256,
                        quarantined,
                        maximum=cfg.obd_max_bundle_bytes,
                    )
                except (BundleError, OSError):
                    row.state = OBDBundleState.QUARANTINED.value
                    row.next_attempt_at = None
                    row.failure_kind = "integrity"
                    row.last_error = redact(
                        "interrupted import left an unreadable or oversized regular "
                        "archive retained in quarantine"
                    )
                else:
                    row.state = OBDBundleState.QUARANTINED.value
                    row.next_attempt_at = None
                    row.failure_kind = "integrity"
                    row.last_error = redact(
                        "interrupted import left the retained archive in quarantine "
                        f"(observed sha256={observed_hash}, bytes={observed_size})"
                    )
            if not safe_quarantine or safe_verified:
                row.state = OBDBundleState.RETRY_WAIT.value
                row.next_attempt_at = now
                row.failure_kind = "interrupted"
                row.last_error = redact(message)
            row.import_started_at = None
            row.updated_at = now
            recovered += 1
    return recovered


async def recover_interrupted_validations(*, config: AppConfig | None = None) -> int:
    """Reconcile manual validation claims left behind by a process restart."""
    cfg = config or get_config()
    recovered = 0
    async with session_scope() as session:
        rows = (
            (
                await session.execute(
                    select(OBDBundle).where(OBDBundle.state == OBDBundleState.VALIDATING.value)
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            verified = cfg.obd_verified_dir / row.filename
            quarantined = cfg.obd_quarantine_dir / row.filename
            if (
                quarantined.is_file()
                and not quarantined.is_symlink()
                and quarantined.resolve().parent == cfg.obd_quarantine_dir.resolve()
            ):
                row.state = OBDBundleState.QUARANTINED.value
                row.failure_kind = row.failure_kind or "interrupted"
                row.next_attempt_at = None
            elif (
                verified.is_file()
                and not verified.is_symlink()
                and verified.resolve().parent == cfg.obd_verified_dir.resolve()
                and row.metadata_trusted
            ):
                # Replaying a trusted identity is safe: HA's immutable idempotency key
                # turns a crash after its response into already_imported.
                row.state = OBDBundleState.READY_TO_IMPORT.value
                row.failure_kind = "interrupted"
                row.next_attempt_at = utcnow()
            else:
                row.state = OBDBundleState.FAILED.value
                row.failure_kind = "local_path"
                row.next_attempt_at = None
            row.last_error = redact("bundle validation was interrupted by a server restart")
            row.updated_at = utcnow()
            recovered += 1
    return recovered


async def _claim_next() -> int | None:
    now = utcnow()
    async with _claim_lock, session_scope() as session:
        row = (
            (
                await session.execute(
                    select(OBDBundle)
                    .where(
                        OBDBundle.state.in_(
                            [
                                OBDBundleState.READY_TO_IMPORT.value,
                                OBDBundleState.RETRY_WAIT.value,
                            ]
                        ),
                        or_(
                            OBDBundle.next_attempt_at.is_(None),
                            OBDBundle.next_attempt_at <= now,
                        ),
                    )
                    .order_by(OBDBundle.drive_started_at.asc(), OBDBundle.id.asc())
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )
        if row is None:
            return None
        row.state = OBDBundleState.IMPORTING.value
        row.attempts += 1
        row.import_started_at = now
        row.next_attempt_at = None
        row.last_error = None
        row.failure_kind = None
        row.updated_at = now
        await session.flush()
        return row.id


async def _mark_success(bundle_id: int, result: dict) -> None:
    now = utcnow()
    async with session_scope() as session:
        row = await session.get(OBDBundle, bundle_id)
        if row is None:
            return
        row.state = OBDBundleState.IMPORTED.value
        row.imported_at = now
        row.import_started_at = None
        row.next_attempt_at = None
        row.last_error = None
        row.failure_kind = None
        row.last_http_status = 200
        row.duplicate = result.get("status") == "already_imported"
        row.ha_result = {**result, "_server_projection_version": HA_PROJECTION_VERSION}
        row.updated_at = now


async def _mark_permanent(
    bundle_id: int, error: Exception, *, status: int | None, kind: str, quarantine: bool = False
) -> None:
    now = utcnow()
    async with session_scope() as session:
        row = await session.get(OBDBundle, bundle_id)
        if row is None:
            return
        row.state = OBDBundleState.QUARANTINED.value if quarantine else OBDBundleState.FAILED.value
        row.import_started_at = None
        row.next_attempt_at = None
        row.last_error = redact(error)
        row.failure_kind = kind
        row.last_http_status = status
        row.updated_at = now


async def _mark_temporary(bundle_id: int, error: TemporaryImportError, *, attempts: int) -> None:
    now = utcnow()
    delay = retry_delay(attempts, retry_after=error.retry_after)
    async with session_scope() as session:
        row = await session.get(OBDBundle, bundle_id)
        if row is None:
            return
        row.state = OBDBundleState.RETRY_WAIT.value
        row.import_started_at = None
        row.next_attempt_at = now + timedelta(seconds=delay)
        row.last_error = redact(error)
        row.failure_kind = "temporary"
        row.last_http_status = error.status
        row.updated_at = now


_SERVER_PROJECTION_VERSION_KEY = "_server_projection_version"


def _prior_success_result(row: OBDBundle) -> dict | None:
    """Return only an acknowledgement shape that this server could have persisted."""
    result = row.ha_result
    if not isinstance(result, dict):
        return None
    if result.get("status") not in {"ok", "already_imported"}:
        return None
    if result.get("drive_id") != row.drive_id:
        return None
    if set(result) - (_SUCCESS_FIELDS | {_SERVER_PROJECTION_VERSION_KEY}):
        return None
    return result


def _has_stale_projection_marker(result: dict) -> bool:
    """Accept a missing legacy-v1 marker or a valid older integer, never a rollback."""
    if _SERVER_PROJECTION_VERSION_KEY not in result:
        return True
    version = result[_SERVER_PROJECTION_VERSION_KEY]
    return (
        isinstance(version, int)
        and not isinstance(version, bool)
        and 1 <= version < HA_PROJECTION_VERSION
    )


async def enqueue_stale_projections() -> int:
    """Queue one proven-forward HA refresh when the projection contract changes."""
    queued = 0
    now = utcnow()
    async with session_scope() as session:
        rows = (
            (
                await session.execute(
                    select(OBDBundle).where(
                        OBDBundle.state.in_(
                            [
                                OBDBundleState.IMPORTED.value,
                                OBDBundleState.FAILED.value,
                            ]
                        ),
                        OBDBundle.metadata_trusted.is_(True),
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            result = _prior_success_result(row)
            if result is None or not _has_stale_projection_marker(result):
                continue
            # IMPORTED is itself durable success evidence.  FAILED is safe to revive only
            # when it retains both halves of an earlier committed import: the timestamp and
            # the allowlisted acknowledgement above.  That is the v1/v2-refresh failure
            # pattern; an ordinary first-import failure has no imported_at and stays failed.
            if row.state == OBDBundleState.FAILED.value and row.imported_at is None:
                continue
            row.state = OBDBundleState.READY_TO_IMPORT.value
            row.next_attempt_at = now
            row.import_started_at = None
            row.last_error = None
            row.failure_kind = None
            row.updated_at = now
            queued += 1
    return queued


async def import_one(bundle_id: int) -> None:
    attempts = 0
    path: Path | None = None
    try:
        async with session_scope() as session:
            row = await session.get(OBDBundle, bundle_id)
            if row is None:
                return
            attempts = row.attempts
            path = bundle_path_for(row)
            drive = (
                await session.execute(select(OBDDrive).where(OBDDrive.bundle_id == row.id))
            ).scalar_one_or_none()
            if drive is None:
                raise PermanentImportError(
                    "verified OBD bundle has no drive projection", kind="projection"
                )
            projection = await reconcile_drive_projection(session, drive)
            if projection["status"] != "ready":
                raise PermanentImportError(
                    "OBD lifecycle projection could not be rebuilt", kind="projection"
                )
            reason = drive.interruption_reason
            if reason is not None and not SAFE_REASON_RE.fullmatch(reason):
                reason = "unclean_end"
            lifecycle: dict[str, object] = {
                "lifecycle_status": drive.lifecycle_status,
                "interruption_reason": reason,
                "gap_count": drive.gap_count,
                "longest_gap_s": float(drive.longest_gap_s or 0.0),
            }
            canonical_summary = dict(drive.summary_json)
        assert path is not None
        validated = await asyncio.to_thread(validate_bundle, path)
        if validated.bundle_sha256 != row.bundle_hash:
            raise BundleError("verified bundle SHA-256 changed on disk")
        result = await post_bundle(
            validated,
            lifecycle=lifecycle,
            canonical_summary=canonical_summary,
        )
    except BundleError as exc:
        if path is None:
            await _mark_permanent(bundle_id, exc, status=None, kind="local_path")
            return
        try:
            await asyncio.to_thread(move_to_quarantine, path)
        except (BundleError, OSError) as move_error:
            await _mark_permanent(
                bundle_id,
                move_error,
                status=None,
                kind="quarantine_io",
                quarantine=False,
            )
            return
        await _mark_permanent(bundle_id, exc, status=None, kind="integrity", quarantine=True)
        return
    except HAConfigurationError as exc:
        await _mark_permanent(bundle_id, exc, status=None, kind="configuration")
        return
    except PermanentImportError as exc:
        await _mark_permanent(bundle_id, exc, status=exc.status, kind=exc.kind)
        return
    except TemporaryImportError as exc:
        await _mark_temporary(bundle_id, exc, attempts=attempts)
        return
    await _mark_success(bundle_id, result)


def move_to_quarantine(path: Path, *, config: AppConfig | None = None) -> Path:
    config = config or get_config()
    config.obd_quarantine_dir.mkdir(parents=True, exist_ok=True)
    target = config.obd_quarantine_dir / path.name
    if target.exists():
        old_digest, _ = file_sha256(target, maximum=config.obd_max_bundle_bytes)
        previous = config.obd_quarantine_dir / f"{path.name}.{old_digest[:12]}.bad"
        if previous.exists():
            previous = config.obd_quarantine_dir / (
                f"{path.name}.{old_digest[:12]}.{int(datetime.now(UTC).timestamp())}.bad"
            )
        os.replace(target, previous)
    os.replace(path, target)
    return target


def restore_from_quarantine(path: Path, *, config: AppConfig | None = None) -> Path:
    config = config or get_config()
    source = path.resolve()
    if source.parent != config.obd_quarantine_dir.resolve() or not is_bundle_name(source.name):
        raise BundleError("quarantine source path is unsafe")
    target = config.obd_verified_dir / source.name
    if target.exists():
        raise BundleError("a verified bundle with this filename already exists")
    os.replace(source, target)
    return target


async def rebuild_queue(*, config: AppConfig | None = None) -> dict[str, int]:
    """Recover interrupted claims and register orphan verified bundles without deletion."""
    cfg = config or get_config()
    registered = duplicate = quarantined = 0
    # Serialize orphan/path reconciliation with queue claims. Existing imports are never
    # reset or moved; startup claim recovery happens separately before the worker begins.
    async with _claim_lock:
        for path in sorted(cfg.obd_verified_dir.iterdir(), key=lambda item: item.name):
            if not path.is_file() or not is_bundle_name(path.name):
                continue
            async with session_scope() as session:
                known = (
                    await session.execute(select(OBDBundle).where(OBDBundle.filename == path.name))
                ).scalar_one_or_none()
            if known is not None and known.state in {
                OBDBundleState.WAITING_FOR_BACKUP.value,
                OBDBundleState.COPYING.value,
                OBDBundleState.VALIDATING.value,
                OBDBundleState.IMPORTING.value,
            }:
                duplicate += 1
                continue
            repairable = known is not None and (
                not known.metadata_trusted
                or known.state == OBDBundleState.QUARANTINED.value
                or (
                    known.state == OBDBundleState.FAILED.value
                    and known.failure_kind in {"integrity", "local_path", "quarantine_io"}
                )
            )
            # Known immutable copies are revalidated immediately before an HA attempt and
            # on explicit Validate. Rehashing every already-imported drive at every boot
            # makes startup cost grow without bound, but repair candidates must not be
            # skipped merely because a fresh copy has the same byte length.
            if (
                known is not None
                and known.verified_at is not None
                and known.size_bytes == path.stat().st_size
                and not repairable
            ):
                duplicate += 1
                continue
            try:
                bundle = await asyncio.to_thread(validate_bundle, path, config=cfg)
                async with session_scope() as session:
                    before = (
                        await session.execute(
                            select(OBDBundle.id).where(
                                OBDBundle.drive_id == bundle.drive_id,
                                OBDBundle.bundle_hash == bundle.bundle_sha256,
                                OBDBundle.schema_version == bundle.schema_version,
                            )
                        )
                    ).scalar_one_or_none()
                    registered_row = await store_validated_bundle(session, bundle)
                    if before is None:
                        registered += 1
                    else:
                        duplicate += 1
                if registered_row.state == OBDBundleState.READY_TO_IMPORT.value:
                    stale = cfg.obd_quarantine_dir / path.name
                    if (
                        stale.is_file()
                        and not stale.is_symlink()
                        and stale.resolve().parent == cfg.obd_quarantine_dir.resolve()
                    ):
                        try:
                            await asyncio.to_thread(stale.unlink)
                        except OSError as cleanup_error:
                            # Registration and raw-history persistence already committed.
                            # A harmless stale quarantine copy must not stop later orphans
                            # from being discovered during this rebuild pass.
                            log.warning(
                                "could not remove a stale OBD quarantine copy during rebuild",
                                bundle=path.name,
                                error=redact(cleanup_error),
                            )
            except BundleError as exc:
                quarantined += 1
                observed_hash: str | None = None
                observed_size = 0
                try:
                    observed_hash, observed_size = await asyncio.to_thread(file_sha256, path)
                except (BundleError, OSError) as hash_error:
                    log.warning(
                        "could not fingerprint an invalid OBD bundle",
                        bundle=path.name,
                        error=redact(hash_error),
                    )
                moved = False
                try:
                    await asyncio.to_thread(move_to_quarantine, path, config=cfg)
                    moved = True
                except (OSError, BundleError) as move_error:
                    log.warning(
                        "could not move an invalid OBD bundle to quarantine",
                        bundle=path.name,
                        error=redact(move_error),
                    )
                async with session_scope() as session:
                    if observed_hash is not None and (known is None or not known.metadata_trusted):
                        await store_rejected_bundle(
                            session,
                            filename=path.name,
                            bundle_hash=observed_hash,
                            size_bytes=observed_size,
                            error=redact(exc),
                            quarantined=moved,
                        )
                    elif known is not None:
                        row = await session.get(OBDBundle, known.id)
                        if row is not None:
                            row.state = (
                                OBDBundleState.QUARANTINED.value
                                if moved
                                else OBDBundleState.FAILED.value
                            )
                            row.next_attempt_at = None
                            row.import_started_at = None
                            row.failure_kind = "integrity" if moved else "quarantine_io"
                            row.last_error = redact(exc)
                            row.updated_at = utcnow()
                log.warning(
                    "quarantined an invalid OBD bundle during queue rebuild",
                    error=redact(exc),
                )
    return {
        "recovered_imports": 0,
        "registered": registered,
        "duplicates": duplicate,
        "quarantined": quarantined,
    }


class HAImportWorker:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        # Cheap DB-only reconciliation before a claim is possible.  Full orphan scanning
        # runs in the worker task after lifespan returns so /health is never gated by a
        # growing archive of retained, already-imported bundles.
        await recover_interrupted_imports()
        await recover_interrupted_validations()
        self._task = asyncio.create_task(self._run(), name="ha-obd-import")

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def wake(self) -> None:
        self._wake.set()

    async def _run(self) -> None:
        try:
            await rebuild_queue()
            queued = await enqueue_stale_projections()
            if queued:
                log.info(
                    "queued Home Assistant refreshes for a newer OBD projection",
                    drives=queued,
                    projection_version=HA_PROJECTION_VERSION,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("OBD queue rebuild failed; the worker will continue", error=redact(exc))
        while not self._stop.is_set():
            self._wake.clear()
            try:
                bundle_id = await _claim_next()
                if bundle_id is not None:
                    await import_one(bundle_id)
                    continue
                if self._stop.is_set():
                    break
                await asyncio.wait_for(self._wake.wait(), timeout=get_config().obd_import_poll_s)
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                continue
            except Exception as exc:
                # A transient SQLite/filesystem defect must not silently kill an
                # independent durable queue. Keep a small bounded delay to avoid a tight
                # error loop while still recovering without a process restart.
                log.exception("OBD import worker iteration failed", error=redact(exc))
                try:
                    await recover_interrupted_imports(
                        message="Home Assistant import was interrupted by a worker error"
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as recovery_error:
                    log.exception(
                        "OBD import claim recovery also failed",
                        error=redact(recovery_error),
                    )
                try:
                    await asyncio.wait_for(
                        self._stop.wait(),
                        timeout=min(5.0, get_config().obd_import_poll_s),
                    )
                except TimeoutError:
                    pass


_worker: HAImportWorker | None = None


def get_import_worker() -> HAImportWorker:
    global _worker
    if _worker is None:
        _worker = HAImportWorker()
    return _worker


async def queue_counts() -> dict[str, int | str | None]:
    async with session_scope() as session:
        rows = (
            await session.execute(select(OBDBundle.state, func.count()).group_by(OBDBundle.state))
        ).all()
        counts = {state: int(count) for state, count in rows}
    return counts


__all__ = [
    "HAConfigurationError",
    "HAImportWorker",
    "PermanentImportError",
    "TemporaryImportError",
    "configuration_status",
    "enqueue_stale_projections",
    "get_import_worker",
    "import_one",
    "import_url",
    "load_token",
    "move_to_quarantine",
    "post_bundle",
    "queue_claim_lock",
    "rebuild_queue",
    "recover_interrupted_imports",
    "recover_interrupted_validations",
    "redact",
    "restore_from_quarantine",
    "retry_delay",
]
