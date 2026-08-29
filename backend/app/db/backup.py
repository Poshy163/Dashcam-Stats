"""Online SQLite backups and restart-safe restore staging."""

from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from app.config import get_config


def backup_dir() -> Path:
    path = get_config().data_dir / "backups"
    path.mkdir(parents=True, exist_ok=True)
    return path


def validate_database(path: Path) -> None:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
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
    finally:
        connection.close()


def create_backup() -> Path:
    source_path = get_config().db_path
    if not source_path.is_file():
        raise FileNotFoundError("Database file does not exist")
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    target = backup_dir() / f"dashcam-{stamp}.db"
    source = sqlite3.connect(str(source_path))
    destination = sqlite3.connect(str(target))
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    validate_database(target)
    return target


def create_pre_migration_backup(from_revision: str, to_revision: str) -> Path:
    """Create and validate an atomic snapshot before Alembic changes an existing DB."""
    source_path = get_config().db_path
    if not source_path.is_file():
        raise FileNotFoundError("Database file does not exist")
    safe_from = "".join(char for char in from_revision if char.isalnum() or char in "-_")
    safe_to = "".join(char for char in to_revision if char.isalnum() or char in "-_")
    if not safe_from or not safe_to:
        raise ValueError("Migration revisions are not safe backup filename components")
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
    directory = backup_dir()
    target = directory / f"pre-migration-{safe_from}-to-{safe_to}-{stamp}.db"
    temporary = directory / f".{target.name}.tmp"
    try:
        source: sqlite3.Connection | None = None
        destination: sqlite3.Connection | None = None
        try:
            source = sqlite3.connect(str(source_path))
            destination = sqlite3.connect(str(temporary))
            source.backup(destination)
        finally:
            if destination is not None:
                destination.close()
            if source is not None:
                source.close()
        validate_database(temporary)
        with temporary.open("r+b") as handle:
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        if os.name != "nt":
            descriptor = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def stage_restore(data: bytes) -> Path:
    directory = backup_dir()
    temporary = directory / "restore-upload.tmp"
    pending = directory / "restore.pending.db"
    temporary.write_bytes(data)
    try:
        validate_database(temporary)
        os.replace(temporary, pending)
    finally:
        temporary.unlink(missing_ok=True)
    return pending


def apply_pending_restore() -> bool:
    pending = backup_dir() / "restore.pending.db"
    if not pending.is_file():
        return False
    validate_database(pending)
    database = get_config().db_path
    if database.is_file():
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        previous = backup_dir() / f"pre-restore-{stamp}.db"
        source = sqlite3.connect(str(database))
        destination = sqlite3.connect(str(previous))
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()
        validate_database(previous)
        database.unlink()
        for suffix in ("-wal", "-shm"):
            Path(f"{database}{suffix}").unlink(missing_ok=True)
    os.replace(pending, database)
    return True
