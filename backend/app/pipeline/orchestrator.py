"""Stage sequencing for one recording.

A corrupt recording must never stall the queue, so each stage is isolated: a failure is
recorded against that stage and the recording, and the run stops for *this* recording only.
Permanent failures (an empty file, a stream with no video) are distinguished from transient
ones so retries are not spent on work that can never succeed.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models import Recording, RecordingState, StageState
from app.db.retry import commit_with_retry, is_locked_error
from app.pipeline.stages import STAGE_ORDER, STAGES, StageError, StageResult

log = get_logger(__name__)

ProgressCallback = Callable[[str, float], None]

#: What the reprocess options in the UI expand to.
REPROCESS_PRESETS: dict[str, tuple[str, ...]] = {
    "metadata": ("metadata", "summarise"),
    "telemetry": ("telemetry", "summarise"),
    "detection": ("detection", "summarise"),
    "plates": ("plates", "summarise"),
    "everything": STAGE_ORDER,
}

_STAGE_FIELDS = {
    "metadata": "metadata_state",
    "telemetry": "telemetry_state",
    "detection": "detection_state",
    "plates": "plate_state",
}

#: Stages whose stored results are built out of another stage's rows, and are therefore
#: invalidated when that stage runs again.
#:
#: Re-running detection deletes and recreates every ``tracked_objects`` row. A plate
#: observation points at one of those, carries a copy of its position, and is what the
#: plate pages read -- so after a detection-only reprocess the observations referred to
#: tracks that no longer existed (the foreign key nulls the column rather than complaining)
#: and still carried the coordinates and offsets of the old ones. Nothing said so; the
#: plate simply kept a location the detection behind it no longer claimed.
#:
#: Telemetry is the same relationship one level up: every position on a track or an
#: observation is chosen from the telemetry, so re-reading the overlay invalidates both.
_STAGE_DEPENDENTS: dict[str, tuple[str, ...]] = {
    "telemetry": ("detection", "plates"),
    "detection": ("plates",),
}


def _with_dependents(stages: set[str]) -> set[str]:
    """Close a stage selection over everything its results feed."""
    resolved = set(stages)
    changed = True
    while changed:
        changed = False
        for name in list(resolved):
            for dependent in _STAGE_DEPENDENTS.get(name, ()):
                if dependent not in resolved:
                    resolved.add(dependent)
                    changed = True
    return resolved


@dataclass(slots=True)
class RunReport:
    recording_id: int
    stages: list[StageResult] = field(default_factory=list)
    ok: bool = True
    error: str | None = None
    permanent: bool = False
    #: The failure was contention rather than a defect -- a SQLite write lock held by
    #: another writer. The queue retries these without spending an attempt, because
    #: spending four of them turns a perfectly good recording into a permanent failure
    #: for a reason that has nothing to do with the recording.
    transient: bool = False
    elapsed_s: float = 0.0
    realtime_factor: float | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "recording_id": self.recording_id,
            "ok": self.ok,
            "error": self.error,
            "permanent": self.permanent,
            "transient": self.transient,
            "elapsed_s": round(self.elapsed_s, 2),
            "realtime_factor": self.realtime_factor,
            "stages": [
                {"name": s.name, "ok": s.ok, "detail": s.detail, "stats": s.stats}
                for s in self.stages
            ],
        }


def expand_stages(requested: list[str] | None) -> tuple[str, ...]:
    """Turn a request into a concrete ordered stage list."""
    if not requested:
        return STAGE_ORDER

    wanted: set[str] = set()
    for item in requested:
        key = item.strip().lower()
        if key in REPROCESS_PRESETS:
            wanted.update(REPROCESS_PRESETS[key])
        elif key in STAGES:
            wanted.add(key)

    if not wanted:
        return STAGE_ORDER
    wanted = _with_dependents(wanted)
    # Summarising is cheap and keeps rollups honest after any change.
    wanted.add("summarise")
    return tuple(name for name in STAGE_ORDER if name in wanted)


def pending_stages(recording: Recording) -> tuple[str, ...]:
    """Stages this recording still needs, for a job that did not name any.

    ``RUNNING`` counts as needed. Nothing is running at the point this is asked -- the
    question is only ever put when a job is about to start -- so a stage left RUNNING is
    one whose worker died partway through it. Treating it as complete is how a recording
    reclaimed after a restart came back with the stage it actually died in silently
    skipped, and finished as COMPLETED with that stage's results missing.
    """
    needed = {
        name
        for name, attr in _STAGE_FIELDS.items()
        if getattr(recording, attr) in (StageState.PENDING, StageState.FAILED, StageState.RUNNING)
    }
    if not needed:
        return ()
    # Metadata underpins everything else; if it is missing, run the lot.
    if "metadata" in needed:
        return STAGE_ORDER
    needed = _with_dependents(needed)
    needed.add("summarise")
    return tuple(name for name in STAGE_ORDER if name in needed)


async def _revive(session: AsyncSession, recording_id: int) -> Recording | None:
    """Get the session back into a state where the failure can actually be recorded.

    A stage that dies inside a flush leaves the session refusing everything until it is
    rolled back, so the very next line -- ``recording.state = FAILED`` -- is discarded, the
    commit after it raises ``PendingRollbackError``, and *that* escapes ``run_stages``
    entirely. The job is then never marked failed at all: it stays RUNNING with no worker,
    is reclaimed five minutes later, and decodes the whole recording again to fail the same
    way. Seven of those on the live library, each logged as a bare "worker loop error" that
    named neither the recording nor the job.

    SQLAlchemy also warns about the first half of that -- *"Session's state has been changed
    on a non-active transaction - this state will be discarded"* -- which is the write
    silently doing nothing, the failure mode §5.2 is about.

    Rolling back expires every mapped object, so the caller must use the recording this
    returns rather than the one it had -- and the id is taken as an argument rather than
    read off the old object for the same reason. Reading *any* attribute of an expired
    instance fires a lazy load, which under the async engine is synchronous IO outside a
    greenlet and raises ``MissingGreenlet``: the failure handler would then fail in a third
    way, on the line meant to repair the first two.

    Unconditional, because it costs nothing: every stage that succeeded has already
    committed, so there is never uncommitted work here worth keeping -- only the failed
    stage's half-written attempt, which is exactly what should go.
    """
    try:
        await session.rollback()
        return await session.get(Recording, recording_id)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("could not reset the session after a stage failure", error=str(exc))
        return None


async def run_stages(
    session: AsyncSession,
    recording: Recording,
    stages: list[str] | None = None,
    *,
    progress: ProgressCallback | None = None,
) -> RunReport:
    """Execute the requested stages against one recording."""
    selected = expand_stages(stages)
    report = RunReport(recording_id=recording.id)
    started = time.monotonic()

    recording.state = RecordingState.PROCESSING
    await session.flush()
    # Commit, do not merely flush. A flush takes SQLite's write lock and a transaction
    # holds it until it ends, so without this the lock is held for the *entire* job --
    # every ffmpeg decode, every inference pass, tens of seconds of it. Meanwhile the
    # second worker, the scheduler and the log sink all block on their next write, which
    # turns two workers into one and is where "database is locked" came from.
    #
    # Committing between stages instead keeps each write transaction to the length of a
    # write. It also means a run that fails at stage four keeps the three stages that
    # succeeded, which is what the per-stage state columns are for.
    #
    # Retried on contention rather than raised. This commit is three columns of
    # bookkeeping; losing the whole run because something else was mid-write when it
    # landed is exactly the trade this codebase kept making by accident.
    await commit_with_retry(session, what="mark recording processing")

    total = len(selected)
    for index, name in enumerate(selected):
        stage = STAGES[name]
        attr = _STAGE_FIELDS.get(name)

        # Both loop variables are bound as defaults. Closing over them by reference would
        # make every stage report the *last* stage's name and index once the loop moved on.
        def stage_progress(
            _stage: str, fraction: float, _i: int = index, _name: str = name
        ) -> None:
            if progress:
                progress(_name, (_i + min(1.0, max(0.0, fraction))) / total)

        if attr:
            setattr(recording, attr, StageState.RUNNING)
            # Commit the marker immediately, and do not carry it into the stage.
            #
            # Leaving the object dirty is enough to hold SQLite's write lock for the whole
            # stage: autoflush fires on the stage's first query, takes the lock, and the
            # transaction then stays open until the stage returns. With a model compiling
            # for the iGPU that is two minutes, and everything else that writes -- the
            # other worker claiming its next job, the scheduler reclaiming stale ones, the
            # log sink -- blows past its busy timeout and fails with "database is locked".
            #
            # The stage's own writes take the lock when they happen, which is brief and
            # near the end. What must not happen is taking it *before* the long work.
            await commit_with_retry(session, what=f"mark stage {name} running")
        if progress:
            progress(name, index / total)

        try:
            result = await stage(session, recording, progress=stage_progress)
            report.stages.append(result)
            await session.flush()
            # Release the write lock between stages rather than holding it across the
            # next decode. See the note above the first commit.
            await commit_with_retry(session, what=f"commit stage {name}")
        except StageError as exc:
            recording = await _revive(session, report.recording_id) or recording
            if attr:
                setattr(recording, attr, StageState.FAILED)
            report.ok = False
            report.error = str(exc)
            report.permanent = exc.permanent
            report.transient = exc.transient or is_locked_error(exc)
            # A permanent failure is a property of the *source*, not of the attempt, so it
            # is recorded on the recording as such. Left as FAILED it stayed in the retry
            # population: every "reprocess failed recordings" handed a zero-byte segment
            # another four attempts, and the queue page filled with the same three files
            # failing for the same unfixable reason on a fresh attempt counter.
            recording.state = RecordingState.INVALID if exc.permanent else RecordingState.FAILED
            recording.error_message = str(exc)
            recording.error_count += 1
            recording.last_error_at = datetime.now(UTC)
            log.warning(
                "stage failed",
                recording=recording.filename,
                stage=name,
                error=str(exc),
                permanent=exc.permanent,
            )
            break
        except Exception as exc:
            recording = await _revive(session, report.recording_id) or recording
            if attr:
                setattr(recording, attr, StageState.FAILED)
            report.ok = False
            report.error = f"{type(exc).__name__}: {exc}"
            # Anything that reaches here is unclassified, so it is transient by default --
            # the expensive mistake is the other way round, and a genuinely permanent
            # fault raises StageError with `permanent` set. `transient` is carried
            # separately so the queue can tell a database lock from a real defect.
            report.transient = is_locked_error(exc)
            recording.state = RecordingState.FAILED
            recording.error_message = report.error
            recording.error_count += 1
            recording.last_error_at = datetime.now(UTC)
            log.exception("stage raised", recording=recording.filename, stage=name)
            break

    report.elapsed_s = time.monotonic() - started
    if report.ok and recording.duration_s and report.elapsed_s > 0:
        # Video seconds processed per wall-clock second — what the queue page reports.
        report.realtime_factor = round(recording.duration_s / report.elapsed_s, 2)

    if progress:
        progress("done" if report.ok else "failed", 1.0)
    return report
