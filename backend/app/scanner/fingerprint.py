"""Cheap, stable change detection for footage files.

Full-file hashing is deliberately not the default. The library is expected to reach
hundreds of gigabytes, and re-reading all of it every hour to notice that nothing changed
would saturate the share for no benefit.

Dashcam segments are write-once: the camera closes a file and never touches it again. So
``(size, mtime)`` is the real change signal, and the digest exists only to catch the rare
case where a file is rewritten at exactly the same size — a re-copy, a repaired segment,
or a filesystem that reports a coarse mtime.
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

#: Bytes read from each of the head, middle and tail. Three windows rather than one
#: because a truncated or partially-rewritten segment usually differs at the tail while
#: sharing its opening bytes.
DEFAULT_SAMPLE_BYTES = 1 << 20


def _fingerprint_sync(path: Path, sample_bytes: int) -> str:
    size = path.stat().st_size
    digest = hashlib.blake2b(digest_size=16)
    # Size participates in the digest so two files sampled identically but of different
    # lengths can never collide.
    digest.update(str(size).encode("ascii"))

    with path.open("rb") as fh:
        if size <= sample_bytes * 3:
            # Small enough that sampling would read most of it anyway.
            while chunk := fh.read(1 << 20):
                digest.update(chunk)
        else:
            for offset in (0, (size - sample_bytes) // 2, size - sample_bytes):
                fh.seek(offset)
                digest.update(fh.read(sample_bytes))

    return digest.hexdigest()


async def fingerprint_file(path: Path | str, sample_bytes: int = DEFAULT_SAMPLE_BYTES) -> str:
    """Partial content fingerprint. Reads at most ``3 * sample_bytes``."""
    return await asyncio.to_thread(_fingerprint_sync, Path(path), sample_bytes)


def _full_hash_sync(path: Path) -> str:
    digest = hashlib.blake2b(digest_size=16)
    with path.open("rb") as fh:
        while chunk := fh.read(1 << 22):
            digest.update(chunk)
    return digest.hexdigest()


async def full_hash(path: Path | str) -> str:
    """Whole-file digest. Only for explicit user-requested verification — never in a scan."""
    return await asyncio.to_thread(_full_hash_sync, Path(path))
