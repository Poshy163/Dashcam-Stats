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


@dataclass(frozen=True, slots=True)
class RemoteFile:
    """One recording on the unit's TF card."""

    name: str
    size: int
    #: Unix seconds. Used only to leave the segment the camera is still writing alone.
    mtime: int


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
