"""The one live view of the ingest, and the single-flight guard around it.

Everything that reports progress -- the API, the Home Assistant REST sensor, the webhook,
MQTT and the Backup page -- reads this one snapshot, so they cannot disagree with each
other. Live progress is deliberately in memory only: a pull writes a row to ``ingest_runs``
when it finishes, but per-second byte counts are not worth a database write each, and the
window they describe is over in two minutes.
"""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import UTC, datetime

from app.core.logging import get_logger
from app.ingest.models import DeltaPlan, RunResult, RunState

log = get_logger(__name__)


class IngestStatus:
    """Mutable progress, guarded because the transport updates it from a worker thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running = False
        self._cancel = threading.Event()

        self.state: RunState = RunState.IDLE
        self.unit_online: bool = False
        self.files_total = 0
        self.files_done = 0
        self.bytes_total = 0
        self.bytes_done = 0
        self.current_file: str | None = None
        self.backlog_files = 0
        self.backlog_bytes = 0
        self.last_success: datetime | None = None
        self.last_error: str | None = None
        self._started_at: float | None = None
        self._run_id: int | None = None

    # -- single flight -------------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._running

    def try_begin(self) -> bool:
        """Claim the right to run. False when a pull is already in flight.

        The window is one to two minutes long and the presence poll fires every few
        seconds, so without this a unit that stays on the network would have a second
        puller start on top of the first and both would extract into the same staging
        directory.
        """
        with self._lock:
            if self._running:
                return False
            self._running = True
            self._cancel.clear()
            self.state = RunState.RUNNING
            self.files_total = self.files_done = 0
            self.bytes_total = self.bytes_done = 0
            self.current_file = None
            self.last_error = None
            self._started_at = time.monotonic()
            return True

    def finish(self, result: RunResult) -> None:
        with self._lock:
            self._running = False
            self.state = result.state
            self.current_file = None
            self.last_error = result.error
            if result.state is RunState.OK and result.files:
                self.last_success = datetime.now(UTC)
            self._started_at = None

    def cancel(self) -> bool:
        """Ask an in-flight pull to stop at the next read. False if nothing is running."""
        with self._lock:
            if not self._running:
                return False
            self._cancel.set()
            return True

    @property
    def cancel_event(self) -> threading.Event:
        return self._cancel

    # -- progress ------------------------------------------------------------------------

    def plan(self, plan: DeltaPlan) -> None:
        with self._lock:
            self.files_total = len(plan.files)
            self.bytes_total = plan.bytes
            self.backlog_files = plan.backlog_files
            self.backlog_bytes = plan.backlog_bytes

    def set_backlog(self, files: int, size: int) -> None:
        with self._lock:
            self.backlog_files = files
            self.backlog_bytes = size

    def set_unit_online(self, online: bool) -> None:
        with self._lock:
            self.unit_online = online

    def set_state(self, state: RunState) -> None:
        with self._lock:
            self.state = state

    def add_bytes(self, count: int) -> None:
        # Called from the receiving thread for every socket read; no logging here.
        with self._lock:
            self.bytes_done += count

    def file_started(self, name: str) -> None:
        with self._lock:
            self.current_file = name

    def file_done(self, name: str) -> None:
        with self._lock:
            self.files_done += 1
            self.current_file = name

    # -- reporting -----------------------------------------------------------------------

    def elapsed(self) -> float:
        started = self._started_at
        return time.monotonic() - started if started is not None else 0.0

    def throughput_mbs(self) -> float:
        elapsed = self.elapsed()
        if elapsed <= 0 or not self.bytes_done:
            return 0.0
        return round(self.bytes_done / elapsed / 1_000_000, 2)

    def snapshot(self) -> dict[str, object]:
        """The shape consumed by /api/ingest/status, Home Assistant and the UI."""
        with self._lock:
            return {
                "state": self.state.value,
                "unit_online": self.unit_online,
                "files_total": self.files_total,
                "files_done": self.files_done,
                "bytes_total": self.bytes_total,
                "bytes_done": self.bytes_done,
                "throughput_mbs": self.throughput_mbs(),
                "current_file": self.current_file,
                "backlog_files": self.backlog_files,
                "backlog_bytes": self.backlog_bytes,
                "last_success_ts": self.last_success.isoformat() if self.last_success else None,
                "last_error": self.last_error,
            }


_status: IngestStatus | None = None
_status_lock = asyncio.Lock()


def get_status() -> IngestStatus:
    global _status
    if _status is None:
        _status = IngestStatus()
    return _status


def reset_status_for_tests() -> None:
    global _status
    _status = None
