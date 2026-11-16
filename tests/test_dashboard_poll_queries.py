"""The pages that poll must get the same answers as before, for a fraction of the work.

``/api/status``, ``/api/jobs`` and ``/api/jobs/stats`` are asked every few seconds for as
long as a dashboard or the Queue page is open. On a real library -- twenty thousand
recordings, a job history ten times that -- each was reading the whole of ``recordings``
several times over, or fetching a recording row for every job ever run, to return a few
hundred bytes. They now read indexes instead, and what they return must not have moved.

So nothing here asserts how the queries are written. Each test builds a library, works the
expected answer out in plain Python from the rows it inserted, and compares. The reference
knows nothing about SQL, so it cannot share a mistake with the implementation.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, update

from app.core.settings_service import local_midnight_utc
from app.db.models import (
    JobKind,
    JobState,
    Journey,
    Plate,
    PlateObservation,
    ProcessingJob,
    Recording,
    RecordingState,
    TelemetryPoint,
    TrackedObject,
)
from app.db.session import session_scope
from app.pipeline.revisions import CURRENT_REVISIONS, INVALIDATED_REVISION
from app.workers import queue

BASE = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def isolated_reset_epoch():
    """The epoch is process-wide durable state, so it must not leak between tests.

    Cleared on the way in as well as restored on the way out: a test elsewhere that reset
    the queue leaves an epoch behind, and these histories are dated before it.
    """
    original = queue._reset_at
    queue._reset_at = None
    yield
    queue._reset_at = original


# --------------------------------------------------------------------------------------
# The queue: a randomised history, and what the pages should say about it
# --------------------------------------------------------------------------------------


async def a_job_history(seed: int, *, around: datetime, jobs: int = 90) -> list[dict]:
    """Jobs in every state, with the awkward rows a real table accumulates.

    Times sit on a coarse grid so that many rows tie and the id tie-break is exercised.
    Some terminal jobs carry no ``finished_at`` (cancelled while queued, or written by an
    older release), some queued ones still carry the ``finished_at`` of the failure they
    were retried from, some belong to hidden recordings and some to no recording at all.
    """
    rng = random.Random(seed)

    def moment() -> datetime:
        return around + timedelta(minutes=rng.randint(-10, 10))

    async with session_scope() as session:
        recordings = []
        for index in range(12):
            recording = Recording(
                rel_path=f"clip-{index}.ts",
                filename=f"clip-{index}.ts",
                size_bytes=1,
                state=RecordingState.COMPLETED,
                ignored=rng.random() < 0.25,
                started_at=None if rng.random() < 0.2 else moment(),
            )
            session.add(recording)
            recordings.append(recording)
        await session.flush()

        rows = []
        for _ in range(jobs):
            recording = None if rng.random() < 0.1 else rng.choice(recordings)
            state = rng.choices(
                [
                    JobState.COMPLETED,
                    JobState.CANCELLED,
                    JobState.FAILED,
                    JobState.QUEUED,
                    JobState.RUNNING,
                ],
                weights=[45, 15, 10, 25, 5],
            )[0]
            if state is JobState.QUEUED:
                started = moment() if rng.random() < 0.2 else None
                finished = moment() if rng.random() < 0.15 else None
            elif state is JobState.RUNNING:
                started = moment() if rng.random() < 0.9 else None
                finished = moment() if rng.random() < 0.1 else None
            else:
                started = moment() if rng.random() < 0.8 else None
                finished = moment() if rng.random() < 0.8 else None
            job = ProcessingJob(
                recording_id=recording.id if recording else None,
                kind=rng.choice([JobKind.PROCESS, JobKind.THUMBNAIL, JobKind.REPROCESS]),
                state=state,
                priority=rng.choice([10, 100, 100, 200]),
                queued_at=moment(),
                started_at=started,
                finished_at=finished,
            )
            session.add(job)
            rows.append((job, recording))
        await session.flush()

        return [
            {
                "id": job.id,
                "state": job.state,
                "kind": job.kind,
                "priority": job.priority,
                "queued_at": job.queued_at,
                "started_at": job.started_at,
                "finished_at": job.finished_at,
                "has_recording": recording is not None,
                "hidden": bool(recording and recording.ignored),
                "recording_started_at": recording.started_at if recording else None,
            }
            for job, recording in rows
        ]


def listed(history: list[dict], state: JobState | None = None) -> list[int]:
    """The ids the job list should return, in order. See ``list_jobs`` for the reasons."""

    def position(job: dict):
        newest = job["finished_at"] or job["started_at"] or job["queued_at"]
        rank = {JobState.RUNNING: 0, JobState.FAILED: 1, JobState.QUEUED: 2}.get(job["state"], 3)
        claim = (0, 0, 0.0, 0.0)
        if job["state"] is JobState.QUEUED:
            first_seen = job["recording_started_at"]
            claim = (
                job["priority"],
                first_seen is None,
                first_seen.timestamp() if first_seen else 0.0,
                job["queued_at"].timestamp(),
            )
        return (rank, *claim, -newest.timestamp(), -job["id"])

    shown = [
        job for job in history if not job["hidden"] and (state is None or job["state"] is state)
    ]
    return [job["id"] for job in sorted(shown, key=position)]


async def every_page(client, *, page_size: int, state: JobState | None, pages: int | None = None):
    """Walk the list a page at a time. Returns (ids, the total each page reported)."""
    ids: list[int] = []
    totals = set()
    page = 1
    while pages is None or page <= pages:
        url = f"/api/jobs?page_size={page_size}&page={page}"
        if state is not None:
            url += f"&state={state.value}"
        body = (await client.get(url)).json()
        totals.add(body["total"])
        ids.extend(item["id"] for item in body["items"])
        if len(body["items"]) < page_size:
            break
        page += 1
    return ids, totals


@pytest.mark.parametrize("seed", [1, 2, 3])
async def test_the_job_list_is_the_same_rows_in_the_same_order(client, seed):
    history = await a_job_history(seed, around=BASE)

    for state in (None, *JobState):
        expected = listed(history, state)

        for page_size in (6, 50):
            ids, totals = await every_page(client, page_size=page_size, state=state)
            assert ids == expected, (state, page_size)
            assert totals == {len(expected)}, (state, page_size)

        # One row to a page is the hardest case for the shortcut: every page boundary
        # falls somewhere new, including between two rows that tie on everything but id.
        ids, _ = await every_page(client, page_size=1, state=state, pages=12)
        assert ids == expected[:12], state


async def test_a_page_past_the_end_is_empty_and_still_counts_the_list(client):
    history = await a_job_history(4, around=BASE, jobs=20)
    expected = listed(history)

    body = (await client.get("/api/jobs?page_size=50&page=3")).json()
    assert body["items"] == []
    assert body["total"] == len(expected)


@pytest.mark.parametrize("seed", [5, 6, 7])
@pytest.mark.parametrize("scoped_to_a_reset", [False, True])
async def test_the_queue_counts_match_the_table(client, seed, scoped_to_a_reset):
    midnight = local_midnight_utc()
    history = await a_job_history(seed, around=midnight)
    # A reset in the middle of the history: jobs queued before it belong to the last run.
    epoch = midnight if scoped_to_a_reset else None
    queue._reset_at = epoch

    counted = [
        job for job in history if not job["hidden"] and (epoch is None or job["queued_at"] >= epoch)
    ]

    def in_state(state: JobState) -> int:
        return sum(1 for job in counted if job["state"] is state)

    stats = (await client.get("/api/jobs/stats")).json()
    assert stats["queued"] == in_state(JobState.QUEUED)
    assert stats["running"] == in_state(JobState.RUNNING)
    assert stats["failed"] == in_state(JobState.FAILED)
    assert stats["completed"] == in_state(JobState.COMPLETED)
    assert stats["thumbnails_pending"] == sum(
        1
        for job in counted
        if job["kind"] is JobKind.THUMBNAIL and job["state"] in (JobState.QUEUED, JobState.RUNNING)
    )
    assert stats["completed_today"] == sum(
        1
        for job in counted
        if job["state"] is JobState.COMPLETED
        and job["finished_at"] is not None
        and job["finished_at"] >= midnight
    )
    # The history is random, so make sure it actually exercised what is being counted.
    assert in_state(JobState.COMPLETED) and in_state(JobState.QUEUED)
    assert any(job["hidden"] for job in history)


async def test_hiding_a_recording_takes_its_jobs_out_of_the_counts_and_the_list(client):
    async with session_scope() as session:
        kept = Recording(rel_path="kept.ts", filename="kept.ts", size_bytes=1)
        gone = Recording(rel_path="gone.ts", filename="gone.ts", size_bytes=1)
        session.add_all([kept, gone])
        await session.flush()
        for recording in (kept, gone, gone):
            session.add(
                ProcessingJob(
                    recording_id=recording.id,
                    state=JobState.COMPLETED,
                    queued_at=BASE,
                    finished_at=BASE,
                )
            )
        session.add(ProcessingJob(recording_id=None, state=JobState.COMPLETED, queued_at=BASE))
        gone_id = gone.id

    assert (await client.get("/api/jobs/stats")).json()["completed"] == 4
    assert (await client.get("/api/jobs")).json()["total"] == 4

    async with session_scope() as session:
        await session.execute(update(Recording).where(Recording.id == gone_id).values(ignored=True))

    assert (await client.get("/api/jobs/stats")).json()["completed"] == 2
    listing = (await client.get("/api/jobs?state=completed")).json()
    assert listing["total"] == 2
    assert {item["recording_filename"] for item in listing["items"]} == {"kept.ts", None}


# --------------------------------------------------------------------------------------
# The dashboard totals
# --------------------------------------------------------------------------------------


def publishes(recording: dict, revision: str | None) -> bool:
    """The shared visibility rule, restated. See ``app.api.visibility.visible_revision``."""
    finalised = recording["state"] is RecordingState.COMPLETED or (
        recording["state"] is RecordingState.DELETED and recording["processed_at"] is not None
    )
    return finalised and not recording["ignored"] and revision != INVALIDATED_REVISION


#: (state, ignored, file_missing, processed, telemetry revision, detection, plates)
CURRENT = "current"
LIBRARY = [
    (RecordingState.COMPLETED, False, False, True, CURRENT, CURRENT, CURRENT),
    (RecordingState.COMPLETED, False, False, True, None, None, None),
    (RecordingState.COMPLETED, False, False, True, INVALIDATED_REVISION, CURRENT, CURRENT),
    (RecordingState.COMPLETED, False, False, True, CURRENT, INVALIDATED_REVISION, CURRENT),
    (RecordingState.COMPLETED, False, False, True, CURRENT, CURRENT, INVALIDATED_REVISION),
    (RecordingState.COMPLETED, True, False, True, CURRENT, CURRENT, CURRENT),
    (RecordingState.DELETED, False, True, True, CURRENT, CURRENT, CURRENT),
    (RecordingState.DELETED, False, True, False, CURRENT, CURRENT, CURRENT),
    (RecordingState.DELETED, True, True, True, CURRENT, CURRENT, CURRENT),
    (RecordingState.PROCESSING, False, False, False, CURRENT, CURRENT, CURRENT),
    (RecordingState.QUEUED, False, False, True, CURRENT, CURRENT, CURRENT),
    (RecordingState.DISCOVERED, False, False, False, None, None, None),
    (RecordingState.METADATA_EXTRACTED, False, False, False, None, None, None),
    (RecordingState.FAILED, False, False, False, CURRENT, None, None),
    (RecordingState.INVALID, False, False, False, None, None, None),
    (RecordingState.SETTLING, False, False, False, None, None, None),
    (RecordingState.FAILED, True, False, False, None, None, None),
]


async def a_library() -> tuple[list[dict], dict[str, set[int]]]:
    """One recording of every kind the dashboard distinguishes, each with analysis rows.

    Returns what was stored about each recording and, per plate, the recordings it was
    read in. Every recording gets a different number of rows, so a total that picked up
    the wrong ones cannot come out right by coincidence.
    """

    def revision(value: str | None, stage: str) -> str | None:
        return CURRENT_REVISIONS[stage] if value == CURRENT else value

    stored: list[dict] = []
    async with session_scope() as session:
        for index, row in enumerate(LIBRARY):
            state, ignored, missing, processed, telemetry, detection, plates = row
            recording = Recording(
                rel_path=f"library-{index}.ts",
                filename=f"library-{index}.ts",
                size_bytes=1,
                state=state,
                ignored=ignored,
                file_missing=missing,
                duration_s=60.0 + index,
                started_at=BASE + timedelta(minutes=index),
                processed_at=BASE + timedelta(hours=1, minutes=index) if processed else None,
                telemetry_revision=revision(telemetry, "telemetry"),
                detection_revision=revision(detection, "detection"),
                plate_revision=revision(plates, "plates"),
            )
            session.add(recording)
            await session.flush()
            points, tracks = index + 2, index + 1
            for step in range(points):
                session.add(
                    TelemetryPoint(
                        recording_id=recording.id,
                        t_offset_s=float(step),
                        captured_at=recording.started_at + timedelta(seconds=step),
                        has_fix=False,
                    )
                )
            for key in range(tracks):
                session.add(
                    TrackedObject(
                        recording_id=recording.id,
                        track_key=key,
                        class_label="car",
                        first_seen_offset_s=0.0,
                        last_seen_offset_s=1.0,
                        frame_count=3,
                    )
                )
            stored.append(
                {
                    "id": recording.id,
                    "state": state,
                    "ignored": ignored,
                    "file_missing": missing,
                    "processed_at": recording.processed_at,
                    "duration_s": recording.duration_s,
                    "telemetry_revision": recording.telemetry_revision,
                    "detection_revision": recording.detection_revision,
                    "plate_revision": recording.plate_revision,
                    "points": points,
                    "tracks": tracks,
                }
            )

        # Plate n is read in recordings n, n+1 and n+2, so most are seen several times and
        # in a mix of recordings that publish plates and recordings that do not. One more
        # has never been read in anything.
        sightings: dict[str, set[int]] = {}
        for index in range(len(stored)):
            text = f"S{index:03d}ABC"
            plate = Plate(normalised_text=text, display_text=text)
            session.add(plate)
            await session.flush()
            sightings[text] = set()
            for recording in stored[index : index + 3]:
                session.add(
                    PlateObservation(
                        plate_id=plate.id,
                        recording_id=recording["id"],
                        t_offset_s=1.0,
                        raw_text=text,
                        normalised_text=text,
                        ocr_confidence=0.9,
                        detection_confidence=0.9,
                    )
                )
                sightings[text].add(recording["id"])
        session.add(Plate(normalised_text="S999NEVER", display_text="S999NEVER"))
        sightings["S999NEVER"] = set()
    return stored, sightings


async def test_the_dashboard_totals_match_the_library(client):
    stored, sightings = await a_library()
    by_id = {recording["id"]: recording for recording in stored}
    shown = [recording for recording in stored if not recording["ignored"]]

    def in_state(*states: RecordingState) -> int:
        return sum(1 for recording in shown if recording["state"] in states)

    status = (await client.get("/api/status")).json()
    totals, processing = status["totals"], status["processing"]

    assert totals["recordings"] == sum(1 for r in shown if not r["file_missing"])
    assert totals["duration_s"] == pytest.approx(sum(r["duration_s"] for r in shown))
    assert totals["telemetry_points"] == sum(
        r["points"] for r in stored if publishes(r, r["telemetry_revision"])
    )
    assert totals["tracked_objects"] == sum(
        r["tracks"] for r in stored if publishes(r, r["detection_revision"])
    )
    assert totals["plates"] == sum(
        1
        for seen_in in sightings.values()
        if any(publishes(by_id[rec], by_id[rec]["plate_revision"]) for rec in seen_in)
    )
    # Some plates must be visible and some not, or the last assertion proves nothing.
    assert 0 < totals["plates"] < len(sightings)

    assert processing["completed"] == in_state(RecordingState.COMPLETED)
    assert processing["pending"] == in_state(
        RecordingState.DISCOVERED, RecordingState.METADATA_EXTRACTED, RecordingState.QUEUED
    )
    assert processing["processing"] == in_state(RecordingState.PROCESSING)
    assert processing["failed"] == in_state(RecordingState.FAILED)
    assert processing["invalid"] == in_state(RecordingState.INVALID)
    assert processing["settling"] == in_state(RecordingState.SETTLING)


async def two_drives() -> tuple[int, int]:
    """Two recordings with a different amount of everything; the second starts hidden."""
    ids = []
    async with session_scope() as session:
        for index, (points, tracks, hidden) in enumerate([(30, 2, False), (12, 5, True)]):
            start = BASE + timedelta(hours=index)
            journey = Journey(
                started_at=start, ended_at=start + timedelta(minutes=1), duration_s=60.0
            )
            session.add(journey)
            await session.flush()
            recording = Recording(
                rel_path=f"drive-{index}.ts",
                filename=f"drive-{index}.ts",
                size_bytes=1,
                state=RecordingState.COMPLETED,
                journey_id=journey.id,
                started_at=start,
                ignored=hidden,
            )
            session.add(recording)
            await session.flush()
            for step in range(points):
                session.add(
                    TelemetryPoint(
                        recording_id=recording.id,
                        journey_id=journey.id,
                        t_offset_s=float(step),
                        captured_at=start + timedelta(seconds=step),
                        lat=-34.8088 + index * 0.01 + step * 1e-4,
                        lon=138.6769,
                        has_fix=True,
                        speed_kmh=40.0,
                    )
                )
            for key in range(tracks):
                session.add(
                    TrackedObject(
                        recording_id=recording.id,
                        track_key=key,
                        class_label="car",
                        first_seen_offset_s=0.0,
                        last_seen_offset_s=1.0,
                        frame_count=3,
                    )
                )
            ids.append(recording.id)
    return ids[0], ids[1]


async def swap_which_is_hidden(now_hidden: int, now_shown: int) -> None:
    """One recording hidden and another restored, with nobody looking in between."""
    async with session_scope() as session:
        await session.execute(
            update(Recording).where(Recording.id == now_hidden).values(ignored=True)
        )
        await session.execute(
            update(Recording).where(Recording.id == now_shown).values(ignored=False)
        )


async def test_swapping_which_recording_is_hidden_moves_the_dashboard_totals(client):
    """Nothing is added or removed, and the number of visible recordings does not change.

    The memoised totals were kept against exactly those things -- row counts, newest ids
    and how many recordings were visible -- so this left them describing the recording
    that had just been hidden until something unrelated changed.
    """
    first, second = await two_drives()
    before = (await client.get("/api/status")).json()["totals"]
    assert (before["telemetry_points"], before["tracked_objects"]) == (30, 2)

    await swap_which_is_hidden(first, second)

    after = (await client.get("/api/status")).json()["totals"]
    assert (after["telemetry_points"], after["tracked_objects"]) == (12, 5)


async def test_swapping_which_recording_is_hidden_redraws_the_map(client):
    first, second = await two_drives()
    before = (await client.get("/api/map/routes?simplify_m=0")).json()
    assert before["points"] == 30

    await swap_which_is_hidden(first, second)

    after = (await client.get("/api/map/routes?simplify_m=0")).json()
    assert after["points"] == 12


async def test_a_reprocess_that_stores_fewer_rows_moves_the_dashboard_totals(client):
    """The older recording loses rows; the newest id in either table does not move.

    That is what reprocessing looks like when the stage produces less than it did last
    time, and the totals have to follow it without counting the tables to notice.
    """
    first, second = await two_drives()
    async with session_scope() as session:
        await session.execute(update(Recording).where(Recording.id == second).values(ignored=False))
    before = (await client.get("/api/status")).json()["totals"]
    assert (before["telemetry_points"], before["tracked_objects"]) == (42, 7)

    async with session_scope() as session:
        await session.execute(
            delete(TelemetryPoint).where(
                TelemetryPoint.recording_id == first, TelemetryPoint.t_offset_s >= 10.0
            )
        )
        await session.execute(delete(TrackedObject).where(TrackedObject.recording_id == first))
        # What the summarise stage does when the reprocess finishes.
        await session.execute(
            update(Recording).where(Recording.id == first).values(processed_at=datetime.now(UTC))
        )

    after = (await client.get("/api/status")).json()["totals"]
    assert (after["telemetry_points"], after["tracked_objects"]) == (22, 5)
