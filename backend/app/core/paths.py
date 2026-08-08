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

import os
import re
from collections.abc import Iterable
from pathlib import Path, PurePath, PureWindowsPath
from uuid import uuid4

from app.config import get_config

__all__ = [
    "PathTraversalError",
    "data_root",
    "directory_size",
    "footage_root",
    "is_mount_point",
    "is_within",
    "is_writable",
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
