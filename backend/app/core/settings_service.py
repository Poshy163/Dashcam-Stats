"""Runtime settings service.

Every value in ``settings_schema`` is editable in the UI and must take effect without a
restart, so a single process-wide cache sits in front of ``app_settings`` and every other
subsystem reads from it instead of querying the table.

Two properties make that safe:

* The cache is **copy-on-write**. Writers build a replacement dict under an ``asyncio.Lock``
  and rebind it in one assignment, so readers need no lock and can never observe a
  half-applied batch.
* Changes carry a monotonic ``version`` and a notification. The scheduler and the worker
  pool either poll ``version`` or await :meth:`SettingsService.wait_for_change`, which is
  how an edited scan interval or worker count is picked up mid-flight.

No session is held for the life of the service; each operation opens one and closes it.
"""

from __future__ import annotations

import asyncio
import itertools
import math
import os
import re
import threading
from collections.abc import Awaitable, Callable, Iterable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, tzinfo
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, TypeVar
from zoneinfo import ZoneInfo

from app.config import get_config
from app.core.logging import get_logger
from app.core.settings_schema import (
    CATEGORIES,
    SETTINGS,
    SETTINGS_BY_KEY,
    SettingDef,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.db.models import AppSetting

__all__ = [
    "SettingValidationError",
    "SettingsChange",
    "SettingsService",
    "SettingsUnsubscribe",
    "coerce_setting",
    "describe_settings",
    "get_settings_service",
    "init_settings_service",
    "set_settings_service",
    "shutdown_settings_service",
]

logger = get_logger(__name__)

T = TypeVar("T")

#: Anything that yields a fresh ``AsyncSession`` per call -- ``async_sessionmaker`` fits,
#: and so does a plain lambda in tests.
#:
#: Quoted because ``AsyncSession`` is imported only under ``TYPE_CHECKING``: this module
#: is imported during start-up before the database layer is wired up, and pulling
#: SQLAlchemy's async stack in at import time would make that ordering load-bearing.
SessionFactory = Callable[[], "AbstractAsyncContextManager[AsyncSession]"]

SettingsUnsubscribe = Callable[[], None]


class _Missing:
    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<missing>"


_MISSING: Any = _Missing()


# --------------------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------------------


class SettingValidationError(ValueError):
    """Raised when a value cannot be stored against its :class:`SettingDef`."""

    def __init__(self, key: str, reason: str) -> None:
        self.key = key
        self.reason = reason
        super().__init__(f"{key}: {reason}")


# --------------------------------------------------------------------------------------
# Validation and coercion
# --------------------------------------------------------------------------------------

_TRUTHY = frozenset({"1", "true", "yes", "on", "y", "t"})
_FALSEY = frozenset({"0", "false", "no", "off", "n", "f"})

_BYTES_RE = re.compile(r"^([0-9]*\.?[0-9]+)\s*([a-z]*)$", re.IGNORECASE)
_BYTE_UNITS: dict[str, int] = {
    "": 1,
    "b": 1,
    "k": 1000,
    "kb": 1000,
    "kib": 1024,
    "m": 1000**2,
    "mb": 1000**2,
    "mib": 1024**2,
    "g": 1000**3,
    "gb": 1000**3,
    "gib": 1024**3,
    "t": 1000**4,
    "tb": 1000**4,
    "tib": 1024**4,
}

#: Types whose domain is small enough that echoing a rejected value in an error message
#: cannot leak anything sensitive. Free text (``maps.tile_url`` may carry an API key) and
#: paths are described but never quoted.
_QUOTABLE_TYPES = frozenset({"bool", "int", "float", "select", "bytes"})


def _quote(defn: SettingDef, value: Any) -> str:
    if defn.type in _QUOTABLE_TYPES:
        return repr(value)
    return f"a {type(value).__name__}"


def _clamp(defn: SettingDef, value: float) -> float:
    if defn.minimum is not None and value < defn.minimum:
        return float(defn.minimum)
    if defn.maximum is not None and value > defn.maximum:
        return float(defn.maximum)
    return value


def _is_absolute_path(value: str) -> bool:
    # The canonical value is a container path like ``/dashcam``, but the settings page may
    # be driven from a Windows host, so accept either flavour of absolute.
    return PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute()


def _coerce_bool(defn: SettingDef, raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)) and raw in (0, 1):
        return bool(raw)
    if isinstance(raw, str):
        token = raw.strip().lower()
        if token in _TRUTHY:
            return True
        if token in _FALSEY:
            return False
    raise SettingValidationError(defn.key, f"expected a boolean, got {_quote(defn, raw)}")


def _coerce_int(defn: SettingDef, raw: Any) -> int:
    if isinstance(raw, bool):
        raise SettingValidationError(defn.key, "expected an integer, got a boolean")
    if isinstance(raw, int):
        value = raw
    elif isinstance(raw, float):
        if not math.isfinite(raw) or raw != int(raw):
            raise SettingValidationError(defn.key, f"expected a whole number, got {raw!r}")
        value = int(raw)
    elif isinstance(raw, str):
        value = _int_from_str(defn, raw)
    else:
        raise SettingValidationError(defn.key, f"expected an integer, got {_quote(defn, raw)}")
    return int(_clamp(defn, float(value)))


def _int_from_str(defn: SettingDef, raw: str) -> int:
    token = raw.strip().replace("_", "").replace(",", "")
    if not token:
        raise SettingValidationError(defn.key, "expected an integer, got an empty string")
    try:
        return int(token, 10)
    except ValueError:
        pass
    try:
        as_float = float(token)
    except ValueError:
        raise SettingValidationError(defn.key, f"expected an integer, got {token!r}") from None
    if not math.isfinite(as_float) or as_float != int(as_float):
        raise SettingValidationError(defn.key, f"expected a whole number, got {token!r}")
    return int(as_float)


def _coerce_float(defn: SettingDef, raw: Any) -> float:
    if isinstance(raw, bool):
        raise SettingValidationError(defn.key, "expected a number, got a boolean")
    if isinstance(raw, (int, float)):
        value = float(raw)
    elif isinstance(raw, str):
        token = raw.strip().replace("_", "").replace(",", "")
        try:
            value = float(token)
        except ValueError:
            raise SettingValidationError(defn.key, f"expected a number, got {token!r}") from None
    else:
        raise SettingValidationError(defn.key, f"expected a number, got {_quote(defn, raw)}")
    if not math.isfinite(value):
        raise SettingValidationError(defn.key, "expected a finite number")
    return _clamp(defn, value)


def _coerce_str(defn: SettingDef, raw: Any) -> str:
    if isinstance(raw, str):
        value = raw.strip()
    elif isinstance(raw, (int, float)) and not isinstance(raw, bool):
        value = str(raw)
    else:
        raise SettingValidationError(defn.key, f"expected text, got {_quote(defn, raw)}")
    if "\x00" in value:
        raise SettingValidationError(defn.key, "text may not contain a null byte")
    return value


def _coerce_select(defn: SettingDef, raw: Any) -> str:
    value = _coerce_str(defn, raw)
    if not defn.choices:
        return value
    allowed = [choice for choice, _label in defn.choices]
    if value not in allowed:
        raise SettingValidationError(
            defn.key, f"{value!r} is not one of {', '.join(repr(a) for a in allowed)}"
        )
    return value


def _coerce_path(defn: SettingDef, raw: Any) -> str:
    value = os.path.expanduser(_coerce_str(defn, raw))
    if not value:
        raise SettingValidationError(defn.key, "a path is required")
    if not _is_absolute_path(value):
        raise SettingValidationError(defn.key, "must be an absolute path")
    # Trailing separators would make two spellings of the same directory compare unequal,
    # but a bare root or a drive letter must keep its separator.
    trimmed = value.rstrip("/\\")
    if not trimmed or trimmed.endswith(":"):
        return value
    return trimmed


def _coerce_bytes(defn: SettingDef, raw: Any) -> int:
    if isinstance(raw, str):
        match = _BYTES_RE.match(raw.strip())
        if match is None:
            raise SettingValidationError(defn.key, f"expected a byte size, got {raw!r}")
        magnitude, unit = match.groups()
        factor = _BYTE_UNITS.get(unit.lower())
        if factor is None:
            raise SettingValidationError(defn.key, f"unknown size unit {unit!r}")
        value = float(magnitude) * factor
        if not math.isfinite(value):
            raise SettingValidationError(defn.key, "expected a finite byte size")
        return int(_clamp(defn, value))
    return _coerce_int(defn, raw)


_COERCERS: dict[str, Callable[[SettingDef, Any], Any]] = {
    "bool": _coerce_bool,
    "int": _coerce_int,
    "float": _coerce_float,
    "string": _coerce_str,
    "select": _coerce_select,
    "path": _coerce_path,
    "bytes": _coerce_bytes,
}


def coerce_setting(defn: SettingDef, raw: Any) -> Any:
    """Validate ``raw`` against ``defn`` and return the value that should be stored.

    Out-of-range numbers are clamped rather than rejected -- a slider that overshoots its
    bound is a UI artefact, not user error -- but a wrong type or an unknown select choice
    always raises.
    """
    if raw is None:
        raise SettingValidationError(defn.key, "value may not be null")
    coercer = _COERCERS.get(defn.type)
    if coercer is None:  # pragma: no cover - guards a schema typo
        raise SettingValidationError(defn.key, f"unsupported setting type {defn.type!r}")
    return coercer(defn, raw)


def _as_python_type(key: str, value: Any, type_: type[T]) -> T:
    # An unbounded stand-in def, so a caller asking for a plain Python type reuses the
    # coercers without inheriting the schema entry's clamp range.
    def spec(kind: str) -> SettingDef:
        return SettingDef(key, key, kind, None, "advanced")  # type: ignore[arg-type]

    if type_ is bool:
        return _coerce_bool(spec("bool"), value)  # type: ignore[return-value]
    if type_ is int:
        return _coerce_int(spec("int"), value)  # type: ignore[return-value]
    if type_ is float:
        return _coerce_float(spec("float"), value)  # type: ignore[return-value]
    if type_ is str:
        return value if isinstance(value, str) else str(value)  # type: ignore[return-value]
    if type_ is Path:
        return Path(str(value))  # type: ignore[return-value]
    if isinstance(value, type_):
        return value
    raise SettingValidationError(key, f"cannot be read as {type_.__name__}")


# --------------------------------------------------------------------------------------
# Change notification
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SettingsChange:
    """What changed, delivered to every subscriber after the cache has been swapped."""

    version: int
    keys: frozenset[str]
    values: Mapping[str, Any]

    def touches(self, *prefixes: str) -> bool:
        """True when any changed key equals or sits beneath one of ``prefixes``."""
        return any(_key_matches(key, prefixes) for key in self.keys)


def _key_matches(key: str, selectors: Iterable[str]) -> bool:
    for selector in selectors:
        if key == selector:
            return True
        prefix = selector if selector.endswith(".") else selector + "."
        if key.startswith(prefix):
            return True
    return False


@dataclass(slots=True)
class _Subscriber:
    callback: Callable[[SettingsChange], Awaitable[None] | None]
    selectors: tuple[str, ...] | None
    name: str

    def wants(self, keys: frozenset[str]) -> bool:
        if self.selectors is None:
            return True
        return any(_key_matches(key, self.selectors) for key in keys)


# --------------------------------------------------------------------------------------
# Service
# --------------------------------------------------------------------------------------


class SettingsService:
    """Cached, validated access to ``app_settings``.

    Reads are lock-free and synchronous underneath; the ``async`` signatures exist so call
    sites do not have to change if the cache ever needs to fault in from the database.
    """

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory
        self._cache: dict[str, Any] = _base_defaults()
        # Keys with a real row in the table, as opposed to keys sitting on their default.
        self._explicit: frozenset[str] = frozenset()
        self._version = 0
        self._loaded = False
        self._write_lock = asyncio.Lock()
        self._subscribers: dict[int, _Subscriber] = {}
        self._sub_ids = itertools.count(1)
        self._change_event = asyncio.Event()
        self._pending: set[asyncio.Task[None]] = set()

    # -- state ------------------------------------------------------------------------

    @property
    def version(self) -> int:
        """Bumps once per batch in which at least one effective value changed."""
        return self._version

    @property
    def loaded(self) -> bool:
        return self._loaded

    def snapshot(self) -> Mapping[str, Any]:
        """A stable read-only view, safe to hand to a worker thread.

        Because the cache is replaced rather than mutated, the returned view keeps showing
        the values that were current when it was taken.
        """
        return MappingProxyType(self._cache)

    # -- reads ------------------------------------------------------------------------

    async def get(self, key: str, default: Any = _MISSING) -> Any:
        return self.get_nowait(key, default)

    def get_nowait(self, key: str, default: Any = _MISSING) -> Any:
        """Synchronous read for code already inside a thread executor."""
        cache = self._cache
        if key in cache:
            return cache[key]
        # A key added to the schema in a later version is usable immediately, without a
        # migration and without a row in the table.
        defn = SETTINGS_BY_KEY.get(key)
        if defn is not None:
            return _effective_default(defn)
        if default is not _MISSING:
            return default
        raise SettingValidationError(key, "unknown setting")

    async def get_typed(self, key: str, type_: type[T], default: Any = _MISSING) -> T:
        return _as_python_type(key, self.get_nowait(key, default), type_)

    async def get_all(self) -> dict[str, Any]:
        return dict(self._cache)

    def is_default(self, key: str) -> bool:
        return key not in self._explicit

    # -- writes -----------------------------------------------------------------------

    async def set(self, key: str, value: Any) -> Any:
        """Validate, persist and publish one setting. Returns the stored value."""
        stored = await self.set_many({key: value})
        return stored[key]

    async def set_many(self, values: Mapping[str, Any]) -> dict[str, Any]:
        """Apply a batch atomically: nothing is written unless every value validates."""
        coerced: dict[str, Any] = {}
        for key, raw in values.items():
            defn = SETTINGS_BY_KEY.get(key)
            if defn is None:
                raise SettingValidationError(key, "unknown setting")
            if defn.read_only:
                raise SettingValidationError(key, "this setting is read-only")
            coerced[key] = coerce_setting(defn, raw)

        if not coerced:
            return {}

        from app.db.models import AppSetting  # deferred: app.db imports app.core

        async with self._write_lock:
            async with self._session_factory() as session:
                for key, value in coerced.items():
                    row = await session.get(AppSetting, key)
                    if row is None:
                        session.add(AppSetting(key=key, value=value))
                    else:
                        row.value = value
                await session.commit()

            cache = dict(self._cache)
            changed = {k: v for k, v in coerced.items() if cache.get(k, _MISSING) != v}
            cache.update(coerced)
            self._cache = cache
            # Writing a value identical to the default still pins it, so a later change to
            # the schema default does not silently move the user's configuration.
            self._explicit = self._explicit | frozenset(coerced)
            change = self._bump(changed) if changed else None

        if change is not None:
            self._dispatch(change)
        return dict(coerced)

    async def reset(self, key: str) -> Any:
        """Drop the stored row so ``key`` falls back to its schema default."""
        result = await self.reset_many([key])
        return result[key]

    async def reset_many(self, keys: Iterable[str]) -> dict[str, Any]:
        wanted = list(dict.fromkeys(keys))
        for key in wanted:
            if key not in SETTINGS_BY_KEY:
                raise SettingValidationError(key, "unknown setting")
        if not wanted:
            return {}

        from sqlalchemy import delete

        from app.db.models import AppSetting  # deferred: app.db imports app.core

        async with self._write_lock:
            async with self._session_factory() as session:
                await session.execute(delete(AppSetting).where(AppSetting.key.in_(wanted)))
                await session.commit()

            defaults = {k: _effective_default(SETTINGS_BY_KEY[k]) for k in wanted}
            cache = dict(self._cache)
            changed = {k: v for k, v in defaults.items() if cache.get(k, _MISSING) != v}
            cache.update(defaults)
            self._cache = cache
            self._explicit = self._explicit - set(wanted)
            change = self._bump(changed) if changed else None

        if change is not None:
            self._dispatch(change)
        return defaults

    async def reset_all(self) -> dict[str, Any]:
        return await self.reset_many(SETTINGS_BY_KEY)

    async def reload(self) -> int:
        """Re-read the table into the cache. Returns the number of values that moved.

        Held against the write lock so a concurrent :meth:`set_many` cannot be clobbered by
        a snapshot taken before it committed.
        """
        from sqlalchemy import select

        from app.db.models import AppSetting  # deferred: app.db imports app.core

        async with self._write_lock:
            async with self._session_factory() as session:
                rows = (await session.execute(select(AppSetting))).scalars().all()

            cache, explicit, unknown = _build_cache(rows)
            previous = self._cache
            # The first load establishes the baseline; only later reloads are "changes".
            changed = (
                {}
                if not self._loaded
                else {k: v for k, v in cache.items() if previous.get(k, _MISSING) != v}
            )
            self._cache = cache
            self._explicit = explicit
            self._loaded = True
            change = self._bump(changed) if changed else None

        if unknown:
            # Left in place rather than deleted: they are most likely from a newer version
            # of the app that was rolled back, and dropping them would lose the user's edit.
            logger.info("settings.unknown_rows_ignored", keys=sorted(unknown))
        if change is not None:
            self._dispatch(change)
        return len(changed)

    # -- notification -----------------------------------------------------------------

    def subscribe(
        self,
        callback: Callable[[SettingsChange], Awaitable[None] | None],
        *,
        keys: Iterable[str] | None = None,
        name: str | None = None,
    ) -> SettingsUnsubscribe:
        """Register ``callback``; returns a handle that removes it.

        ``keys`` accepts exact keys or dotted prefixes (``"processing"`` matches every
        ``processing.*`` key), so the worker pool only wakes for changes it cares about.
        Coroutine callbacks are scheduled on the running loop; sync callbacks run inline
        and must stay cheap.
        """
        token = next(self._sub_ids)
        selectors = tuple(keys) if keys is not None else None
        self._subscribers[token] = _Subscriber(
            callback=callback,
            selectors=selectors,
            name=name or getattr(callback, "__qualname__", repr(callback)),
        )

        def unsubscribe() -> None:
            self._subscribers.pop(token, None)

        return unsubscribe

    async def wait_for_change(self, since: int, timeout: float | None = None) -> int:
        """Block until ``version`` exceeds ``since``; returns the new version.

        Raises :class:`asyncio.TimeoutError` if ``timeout`` elapses first, which lets a
        scheduler use a settings change as one of several wakeup sources.
        """
        while self._version <= since:
            waiter = self._change_event.wait()
            if timeout is None:
                await waiter
            else:
                await asyncio.wait_for(waiter, timeout)
        return self._version

    def _bump(self, changed: Mapping[str, Any]) -> SettingsChange:
        self._version += 1
        change = SettingsChange(
            version=self._version,
            keys=frozenset(changed),
            values=MappingProxyType(dict(changed)),
        )
        # Swap in a fresh event so waiters that have not been scheduled yet still see the
        # edge they were waiting for.
        stale, self._change_event = self._change_event, asyncio.Event()
        stale.set()
        return change

    def _dispatch(self, change: SettingsChange) -> None:
        # Keys only. Redaction would catch an obvious `maps.tile_url` API key, but there is
        # no reason to put user values on the wire at all.
        logger.info("settings.changed", version=change.version, keys=sorted(change.keys))
        for subscriber in list(self._subscribers.values()):
            if not subscriber.wants(change.keys):
                continue
            try:
                result = subscriber.callback(change)
            except Exception:
                # A broken listener must not roll back a write that has already committed.
                logger.exception("settings.subscriber_failed", subscriber=subscriber.name)
                continue
            if asyncio.iscoroutine(result):
                self._spawn(result, subscriber.name)

    def _spawn(self, coro: Awaitable[None], name: str) -> None:
        try:
            task = asyncio.get_running_loop().create_task(coro)  # type: ignore[arg-type]
        except RuntimeError:  # pragma: no cover - dispatch always runs on the loop
            logger.warning("settings.subscriber_dropped", subscriber=name, reason="no running loop")
            if asyncio.iscoroutine(coro):
                coro.close()
            return
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def aclose(self) -> None:
        """Drop subscribers and await in-flight notifications."""
        self._subscribers.clear()
        pending = list(self._pending)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._pending.clear()

    # -- typed accessors ---------------------------------------------------------------

    async def footage_dir(self) -> Path:
        return Path(self.get_nowait("general.footage_dir"))

    async def media_extensions(self) -> frozenset[str]:
        raw = str(self.get_nowait("general.media_extensions"))
        exts = {
            token if token.startswith(".") else f".{token}"
            for token in (part.strip().lower() for part in raw.split(","))
            if token
        }
        # An empty list would make the scanner index nothing at all, which reads as "the
        # share is gone" everywhere downstream.
        return frozenset(exts) or frozenset({".ts"})

    async def timezone(self) -> tzinfo:
        name = str(self.get_nowait("general.timezone")).strip()
        try:
            return ZoneInfo(name)
        except Exception:
            logger.warning("settings.timezone_unknown", timezone=name, fallback="UTC")
            try:
                return ZoneInfo("UTC")
            except Exception:  # pragma: no cover - no tzdata available at all
                return UTC

    async def max_workers(self) -> int:
        return int(self.get_nowait("processing.max_workers"))

    async def scan_enabled(self) -> bool:
        return bool(self.get_nowait("scanner.enabled"))

    async def scan_interval_s(self) -> float:
        return float(self.get_nowait("scanner.interval_minutes")) * 60.0

    async def settle_seconds(self) -> float:
        return float(self.get_nowait("scanner.settle_seconds"))

    async def auto_process(self) -> bool:
        return bool(self.get_nowait("scanner.auto_process"))

    async def hardware_acceleration(self) -> bool:
        return bool(self.get_nowait("processing.hardware_acceleration"))

    async def frame_sample_fps(self) -> float:
        return float(self.get_nowait("processing.frame_sample_fps"))

    async def detection_classes(self) -> frozenset[str]:
        raw = str(self.get_nowait("processing.detection_classes"))
        return frozenset(p.strip().lower() for p in raw.split(",") if p.strip())

    async def cleanup_interval_s(self) -> float:
        return float(self.get_nowait("storage.cleanup_interval_minutes")) * 60.0

    async def deletion_enabled(self) -> bool:
        # Retention still runs its own guards; this is only the user's consent bit.
        return bool(self.get_nowait("storage.enable_deletion"))

    async def job_heartbeat_timeout_s(self) -> float:
        return float(self.get_nowait("advanced.job_heartbeat_timeout_s"))

    async def log_level(self) -> str:
        return str(self.get_nowait("advanced.log_level")).upper()

    # -- catalogue ---------------------------------------------------------------------

    def describe_settings(self) -> list[dict[str, Any]]:
        """Serialise the whole catalogue with current values, grouped by category."""
        cache = self._cache
        explicit = self._explicit
        known = {key for key, _label, _desc in CATEGORIES}

        buckets: dict[str, list[dict[str, Any]]] = {key: [] for key in known}
        for defn in SETTINGS:
            bucket = defn.category if defn.category in known else "advanced"
            buckets.setdefault(bucket, []).append(_describe(defn, cache, explicit))

        described = [
            {
                "key": key,
                "label": label,
                "description": description,
                "settings": buckets.get(key, []),
            }
            for key, label, description in CATEGORIES
        ]
        return [category for category in described if category["settings"]]


def _describe(
    defn: SettingDef, cache: Mapping[str, Any], explicit: frozenset[str]
) -> dict[str, Any]:
    return {
        "key": defn.key,
        "label": defn.label,
        "type": defn.type,
        "category": defn.category,
        "description": defn.description,
        "default": _effective_default(defn),
        "value": cache.get(defn.key, _effective_default(defn)),
        "is_default": defn.key not in explicit,
        "minimum": defn.minimum,
        "maximum": defn.maximum,
        # Always a list, never None. A setting that is not a select simply has no choices,
        # and an empty list says that without forcing every consumer to handle a null --
        # the API schema types this as a list, and the UI maps over it directly.
        "choices": [{"value": value, "label": label} for value, label in defn.choices],
        "unit": defn.unit,
        "dangerous": defn.dangerous,
        "read_only": defn.read_only,
        "requires": defn.requires,
    }


# --------------------------------------------------------------------------------------
# Defaults and cache construction
# --------------------------------------------------------------------------------------


def _effective_default(defn: SettingDef) -> Any:
    """The default as this deployment sees it.

    ``DASHCAM_FOOTAGE_DIR`` is the deployment's answer for where footage is mounted, so it
    supersedes the schema literal until the user edits the setting; every other default is
    the schema's.
    """
    if defn.key == "general.footage_dir":
        try:
            return _coerce_path(defn, str(get_config().footage_dir))
        except SettingValidationError:
            logger.warning("settings.env_footage_dir_invalid", fallback=defn.default)
    return defn.default


def _base_defaults() -> dict[str, Any]:
    return {defn.key: _effective_default(defn) for defn in SETTINGS}


def _build_cache(
    rows: Iterable[AppSetting],
) -> tuple[dict[str, Any], frozenset[str], list[str]]:
    values = _base_defaults()
    explicit: set[str] = set()
    unknown: list[str] = []
    for row in rows:
        defn = SETTINGS_BY_KEY.get(row.key)
        if defn is None:
            unknown.append(row.key)
            continue
        try:
            values[row.key] = coerce_setting(defn, row.value)
        except SettingValidationError as exc:
            # A value that no longer validates (schema tightened, hand-edited database)
            # must not stop the app from starting.
            logger.warning("settings.stored_value_invalid", key=row.key, reason=exc.reason)
            continue
        explicit.add(row.key)
    return values, frozenset(explicit), unknown


# --------------------------------------------------------------------------------------
# Process-wide accessor
# --------------------------------------------------------------------------------------

_instance: SettingsService | None = None
# A plain threading lock, not asyncio: worker threads running ffmpeg/inference call the
# accessor while off the event loop.
_instance_lock = threading.Lock()


async def init_settings_service(
    session_factory: SessionFactory, *, replace: bool = False
) -> SettingsService:
    """Create the process-wide service and load the cache. Call once, from the lifespan."""
    global _instance
    with _instance_lock:
        existing = _instance
        if existing is not None and not replace:
            return existing
        service = SettingsService(session_factory)
        _instance = service
    await service.reload()
    return service


def get_settings_service() -> SettingsService:
    service = _instance
    if service is None:
        raise RuntimeError(
            "settings service is not initialised; call init_settings_service() during startup"
        )
    return service


def set_settings_service(service: SettingsService | None) -> None:
    """Install a service directly. Intended for tests and for embedding."""
    global _instance
    with _instance_lock:
        _instance = service


async def shutdown_settings_service() -> None:
    global _instance
    with _instance_lock:
        service, _instance = _instance, None
    if service is not None:
        await service.aclose()


def describe_settings() -> list[dict[str, Any]]:
    """Module-level catalogue export for the settings API."""
    return get_settings_service().describe_settings()
