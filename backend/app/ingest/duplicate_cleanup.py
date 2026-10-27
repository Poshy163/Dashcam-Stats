"""Bounded, optional reclaim of old card files with a complete server-side copy.

The delta's same-name/size observation is only a candidate. A private independent copy,
two full local reads and the ADB helper's full remote proofs precede any deletion. The
private copy survives an uncertain acknowledgement or a concurrent retention unlink.
Unresolved ``.verified`` files and their receipts are deliberately never auto-pruned:
restore/reconcile them with the named library recording before removing either file.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import math
import os
import stat
import tempfile
import threading
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import TypeVar

from app.core.logging import get_logger
from app.ingest import adb
from app.ingest.models import RemoteFile, UnitInfo

log = get_logger(__name__)
PRESERVATION_DIRNAME = ".ingest-duplicate-preservation"
MAX_FILES = 4
MAX_BYTES = 256 * 1024 * 1024
MAX_PASS_SECONDS = 45.0
NATIVE_RESERVE_SECONDS = 15.0
REMOTE_HASH_SECONDS = 10.0
_CHUNK_BYTES = 1024 * 1024

# A timeout cannot interrupt a blocked NAS syscall. Keep this one optional slot owned
# until the late worker has closed its own descriptors and finished safe cleanup.
_local_io_slot = threading.Lock()
_T = TypeVar("_T")


@dataclass(slots=True)
class DuplicateCleanupResult:
    attempted_files: int = 0
    verified_files: int = 0
    deleted_files: int = 0
    deleted_bytes: int = 0
    retained_preservations: int = 0
    skipped_files: int = 0
    stop_reason: str | None = None


class _LocalRefusal(Exception):
    """A bounded reason, never an OS error or untrusted path."""


@dataclass(frozen=True, slots=True)
class _Preservation:
    root: Path
    original: Path
    root_identity: tuple[int, ...]
    private_identity: tuple[int, ...]
    original_identity: tuple[int, ...]
    snapshot: Path
    snapshot_identity: tuple[int, ...]
    receipt: Path
    name: str
    size: int
    sha256: str


def _identity(value: os.stat_result) -> tuple[int, ...]:
    # Windows path-stat and handle-stat can disagree on change-time after a hardlink
    # publication. Its stable birth time supplements file ID there; full hashes still
    # prove content. POSIX supplies consistent nanosecond ctime as an extra mutation gate.
    changed = getattr(value, "st_birthtime_ns", 0) if os.name == "nt" else value.st_ctime_ns
    return (value.st_dev, value.st_ino, value.st_mode, value.st_size, value.st_mtime_ns, changed)


def _directory_identity(path: Path) -> tuple[int, ...]:
    value = path.stat()
    if not stat.S_ISDIR(value.st_mode):
        raise _LocalRefusal("invalid_footage_root")
    return value.st_dev, value.st_ino


def _sync_directory(path: Path) -> None:
    # Same portability boundary as the normal ingest commit: Windows cannot fsync a
    # directory handle, while its flushed file + rename semantics remain available.
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _private_directory(root: Path) -> Path:
    private = root / PRESERVATION_DIRNAME
    try:
        private.mkdir(mode=0o700)
    except FileExistsError:
        pass
    value = private.lstat()
    if not stat.S_ISDIR(value.st_mode) or private.resolve() != private:
        raise _LocalRefusal("unsafe_preservation_directory")
    # Repeat on retry: an earlier mkdir may have succeeded before its parent fsync failed.
    _sync_directory(root)
    return private


def _open_original(path: Path, size: int):
    try:
        before = path.lstat()
    except FileNotFoundError as exc:
        raise _LocalRefusal("local_not_regular_or_size_changed") from exc
    if not stat.S_ISREG(before.st_mode) or before.st_size != size:
        raise _LocalRefusal("local_not_regular_or_size_changed")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError as exc:
        raise _LocalRefusal("local_identity_changed") from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or _identity(opened) != _identity(before):
            raise _LocalRefusal("local_identity_changed")
        return os.fdopen(descriptor, "rb"), _identity(opened)
    except BaseException:
        os.close(descriptor)
        raise


def _write_receipt(copy: _Preservation, phase: str, reason: str | None = None) -> None:
    _check_private_directory(copy)
    payload = {
        "schema_version": 1,
        "recording_name": copy.name,
        "preserved_file": copy.snapshot.name,
        "size_bytes": copy.size,
        "sha256": copy.sha256,
        "phase": phase,
        "reason": reason,
        "updated_at_utc": datetime.now(UTC).isoformat(),
        "recovery": "Restore or reconcile this verified copy with the named library file before removing it.",
    }
    descriptor, temporary = tempfile.mkstemp(prefix=".receipt-", dir=copy.snapshot.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, copy.receipt)
        _sync_directory(copy.snapshot.parent)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(temporary)


def _discard_preservation(copy: _Preservation) -> None:
    _check_private_directory(copy)
    # Never unlink a replacement planted at a private filename we no longer own.
    try:
        if _identity(copy.snapshot.lstat()) != copy.snapshot_identity:
            return
    except FileNotFoundError:
        copy.receipt.unlink(missing_ok=True)
        _sync_directory(copy.snapshot.parent)
        return
    copy.snapshot.unlink()
    copy.receipt.unlink(missing_ok=True)
    _sync_directory(copy.snapshot.parent)


def _prepare_preservation(footage: Path, item: RemoteFile) -> _Preservation:
    root = footage.resolve(strict=True)
    root_identity = _directory_identity(root)
    private = _private_directory(root)
    private_identity = _directory_identity(private)
    original = root / item.name
    source, before = _open_original(original, item.size)
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".copy-", suffix=".partial", dir=private
        )
    except BaseException:
        source.close()
        raise
    temporary = Path(temporary_name)
    copy: _Preservation | None = None
    snapshot: Path | None = None
    published = False
    copied_inode: tuple[int, ...] | None = None
    try:
        digest = hashlib.sha256()
        total = 0
        with source, os.fdopen(descriptor, "wb") as output:
            copied_inode = _identity(os.fstat(output.fileno()))[:2]
            while chunk := source.read(_CHUNK_BYTES):
                total += len(chunk)
                if total > item.size:
                    raise _LocalRefusal("local_size_changed")
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
            if (
                total != item.size
                or _identity(os.fstat(source.fileno())) != before
                or _identity(original.lstat()) != before
                or _directory_identity(root) != root_identity
                or private.resolve() != private
                or _directory_identity(private) != private_identity
            ):
                raise _LocalRefusal("local_identity_changed")
        checksum = digest.hexdigest()
        key = hashlib.sha256(f"{item.name}\0{checksum}".encode()).hexdigest()
        snapshot = private / f"{key}.verified"
        receipt = private / f"{key}.json"
        if os.path.lexists(snapshot) or os.path.lexists(receipt):
            snapshot = None
            raise _LocalRefusal("preservation_pending")
        # Linking the new, independently copied file publishes without overwriting an
        # unresolved preservation. This never links the mutable original recording.
        os.link(temporary, snapshot)
        published = True
        temporary.unlink()
        _sync_directory(private)
        copy = _Preservation(
            root,
            original,
            root_identity,
            private_identity,
            before,
            snapshot,
            _identity(snapshot.lstat()),
            receipt,
            item.name,
            item.size,
            checksum,
        )
        _write_receipt(copy, "prepared")
        return copy
    except BaseException:
        if copy is not None:
            with contextlib.suppress(OSError):
                _discard_preservation(copy)
        elif snapshot is not None and published:
            with contextlib.suppress(OSError):
                if _identity(snapshot.lstat())[:2] == copied_inode:
                    snapshot.unlink()
        raise
    finally:
        source.close()
        with contextlib.suppress(OSError):
            temporary.unlink()


def _check_private_directory(copy: _Preservation) -> None:
    private = copy.snapshot.parent
    value = private.lstat()
    if (
        not stat.S_ISDIR(value.st_mode)
        or _directory_identity(private) != copy.private_identity
        or private.resolve() != private
        or _directory_identity(copy.root) != copy.root_identity
    ):
        raise _LocalRefusal("preservation_directory_changed")


def _file_still_matches(path: Path, size: int, identity: tuple[int, ...], checksum: str) -> bool:
    try:
        source, before = _open_original(path, size)
        with source:
            if before != identity:
                return False
            digest = hashlib.sha256()
            total = 0
            while chunk := source.read(_CHUNK_BYTES):
                total += len(chunk)
                if total > size:
                    return False
                digest.update(chunk)
            return (
                total == size
                and digest.hexdigest() == checksum
                and _identity(os.fstat(source.fileno())) == before
                and _identity(path.lstat()) == before
            )
    except (OSError, _LocalRefusal):
        return False


def _original_still_matches(copy: _Preservation) -> bool:
    try:
        return (
            _directory_identity(copy.root) == copy.root_identity
            and _file_still_matches(copy.original, copy.size, copy.original_identity, copy.sha256)
            and _directory_identity(copy.root) == copy.root_identity
        )
    except OSError:
        return False


def _arm_deletion(copy: _Preservation) -> None:
    _check_private_directory(copy)
    if not _file_still_matches(copy.snapshot, copy.size, copy.snapshot_identity, copy.sha256):
        raise _LocalRefusal("preservation_changed")
    _write_receipt(copy, "delete_requested")


async def _local_io(
    operation: Callable[[], _T], timeout: float, *, late_cleanup: Callable[[_T], None] | None = None
) -> _T:
    if not _local_io_slot.acquire(blocking=False):
        raise _LocalRefusal("local_io_pending")
    abandoned = threading.Event()

    def worker() -> _T:
        try:
            value = operation()
            if abandoned.is_set() and late_cleanup is not None:
                late_cleanup(value)
            return value
        finally:
            # The actual thread owns this release. Cancelling an asyncio wrapper or
            # shutting down its loop cannot release a slot while NAS IO is still alive.
            _local_io_slot.release()

    task = asyncio.create_task(asyncio.to_thread(worker))
    try:
        result = await asyncio.wait_for(asyncio.shield(task), timeout)
    except (TimeoutError, asyncio.CancelledError):
        abandoned.set()

        # Consume late errors without an awaiter; the thread retains all IO ownership.
        def completed(done: asyncio.Task[_T]) -> None:
            if not done.cancelled():
                done.exception()

        task.add_done_callback(completed)
        raise
    return result


async def cleanup_verified_duplicates(
    info: UnitInfo,
    footage: Path,
    candidates: list[RemoteFile],
    *,
    native_remaining: Callable[[], float | None],
    cancelled: Callable[[], bool] = lambda: False,
    budget_s: float = MAX_PASS_SECONDS,
) -> DuplicateCleanupResult:
    """Call only after normal transfer drains, while the ingest run still owns the unit."""
    result = DuplicateCleanupResult()
    if _local_io_slot.locked():
        result.stop_reason = "local_io_pending"
        return result
    deadline = time.monotonic() + max(0.0, min(MAX_PASS_SECONDS, budget_s))
    collisions = {
        name for name, count in Counter(item.name for item in candidates).items() if count > 1
    }
    attempted_bytes = 0

    def remaining() -> float:
        if cancelled():
            raise _LocalRefusal("cancelled")
        native = native_remaining()
        if native is None or not math.isfinite(native) or native <= NATIVE_RESERVE_SECONDS:
            raise _LocalRefusal("native_window_unavailable_or_short")
        budget = min(deadline - time.monotonic(), native - NATIVE_RESERVE_SECONDS)
        if budget <= 0:
            raise _LocalRefusal("budget_exhausted")
        return budget

    async def parked() -> None:
        timeout = min(3.0, remaining())
        async with asyncio.timeout(timeout):
            state = await adb.ignition_state(info.address, timeout=timeout)
        if state != "off":
            raise _LocalRefusal("ignition_not_off")

    for item in candidates:
        if result.attempted_files >= MAX_FILES:
            result.stop_reason = "file_budget"
            break
        if (
            item.name in collisions
            or not adb.is_safe_name(item.name)
            or type(item.size) is not int
            or item.size <= 0
        ):
            result.skipped_files += 1
            continue
        if item.size > MAX_BYTES - attempted_bytes:
            result.skipped_files += 1
            continue
        copy: _Preservation | None = None
        destructive = False
        try:
            remaining()
            result.attempted_files += 1
            attempted_bytes += item.size
            copy = await _local_io(
                partial(_prepare_preservation, footage, item),
                remaining(),
                late_cleanup=_discard_preservation,
            )
            await parked()
            timeout = min(REMOTE_HASH_SECONDS, remaining())
            async with asyncio.timeout(timeout):
                proof = await adb.verify_remote_file(
                    info.address,
                    item.directory or info.source or "",
                    item,
                    copy.sha256,
                    timeout=timeout,
                )
            if proof is None:
                raise _LocalRefusal("remote_not_verified")
            if not await _local_io(partial(_original_still_matches, copy), remaining()):
                raise _LocalRefusal("local_identity_changed")
            result.verified_files += 1
            await parked()
            await _local_io(partial(_arm_deletion, copy), remaining())
            timeout = min(REMOTE_HASH_SECONDS, remaining())
            destructive = True
            async with asyncio.timeout(timeout):
                deleted = await adb.delete_verified_remote_file(
                    info.address, item.directory or info.source or "", item, proof, timeout=timeout
                )
            if not deleted:
                raise _LocalRefusal("delete_unconfirmed")
            result.deleted_files += 1
            result.deleted_bytes += item.size
            if not await _local_io(partial(_original_still_matches, copy), remaining()):
                raise _LocalRefusal("canonical_changed_after_delete")
            await _local_io(partial(_discard_preservation, copy), remaining())
            copy = None
        except asyncio.CancelledError:
            if copy is not None and destructive:
                log.warning(
                    "duplicate cleanup preservation retained",
                    reason="cancelled_after_delete_request",
                )
            elif copy is not None and not _local_io_slot.locked():
                with contextlib.suppress(Exception):
                    await _local_io(partial(_discard_preservation, copy), 1.0)
            elif copy is not None:
                log.warning(
                    "duplicate cleanup preservation retained",
                    reason="cancelled_with_local_io_pending",
                )
            raise
        except Exception as exc:
            reason = (
                str(exc)
                if isinstance(exc, _LocalRefusal)
                else (
                    "operation_timeout"
                    if isinstance(exc, TimeoutError)
                    else "verification_unavailable"
                )
            )
            result.skipped_files += 1
            result.stop_reason = reason
            if copy is not None:
                if destructive or _local_io_slot.locked():
                    result.retained_preservations += 1
                    if not _local_io_slot.locked():
                        with contextlib.suppress(Exception):
                            await _local_io(partial(_write_receipt, copy, "retained", reason), 1.0)
                else:
                    try:
                        await _local_io(
                            partial(_discard_preservation, copy),
                            min(1.0, max(0.01, deadline - time.monotonic())),
                        )
                    except Exception:
                        result.retained_preservations += 1
            if (
                not destructive
                and reason
                in {
                    "preservation_pending",
                    "local_not_regular_or_size_changed",
                    "local_identity_changed",
                }
                and not _local_io_slot.locked()
            ):
                result.stop_reason = None
                continue
            break
    log.info(
        "verified duplicate cleanup finished",
        attempted=result.attempted_files,
        verified=result.verified_files,
        deleted=result.deleted_files,
        deleted_bytes=result.deleted_bytes,
        preserved=result.retained_preservations,
        reason=result.stop_reason,
    )
    return result
