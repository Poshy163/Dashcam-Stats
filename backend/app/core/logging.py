"""Structured logging.

Three things this module has to get right:

* **Nothing sensitive is ever rendered.** The share this application reads was mounted
  with SMB credentials; those credentials appear in mount commands, URLs and settings
  payloads that could otherwise be logged verbatim. Redaction runs *before* both the
  console/JSON renderer and the database sink, so there is no path from a bound value to
  a stored log line that skips it.
* **The database sink must not become the highest-cardinality table in the schema.**
  ``log_entries`` is the UI's operator log, not a trace. Three independent brakes are
  applied: a level floor, duplicate suppression with a repeat counter, and a bounded
  drop-oldest buffer. Per-frame logging is DEBUG and never reaches the database at all.
* **Logging can never block or crash the pipeline.** Emission is a lock-protected deque
  append callable from any executor thread; a background task does the I/O. If the
  database is unavailable the batch is dropped and counted, never retried forever and
  never raised into the caller.

The stdlib logging tree is routed through the same processor chain, so uvicorn and
SQLAlchemy output is redacted and rendered identically.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
import threading
import time
import traceback
from collections import deque
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import (
    AbstractAsyncContextManager,
    asynccontextmanager,
    contextmanager,
    suppress,
)
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from app.config import get_config
from app.core.settings_schema import DEFAULTS

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "DatabaseLogSink",
    "bind_job",
    "bind_log_context",
    "bind_recording",
    "clear_log_context",
    "configure_logging",
    "db_sink_lifespan",
    "get_db_sink",
    "get_logger",
    "install_db_sink",
    "log_context",
    "prune_logs",
    "redact",
    "redact_text",
    "set_log_level",
    "shutdown_db_sink",
    "unbind_log_context",
]

DEFAULT_DB_LEVEL = "INFO"

#: Loggers whose records are never written to the database, whatever the level.
#:
#: The HTTP access log is one line per request, and the UI polls: the Queue page alone asks
#: for /api/jobs/stats every few seconds. On a real deployment that made 135 of every 200
#: stored rows access noise, which cost twice over. The Logs page became unreadable — the
#: events worth seeing were buried and, at 100 rows a page, unreachable — and every request
#: turned into an INSERT competing with the workers for SQLite's single write lock, which
#: showed up as "database is locked" in the worker claiming its next job.
#:
#: Dedupe cannot help here: the key includes the message, and every access line carries a
#: different URL and source port, so no two are ever alike.
#:
#: These records are not lost. They still go to stdout, which is where a request log
#: belongs and where `docker compose logs` will find it.
_NEVER_PERSIST = frozenset({"uvicorn.access", "uvicorn.asgi", "watchfiles.main"})
DEFAULT_DEDUPE_WINDOW_S = 30.0
DEFAULT_QUEUE_CAPACITY = 2048
DEFAULT_FLUSH_INTERVAL_S = 1.0

_MAX_MESSAGE_LEN = 4000
_MAX_LOGGER_LEN = 64  # log_entries.logger is String(64)
_MAX_LEVEL_LEN = 16
_MAX_DEDUPE_KEYS = 1024
_MAX_BATCH = 500

REDACTED = "***REDACTED***"


# --------------------------------------------------------------------------------------
# Redaction
# --------------------------------------------------------------------------------------

#: Keys whose *value* is a credential regardless of what it looks like.
_SENSITIVE_KEY_RE = re.compile(
    r"(pass(word|wd|phrase)?|pwd|secret|token|api[_-]?key|apikey|access[_-]?key|"
    r"auth(orization)?|credential|cookie|session[_-]?key|private[_-]?key|"
    r"smb[_-]?(user|pass|password|credential)s?)",
    re.IGNORECASE,
)

#: ``scheme://user:pass@host`` -- the whole userinfo goes, not just the password.
_URL_CREDS_RE = re.compile(
    r"(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*://)(?P<userinfo>[^/\s@]+:[^/\s@]*)@"
)

#: ``password=x``, ``"token": "y"``, ``--secret 'z'`` in free text, JSON and command lines.
#: The key may be quoted (JSON) and the value may carry an auth scheme, which has to be
#: swallowed too -- otherwise ``Authorization: Bearer <jwt>`` redacts the word "Bearer"
#: and leaves the token.
_INLINE_SECRET_RE = re.compile(
    r"(?P<key>(?P<q>[\"']?)\b(?:pass(?:word|wd|phrase)?|pwd|secret|token|api[_-]?key|apikey|"
    r"access[_-]?key|authorization|auth|credentials?|smbpass(?:word)?)\b(?P=q))"
    r"(?P<sep>\s*[:=]\s*)"
    r"(?P<value>\"[^\"]*\"|'[^']*'|(?:bearer|basic|token|digest)\s+\S+|[^\s,;&)\]}]+)",
    re.IGNORECASE,
)

#: ``smbclient -U user%password`` and ``mount.cifs ... -U user%password``.
_SMB_USER_PERCENT_RE = re.compile(r"(?P<flag>-U\s+[^\s%]+)%\S+")

#: ``Authorization: Bearer <jwt>`` survives the inline rule; a bare header value does not.
_BEARER_RE = re.compile(r"\b(?P<kind>Bearer|Basic)\s+[A-Za-z0-9._~+/=\-]{8,}", re.IGNORECASE)


def redact_text(value: str) -> str:
    """Strip anything that looks like a credential out of a rendered string."""
    out = _URL_CREDS_RE.sub(lambda m: f"{m.group('scheme')}{REDACTED}@", value)
    out = _SMB_USER_PERCENT_RE.sub(lambda m: f"{m.group('flag')}%{REDACTED}", out)
    out = _INLINE_SECRET_RE.sub(lambda m: f"{m.group('key')}{m.group('sep')}{REDACTED}", out)
    out = _BEARER_RE.sub(lambda m: f"{m.group('kind')} {REDACTED}", out)
    return out


def redact(value: Any, *, _depth: int = 0) -> Any:
    """Recursively redact a value. Safe to reuse outside logging (settings payloads)."""
    if _depth > 6:
        return REDACTED
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        result: dict[Any, Any] = {}
        for key, val in value.items():
            if isinstance(key, str) and _SENSITIVE_KEY_RE.search(key):
                result[key] = REDACTED
            else:
                result[key] = redact(val, _depth=_depth + 1)
        return result
    if isinstance(value, (list, tuple, set)):
        redacted = [redact(v, _depth=_depth + 1) for v in value]
        return type(value)(redacted) if isinstance(value, (list, tuple)) else set(redacted)
    return value


def _redact_processor(logger: Any, method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    for key in list(event_dict):
        if key.startswith("_"):  # ProcessorFormatter bookkeeping; not user data
            continue
        if _SENSITIVE_KEY_RE.search(key):
            event_dict[key] = REDACTED
            continue
        event_dict[key] = redact(event_dict[key])
    return event_dict


# --------------------------------------------------------------------------------------
# Database sink
# --------------------------------------------------------------------------------------

#: Set while the sink is writing, so a failure inside the writer cannot be captured by
#: the sink and recurse. ContextVars are per-task, so this never masks unrelated logs.
_in_sink: ContextVar[bool] = ContextVar("dashcam_in_log_sink", default=False)

_LEVEL_ALIASES = {"warn": "WARNING", "exception": "ERROR", "fatal": "CRITICAL"}

#: Keys consumed by the pipeline itself; they are never part of a log entry's context.
_CONTEXT_EXCLUDE = frozenset(
    {
        "event",
        "level",
        "logger",
        "logger_name",
        "timestamp",
        "exc_info",
        "exception",
        "stack",
        "stack_info",
        "recording_id",
        "job_id",
        "_record",
        "_from_structlog",
    }
)

SessionFactory = Callable[[], "AbstractAsyncContextManager[AsyncSession] | Any"]
"""Anything callable that yields an async context manager over an ``AsyncSession`` --
``async_sessionmaker(...)`` satisfies this directly."""


def _level_number(name: object) -> int:
    text = str(name or "").lower()
    resolved = _LEVEL_ALIASES.get(text, text.upper())
    value = logging.getLevelName(resolved)
    return value if isinstance(value, int) else logging.INFO


def _jsonable(value: Any, depth: int = 0) -> Any:
    """Coerce to something the JSON column can store without surprises."""
    if depth > 4:
        return repr(value)[:200]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:2000]
    if isinstance(value, (datetime,)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k)[:64]: _jsonable(v, depth + 1) for k, v in list(value.items())[:50]}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v, depth + 1) for v in list(value)[:50]]
    return str(value)[:500]


def _coerce_id(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


@dataclass(slots=True)
class _PendingLog:
    ts: datetime
    level: str
    logger: str
    message: str
    context: dict[str, Any] | None
    recording_id: int | None
    job_id: int | None


@dataclass(slots=True)
class _DedupeState:
    window_started: float
    suppressed: int
    last: _PendingLog


@dataclass(slots=True)
class SinkStats:
    seen: int = 0
    enqueued: int = 0
    suppressed: int = 0
    dropped: int = 0
    written: int = 0
    write_errors: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "seen": self.seen,
            "enqueued": self.enqueued,
            "suppressed": self.suppressed,
            "dropped": self.dropped,
            "written": self.written,
            "write_errors": self.write_errors,
        }


class DatabaseLogSink:
    """Buffers log records and writes them to ``log_entries`` in batches.

    Emission is synchronous, lock-guarded and never blocks: the deque has a ``maxlen``, so
    a stalled database costs the oldest buffered records rather than the pipeline's
    throughput or the process's memory.
    """

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        min_level: str | int = DEFAULT_DB_LEVEL,
        dedupe_window_s: float = DEFAULT_DEDUPE_WINDOW_S,
        capacity: int = DEFAULT_QUEUE_CAPACITY,
        flush_interval_s: float = DEFAULT_FLUSH_INTERVAL_S,
    ) -> None:
        self._session_factory = session_factory
        self._min_level = min_level if isinstance(min_level, int) else _level_number(min_level)
        self._dedupe_window_s = max(0.0, dedupe_window_s)
        self._flush_interval_s = max(0.05, flush_interval_s)
        self._lock = threading.Lock()
        self._queue: deque[_PendingLog] = deque(maxlen=max(16, capacity))
        self._dedupe: dict[tuple[str, str, str], _DedupeState] = {}
        self._stats = SinkStats()
        self._task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None

    # -- configuration -----------------------------------------------------------------
    @property
    def min_level(self) -> int:
        return self._min_level

    def set_min_level(self, level: str | int) -> None:
        self._min_level = level if isinstance(level, int) else _level_number(level)

    @property
    def stats(self) -> dict[str, int]:
        with self._lock:
            return self._stats.as_dict()

    # -- lifecycle ---------------------------------------------------------------------
    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.get_running_loop().create_task(self._run(), name="log-sink-writer")

    async def stop(self) -> None:
        """Stop writing, after one last drain so shutdown messages survive."""
        if self._stop_event is not None:
            self._stop_event.set()
        task, self._task = self._task, None
        if task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
            except (TimeoutError, asyncio.CancelledError):
                task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await task
            except Exception:  # a writer crash must not block shutdown
                pass
        await self.flush(force=True)

    async def _run(self) -> None:
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop_event.wait(), self._flush_interval_s)
            await self.flush()

    # -- emission ----------------------------------------------------------------------
    def emit(self, record: _PendingLog, *, force: bool = False) -> None:
        """Record one entry. Callable from any thread; never blocks, never raises."""
        with self._lock:
            self._stats.seen += 1
            if not force and _level_number(record.level) < self._min_level:
                return
            # The identity is part of the key, because the message is not.
            #
            # Every log call in this codebase uses a constant event name with the variable
            # data in kwargs -- "job failed permanently", "stage failed", "processed
            # recording" -- so keying on the text alone collapsed *different recordings*
            # failing in the same way into one row with a repeat count. The Logs page's
            # recording and job filters then found nothing for all but the first of them,
            # which is precisely when somebody is looking. A message carrying no identity
            # keys exactly as before, so the noisy lines this brake exists for are
            # unaffected.
            key = (
                record.level,
                record.logger,
                record.message,
                record.recording_id,
                record.job_id,
            )
            now = time.monotonic()
            state = self._dedupe.get(key)
            if state is not None and now - state.window_started < self._dedupe_window_s:
                # Same message again inside the window: count it, do not store it.
                state.suppressed += 1
                state.last = record
                self._stats.suppressed += 1
                return
            if state is not None and state.suppressed:
                self._enqueue_locked(self._summary(state))
            self._dedupe[key] = _DedupeState(window_started=now, suppressed=0, last=record)
            self._trim_dedupe_locked()
            self._enqueue_locked(record)

    def _enqueue_locked(self, record: _PendingLog) -> None:
        if self._queue.maxlen is not None and len(self._queue) == self._queue.maxlen:
            # deque drops the oldest for us; account for it so the loss is visible.
            self._stats.dropped += 1
        self._queue.append(record)
        self._stats.enqueued += 1

    def _trim_dedupe_locked(self) -> None:
        # Messages that embed a filename are unique per file, so the key space is
        # unbounded without this. Oldest window first -- it is the closest to expiry.
        while len(self._dedupe) > _MAX_DEDUPE_KEYS:
            oldest = min(self._dedupe, key=lambda k: self._dedupe[k].window_started)
            state = self._dedupe.pop(oldest)
            if state.suppressed:
                self._enqueue_locked(self._summary(state))

    @staticmethod
    def _summary(state: _DedupeState) -> _PendingLog:
        sample = state.last
        context = dict(sample.context or {})
        context["suppressed_count"] = state.suppressed
        return _PendingLog(
            ts=datetime.now(UTC),
            level=sample.level,
            logger=sample.logger,
            message=f"{sample.message} (repeated {state.suppressed} more times)"[:_MAX_MESSAGE_LEN],
            context=context,
            recording_id=sample.recording_id,
            job_id=sample.job_id,
        )

    def emit_event(self, event_dict: dict[str, Any], *, force: bool = False) -> None:
        """Build a record from a structlog event dict and emit it."""
        if _in_sink.get():
            return
        if str(event_dict.get("logger") or event_dict.get("logger_name") or "") in _NEVER_PERSIST:
            # Still printed to stdout by the renderer; simply not worth a database row.
            return
        level = _LEVEL_ALIASES.get(
            str(event_dict.get("level", "info")).lower(),
            str(event_dict.get("level", "info")).upper(),
        )
        logger_name = str(event_dict.get("logger") or event_dict.get("logger_name") or "app")
        message = str(event_dict.get("event", ""))

        context: dict[str, Any] = {}
        for key, value in event_dict.items():
            if key in _CONTEXT_EXCLUDE:
                continue
            context[key] = _jsonable(value)

        exc_info = event_dict.get("exc_info")
        rendered = _render_exception(exc_info)
        if rendered:
            context["exception"] = rendered

        self.emit(
            _PendingLog(
                ts=datetime.now(UTC),
                level=level[:_MAX_LEVEL_LEN],
                logger=logger_name[:_MAX_LOGGER_LEN],
                message=message[:_MAX_MESSAGE_LEN],
                context=context or None,
                recording_id=_coerce_id(event_dict.get("recording_id")),
                job_id=_coerce_id(event_dict.get("job_id")),
            ),
            force=force,
        )

    # -- draining ----------------------------------------------------------------------
    def _sweep_locked(self, *, force: bool) -> None:
        now = time.monotonic()
        for key in list(self._dedupe):
            state = self._dedupe[key]
            if not force and now - state.window_started < self._dedupe_window_s:
                continue
            if state.suppressed:
                self._enqueue_locked(self._summary(state))
            del self._dedupe[key]

    def _take_locked(self) -> list[_PendingLog]:
        batch: list[_PendingLog] = []
        while self._queue and len(batch) < _MAX_BATCH:
            batch.append(self._queue.popleft())
        return batch

    async def flush(self, *, force: bool = False) -> None:
        """Write everything currently buffered. Never raises."""
        while True:
            with self._lock:
                self._sweep_locked(force=force)
                batch = self._take_locked()
            if not batch:
                return
            await self._write(batch)
            force = False  # summaries only need flushing once per call

    async def _write(self, batch: list[_PendingLog]) -> None:
        from app.db.models import LogEntry  # deferred: app.db imports app.core

        token = _in_sink.set(True)
        try:
            async with self._session_factory() as session:
                session.add_all(
                    [
                        LogEntry(
                            ts=r.ts,
                            level=r.level,
                            logger=r.logger,
                            message=r.message,
                            context=r.context,
                            recording_id=r.recording_id,
                            job_id=r.job_id,
                        )
                        for r in batch
                    ]
                )
                await session.commit()
            with self._lock:
                self._stats.written += len(batch)
        except Exception:
            # Dropped, not requeued: a persistent database fault would otherwise turn the
            # buffer into an infinite retry loop. The count is exposed via `stats`.
            with self._lock:
                self._stats.write_errors += 1
            logging.getLogger(__name__).warning(
                "log sink write failed, dropped %d entries", len(batch), exc_info=True
            )
        finally:
            _in_sink.reset(token)


def _render_exception(exc_info: Any) -> str | None:
    if not exc_info:
        return None
    try:
        if exc_info is True:
            exc_info = sys.exc_info()
        if isinstance(exc_info, BaseException):
            exc_info = (type(exc_info), exc_info, exc_info.__traceback__)
        if not isinstance(exc_info, tuple) or exc_info[0] is None:
            return None
        text = "".join(traceback.format_exception(*exc_info))
    except Exception:
        return None
    return redact_text(text)[:_MAX_MESSAGE_LEN]


_db_sink: DatabaseLogSink | None = None


def _db_capture(logger: Any, method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Hand the (already redacted) event to the database sink, if one is installed.

    ``db_log=False`` opts a call out entirely -- used by hot loops that legitimately log
    at INFO but must never produce a row per item.
    """
    persist = event_dict.pop("db_log", None)
    sink = _db_sink
    if sink is not None and persist is not False:
        sink.emit_event(event_dict, force=persist is True)
    return event_dict


def install_db_sink(
    session_factory: SessionFactory,
    *,
    min_level: str | int = DEFAULT_DB_LEVEL,
    dedupe_window_s: float = DEFAULT_DEDUPE_WINDOW_S,
    capacity: int = DEFAULT_QUEUE_CAPACITY,
    flush_interval_s: float = DEFAULT_FLUSH_INTERVAL_S,
    start: bool = True,
) -> DatabaseLogSink:
    """Install the process-wide database sink and start its writer task."""
    global _db_sink
    _db_sink = DatabaseLogSink(
        session_factory,
        min_level=min_level,
        dedupe_window_s=dedupe_window_s,
        capacity=capacity,
        flush_interval_s=flush_interval_s,
    )
    if start:
        _db_sink.start()
    return _db_sink


def get_db_sink() -> DatabaseLogSink | None:
    return _db_sink


async def shutdown_db_sink() -> None:
    """Detach the sink and flush what it still holds. Safe to call when none is installed."""
    global _db_sink
    sink, _db_sink = _db_sink, None
    if sink is not None:
        await sink.stop()


@asynccontextmanager
async def db_sink_lifespan(
    session_factory: SessionFactory, **kwargs: Any
) -> AsyncIterator[DatabaseLogSink]:
    """Convenience wrapper for the FastAPI lifespan handler."""
    sink = install_db_sink(session_factory, **kwargs)
    try:
        yield sink
    finally:
        await shutdown_db_sink()


# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------

_NOISY_LOGGERS = {
    "sqlalchemy.engine": logging.WARNING,
    "sqlalchemy.pool": logging.WARNING,
    "aiosqlite": logging.WARNING,
    "asyncio": logging.WARNING,
    "PIL": logging.WARNING,
    "urllib3": logging.WARNING,
    "watchfiles": logging.WARNING,
    "multipart": logging.WARNING,
}

_configured = False


def _use_console(json_logs: bool | None) -> bool:
    if json_logs is not None:
        return not json_logs
    try:
        return bool(sys.stdout.isatty())
    except (AttributeError, ValueError):
        return False


def _shared_processors() -> list[Any]:
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        # Redaction sits ahead of both the renderer and the sink; there is no route to
        # output that bypasses it.
        _redact_processor,
    ]


def configure_logging(
    level: str | int | None = None,
    *,
    json_logs: bool | None = None,
    force: bool = False,
) -> None:
    """Configure structlog and the stdlib root logger.

    Human-readable when stdout is a TTY, single-line JSON otherwise (which is what the
    container gets). *level* defaults to ``DASHCAM_LOG_LEVEL``.
    """
    global _configured
    if _configured and not force:
        set_log_level(level if level is not None else get_config().log_level)
        return

    resolved_level = _level_number(level if level is not None else get_config().log_level)
    console = _use_console(json_logs)
    shared = _shared_processors()

    if console:
        try:
            renderer: Any = structlog.dev.ConsoleRenderer(colors=_supports_colour())
        except (SystemError, ImportError):
            # Colour on a Windows console needs colorama, which is not a dependency.
            renderer = structlog.dev.ConsoleRenderer(colors=False)
        final: list[Any] = [structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer]
    else:
        final = [
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ]

    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            *shared,
            _db_capture,
            structlog.processors.UnicodeDecoder(),
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        # Records from uvicorn/SQLAlchemy arrive here; they get the same treatment,
        # including redaction and database capture.
        foreign_pre_chain=[*shared, _db_capture],
        processors=final,
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(resolved_level)

    for name, lvl in _NOISY_LOGGERS.items():
        logging.getLogger(name).setLevel(max(lvl, resolved_level))
    # uvicorn installs its own handlers; drop them so output is not duplicated.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "fastapi"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True

    logging.captureWarnings(True)
    _configured = True


def _supports_colour() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    try:
        return bool(sys.stdout.isatty())
    except (AttributeError, ValueError):
        return False


def set_log_level(level: str | int) -> None:
    """Apply a new level at runtime (``advanced.log_level`` changes without a restart)."""
    resolved = _level_number(level)
    logging.getLogger().setLevel(resolved)
    for name, lvl in _NOISY_LOGGERS.items():
        logging.getLogger(name).setLevel(max(lvl, resolved))


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a bound logger. Call after ``configure_logging`` for the full chain."""
    return structlog.stdlib.get_logger(name) if name else structlog.stdlib.get_logger()


# --------------------------------------------------------------------------------------
# Context binding
# --------------------------------------------------------------------------------------


def bind_log_context(**values: Any) -> None:
    """Bind values onto every subsequent log record in this task/thread context."""
    structlog.contextvars.bind_contextvars(**{k: v for k, v in values.items() if v is not None})


def unbind_log_context(*keys: str) -> None:
    structlog.contextvars.unbind_contextvars(*keys)


def clear_log_context() -> None:
    structlog.contextvars.clear_contextvars()


def bind_recording(recording_id: int | None) -> None:
    """Attach a recording to subsequent records; also fills ``log_entries.recording_id``."""
    bind_log_context(recording_id=recording_id)


def bind_job(job_id: int | None) -> None:
    """Attach a job to subsequent records; also fills ``log_entries.job_id``."""
    bind_log_context(job_id=job_id)


@contextmanager
def log_context(
    *,
    recording_id: int | None = None,
    job_id: int | None = None,
    **extra: Any,
) -> Iterator[None]:
    """Scope context to a block, restoring whatever was bound before.

    Worker tasks wrap a whole job in this so every line -- including exceptions raised
    from a thread executor -- carries the recording and job it belongs to.
    """
    values = {
        k: v
        for k, v in {"recording_id": recording_id, "job_id": job_id, **extra}.items()
        if v is not None
    }
    if not values:
        yield
        return
    tokens = structlog.contextvars.bind_contextvars(**values)
    try:
        yield
    finally:
        structlog.contextvars.reset_contextvars(**tokens)


# --------------------------------------------------------------------------------------
# Retention
# --------------------------------------------------------------------------------------


async def prune_logs(session: AsyncSession, days: int | None = None) -> int:
    """Delete ``log_entries`` older than *days*, returning how many rows went.

    *days* defaults to ``advanced.log_retention_days``, read straight from
    ``app_settings`` so this works from a scheduler with no settings service loaded.
    """
    from sqlalchemy import delete, select

    from app.db.models import AppSetting, LogEntry

    if days is None:
        raw = (
            await session.execute(
                select(AppSetting.value).where(AppSetting.key == "advanced.log_retention_days")
            )
        ).scalar_one_or_none()
        try:
            days = int(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            days = int(DEFAULTS["advanced.log_retention_days"])

    days = max(1, int(days))
    cutoff = datetime.now(UTC) - timedelta(days=days)
    result = await session.execute(delete(LogEntry).where(LogEntry.ts < cutoff))
    await session.commit()
    return int(result.rowcount or 0)
