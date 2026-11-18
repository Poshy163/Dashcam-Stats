"""Disposable geometry for the default routes view, keyed by each journey's source revision."""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from collections import OrderedDict
from contextlib import closing

from pydantic import TypeAdapter, ValidationError

from app.api.route_geometry_cache import Lines
from app.config import get_config

# Increment when the default simplification or the meaning of a route group changes.
_FORMAT = b"route-geometry-v1:"
_GEOMETRY = TypeAdapter(Lines | None)
_MAX_GROUP_BYTES = 16 * 1024**2
_DISK_BUDGET = 64 * 1024**2
_MEMORY_BUDGET = 16 * 1024**2
_TABLE = """CREATE TABLE IF NOT EXISTS geometry (
    group_key TEXT NOT NULL UNIQUE, token BLOB NOT NULL UNIQUE,
    checksum BLOB NOT NULL, payload BLOB NOT NULL
)"""

# If the cache volume is full or read-only, requests can still share calculated groups.
_fallback: OrderedDict[bytes, bytes] = OrderedDict()
_fallback_bytes = 0
_lock = threading.Lock()


def clear() -> None:
    global _fallback_bytes
    with _lock:
        _fallback.clear()
        _fallback_bytes = 0


def _token(group: int | None, revision: bytes) -> bytes:
    return hashlib.sha256(_FORMAT + str(group).encode() + b":" + revision).digest()


def _open_readonly() -> sqlite3.Connection:
    path = get_config().cache_dir / "route-journeys.sqlite3"
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.1)


def has_any(revisions: dict[int | None, bytes]) -> bool:
    """Find a reusable revision without reading the geometry of later journeys."""
    tokens = [_token(group, rev) for group, rev in revisions.items()]
    with _lock:
        if any(token in _fallback for token in tokens):
            return True
    try:
        with closing(_open_readonly()) as db:
            for offset in range(0, len(tokens), 500):
                chunk = tokens[offset : offset + 500]
                params = ",".join("?" for _ in chunk)
                if db.execute(
                    f"SELECT 1 FROM geometry WHERE token IN ({params}) LIMIT 1", chunk
                ).fetchone():
                    return True
    except (OSError, sqlite3.Error):
        pass
    return False


def load(revisions: dict[int | None, bytes]) -> dict[int | None, Lines | None]:
    """A missing key needs recomputing. None denotes no fixes and [] denotes no lines."""
    tokens = {_token(group, revision): group for group, revision in revisions.items()}
    found = {}
    with _lock:
        for token in tuple(tokens):
            if token in _fallback:
                found[tokens.pop(token)] = _GEOMETRY.validate_json(_fallback[token])
                _fallback.move_to_end(token)
    if not tokens:
        return found

    try:
        # The caller supplies one bounded window of journeys, in map order.
        params = ",".join("?" for _ in tokens)
        with closing(_open_readonly()) as db:
            rows = db.execute(
                f"SELECT token, checksum, payload FROM geometry WHERE token IN ({params}) "
                "AND length(payload) <= ?",
                (*tokens, _MAX_GROUP_BYTES),
            )
            for token, checksum, payload in rows:
                if (
                    not isinstance(payload, bytes)
                    or hashlib.sha256(token + payload).digest() != checksum
                ):
                    continue
                try:
                    found[tokens[token]] = _GEOMETRY.validate_json(payload)
                except ValidationError:
                    continue
    except (OSError, sqlite3.Error):
        pass
    return found


def _trim(db: sqlite3.Connection) -> None:
    page_size = db.execute("PRAGMA page_size").fetchone()[0]
    pages = db.execute("PRAGMA page_count").fetchone()[0]
    free = db.execute("PRAGMA freelist_count").fetchone()[0]
    if (pages - free) * page_size <= _DISK_BUDGET:
        return
    # Leave headroom on a full volume. REPLACE gives each refreshed group a new rowid.
    remaining = _DISK_BUDGET // 2
    for rowid, size in db.execute(
        "SELECT rowid, length(payload) FROM geometry ORDER BY rowid DESC"
    ):
        remaining -= size + 256
        if remaining < 0:
            db.execute("DELETE FROM geometry WHERE rowid <= ?", (rowid,))
            break


def save(changes: dict[int | None, tuple[bytes, Lines | None]]) -> None:
    """Replace only rebuilt groups, in the sidecar rather than the application's write queue."""
    global _fallback_bytes
    records = []
    for group, (revision, lines) in changes.items():
        payload = _GEOMETRY.dump_json(lines)
        if len(payload) <= _MAX_GROUP_BYTES:
            token = _token(group, revision)
            records.append((str(group), token, hashlib.sha256(token + payload).digest(), payload))
    if not records:
        return
    try:
        with (
            closing(
                sqlite3.connect(get_config().cache_dir / "route-journeys.sqlite3", timeout=0.1)
            ) as db,
            db,
        ):
            db.execute(_TABLE)
            db.executemany("INSERT OR REPLACE INTO geometry VALUES (?, ?, ?, ?)", records)
            _trim(db)
        return
    except (OSError, sqlite3.Error):
        pass

    with _lock:
        for _, token, _, payload in records:
            old = _fallback.pop(token, None)
            if old is not None:
                _fallback_bytes -= len(old) + 128
            _fallback[token] = payload
            _fallback_bytes += len(payload) + 128
        while _fallback_bytes > _MEMORY_BUDGET:
            _, old = _fallback.popitem(last=False)
            _fallback_bytes -= len(old) + 128
