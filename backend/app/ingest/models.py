"""Value types for the head-unit ingest.

Deliberately plain dataclasses with no database or HTTP in them: the puller, the API, the
Home Assistant reporter and the tests all speak this vocabulary, and keeping it free of
SQLAlchemy is what lets the whole transport be tested without a device or a session.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class UnitState(str, Enum):
    """What the ADB control channel says about the head unit."""

    DEVICE = "device"
    OFFLINE = "offline"
    UNAUTHORIZED = "unauthorized"
    UNKNOWN = "unknown"


class RunState(str, Enum):
    """The state a pull ended in, and the state the UI/Home Assistant display."""

    DISABLED = "disabled"
    IDLE = "idle"
    RUNNING = "running"
    OK = "ok"
    PARTIAL = "partial"
    ERROR = "error"
    OFFLINE = "offline"
    UNAUTHORIZED = "unauthorized"
    CANCELLED = "cancelled"


class Phase(str, Enum):
    """*Where* a running pull currently is, as opposed to how it ended.

    Separate from :class:`RunState` rather than folded into it, because the two answer
    different questions and one of them is already load-bearing: ``RunState`` is what the
    Home Assistant sensor, the webhook and MQTT publish, so adding "scanning" to it would
    change an established contract to say something those consumers never asked about.
    ``RunState.RUNNING`` stays exactly as broad as it was; this says which part of it.

    Only phases the code can actually report are listed. There is deliberately no
    "pausing recording" here -- nothing pauses recording, and a state the app can never
    enter is a promise in the UI that it does something it does not.
    """

    IDLE = "idle"
    CONNECTING = "connecting"
    SCANNING = "scanning"
    PREPARING = "preparing"
    TRANSFERRING = "transferring"
    VERIFYING = "verifying"


@dataclass(frozen=True, slots=True)
class RemoteFile:
    """One recording on the unit's TF card."""

    name: str
    size: int
    #: Unix seconds. Used only to leave the segment the camera is still writing alone.
    mtime: int
    #: The absolute directory on the unit this file is in.
    #:
    #: Carried per-file because a card holds recordings in more than one place: ordinary
    #: segments in ``DCIM/Video`` and incident-locked ones in ``DCIM/LockVideo``. The
    #: transfer groups by this and runs one ``tar`` per directory, rather than one ``tar``
    #: rooted at the parent -- a member arriving as ``LockVideo/x.ts`` would carry a path,
    #: and the receiver rejects those outright to prevent traversal. Grouping keeps that
    #: guard exactly as strict as it was.
    directory: str = ""


@dataclass(slots=True)
class UnitInfo:
    address: str
    state: UnitState = UnitState.UNKNOWN
    source: str | None = None
    #: Set when the unit answered but its card could not be located, which is a different
    #: problem from the unit being absent and needs to read as one.
    card_error: str | None = None

    @property
    def online(self) -> bool:
        return self.state is UnitState.DEVICE


@dataclass(slots=True)
class DeltaPlan:
    """What this run intends to fetch, and what remains on the unit overall."""

    files: list[RemoteFile] = field(default_factory=list)
    #: Everything the unit holds that we do not, including what this run skips.
    backlog_files: int = 0
    backlog_bytes: int = 0
    #: Files excluded because the camera is still writing them.
    active_skipped: int = 0
    #: Recordings the card still holds that the library already has, byte-for-byte.
    #:
    #: Not fetched -- that is the point of the delta -- but with delete-after-verify on they
    #: are still the card's to give back. Without this the only files ever reclaimed were
    #: the ones a run happened to copy itself, so everything copied before the setting was
    #: switched on stayed on the card for good, and the card stayed full of footage that was
    #: already safely in the library.
    already_local: list[RemoteFile] = field(default_factory=list)

    @property
    def bytes(self) -> int:
        return sum(item.size for item in self.files)


@dataclass(slots=True)
class RunResult:
    state: RunState = RunState.IDLE
    files: int = 0
    bytes: int = 0
    seconds: float = 0.0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.state in (RunState.OK, RunState.IDLE)

    @property
    def throughput_mbs(self) -> float:
        return round(self.bytes / self.seconds / 1_000_000, 2) if self.seconds > 0 else 0.0
