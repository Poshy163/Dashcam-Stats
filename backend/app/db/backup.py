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
