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
from collections import deque
from datetime import UTC, datetime

from app.core.logging import get_logger
from app.ingest.models import DeltaPlan, Phase, RunResult, RunState

log = get_logger(__name__)

#: How far back the *recent* speed looks.
#:
#: The run average is the wrong number to put in front of someone watching a transfer: it
#: is dragged down for the whole run by the seconds spent connecting before any bytes moved,
#: so it reads low exactly when somebody is deciding whether this is working. Five seconds
#: is long enough to ride out a stalled socket read and short enough to react to the car
#: driving out of range.
_SPEED_WINDOW_S = 5.0

#: Sampling floor. ``add_bytes`` fires for every socket read -- tens of times a second at
#: full rate -- and a sample per read would be pure churn for a number rendered every 1.5s.
_SAMPLE_INTERVAL_S = 0.25


class IngestStatus:
    """Mutable progress, guarded because the transport updates it from a worker thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running = False
        self._cancel = threading.Event()

        self.state: RunState = RunState.IDLE
        self.phase: Phase = Phase.IDLE
        self.unit_online: bool = False
        #: When the unit was last seen arriving, so `online_for` can answer "is the car
        #: actually parked, or just passing through". Cleared the moment it drops off.
        self._online_since: float | None = None
        self.files_total = 0
        self.files_done = 0
        self.bytes_total = 0
        self.bytes_done = 0
        self.current_file: str | None = None
        self.backlog_files = 0
        self.backlog_bytes = 0
        self.active_skipped = 0
        self.last_success: datetime | None = None
        self.last_error: str | None = None
        self._started_at: float | None = None
        self._started_wall: datetime | None = None
        #: (monotonic, cumulative bytes) pairs inside the speed window.
        self._samples: deque[tuple[float, int]] = deque(maxlen=64)
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
            self.phase = Phase.CONNECTING
            self.files_total = self.files_done = 0
            self.bytes_total = self.bytes_done = 0
            self.active_skipped = 0
            self.current_file = None
            self.last_error = None
            self._samples.clear()
            self._started_at = time.monotonic()
            self._started_wall = datetime.now(UTC)
            return True

    def finish(self, result: RunResult) -> None:
        with self._lock:
            self._running = False
            self.state = result.state
            self.phase = Phase.IDLE
            self.current_file = None
            self.last_error = result.error
            if result.state is RunState.OK and result.files:
                self.last_success = datetime.now(UTC)
            self._started_at = None
            self._started_wall = None
            self._samples.clear()

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
            self.active_skipped = plan.active_skipped

    def set_phase(self, phase: Phase) -> None:
        with self._lock:
            self.phase = phase

    def set_backlog(self, files: int, size: int) -> None:
        with self._lock:
            self.backlog_files = files
            self.backlog_bytes = size

    def set_unit_online(self, online: bool) -> None:
        with self._lock:
            if online and self._online_since is None:
                self._online_since = time.monotonic()
            elif not online:
                self._online_since = None
            self.unit_online = online

    def online_for(self) -> float:
        """Seconds the unit has been continuously on the network; 0.0 while it is not.

        This is what gates the radio quieting: a connection younger than its guard is a
        car that may be about to reverse out of range, and its Bluetooth is left alone.
        Undercounting is the safe direction, which is why a fresh process that finds the
        unit already present starts the clock at first sight rather than guessing.
        """
        with self._lock:
            if self._online_since is None:
                return 0.0
            return time.monotonic() - self._online_since

    def set_state(self, state: RunState) -> None:
        with self._lock:
            self.state = state

    def add_bytes(self, count: int) -> None:
        # Called from the receiving thread for every socket read; no logging here.
        with self._lock:
            self.bytes_done += count
            now = time.monotonic()
            if not self._samples or now - self._samples[-1][0] >= _SAMPLE_INTERVAL_S:
                self._samples.append((now, self.bytes_done))
                while len(self._samples) > 1 and now - self._samples[0][0] > _SPEED_WINDOW_S:
                    self._samples.popleft()

    def file_started(self, name: str) -> None:
        with self._lock:
            self.current_file = name

    def file_done(self, name: str) -> None:
        # Cleared rather than left pointing at *name*. This used to hold the file that had
        # just finished, so the page named the previous recording during the gap before the
        # next one started -- reading, to anyone watching, as though the transfer had gone
        # backwards.
        with self._lock:
            self.files_done += 1
            self.current_file = None

    # -- reporting -----------------------------------------------------------------------

    def elapsed(self) -> float:
        started = self._started_at
        return time.monotonic() - started if started is not None else 0.0

    def throughput_mbs(self) -> float:
        """The whole-run average, which is what the history table and MQTT report."""
        elapsed = self.elapsed()
        if elapsed <= 0 or not self.bytes_done:
            return 0.0
        return round(self.bytes_done / elapsed / 1_000_000, 2)

    def speed_recent_mbs(self) -> float:
        """The rate over the last few seconds, which is what a person watching wants."""
        if len(self._samples) < 2:
            return 0.0
        first_at, first_bytes = self._samples[0]
        last_at, last_bytes = self._samples[-1]
        span = last_at - first_at
        if span <= 0:
            return 0.0
        return round((last_bytes - first_bytes) / span / 1_000_000, 2)

    def eta_seconds(self) -> float | None:
        """Time to the end of *this run's* plan, or None when that cannot be said honestly.

        Only offered while bytes are actually moving. An estimate produced from the run
        average during the connect would be wrong by a factor of several, and one offered
        while the transfer is stalled would count down through a transfer that is not
        happening -- both worse than showing nothing.
        """
        if self.phase is not Phase.TRANSFERRING:
            return None
        remaining = self.bytes_total - self.bytes_done
        rate = self.speed_recent_mbs() * 1_000_000
        if remaining <= 0 or rate <= 0:
            return None
        return round(remaining / rate, 1)

    def snapshot(self) -> dict[str, object]:
        """The shape consumed by /api/ingest/status, Home Assistant and the UI.

        Every field added here is additive. ``state`` in particular keeps exactly the
        meaning it has always had, because it is what the Home Assistant REST sensor, the
        webhook and the MQTT topics publish -- ``phase`` sits alongside it rather than
        refining it, so an existing automation cannot be broken by this file.
        """
        with self._lock:
            return {
                "state": self.state.value,
                "phase": self.phase.value,
                "unit_online": self.unit_online,
                "files_total": self.files_total,
                "files_done": self.files_done,
                "bytes_total": self.bytes_total,
                "bytes_done": self.bytes_done,
                "throughput_mbs": self.throughput_mbs(),
                "speed_mbs_recent": self.speed_recent_mbs(),
                "eta_seconds": self.eta_seconds(),
                "current_file": self.current_file,
                "backlog_files": self.backlog_files,
                "backlog_bytes": self.backlog_bytes,
                "active_skipped": self.active_skipped,
                "started_at": self._started_wall.isoformat() if self._started_wall else None,
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
