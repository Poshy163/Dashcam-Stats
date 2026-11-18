"""Keep SQLite's persisted heat-cell keys in sync with its native ROUND implementation."""

from __future__ import annotations

import sqlite3

# Alembic tracks the application schema in alembic_version. The application-owned
# SQLite user_version header records the library that computed the expression index.
_major, _minor, _patch = sqlite3.sqlite_version_info
SQLITE_VERSION_NUMBER = _major * 1_000_000 + _minor * 1_000 + _patch


def heat_index_needs_refresh(connection: sqlite3.Connection) -> bool:
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    return (
        version != SQLITE_VERSION_NUMBER
        and connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = 'ix_telemetry_heat_cells'"
        ).fetchone()
        is not None
    )


def refresh_heat_index(connection: sqlite3.Connection) -> None:
    if not heat_index_needs_refresh(connection):
        return
    # ROUND on half-grid decimal coordinates changed between SQLite 3.40 and 3.50.
    # Replace old keys and their version together, including when a caller already
    # owns a transaction (the programmatic Alembic path).
    connection.execute("SAVEPOINT refresh_heat_index")
    try:
        connection.execute("REINDEX ix_telemetry_heat_cells")
        connection.execute(f"PRAGMA user_version = {SQLITE_VERSION_NUMBER}")
    except BaseException:
        connection.execute("ROLLBACK TO refresh_heat_index")
        raise
    finally:
        connection.execute("RELEASE refresh_heat_index")
