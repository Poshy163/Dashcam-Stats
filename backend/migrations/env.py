"""Alembic environment.

Runs both from the command line (``alembic -c backend/alembic.ini upgrade head``) and
in-process from ``app.db.session.upgrade_to_head`` during startup.
"""
from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, event, pool
from sqlalchemy.engine import Connection

# `alembic` invoked from anywhere still has to be able to import the app package.
_BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from app.config import get_config  # noqa: E402
from app.db import models  # noqa: E402
from app.db.session import SQLITE_PRAGMAS  # noqa: E402

config = context.config

# Skipped when the application drives migrations itself -- fileConfig() would otherwise
# replace the logging configuration the app has already installed.
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)

target_metadata = models.Base.metadata


def _database_url() -> str:
    """Deployment config is authoritative; the ini value is only a fallback."""
    return get_config().sync_sqlalchemy_url


def _prepare_sqlite_connection(dbapi_connection: object, _record: object) -> None:
    """Set pragmas at DBAPI connect time, never on the SQLAlchemy Connection.

    Issuing them through the Connection would autobegin a transaction, which Alembic
    reads as "the caller owns the transaction" and then declines to commit -- silently
    losing the alembic_version row while the DDL itself lands in autocommit.
    """
    cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
    try:
        for pragma, value in SQLITE_PRAGMAS:
            cursor.execute(f"PRAGMA {pragma}={value}")
        # Batch migrations copy a table, drop the original and rename; enforcement would
        # fire ON DELETE CASCADE against the copy midway through.
        cursor.execute("PRAGMA foreign_keys=OFF")
    finally:
        cursor.close()


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # SQLite cannot ALTER most things in place; batch mode rewrites the table instead.
        render_as_batch=True,
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # A caller may hand us a live connection (tests, or migrating inside an existing
    # transaction); it then owns the transaction and Alembic will not commit.
    existing = config.attributes.get("connection", None)
    if existing is not None:
        _run(existing)
        return

    section = dict(config.get_section(config.config_ini_section) or {})
    # '%' is ConfigParser's interpolation character and a data dir may contain one.
    section["sqlalchemy.url"] = _database_url().replace("%", "%%")

    engine = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    if engine.dialect.name == "sqlite":
        event.listen(engine, "connect", _prepare_sqlite_connection)
    try:
        with engine.connect() as connection:
            _run(connection)
    finally:
        engine.dispose()


def _run(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
