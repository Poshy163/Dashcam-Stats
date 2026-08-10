"""Is this file finished being written, and is there anything in it worth reading?

The scanner sees files while the dashcam is still writing them and files the dashcam
never finished writing at all, and it has to tell those two apart from a single
``stat()``. Getting it wrong in either direction is expensive:

* Treating a growing file as complete caches a truncated duration, a partial telemetry
  track and a thumbnail of half a segment, and nothing ever asks again.
* Treating a permanently empty file as "not ready yet" leaves it in the queue forever;
  treating it as a *transient* failure spends every retry attempt on a file that has no
  bytes in it. Three zero-byte segments in the real corpus did exactly that, and each
  bulk requeue handed them another four attempts.

The decision uses only what the directory listing already carries, so it costs nothing
per file, and it is a *stability* test rather than a delay: a file qualifies when its
modification time is older than the settle window **and** its size and mtime are the
ones the previous scan recorded. Nothing here sleeps or blocks -- the second observation
arrives with the next scan, whenever that is.

Only once a file has proved it is not moving is its size read as a verdict. A zero-byte
file that was written a second ago is a copy that has just started; a zero-byte file that
has been unchanged since the previous scan and was last touched half an hour ago is a
zero-byte file.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


class Readiness(enum.Enum):
    """What a scan concluded about one file."""

    #: Complete as far as anything here can tell; safe to fingerprint and process.
    READY = "ready"
    #: Still moving, or not yet seen twice. Not an error -- ask again next scan.
    SETTLING = "settling"
    #: Stable and unusable. No number of retries changes this.
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class Verdict:
    readiness: Readiness
    #: Why, in words a user reading the recordings list can act on. ``None`` when ready.
    reason: str | None = None

    @property
    def ready(self) -> bool:
        return self.readiness is Readiness.READY

    @property
    def settling(self) -> bool:
        return self.readiness is Readiness.SETTLING

    @property
    def invalid(self) -> bool:
        return self.readiness is Readiness.INVALID


_READY = Verdict(Readiness.READY)


def assess(
    *,
    size: int,
    mtime_ns: int,
    now_ns: int,
    settle_ns: int,
    previous_size: int | None = None,
    previous_mtime_ns: int | None = None,
) -> Verdict:
    """Decide whether *this* file, seen now, is ready, still settling, or unusable.

    ``previous_size``/``previous_mtime_ns`` are what the last scan recorded, or ``None``
    for a file being indexed for the first time. A first sighting is not automatically
    held back -- a library being imported for the first time is hundreds of files whose
    mtimes are days old, and making every one of them wait a whole scan interval would be
    a delay with nothing behind it. The mtime is the primary evidence; the cross-scan
    comparison is the second opinion that catches a writer preserving timestamps or a
    share whose clock disagrees with ours.
    """
    age_ns = now_ns - mtime_ns

    # A negative age means the file's mtime is in the future, which is clock skew between
    # this container and the share rather than a file being written. Deliberately *not*
    # treated as "recently written": that would stall such a share forever. It falls
    # through to the cross-scan check below, which needs no clocks to agree.
    if settle_ns > 0 and 0 <= age_ns < settle_ns:
        return Verdict(
            Readiness.SETTLING,
            f"last modified {age_ns / 1_000_000_000:.0f}s ago; waiting for it to settle",
        )

    if previous_size is not None and (previous_size != size or previous_mtime_ns != mtime_ns):
        return Verdict(
            Readiness.SETTLING,
            f"size or timestamp changed since the last scan "
            f"({previous_size} -> {size} bytes); waiting for a stable observation",
        )

    if size == 0:
        # Stable and empty. The dashcam created the file and never wrote a byte into it,
        # which happens to about one segment in two hundred here.
        return Verdict(Readiness.INVALID, "the file is empty (0 bytes)")

    return _READY


__all__ = ["Readiness", "Verdict", "assess"]
