"""Filesystem path safety.

Two independent problems are solved here:

* **Containment.** Every path that arrives from the HTTP API is attacker-controlled.
  ``safe_join`` is the only sanctioned way to turn a client-supplied string into a real
  path: it rejects absolute paths, drive letters and ``..`` segments up front, then
  resolves symlinks and proves the result is still inside the root. A symlink planted
  inside the footage share must not become a window onto the host filesystem.
* **Mount introspection.** Retention refuses to delete unless the footage directory is a
  real mount, on its own filesystem, and genuinely writable. Those checks cannot be
  inferred from configuration -- an unmounted share looks exactly like an empty directory,
  and a read-only bind mount looks exactly like a writable one until you try to write.
  See ARCHITECTURE.md section 6.

Nothing here ever writes into the footage directory except the writability probe, which
creates and immediately removes a single dotfile and is the only way to answer the
question the retention guard actually asks.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from collections.abc import Iterable
from pathlib import Path, PurePath, PureWindowsPath
from uuid import uuid4

from app.config import get_config

__all__ = [
    "FOOTAGE_MEASURE_TTL_S",
    "PathTraversalError",
    "data_root",
    "directory_size",
    "footage_root",
    "forget_tree_measurements",
    "is_mount_point",
    "is_within",
    "is_writable",
    "measure_tree",
    "media_root",
    "relative_to_footage",
    "relative_to_media",
    "resolve_footage_path",
    "resolve_media_path",
    "safe_join",
    "same_filesystem",
]


class PathTraversalError(Exception):
    """A supplied path escaped, or tried to escape, its permitted root."""


_SEPARATORS = re.compile(r"[\\/]+")


# --------------------------------------------------------------------------------------
# Roots
# --------------------------------------------------------------------------------------


def data_root() -> Path:
    """Resolved ``/data``. Never a deletion target, under any circumstance."""
    return get_config().data_dir.expanduser().resolve()


def footage_root() -> Path:
    """Resolved footage mount from the environment.

    Deliberately the *deployment* root rather than the ``general.footage_dir`` UI setting:
    the UI value is validated against this one, so a setting change can never widen what
    the web layer is able to reach.
    """
    return get_config().footage_dir.expanduser().resolve()


def media_root() -> Path:
    """Resolved ``/data/media`` -- thumbnails and crops."""
    return get_config().media_dir.expanduser().resolve()


# --------------------------------------------------------------------------------------
# Containment
# --------------------------------------------------------------------------------------


def _validate_part(part: str | os.PathLike[str]) -> list[str]:
    """Split one caller-supplied component into safe segments, or refuse it."""
    raw = os.fspath(part)
    if not isinstance(raw, str):  # os.PathLike[bytes]
        raw = raw.decode("utf-8", "replace")
    if "\x00" in raw:
        raise PathTraversalError("path contains a NUL byte")
    if raw.startswith(("/", "\\")):
        raise PathTraversalError(f"absolute path not permitted: {raw!r}")
    # Catches both ``C:\x`` and the drive-relative ``C:x`` form, which Windows resolves
    # against a per-drive cwd and which would otherwise slip past the join.
    if PureWindowsPath(raw).drive:
        raise PathTraversalError(f"drive-qualified path not permitted: {raw!r}")

    segments: list[str] = []
    for segment in _SEPARATORS.split(raw):
        if segment in ("", "."):
            continue
        if segment == "..":
            raise PathTraversalError(f"parent traversal not permitted: {raw!r}")
        segments.append(segment)
    return segments


def _contains(root: Path, candidate: Path) -> bool:
    """Containment test for two *already resolved* paths."""
    root_s = os.path.normcase(str(root))
    cand_s = os.path.normcase(str(candidate))
    if root_s == cand_s:
        return True
    if not root_s.endswith(os.sep):
        root_s += os.sep
    return cand_s.startswith(root_s)


def safe_join(root: Path | str, *parts: str | os.PathLike[str]) -> Path:
    """Join *parts* onto *root* and guarantee the result stays inside it.

    Resolution is symlink-aware, so a link inside the root that points outside is an
    escape and is refused. Raises :class:`PathTraversalError` on any violation; callers
    can therefore treat a returned path as trusted.
    """
    try:
        root_resolved = Path(root).expanduser().resolve()
    except OSError as exc:
        raise PathTraversalError(f"root cannot be resolved: {root!r}") from exc

    segments: list[str] = []
    for part in parts:
        segments.extend(_validate_part(part))

    candidate = root_resolved.joinpath(*segments) if segments else root_resolved
    try:
        resolved = candidate.resolve()
    except OSError as exc:
        # ELOOP, an invalid component on Windows, or a broken mount all land here.
        raise PathTraversalError(f"path cannot be resolved: {candidate}") from exc

    if not _contains(root_resolved, resolved):
        raise PathTraversalError(f"{'/'.join(segments)!r} escapes {root_resolved}")
    return resolved


def is_within(root: Path | str, candidate: Path | str) -> bool:
    """True when *candidate* resolves to *root* itself or something beneath it."""
    try:
        root_resolved = Path(root).expanduser().resolve()
        cand_resolved = Path(candidate).expanduser().resolve()
    except OSError:
        return False
    return _contains(root_resolved, cand_resolved)


def resolve_footage_path(rel_path: str | os.PathLike[str], *, must_exist: bool = False) -> Path:
    """Turn a stored/relative footage path into an absolute one inside the footage root."""
    resolved = safe_join(footage_root(), rel_path)
    if must_exist and not resolved.exists():
        raise FileNotFoundError(str(resolved))
    return resolved


def resolve_media_path(rel_path: str | os.PathLike[str], *, must_exist: bool = False) -> Path:
    """Turn a stored media path (thumbnail, crop) into an absolute one under /data/media."""
    resolved = safe_join(media_root(), rel_path)
    if must_exist and not resolved.exists():
        raise FileNotFoundError(str(resolved))
    return resolved


def _relative_to(root: Path, abs_path: Path | str) -> str:
    try:
        resolved = Path(abs_path).expanduser().resolve()
    except OSError as exc:
        raise PathTraversalError(f"path cannot be resolved: {abs_path!r}") from exc
    if not _contains(root, resolved) or os.path.normcase(str(resolved)) == os.path.normcase(
        str(root)
    ):
        raise PathTraversalError(f"{resolved} is not inside {root}")
    try:
        relative: PurePath = resolved.relative_to(root)
    except ValueError:
        # Case-insensitive filesystems can resolve to a different casing than the root
        # was recorded with; _contains already normalised for that, so slice instead.
        relative = PurePath(str(resolved)[len(str(root)) :].lstrip("\\/"))
    return relative.as_posix()


def relative_to_footage(abs_path: Path | str) -> str:
    """Portable ``recordings.rel_path`` value: POSIX separators, relative to the mount.

    Storing relative keeps the index valid when the share is remounted elsewhere.
    """
    return _relative_to(footage_root(), abs_path)


def relative_to_media(abs_path: Path | str) -> str:
    """Portable media path for the ``*_path`` columns, relative to /data/media."""
    return _relative_to(media_root(), abs_path)


# --------------------------------------------------------------------------------------
# Mount and filesystem introspection
# --------------------------------------------------------------------------------------


def is_mount_point(path: Path | str) -> bool:
    """True when *path* is a mount point, or otherwise sits on its own filesystem.

    The retention guard uses this to tell "the share is mounted and happens to be empty"
    apart from "the share is not mounted and the empty directory underneath is showing".
    """
    p = Path(path)
    try:
        if os.path.ismount(p):
            return True
    except OSError:
        return False
    # ismount misses some container bind mounts and network drives; a differing device id
    # from the parent is the same evidence by another route.
    try:
        return p.stat().st_dev != p.parent.stat().st_dev
    except OSError:
        return False


def same_filesystem(a: Path | str, b: Path | str) -> bool:
    """True when both paths live on the same device. False if either cannot be stat'ed."""
    try:
        return Path(a).stat().st_dev == Path(b).stat().st_dev
    except OSError:
        return False


def directory_size(
    path: Path | str,
    extensions: Iterable[str] | str | None = None,
) -> tuple[int, int]:
    """Recursive ``(bytes, file_count)``, optionally restricted to *extensions*.

    Symlinks are never followed -- neither into directories (which could loop or escape
    the share) nor for sizing, so a link is never counted as the bytes of its target.
    Unreadable entries are skipped rather than aborting the walk; a single bad file must
    not stop retention from producing a report.
    """
    wanted: set[str] | None = None
    if isinstance(extensions, str):
        # ``general.media_extensions`` is a comma-separated string; iterating it as a
        # sequence would filter on single characters.
        extensions = extensions.split(",")
    if extensions is not None:
        wanted = {("." + e.lstrip(".").lower()) for e in extensions if e and e.strip(". ")}
        if not wanted:
            wanted = None

    total_bytes = 0
    total_files = 0
    stack: list[str] = [str(path)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        if (
                            wanted is not None
                            and os.path.splitext(entry.name)[1].lower() not in wanted
                        ):
                            continue
                        total_bytes += entry.stat(follow_symlinks=False).st_size
                        total_files += 1
                    except OSError:
                        continue
        except OSError:
            continue
    return total_bytes, total_files


#: How long a footage-tree measurement may be reused, in seconds.
#:
#: The walk is the single most expensive thing the API does: one ``scandir``/``stat`` per
#: file over what is usually a network share. ``/api/status`` used to do it *twice* per
#: request -- once for the storage bar and once inside the retention safety report -- on the
#: event loop, and the dashboard polls that endpoint every ten seconds. On a twenty-thousand
#: file share that is ~240,000 stat calls a minute against the NAS, with everything else in
#: the process (video range requests, the auth gate, both workers' heartbeats) blocked
#: behind each one.
#:
#: A minute is far finer-grained than a storage bar needs, and the number only moves when
#: the scanner or retention does something -- which is why the deletion path invalidates
#: this explicitly rather than waiting it out.
FOOTAGE_MEASURE_TTL_S = 60.0

_measure_cache: dict[tuple[str, tuple[str, ...]], tuple[float, tuple[int, int]]] = {}

#: Bumped whenever the tree is known to have changed. A walk that started before the bump
#: describes a share that no longer exists, so its result is returned to its own caller but
#: never cached -- otherwise an invalidation issued mid-walk is undone the moment that walk
#: finishes, which is exactly when a retention run is deleting things.
_measure_generation = 0


def _measure_key(path: Path | str, extensions: tuple[str, ...]) -> tuple[str, tuple[str, ...]]:
    """A cache key that two spellings of the same directory share, without touching disk.

    The *canonical* path matters here: `evaluate_safety` measures a resolved path while
    `current_usage` measures the setting's raw value, and `general.footage_dir` is free
    text -- so a non-canonical spelling gave the two callers different keys and neither
    ever saw the other's entry, which is the whole point of sharing the cache.

    ``normpath`` rather than ``resolve``, though, and that is the difference between a
    lexical operation and a syscall. ``Path.resolve`` calls ``realpath``, which walks the
    path component by component against the filesystem -- and this runs on the event loop,
    on *every* call including a cache hit, against the network mount the whole cache exists
    to stop touching. A symlinked footage root is the one case ``normpath`` cannot collapse;
    it costs a duplicate cache entry, not a wrong answer.
    """
    return (os.path.normpath(str(path)), extensions)


def _normalise_extensions(extensions: Iterable[str] | str | None) -> tuple[str, ...]:
    if extensions is None:
        return ()
    if isinstance(extensions, str):
        return tuple(sorted(extensions.split(",")))
    return tuple(sorted(extensions))


async def measure_tree(
    path: Path | str,
    extensions: Iterable[str] | str | None = None,
    *,
    max_age_s: float = 0.0,
) -> tuple[int, int]:
    """:func:`directory_size`, off the event loop and optionally memoised.

    ``max_age_s`` of zero -- the default -- always walks, which is what the retention
    guards want: they are deciding whether to delete, and a cached count is a count of a
    share that may no longer be the one in front of them. Read-only callers that just want
    a number on a screen pass :data:`FOOTAGE_MEASURE_TTL_S`.
    """
    # Materialised before it is used twice. `sorted()` exhausts a one-shot iterable, so a
    # generator passed here would leave `directory_size` with nothing to filter on -- and
    # `directory_size` reads an empty filter as "no filter", counting every file in the
    # tree. That inflates the storage bar and, worse, `SafetyReport.file_count`, which two
    # retention guards are decided on.
    wanted = _normalise_extensions(extensions)
    key = _measure_key(path, wanted)
    if max_age_s > 0:
        cached = _measure_cache.get(key)
        if cached is not None and (time.monotonic() - cached[0]) < max_age_s:
            return cached[1]

    generation = _measure_generation
    result = await asyncio.to_thread(directory_size, path, wanted or None)
    if generation == _measure_generation:
        _measure_cache[key] = (time.monotonic(), result)
    return result


def forget_tree_measurements() -> None:
    """Drop every cached measurement, because the tree has just been changed."""
    global _measure_generation

    _measure_generation += 1
    _measure_cache.clear()


def is_writable(path: Path | str) -> bool:
    """Prove writability by writing, not by asking.

    ``os.access`` lies on read-only mounts, overlays and network shares -- it answers from
    the permission bits, which stay writable when the mount itself is ``ro``. Retention is
    only allowed to delete when this returns True, so it has to be the real test: a probe
    file is created exclusively and removed again immediately, leaving nothing behind.
    """
    p = Path(path)
    if p.is_dir():
        probe = p / f".dashcam-writetest-{os.getpid()}-{uuid4().hex[:8]}"
        fd = None
        try:
            fd = os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            return True
        except OSError:
            return False
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
                try:
                    os.unlink(probe)
                except OSError:
                    pass
    if p.exists():
        try:
            # r+b appends nothing and truncates nothing; it just demands write access.
            with open(p, "r+b"):
                return True
        except OSError:
            return False
    # Nothing there yet -- the question becomes whether it could be created.
    parent = p.parent
    return is_writable(parent) if parent != p and parent.is_dir() else False
