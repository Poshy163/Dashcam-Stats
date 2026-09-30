"""Online SQLite backups and restart-safe restore staging."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

from app.config import get_config

_RESTORE_PUBLISH_LOCK = threading.Lock()
RESTORE_DISK_RESERVE = 64 * 1024**2


class RestoreTooLarge(ValueError):
    pass


class RestoreStorageError(OSError):
    pass


class RestoreValidationUnavailable(RuntimeError):
    pass


def _sync_directory(path: Path) -> None:
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


@lru_cache(maxsize=32)
def _expected_schema(revision: str) -> dict:
    # A subprocess is deliberate: Alembic uses process-global contexts, and historical
    # migration bodies may touch configuration/cache files. Neither may affect this app.
    try:
        result = subprocess.run(
            [sys.executable, "-m", "app.db.restore_schema", revision],
            cwd=Path(__file__).resolve().parents[2],
            capture_output=True,
            text=True,
            timeout=120,
            check=True,
        )
        return json.loads(result.stdout.strip().splitlines()[-1])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError) as exc:
        raise RestoreValidationUnavailable(
            "Backup compatibility validation is temporarily unavailable"
        ) from exc


def _validate_compatibility(connection: sqlite3.Connection) -> str:
    from alembic.script import ScriptDirectory

    from app.db.restore_schema import describe_schema
    from app.db.session import alembic_config

    revisions = connection.execute("SELECT version_num FROM alembic_version").fetchall()
    if len(revisions) != 1:
        raise ValueError("Backup must contain exactly one supported migration revision")
    revision = revisions[0][0]
    supported = {
        entry.revision for entry in ScriptDirectory.from_config(alembic_config()).walk_revisions()
    }
    if revision not in supported:
        raise ValueError("Backup migration revision is not supported by this application version")
    actual = describe_schema(connection)
    for table, expected_table in _expected_schema(revision).items():
        if table not in actual:
            raise ValueError(f"Backup schema is missing required table {table}")
        for name, expected in expected_table["columns"].items():
            if actual[table]["columns"].get(name) != expected:
                raise ValueError(f"Backup schema is incompatible at {table}.{name}")
        if actual[table]["foreign_keys"] != expected_table["foreign_keys"]:
            raise ValueError(f"Backup foreign-key schema is incompatible at {table}")
        if [index for index in actual[table]["indexes"] if index["unique"]] != [
            index for index in expected_table["indexes"] if index["unique"]
        ]:
            raise ValueError(f"Backup unique constraints are incompatible at {table}")
        for index in expected_table["indexes"]:
            if index not in actual[table]["indexes"]:
                raise ValueError(f"Backup index or unique constraint is missing at {table}")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise ValueError("Backup contains invalid foreign-key references")
    return revision


def backup_dir() -> Path:
    path = get_config().data_dir / "backups"
    path.mkdir(parents=True, exist_ok=True)
    return path


def validate_database(path: Path, *, compatible: bool = False) -> str | None:
    # Validation only receives standalone snapshots, never the active WAL database.
    # Immutable reads avoid creating sidecars beside uploads and cannot change bytes.
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro&immutable=1", uri=True)
    try:
        result = connection.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            raise ValueError(
                f"SQLite integrity check failed: {result[0] if result else 'no result'}"
            )
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        required = {"recordings", "processing_jobs", "alembic_version"}
        missing = required - tables
        if missing:
            raise ValueError(
                f"Not a Dashcam Analyser database; missing {', '.join(sorted(missing))}"
            )
        if compatible:
            return _validate_compatibility(connection)
        return None
    except sqlite3.DatabaseError as exc:
        raise ValueError("Backup is not a valid SQLite application database") from exc
    finally:
        connection.close()


def _create_snapshot(target: Path, *, checkpoint: bool = False) -> Path:
    """Keep incomplete snapshots private, and leave room for the live database."""
    source_path = get_config().db_path
    if not source_path.is_file():
        raise FileNotFoundError("Database file does not exist")
    source = destination = None
    temporary = None
    try:
        source = sqlite3.connect(str(source_path))
        if checkpoint:
            result = source.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if result and result[0]:
                raise OSError("Cannot restore while the existing database is busy")
        page_size = source.execute("PRAGMA page_size").fetchone()[0]
        page_count = source.execute("PRAGMA page_count").fetchone()[0]

        def check_space(_status: int, remaining: int, _total: int) -> None:
            if shutil.disk_usage(target.parent).free < remaining * page_size + RESTORE_DISK_RESERVE:
                raise RestoreStorageError("Insufficient free space to safely create the backup")

        check_space(0, page_count, page_count)
        descriptor, name = tempfile.mkstemp(prefix="backup-", suffix=".tmp", dir=target.parent)
        temporary = Path(name)
        os.close(descriptor)
        destination = sqlite3.connect(str(temporary))
        source.backup(destination, pages=1024, progress=check_space)
        destination.close()
        destination = None
        validate_database(temporary)
        with temporary.open("r+b") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        _sync_directory(target.parent)
        return target
    finally:
        if destination is not None:
            destination.close()
        if source is not None:
            source.close()
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def create_backup() -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
    return _create_snapshot(backup_dir() / f"dashcam-{stamp}.db")


def create_pre_migration_backup(from_revision: str, to_revision: str) -> Path:
    """Create and validate an atomic snapshot before Alembic changes an existing DB."""
    safe_from = "".join(char for char in from_revision if char.isalnum() or char in "-_")
    safe_to = "".join(char for char in to_revision if char.isalnum() or char in "-_")
    if not safe_from or not safe_to:
        raise ValueError("Migration revisions are not safe backup filename components")
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
    directory = backup_dir()
    target = directory / f"pre-migration-{safe_from}-to-{safe_to}-{stamp}.db"
    return _create_snapshot(target)


def ensure_plate_repair_backup(revision: str) -> Path:
    """One atomically published recovery snapshot before each plate algorithm upgrade."""
    if not revision or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in revision):
        raise ValueError("Invalid plate revision")
    target = backup_dir() / f"before-{revision}.db"
    if target.is_file():
        return target
    # Reuse the validated, fsynced online-backup implementation. Publishing a stable
    # name means restarts/sweeps do not create another gigabyte-sized backup each time.
    temporary = create_pre_migration_backup("plate-repair", revision)
    os.replace(temporary, target)
    return target


class RestoreUpload:
    """One bounded upload, with private bytes until validation and atomic publication."""

    def __init__(self, expected_size: int | None = None):
        self.limit = get_config().restore_max_bytes
        if expected_size is not None and expected_size > self.limit:
            raise RestoreTooLarge(f"Backup exceeds the {self.limit} byte restore limit")
        self.directory = backup_dir()
        self._check_space(expected_size or 0)
        fd, name = tempfile.mkstemp(prefix="restore-upload-", suffix=".tmp", dir=self.directory)
        self.path = Path(name)
        self.handle = os.fdopen(fd, "wb")
        self.size = 0
        self.expected_size = expected_size
        self.revision: str | None = None
        self.abandoned = threading.Event()

    def _check_space(self, additional: int) -> None:
        if shutil.disk_usage(self.directory).free < additional + RESTORE_DISK_RESERVE:
            raise RestoreStorageError("Insufficient free space to safely receive the backup")

    def write(self, data: bytes) -> None:
        if self.size + len(data) > self.limit:
            raise RestoreTooLarge(f"Backup exceeds the {self.limit} byte restore limit")
        self._check_space(len(data))
        self.handle.write(data)
        self.size += len(data)

    def validate(self) -> None:
        if not self.size:
            raise ValueError("Backup is empty")
        if self.expected_size is not None and self.size != self.expected_size:
            raise ValueError("Backup upload is incomplete")
        self.handle.flush()
        os.fsync(self.handle.fileno())
        self.handle.close()
        self.revision = validate_database(self.path, compatible=True)

    def publish(self) -> Path:
        if self.revision is None:
            raise ValueError("Backup must be validated before publication")
        pending = self.directory / "restore.pending.db"
        # Concurrent complete uploads may replace each other, but can never mix bytes or
        # validate one request's file and publish another's. Last completed publish wins.
        with _RESTORE_PUBLISH_LOCK:
            if self.abandoned.is_set():
                raise ValueError("Backup upload was cancelled")
            os.replace(self.path, pending)
            _sync_directory(self.directory)
        return pending

    def close(self) -> None:
        try:
            self.handle.close()
        finally:
            self.path.unlink(missing_ok=True)


def stage_restore(data: bytes) -> Path:
    """Compatibility helper for local callers; HTTP uploads use the streaming class."""
    upload = RestoreUpload(len(data))
    try:
        upload.write(data)
        upload.validate()
        return upload.publish()
    finally:
        upload.close()


def apply_pending_restore() -> bool:
    pending = backup_dir() / "restore.pending.db"
    if not pending.is_file():
        return False
    validate_database(pending, compatible=True)
    database = get_config().db_path
    if database.is_file():
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
        previous = backup_dir() / f"pre-restore-{stamp}.db"
        _create_snapshot(previous, checkpoint=True)
        for suffix in ("-wal", "-shm"):
            Path(f"{database}{suffix}").unlink(missing_ok=True)
    os.replace(pending, database)
    _sync_directory(database.parent)
    return True
