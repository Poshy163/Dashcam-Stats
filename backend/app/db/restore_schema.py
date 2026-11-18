"""Build a historical schema in an isolated process without touching application data."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import tempfile


def describe_schema(connection: sqlite3.Connection) -> dict:
    # Restoring a cache token without its invalidation triggers could serve old GPS
    # positions indefinitely after the next drive. Compare the installed definitions
    # to a freshly migrated database of the uploaded revision.
    map_triggers: dict[str, dict[str, str]] = {}
    for table, name, sql in connection.execute(
        "SELECT tbl_name, name, sql FROM sqlite_master "
        "WHERE type='trigger' AND (name GLOB 'map_source_*' OR tbl_name='map_source_state' "
        "OR (tbl_name='journeys' AND EXISTS (SELECT 1 FROM pragma_table_info('journeys') "
        "WHERE name='route_revision')))"
    ):
        map_triggers.setdefault(table, {})[name] = sql
    tables = [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    result = {}
    for table in tables:
        quoted = '"' + table.replace('"', '""') + '"'
        columns = {
            row[1]: {"type": row[2].upper(), "notnull": bool(row[3]), "pk": row[5]}
            for row in connection.execute(f"PRAGMA table_info({quoted})")
        }
        indexes = []
        for index in connection.execute(f"PRAGMA index_list({quoted})"):
            index_name = '"' + index[1].replace('"', '""') + '"'
            sql = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (index[1],)
            ).fetchone()[0]
            predicate = (
                re.split(r"\bWHERE\b", sql, maxsplit=1, flags=re.IGNORECASE)[-1]
                if index[4]
                else None
            )
            terms = list(connection.execute(f"PRAGMA index_xinfo({index_name})"))
            indexes.append(
                {
                    "unique": bool(index[2]),
                    "partial": bool(index[4]),
                    "predicate": predicate.strip() if predicate else None,
                    "columns": [list(row[2:]) for row in terms],
                    # SQLite describes every expression term with the same -2
                    # placeholder. Compare the actual defining SQL before REINDEX.
                    "expression_sql": sql if any(row[1] == -2 for row in terms) else None,
                }
            )
        result[table] = {
            "columns": columns,
            "indexes": sorted(indexes, key=lambda entry: json.dumps(entry, sort_keys=True)),
            "foreign_keys": sorted(
                [list(row[1:]) for row in connection.execute(f"PRAGMA foreign_key_list({quoted})")]
            ),
            "map_triggers": map_triggers.get(table, {}),
        }
    return result


def main(revision: str) -> None:
    from alembic import command
    from sqlalchemy import create_engine

    from app.config import get_config
    from app.db.session import alembic_config

    # Some historical migrations remove a learned-template cache. Keep every config
    # lookup, including those inside migration bodies, confined to this temporary root.
    with tempfile.TemporaryDirectory(prefix="dashcam-schema-") as directory:
        os.environ["DASHCAM_DATA_DIR"] = directory
        os.environ["DASHCAM_FOOTAGE_DIR"] = directory
        os.environ.pop("DASHCAM_DATABASE_URL", None)
        get_config.cache_clear()
        engine = create_engine("sqlite://")
        try:
            with engine.begin() as connection:
                config = alembic_config()
                config.attributes["connection"] = connection
                command.upgrade(config, revision)
                description = describe_schema(connection.connection.driver_connection)
            print(json.dumps(description))
        finally:
            engine.dispose()


if __name__ == "__main__":
    main(sys.argv[1])
