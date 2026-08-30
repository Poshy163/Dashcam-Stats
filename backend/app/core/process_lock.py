"""Small portable advisory locks for process-lifetime safety fences.

SQLite leases tell another worker *when* an owner last made progress.  They cannot tell
whether that owner's process is still alive inside a blocking syscall or worker thread.
This module supplies the missing local-host fact: the operating system releases the lock
only when its file descriptor is closed or the owning process exits.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path
from typing import BinaryIO


class ProcessFileLock:
    """An acquired exclusive advisory lock backed by one open file descriptor."""

    __slots__ = ("_file", "path")

    def __init__(self, path: Path, stream: BinaryIO) -> None:
        self.path = path
        self._file: BinaryIO | None = stream

    @property
    def held(self) -> bool:
        return self._file is not None

    def release(self) -> None:
        stream, self._file = self._file, None
        if stream is None:
            return
        try:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()

    def __enter__(self) -> ProcessFileLock:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


def try_acquire(path: Path) -> ProcessFileLock | None:
    """Acquire *path* without waiting, or return ``None`` when another process owns it.

    The file is deliberately retained after release.  Its inode is the rendezvous point;
    unlinking it would let a concurrent opener lock a new inode while an old owner still
    holds the original one.
    """

    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+b")
    try:
        # Windows byte-range locking requires the byte to exist and operates from the
        # current file position. POSIX flock ignores both details, so the same file shape
        # is valid on both platforms.
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    stream.close()
                    return None
                raise
        else:
            import fcntl

            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN):
                    stream.close()
                    return None
                raise
        return ProcessFileLock(path, stream)
    except Exception:
        if not stream.closed:
            stream.close()
        raise


__all__ = ["ProcessFileLock", "try_acquire"]
