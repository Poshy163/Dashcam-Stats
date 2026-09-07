"""Small shared helpers for durable OBD bundle storage."""

from __future__ import annotations

import asyncio
import os
import re
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from app.config import AppConfig, get_config
from app.core.logging import get_logger
from app.db.models import OBDBundle, OBDBundleState, utcnow
from app.db.session import session_scope
from app.ingest.obd_bundle import (
    BundleError,
    file_sha256,
    is_bundle_name,
    store_rejected_bundle,
    store_validated_bundle,
    validate_bundle,
)

log = get_logger(__name__)

MAX_ERROR_TEXT = 1000
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{8,}")
_storage_claim_lock = asyncio.Lock()


def redact(value: object, *, token: str | None = None) -> str:
    """Return bounded single-line error text with bearer credentials removed."""
    text = str(value).replace("\r", " ").replace("\n", " ")
    text = _BEARER_RE.sub("Bearer [REDACTED]", text)
    if token:
        text = text.replace(token, "[REDACTED]")
    return text[:MAX_ERROR_TEXT]


def storage_claim_lock() -> asyncio.Lock:
    """Serialize manual validation with bundle registration and repair."""
    return _storage_claim_lock


async def recover_interrupted_validations(*, config: AppConfig | None = None) -> int:
    """Resolve manual validation claims left behind by a process restart.

    The filesystem is authoritative for which safe state can be resumed. Existing drive,
    sample and diagnostic rows are never touched.
    """
    config = config or get_config()
    recovered = 0
    async with storage_claim_lock(), session_scope() as session:
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
            verified = config.obd_verified_dir / row.filename
            quarantined = config.obd_quarantine_dir / row.filename
            if (
                quarantined.is_file()
                and not quarantined.is_symlink()
                and quarantined.resolve().parent == config.obd_quarantine_dir.resolve()
            ):
                row.state = OBDBundleState.QUARANTINED.value
                row.failure_kind = row.failure_kind or "interrupted"
            elif (
                verified.is_file()
                and not verified.is_symlink()
                and verified.resolve().parent == config.obd_verified_dir.resolve()
                and row.metadata_trusted
            ):
                try:
                    digest, size = await asyncio.to_thread(
                        file_sha256, verified, maximum=config.obd_max_bundle_bytes
                    )
                except (BundleError, OSError):
                    digest, size = "", -1
                if digest == row.bundle_hash and size == row.size_bytes:
                    row.state = OBDBundleState.STORED.value
                    row.failure_kind = None
                    row.last_error = None
                    row.verified_at = row.verified_at or utcnow()
                    row.updated_at = utcnow()
                    recovered += 1
                    continue
                try:
                    await asyncio.to_thread(move_to_quarantine, verified, config=config)
                except (BundleError, OSError):
                    row.state = OBDBundleState.FAILED.value
                    row.failure_kind = "quarantine_io"
                else:
                    row.state = OBDBundleState.QUARANTINED.value
                    row.failure_kind = "integrity"
            else:
                row.state = OBDBundleState.FAILED.value
                row.failure_kind = "local_path"
            row.last_error = redact("bundle validation was interrupted by a server restart")
            row.updated_at = utcnow()
            recovered += 1
    return recovered


async def reconcile_orphan_bundles(*, config: AppConfig | None = None) -> dict[str, int]:
    """Register orphan verified bundles and quarantine invalid copies without deletion."""
    config = config or get_config()
    registered = duplicate = quarantined_count = 0
    async with storage_claim_lock():
        for path in sorted(config.obd_verified_dir.iterdir(), key=lambda item: item.name):
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
            if (
                known is not None
                and known.verified_at is not None
                and known.size_bytes == path.stat().st_size
                and not repairable
            ):
                duplicate += 1
                continue
            try:
                bundle = await asyncio.to_thread(validate_bundle, path, config=config)
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
                    stored = await store_validated_bundle(session, bundle)
                    if before is None:
                        registered += 1
                    else:
                        duplicate += 1
                if stored.state == OBDBundleState.STORED.value:
                    stale = config.obd_quarantine_dir / path.name
                    if (
                        stale.is_file()
                        and not stale.is_symlink()
                        and stale.resolve().parent == config.obd_quarantine_dir.resolve()
                    ):
                        try:
                            await asyncio.to_thread(stale.unlink)
                        except OSError as exc:
                            log.warning(
                                "could not remove stale OBD quarantine copy",
                                bundle=path.name,
                                error=redact(exc),
                            )
            except BundleError as exc:
                quarantined_count += 1
                observed_hash: str | None = None
                observed_size = 0
                try:
                    observed_hash, observed_size = await asyncio.to_thread(file_sha256, path)
                except (BundleError, OSError) as hash_error:
                    log.warning(
                        "could not fingerprint invalid OBD bundle",
                        bundle=path.name,
                        error=redact(hash_error),
                    )
                moved = False
                try:
                    await asyncio.to_thread(move_to_quarantine, path, config=config)
                    moved = True
                except (BundleError, OSError) as move_error:
                    log.warning(
                        "could not quarantine invalid OBD bundle",
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
                            row.failure_kind = "integrity" if moved else "quarantine_io"
                            row.last_error = redact(exc)
                            row.updated_at = utcnow()
                log.warning("quarantined invalid OBD bundle", bundle=path.name, error=redact(exc))
    return {
        "registered": registered,
        "duplicates": duplicate,
        "quarantined": quarantined_count,
    }


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
