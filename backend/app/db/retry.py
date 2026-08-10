"""Surviving the moment when someone else holds SQLite's write lock.

``busy_timeout`` is the first line of defence and it covers most of this: a writer that
arrives while another writer holds the lock waits rather than failing. What it does not
cover is the case that actually reached production here -- a lock held for *longer than
the timeout*. Thirty seconds is a long time for a write and no time at all for a journey
rebuild that walks every telemetry point in the library, and when the two overlapped the
worker's first write threw ``OperationalError: database is locked`` and took a finished
run down with it.

Two things are needed and they are not alternatives:

1. **Nobody may hold the write lock for that long.** That is fixed where each long
   transaction lives -- see ``JourneyBuilder.refresh`` and the stage write phases, which
   now do their reading before they open a transaction and commit per unit of work rather
   than per pass. This module cannot fix that and does not try.
2. **A writer that loses anyway must not lose the work.** Contention is not a defect in
   the recording, and turning it into a permanently failed job is the worst possible
   response: minutes of decode and inference discarded because a different task was busy.

This module is the second one. It retries at the *transaction* boundary rather than the
statement, which is the only correct granularity: after a failed statement SQLAlchemy
requires a rollback before the session will accept anything else, so retrying the
statement alone raises ``PendingRollbackError`` instead of the original error and looks
like a different bug entirely.

The work being retried must therefore be re-runnable. Every caller here re-runs a
delete-then-insert over data it is already holding in memory, which is idempotent by
construction.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger

log = get_logger(__name__)

T = TypeVar("T")

#: Attempts, including the first. Four attempts across the backoff below spans roughly
#: fifteen seconds, which is on top of the thirty second ``busy_timeout`` each one
#: already waits out -- so a lock has to be held for about a minute before this gives up.
DEFAULT_ATTEMPTS = 4

#: Base delay between attempts, doubled each time and jittered. Jitter matters with two
#: workers: without it they back off in lockstep and collide again on every retry.
DEFAULT_BASE_DELAY_S = 0.5

#: What SQLite says when it could not get the lock. Matched on text because the DBAPI
#: reports all of these as a plain ``sqlite3.OperationalError`` with no distinguishing
#: code, and because the same messages arrive wrapped in SQLAlchemy's ``OperationalError``
#: or bare from a raw connection.
_LOCK_MARKERS = (
    "database is locked",
    "database table is locked",
    "database schema is locked",
    "cannot start a transaction within a transaction",
    "sqlite_busy",
)


def is_locked_error(exc: BaseException | None) -> bool:
    """Is this exception SQLite refusing to wait any longer for the write lock?

    Walks the cause chain: the interesting exception is usually the DBAPI's, wrapped by
    SQLAlchemy, and sometimes wrapped again by whatever the caller raised.
    """
    current: BaseException | None = exc
    for _ in range(8):
        if current is None:
            break
        # Matched on the message rather than the type. SQLAlchemy wraps the DBAPI error,
        # the DBAPI reports every flavour of contention as one ``OperationalError``, and
        # the worker's own bookkeeping has raised it as a bare ``OSError`` in tests. The
        # sentence SQLite prints is the only thing common to all of them.
        if any(marker in str(current).lower() for marker in _LOCK_MARKERS):
            return True
        current = current.__cause__ or current.__context__
    return False


async def retry_on_locked(
    operation: Callable[[], Awaitable[T]],
    *,
    what: str,
    attempts: int = DEFAULT_ATTEMPTS,
    base_delay_s: float = DEFAULT_BASE_DELAY_S,
    on_retry: Callable[[], Awaitable[None]] | None = None,
) -> T:
    """Run *operation*, retrying it while SQLite reports the database as locked.

    ``on_retry`` is awaited before each retry and is where the session gets rolled back;
    it is a parameter rather than hard-coded so this stays usable for callers that hold a
    connection rather than a session.

    The final attempt's exception propagates unchanged. Swallowing it would be worse than
    the lock: a write that silently did not happen is the failure mode this codebase is
    most prone to.
    """
    last: BaseException | None = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            return await operation()
        except Exception as exc:
            if not is_locked_error(exc) or attempt >= attempts:
                raise
            last = exc
            if on_retry is not None:
                await on_retry()
            delay = base_delay_s * (2 ** (attempt - 1)) * (0.5 + random.random())
            log.warning(
                "the database was locked; retrying",
                operation=what,
                attempt=attempt,
                of=attempts,
                retry_in_s=round(delay, 2),
                error=str(exc)[:200],
            )
            await asyncio.sleep(delay)
    raise AssertionError(f"unreachable: {what} exhausted retries without raising") from last


async def write_with_retry(
    session: AsyncSession,
    operation: Callable[[], Awaitable[T]],
    *,
    what: str,
    attempts: int = DEFAULT_ATTEMPTS,
) -> T:
    """Run a write phase and commit it, re-running the whole thing if the lock is taken.

    This is the form that can safely roll back, and the reason is that it puts the work
    back afterwards. Every caller's write phase is a delete followed by inserts built from
    data it is already holding in memory, so re-running produces the same rows however many
    times it happens.

    Wrapped around exactly the statements that failed in production -- ``DELETE FROM
    tracked_objects`` and ``DELETE FROM telemetry_points``, each the first write of its
    stage and therefore the moment its transaction opens.
    """

    async def attempt() -> T:
        result = await operation()
        await session.commit()
        return result

    async def rollback() -> None:
        # Required before a retry: after a failed statement the session refuses everything
        # else until it is rolled back. Suppressed, because a rollback that itself fails
        # must not replace the error that is about to be retried or raised.
        try:
            await session.rollback()
        except Exception as exc:  # pragma: no cover - defensive
            log.debug("rollback before retry failed", operation=what, error=str(exc))

    return await retry_on_locked(attempt, what=what, attempts=attempts, on_retry=rollback)


async def commit_with_retry(session: AsyncSession, *, what: str) -> None:
    """Commit, waiting out a lock rather than failing the job that produced the work.

    Pointedly **without** a rollback between attempts, which is the difference between
    this and :func:`write_with_retry`. The uncommitted work is the whole point of the
    call; discarding it and then committing an empty transaction would report success
    having written nothing -- the exact class of defect this codebase has been bitten by
    most often, and far worse than the lock it was trying to survive.

    So a commit is retried in place while that is possible, and otherwise the error
    propagates. That is not a loss: the caller fails the job as transient, the queue
    requeues it without spending an attempt, and the work is done again from a clean
    session rather than half-written from a dirty one.
    """

    async def attempt() -> None:
        await session.commit()

    await retry_on_locked(attempt, what=what)


__all__ = [
    "DEFAULT_ATTEMPTS",
    "commit_with_retry",
    "is_locked_error",
    "retry_on_locked",
    "write_with_retry",
]
