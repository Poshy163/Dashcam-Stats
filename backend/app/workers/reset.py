"""Wiping the processing queue and rebuilding it from the footage.

What "reprocess all footage" means, and what it used to mean, are different operations.

It used to *add*: every previously-analysed recording was enqueued on top of whatever was
already in the queue. Two presses no longer stacked jobs -- ``enqueue(force=True)`` sees to
that -- but everything else survived. Yesterday's failures stayed failed, a job the pool was
halfway through stayed running, the counters on the Queue page carried the old run's
arithmetic into the new one, and the order the library came out in was inherited from
whatever had been queued before.

It now *replaces*. The queue is emptied, the runs in flight are stopped, the counters start
again, and the work is derived from the recordings that exist rather than from the rows that
happened to be in the table. Two things follow from that, and both are the point:

* **Thumbnails come first.** A thumbnail is a couple of ffmpeg seeks; a full analysis is
  minutes of decoding and inference. Making every missing thumbnail before any analysis
  starts gives the library pictures in the time it would otherwise take to analyse a handful
  of clips -- so a recording imported this morning is visible immediately instead of after
  the eight hundred older ones in front of it have been re-analysed.
* **Then the analysis runs oldest first.** Chronologically, across the whole library, with
  nothing able to jump the queue. Progress is then legible -- the date reached *is* the
  progress -- and an interrupted run leaves a contiguous processed span rather than holes.

A recording holds exactly one job throughout. The analysis job is created when the thumbnail
job finishes (see ``queue._follow_up_after_thumbnail``) rather than alongside it, because
queueing both up front would put every recording in the queue twice.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models import (
    BULK_PRIORITY,
    THUMBNAIL_PRIORITY,
    JobKind,
    Recording,
    RecordingState,
)
from app.pipeline.orchestrator import invalidate_recordings
from app.pipeline.stages import thumbnail_is_usable
from app.workers import queue
from app.workers.worker import get_worker_pool

log = get_logger(__name__)

#: Row ids per statement. SQLite bounds the number of bound parameters in one statement,
#: and a library of any size passed whole would be a single ``IN`` clause with one parameter
#: per recording.
_BATCH = 500


@dataclass(slots=True)
class RebuildSummary:
    """What the reset did, in the terms the user asked for it in."""

    #: Jobs retired by the wipe, keyed by the state they were in.
    cleared: dict[str, int] = field(default_factory=dict)
    #: Runs that were in flight and had to be stopped.
    aborted: int = 0
    #: Recordings the rebuild found eligible.
    recordings: int = 0
    #: Of those, the ones queued for the thumbnail-first pass.
    thumbnails: int = 0
    #: And the ones that already had a thumbnail and went straight to the analysis queue.
    analysis: int = 0
    #: Recordings released from a queued/processing state they no longer had a job for.
    released: int = 0
    stages: tuple[str, ...] = ()
    started_at: datetime | None = None

    @property
    def queued(self) -> int:
        return self.thumbnails + self.analysis

    def as_dict(self) -> dict[str, object]:
        return {
            "queued": self.queued,
            "recordings": self.recordings,
            "thumbnails_queued": self.thumbnails,
            "analysis_queued": self.analysis,
            "cleared": self.cleared,
            "aborted": self.aborted,
            "released": self.released,
            "stages": list(self.stages),
            "reset_at": self.started_at,
        }


def _batched(ids: Sequence[int]) -> Iterator[list[int]]:
    for start in range(0, len(ids), _BATCH):
        yield list(ids[start : start + _BATCH])


def _needs_thumbnail(rows: Sequence[tuple[int, str | None]]) -> set[int]:
    """Ids whose thumbnail is absent, unrecorded or an empty file. Blocking by design.

    One pass over the media volume rather than a stat from the event loop per recording:
    this is thousands of local filesystem calls and the caller hands the whole lot to a
    thread.
    """
    return {row_id for row_id, rel_path in rows if not thumbnail_is_usable(rel_path)}


async def reset_and_rebuild(
    session: AsyncSession, *, stages: list[str] | None = None
) -> RebuildSummary:
    """Empty the queue, stop what is running, and rebuild it from the footage.

    The caller's session is **committed part-way through**, deliberately. The wipe has to be
    durable before the runs in flight are stopped -- a worker that finishes during the abort
    re-reads its job row and declines to write an outcome only if it can see the
    cancellation -- and the abort waits seconds for stages to reach an interruption point.
    SQLite has one writer, so holding the write lock across that wait would block the
    scanner, the log sink and every other request for the duration.
    """
    summary = RebuildSummary(stages=tuple(stages) if stages else ())

    # 1. Empty the queue. Nothing waiting, nothing running, nothing failed.
    summary.cleared = await queue.clear(session)
    await session.commit()

    # 2. Stop the work itself. The rows above say the jobs are cancelled; this is what makes
    #    the runs stop, and it must finish before any replacement job can be claimed.
    try:
        summary.aborted = await get_worker_pool().abort_active()
    except Exception as exc:  # pragma: no cover - defensive; the rebuild must still happen
        log.warning("could not stop in-flight processing cleanly", error=str(exc))

    # 3. Open the new run. Everything queued from here counts towards it, and everything
    #    before it -- including the rows just cancelled -- does not.
    summary.started_at = datetime.now(UTC)
    queue.record_reset(summary.started_at)

    # 4. Discover the work from the footage, not from what used to be queued.
    #
    #    Ordered oldest first so the rows are created in the order they will run. The claim
    #    sorts by recording time regardless, but a queue whose insertion order agrees with
    #    its claim order is one where `queued_at` breaks ties in the same direction rather
    #    than against it.
    recordings = list(
        (
            await session.execute(
                select(Recording)
                .where(
                    Recording.ignored.is_(False),
                    Recording.file_missing.is_(False),
                    # Neither of these is work that is waiting. An INVALID file cannot be
                    # processed by any number of attempts -- including it is what let three
                    # zero-byte segments reappear in the failure list after every bulk
                    # requeue -- and a SETTLING one is still being written.
                    Recording.state.notin_([RecordingState.INVALID, RecordingState.SETTLING]),
                    # The fingerprint is withheld from a file whose mtime is still inside
                    # the settle window, which makes its absence an exact marker for "the
                    # dashcam is probably still writing this". A recording that has already
                    # been analysed is settled whatever its fingerprint says.
                    or_(Recording.fingerprint.is_not(None), Recording.processed_at.is_not(None)),
                )
                .order_by(
                    Recording.started_at.is_(None),
                    Recording.started_at.asc(),
                    Recording.id.asc(),
                )
            )
        ).scalars()
    )
    summary.recordings = len(recordings)
    ids = [recording.id for recording in recordings]

    # 5. Which of them have no picture. Asked of the filesystem: a stored path whose file is
    #    gone is a missing thumbnail, and it is the case a column check cannot see.
    missing = await asyncio.to_thread(
        _needs_thumbnail, [(r.id, r.thumbnail_path) for r in recordings]
    )

    # 6. Retire the analysis that is about to be redone, so nothing serves stale results
    #    while the new run repopulates them.
    selected: set[str] = set()
    for batch in _batched(ids):
        selected.update(await invalidate_recordings(session, batch, stages))
    summary.stages = tuple(sorted(selected)) if selected else summary.stages

    # 7. One job per recording. Not two: a recording waiting for a thumbnail is not also
    #    waiting for analysis, it is waiting for the thumbnail, and its analysis job is
    #    created when that finishes.
    queued: set[int] = set()
    for recording in recordings:
        if recording.id in queued:
            continue
        queued.add(recording.id)
        analysed = recording.processed_at is not None
        if recording.id in missing:
            await queue.enqueue(
                session,
                recording.id,
                kind=JobKind.THUMBNAIL,
                # Carried, not used: the thumbnail job hands these to the analysis job it
                # creates when it finishes.
                stages=stages if analysed else None,
                priority=THUMBNAIL_PRIORITY,
            )
            summary.thumbnails += 1
            continue
        await queue.enqueue(
            session,
            recording.id,
            kind=JobKind.REPROCESS if analysed else JobKind.PROCESS,
            # A recording that has never been analysed has no stage selection to honour:
            # "telemetry only" cannot mean anything to a file whose metadata has not been
            # read. None means "whatever this recording still needs".
            stages=stages if analysed else None,
            priority=BULK_PRIORITY,
        )
        summary.analysis += 1

    # 8. Put the recordings themselves back to a clean waiting state. `enqueue` already
    #    moves most of them, but not one left PROCESSING by a worker that has just been
    #    stopped -- which is exactly the row that would otherwise go on reporting a run that
    #    is not happening. The error text goes with it: it describes an attempt that has
    #    been superseded, and leaving it puts a stale fault on a recording that has not been
    #    tried yet.
    for batch in _batched(ids):
        await session.execute(
            update(Recording)
            .where(Recording.id.in_(batch))
            .values(state=RecordingState.QUEUED, error_message=None)
            .execution_options(synchronize_session=False)
        )
    await session.flush()

    # 9. Anything the rebuild did not queue must not be left claiming to be queued. A
    #    recording that became ignored or invalid since it was last enqueued has no job now
    #    and would otherwise sit in `queued` for good.
    summary.released = await queue.release_stranded_recordings(session)

    log.info(
        "rebuilt the processing queue",
        recordings=summary.recordings,
        thumbnails_first=summary.thumbnails,
        analysis=summary.analysis,
        cleared=summary.cleared,
        interrupted=summary.aborted,
        released=summary.released,
        stages=list(summary.stages),
    )
    return summary
