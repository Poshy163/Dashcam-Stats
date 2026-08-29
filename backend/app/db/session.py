"""Engine, session plumbing and first-boot initialisation.

The pragmas applied on every connect are what make the "one writer, many readers" claim
in ARCHITECTURE.md section 2 true. WAL lets readers keep reading while the writer holds
the write lock, and ``busy_timeout`` turns the contention that remains into a bounded
wait rather than an immediate ``SQLITE_BUSY``.

Note on transactions: pysqlite/aiosqlite only emit ``BEGIN`` at the first DML statement,
so a session that reads and then writes takes its read snapshot *outside* the write
transaction. That is exactly what we want here -- forcing an explicit early ``BEGIN``
would turn the read-then-write pattern into ``SQLITE_BUSY_SNAPSHOT``, which
``busy_timeout`` cannot wait out. The durable queue therefore claims work with a single
atomic UPDATE rather than a read-modify-write.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import event
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.config import get_config
from app.db.seed import seed_defaults

#: ``backend/`` -- alembic.ini and migrations/ live here, next to the ``app`` package.
BACKEND_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI_PATH = BACKEND_ROOT / "alembic.ini"
MIGRATIONS_PATH = BACKEND_ROOT / "migrations"

#: Applied to every new SQLite connection, including the ones Alembic opens.
SQLITE_PRAGMAS: tuple[tuple[str, str], ...] = (
    ("journal_mode", "WAL"),
    ("synchronous", "NORMAL"),
    # SQLite defaults foreign keys to OFF; the schema leans on ON DELETE CASCADE.
    ("foreign_keys", "ON"),
    ("busy_timeout", "30000"),
    # Negative values are KiB, so this is a 64 MiB page cache per connection. Telemetry
    # queries sweep long index ranges and benefit far more than the memory costs.
    ("cache_size", "-65536"),
    ("temp_store", "MEMORY"),
)

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None
#: get_engine() is reachable from worker threads as well as the event loop.
_engine_lock = threading.Lock()


# ------------------------------------------------------------------------------------
# Engine
# ------------------------------------------------------------------------------------


def _is_memory_url(url: URL) -> bool:
    if url.get_backend_name() != "sqlite":
        return False
    if not url.database or url.database == ":memory:":
        return True
    return url.query.get("mode") == "memory"


def _apply_sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
    cursor = dbapi_connection.cursor()
    try:
        for pragma, value in SQLITE_PRAGMAS:
            cursor.execute(f"PRAGMA {pragma}={value}")
    finally:
        cursor.close()

    # Transactions are left deferred on purpose.
    #
    # An earlier attempt forced `BEGIN IMMEDIATE` on every transaction to stop the job
    # claim losing a lock-upgrade race. It worked, and made things worse: a deferred
    # transaction that only reads never takes the write lock at all, but an immediate one
    # always does, so every read-only API request queued behind the workers' long write
    # transactions and eventually failed on `BEGIN IMMEDIATE` itself. Under WAL, readers
    # and a single writer coexist happily -- serialising them threw that away.
    #
    # The upgrade race is fixed where it actually lives instead: `queue.claim_next` is a
    # single atomic UPDATE rather than a SELECT followed by an UPDATE.


def _build_engine() -> AsyncEngine:
    url = make_url(get_config().sqlalchemy_url)
    kwargs: dict[str, Any] = {"echo": False}

    if url.get_backend_name() == "sqlite":
        if _is_memory_url(url):
            # An in-memory database lives inside its connection, so every session has to
            # share one. Used by tests; never by the deployed app.
            kwargs["poolclass"] = StaticPool
            kwargs["connect_args"] = {"check_same_thread": False}
        else:
            # aiosqlite runs each connection on its own thread, so the pool is also a
            # thread budget. Readers are cheap, the writer is effectively serialised.
            kwargs.update(pool_size=8, max_overflow=16, pool_timeout=30, pool_recycle=-1)
            if url.database:
                Path(url.database).expanduser().parent.mkdir(parents=True, exist_ok=True)

    engine = create_async_engine(url, **kwargs)
    if url.get_backend_name() == "sqlite":
        event.listen(engine.sync_engine, "connect", _apply_sqlite_pragmas)
    return engine


def get_engine() -> AsyncEngine:
    """The process-wide async engine, created on first use."""
    global _engine, _session_factory
    with _engine_lock:
        if _engine is None:
            _engine = _build_engine()
            _session_factory = async_sessionmaker(
                _engine,
                class_=AsyncSession,
                # Attributes must stay readable after commit; an async refresh would
                # otherwise fire lazy IO from response serialisation.
                expire_on_commit=False,
                autoflush=True,
            )
        return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    get_engine()
    assert _session_factory is not None  # set in lockstep with _engine
    return _session_factory


def __getattr__(name: str) -> Any:
    # `engine` and `async_session_factory` are documented module attributes, but binding
    # them eagerly would open the database at import time -- before init_db() has had a
    # chance to create /data or run migrations.
    if name == "engine":
        return get_engine()
    if name == "async_session_factory":
        return get_session_factory()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


async def dispose_engine() -> None:
    """Close every pooled connection. Safe to call when no engine was ever created."""
    global _engine, _session_factory
    with _engine_lock:
        engine, _engine, _session_factory = _engine, None, None
    if engine is not None:
        await engine.dispose()


# ------------------------------------------------------------------------------------
# Sessions
# ------------------------------------------------------------------------------------


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: commits when the handler returns, rolls back if it raises.

    The commit is not optional. Without it every write route silently did nothing --
    flushing inside the request and then rolling back when the session closed. Queueing a
    reprocess, flagging a plate, editing its notes, merging journeys and retrying a job
    all reported success and changed nothing, which is a far worse failure than an error
    would have been.

    Read-only handlers are unaffected: committing a session with nothing dirty is a no-op.
    """
    async with get_session_factory()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Transactional scope for background workers: commits on success, rolls back on error."""
    async with get_session_factory()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# ------------------------------------------------------------------------------------
# Migrations and startup
# ------------------------------------------------------------------------------------


def alembic_config() -> AlembicConfig:
    """Alembic config wired to this deployment rather than to the caller's cwd."""
    cfg = AlembicConfig(str(ALEMBIC_INI_PATH)) if ALEMBIC_INI_PATH.is_file() else AlembicConfig()
    cfg.set_main_option("script_location", str(MIGRATIONS_PATH))
    # ConfigParser treats '%' as interpolation, and a data dir may legitimately contain one.
    cfg.set_main_option("sqlalchemy.url", get_config().sync_sqlalchemy_url.replace("%", "%%"))
    # env.py honours this: running fileConfig() in-process would tear down the logging
    # the application has already set up.
    cfg.attributes["configure_logger"] = False
    return cfg


def upgrade_to_head() -> None:
    """Back up an existing stale SQLite schema, then upgrade it to Alembic head."""
    config = get_config()
    alembic = alembic_config()
    if not config.database_url and config.db_path.is_file() and config.db_path.stat().st_size:
        from alembic.script import ScriptDirectory

        revision = current_revision()
        head = ScriptDirectory.from_config(alembic).get_current_head()
        # ``None`` is the ordinary brand-new database (SQLite may create the file while
        # inspecting it). It contains no user schema to preserve. Any stamped, stale
        # deployment gets exactly one backup because the next boot is already at head.
        if revision is not None and head is not None and revision != head:
            from app.db.backup import create_pre_migration_backup

            # Deliberately outside a suppress/finally: if backup or integrity validation
            # fails, Alembic must not touch the current database.
            create_pre_migration_backup(revision, head)
    command.upgrade(alembic, "head")


def current_revision() -> str | None:
    """Blocking. The revision stamped in the database, or None if it has never migrated."""
    from alembic.runtime.migration import MigrationContext
    from sqlalchemy import create_engine

    engine = create_engine(get_config().sync_sqlalchemy_url, poolclass=StaticPool)
    try:
        with engine.connect() as conn:
            return MigrationContext.configure(conn).get_current_revision()
    finally:
        engine.dispose()


async def init_db(*, run_migrations: bool = True, seed: bool = True) -> None:
    """Bring the database up to date and make sure the rows the app assumes exist do.

    Idempotent: this runs on every boot.
    """
    config = get_config()
    await asyncio.to_thread(config.ensure_dirs)

    # Restore is staged by the API and applied before the first database connection on
    # the next restart. Replacing a live WAL database would risk a split-brain process.
    if not config.database_url:
        from app.db.backup import apply_pending_restore

        await asyncio.to_thread(apply_pending_restore)

    if run_migrations:
        # Alembic is synchronous all the way down, so it never touches the event loop.
        await asyncio.to_thread(upgrade_to_head)

    if seed:
        async with session_scope() as session:
            await seed_defaults(session)


__all__ = [
    "ALEMBIC_INI_PATH",
    "BACKEND_ROOT",
    "MIGRATIONS_PATH",
    "SQLITE_PRAGMAS",
    "alembic_config",
    "current_revision",
    "dispose_engine",
    "get_engine",
    "get_session",
    "get_session_factory",
    "init_db",
    "session_scope",
    "upgrade_to_head",
]
