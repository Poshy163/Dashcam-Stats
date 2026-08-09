"""Recordings, journeys, plates, vehicles, jobs and search."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query, Response, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import selectinload

from app.api.deps import PaginationDep, SessionDep
from app.api.schemas import (
    JobOut,
    JourneyDetailOut,
    JourneyOut,
    MergeRequest,
    Paginated,
    PlateObservationOut,
    PlateOut,
    PlatePatch,
    QueueStatsOut,
    RecordingOut,
    ReprocessRequest,
    SearchResults,
    SplitRequest,
    TelemetryPointOut,
    TrackedObjectOut,
    VehicleOut,
)
from app.core.logging import get_logger
from app.core.settings_service import get_settings_service
from app.db.models import (
    Camera,
    JobKind,
    Journey,
    Plate,
    PlateObservation,
    ProcessingJob,
    Recording,
    TelemetryPoint,
    TrackedObject,
    Vehicle,
)
from app.journeys.builder import JourneyBuilder
from app.workers import queue

log = get_logger(__name__)

router = APIRouter(prefix="/api", tags=["content"])


def _local_zone() -> ZoneInfo:
    """The timezone the user's date picker is expressing dates in."""
    try:
        return ZoneInfo(str(get_settings_service().get_nowait("general.timezone")))
    except Exception:
        return ZoneInfo("UTC")


def _is_date_only(value: datetime) -> bool:
    return (value.hour, value.minute, value.second, value.microsecond) == (0, 0, 0, 0)


def _day_start(value: datetime) -> datetime:
    """Interpret a bare date as local midnight, in UTC.

    ``started_at`` is stored in UTC while the picker speaks the user's local time, and for
    Adelaide those differ by nine and a half hours. Comparing the raw value against UTC
    would slide every boundary by that much, so a date is anchored in the configured zone
    before being converted.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=_local_zone() if _is_date_only(value) else UTC)
    return value.astimezone(UTC)


def _before_end_of(column, value: datetime):
    """Upper-bound condition for a date filter.

    A bare date means the whole of that day, so the bound becomes the start of the day
    after it, exclusive. ``<input type="date">`` sends ``2026-08-08``, which parses to
    midnight, and comparing ``<=`` against that excluded the entire day: picking a single
    date returned "No recordings match" while two dozen recordings sat under it.

    An explicit timestamp is left alone and stays inclusive. Someone who supplied a precise
    instant meant that instant, and silently widening it to the end of the day would be a
    different bug in the other direction.
    """
    if _is_date_only(value):
        return column < _day_start(value) + timedelta(days=1)
    return column <= _day_start(value)


async def _paginate(session, stmt, page, schema):
    total = int(
        (
            await session.execute(select(func.count()).select_from(stmt.order_by(None).subquery()))
        ).scalar()
        or 0
    )
    rows = (
        (await session.execute(stmt.offset(page.offset).limit(page.page_size)))
        .scalars()
        .unique()
        .all()
    )
    return Paginated[schema](
        items=[schema.model_validate(r) for r in rows],
        total=total,
        page=page.page,
        page_size=page.page_size,
        pages=page.pages(total),
    )


# --------------------------------------------------------------------------------------
# Recordings
# --------------------------------------------------------------------------------------


@router.get("/recordings", response_model=Paginated[RecordingOut])
async def list_recordings(
    session: SessionDep,
    page: PaginationDep,
    camera_id: int | None = None,
    journey_id: int | None = None,
    state: str | None = None,
    has_gps: bool | None = None,
    has_detections: bool | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    search: str | None = None,
    sort: str = Query("started_desc"),
):
    stmt = select(Recording).options(selectinload(Recording.camera))

    if camera_id is not None:
        stmt = stmt.where(Recording.camera_id == camera_id)
    if journey_id is not None:
        stmt = stmt.where(Recording.journey_id == journey_id)
    if state:
        stmt = stmt.where(Recording.state == state)
    if has_gps is not None:
        stmt = stmt.where(Recording.has_gps.is_(has_gps))
    if has_detections is not None:
        condition = (Recording.vehicle_count > 0) | (Recording.plate_count > 0)
        stmt = stmt.where(condition if has_detections else ~condition)
    if date_from:
        stmt = stmt.where(Recording.started_at >= _day_start(date_from))
    if date_to:
        # Exclusive upper bound on the *next* day, not inclusive on midnight of this one.
        # The picker sends "2026-08-08", which parses to 00:00, so comparing `<=` against
        # it excluded the whole day: choosing a single date — the most obvious thing to do
        # with a date filter — returned "No recordings match. Adjust the filters, or run a
        # scan", telling the user their footage was not indexed while 24 recordings sat
        # under that date.
        stmt = stmt.where(_before_end_of(Recording.started_at, date_to))
    if search:
        stmt = stmt.where(Recording.filename.ilike(f"%{search}%"))

    order = {
        "started_desc": Recording.started_at.desc().nullslast(),
        "started_asc": Recording.started_at.asc().nullslast(),
        "size_desc": Recording.size_bytes.desc(),
        "plates_desc": Recording.plate_count.desc(),
        "vehicles_desc": Recording.vehicle_count.desc(),
    }.get(sort, Recording.started_at.desc().nullslast())

    return await _paginate(session, stmt.order_by(order, Recording.id.desc()), page, RecordingOut)


@router.get("/recordings/{recording_id}", response_model=RecordingOut)
async def get_recording(recording_id: int, session: SessionDep):
    recording = (
        await session.execute(
            select(Recording)
            .options(selectinload(Recording.camera))
            .where(Recording.id == recording_id)
        )
    ).scalar_one_or_none()
    if recording is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Recording not found")
    return RecordingOut.model_validate(recording)


@router.get("/recordings/{recording_id}/telemetry", response_model=list[TelemetryPointOut])
async def get_telemetry(recording_id: int, session: SessionDep):
    rows = (
        (
            await session.execute(
                select(TelemetryPoint)
                .where(TelemetryPoint.recording_id == recording_id)
                .order_by(TelemetryPoint.t_offset_s.asc())
            )
        )
        .scalars()
        .all()
    )
    return [TelemetryPointOut.model_validate(r) for r in rows]


@router.get("/recordings/{recording_id}/detections", response_model=list[TrackedObjectOut])
async def get_detections(recording_id: int, session: SessionDep):
    rows = (
        (
            await session.execute(
                select(TrackedObject)
                .where(TrackedObject.recording_id == recording_id)
                .order_by(TrackedObject.first_seen_offset_s.asc())
            )
        )
        .scalars()
        .all()
    )
    return [TrackedObjectOut.model_validate(r) for r in rows]


@router.get("/recordings/{recording_id}/plates", response_model=list[PlateObservationOut])
async def get_recording_plates(recording_id: int, session: SessionDep):
    rows = (
        await session.execute(
            select(PlateObservation, Recording.filename, Camera.name)
            .join(Recording, Recording.id == PlateObservation.recording_id)
            .outerjoin(Camera, Camera.id == PlateObservation.camera_id)
            .where(PlateObservation.recording_id == recording_id)
            .order_by(PlateObservation.t_offset_s.asc())
        )
    ).all()
    return [_observation_out(obs, filename, camera) for obs, filename, camera in rows]


def _observation_out(obs: PlateObservation, filename: str | None, camera: str | None):
    data = PlateObservationOut.model_validate(obs)
    data.recording_filename = filename
    data.camera_name = camera
    return data


@router.post("/recordings/{recording_id}/reprocess")
async def reprocess_recording(recording_id: int, body: ReprocessRequest, session: SessionDep):
    recording = await session.get(Recording, recording_id)
    if recording is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Recording not found")

    job = await queue.enqueue(
        session,
        recording_id,
        kind=JobKind.REPROCESS,
        stages=body.stages,
        priority=50,
        force=True,
    )
    return {"job_id": job.id if job else None, "stages": body.stages}


# --------------------------------------------------------------------------------------
# Journeys
# --------------------------------------------------------------------------------------


@router.get("/journeys", response_model=Paginated[JourneyOut])
async def list_journeys(
    session: SessionDep,
    page: PaginationDep,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    has_gps: bool | None = None,
    sort: str = Query("started_desc"),
):
    stmt = select(Journey)
    if date_from:
        stmt = stmt.where(Journey.started_at >= _day_start(date_from))
    if date_to:
        # Same inclusive-day handling as the recordings filter above.
        stmt = stmt.where(_before_end_of(Journey.started_at, date_to))
    if has_gps is not None:
        stmt = stmt.where(Journey.has_gps.is_(has_gps))

    order = {
        "started_desc": Journey.started_at.desc(),
        "started_asc": Journey.started_at.asc(),
        "distance_desc": Journey.distance_m.desc().nullslast(),
        "duration_desc": Journey.duration_s.desc(),
    }.get(sort, Journey.started_at.desc())

    return await _paginate(session, stmt.order_by(order), page, JourneyOut)


def _simplify(points: list[tuple[float, float]], tolerance_m: float) -> list[tuple[float, float]]:
    """Douglas-Peucker in degrees, with the tolerance converted from metres.

    A long journey is tens of thousands of 1 Hz fixes; sending them all makes the map
    sluggish for a line that looks identical at any usable zoom.
    """
    if len(points) < 3 or tolerance_m <= 0:
        return points
    tolerance = tolerance_m / 111_320.0

    def rdp(subset: list[tuple[float, float]]) -> list[tuple[float, float]]:
        if len(subset) < 3:
            return subset
        start, end = subset[0], subset[-1]
        dx, dy = end[0] - start[0], end[1] - start[1]
        norm = (dx * dx + dy * dy) ** 0.5
        worst_index, worst = 0, -1.0
        for i in range(1, len(subset) - 1):
            px, py = subset[i]
            if norm == 0:
                distance = ((px - start[0]) ** 2 + (py - start[1]) ** 2) ** 0.5
            else:
                distance = abs(dy * px - dx * py + end[0] * start[1] - end[1] * start[0]) / norm
            if distance > worst:
                worst_index, worst = i, distance
        if worst <= tolerance:
            return [start, end]
        return rdp(subset[: worst_index + 1])[:-1] + rdp(subset[worst_index:])

    return rdp(points)


@router.get("/journeys/{journey_id}", response_model=JourneyDetailOut)
async def get_journey(journey_id: int, session: SessionDep):
    journey = await session.get(Journey, journey_id)
    if journey is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Journey not found")

    recordings = (
        (
            await session.execute(
                select(Recording)
                .options(selectinload(Recording.camera))
                .where(Recording.journey_id == journey_id)
                .order_by(Recording.started_at.asc())
            )
        )
        .scalars()
        .all()
    )

    fixes = (
        await session.execute(
            select(TelemetryPoint.lat, TelemetryPoint.lon)
            .where(
                TelemetryPoint.journey_id == journey_id,
                TelemetryPoint.has_fix.is_(True),
            )
            .order_by(TelemetryPoint.captured_at.asc(), TelemetryPoint.t_offset_s.asc())
        )
    ).all()

    tolerance = float(get_settings_service().get_nowait("maps.route_simplify_m"))
    route = _simplify([(lat, lon) for lat, lon in fixes if lat is not None], tolerance)

    # Validate against JourneyOut, not JourneyDetailOut. They differ by one field, and that
    # field is the whole problem: JourneyDetailOut declares `recordings`, so validating the
    # ORM object against it makes Pydantic read `journey.recordings` — a lazy relationship —
    # while extracting attributes. Under the async engine that lazy load has no greenlet to
    # run in and raises MissingGreenlet, so *every* journey detail request failed with a 400
    # and the Journeys page led nowhere. Assigning the recordings on the next line came too
    # late; the read had already happened.
    detail = JourneyDetailOut(
        **JourneyOut.model_validate(journey).model_dump(),
        recordings=[RecordingOut.model_validate(r) for r in recordings],
        route=route,
    )
    return detail


@router.post("/journeys/merge", response_model=JourneyOut)
async def merge_journeys(body: MergeRequest, session: SessionDep):
    merged = await JourneyBuilder().merge(session, body.journey_ids)
    if merged is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Need at least two valid journeys")
    return JourneyOut.model_validate(merged)


@router.post("/journeys/{journey_id}/split", response_model=list[JourneyOut])
async def split_journey(journey_id: int, body: SplitRequest, session: SessionDep):
    result = await JourneyBuilder().split(session, journey_id, body.at_recording_id)
    if result is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Cannot split there — the recording must be in this journey and not its first",
        )
    return [JourneyOut.model_validate(j) for j in result]


@router.post("/journeys/{journey_id}/reprocess")
async def reprocess_journey(journey_id: int, body: ReprocessRequest, session: SessionDep):
    recordings = (
        (await session.execute(select(Recording.id).where(Recording.journey_id == journey_id)))
        .scalars()
        .all()
    )
    for recording_id in recordings:
        await queue.enqueue(
            session,
            recording_id,
            kind=JobKind.REPROCESS,
            stages=body.stages,
            priority=60,
            force=True,
        )
    return {"queued": len(recordings)}


# --------------------------------------------------------------------------------------
# Plates
# --------------------------------------------------------------------------------------


@router.get("/plates", response_model=Paginated[PlateOut])
async def list_plates(
    session: SessionDep,
    page: PaginationDep,
    q: str | None = Query(None, description="Full or partial plate text"),
    flagged: bool | None = None,
    min_confidence: float | None = None,
    sort: str = Query("last_seen_desc"),
):
    stmt = select(Plate)
    if q:
        # Partial matching is the point: "ABC" must find "ABC123". Both the normalised
        # and the display text are searched, since a plate that failed normalisation is
        # still findable by what the OCR actually read.
        pattern = f"%{q.strip().upper().replace(' ', '')}%"
        stmt = stmt.where(
            or_(Plate.normalised_text.like(pattern), Plate.display_text.like(pattern))
        )
    if flagged is not None:
        stmt = stmt.where(Plate.flagged.is_(flagged))
    if min_confidence is not None:
        stmt = stmt.where(Plate.best_confidence >= min_confidence)

    order = {
        "last_seen_desc": Plate.last_seen_at.desc().nullslast(),
        "first_seen_desc": Plate.first_seen_at.desc().nullslast(),
        "observations_desc": Plate.observation_count.desc(),
        "confidence_desc": Plate.best_confidence.desc(),
        "alpha": Plate.normalised_text.asc(),
    }.get(sort, Plate.last_seen_at.desc().nullslast())

    return await _paginate(session, stmt.order_by(order), page, PlateOut)


async def _attach_representative(session, plate: Plate) -> PlateOut:
    """Pick the highest-confidence observation's crops to represent the plate."""
    best = (
        await session.execute(
            select(PlateObservation)
            .where(PlateObservation.plate_id == plate.id)
            .order_by(PlateObservation.ocr_confidence.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    out = PlateOut.model_validate(plate)
    if best is not None:
        out.representative_crop_path = best.plate_crop_path
        out.representative_vehicle_path = best.vehicle_crop_path
    return out


@router.get("/plates/{plate_id}", response_model=PlateOut)
async def get_plate(plate_id: int, session: SessionDep):
    plate = await session.get(Plate, plate_id)
    if plate is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Plate not found")
    return await _attach_representative(session, plate)


@router.get("/plates/{plate_id}/observations", response_model=Paginated[PlateObservationOut])
async def get_plate_observations(plate_id: int, session: SessionDep, page: PaginationDep):
    base = (
        select(PlateObservation, Recording.filename, Camera.name)
        .join(Recording, Recording.id == PlateObservation.recording_id)
        .outerjoin(Camera, Camera.id == PlateObservation.camera_id)
        .where(PlateObservation.plate_id == plate_id)
        .order_by(PlateObservation.captured_at.desc().nullslast())
    )
    total = int(
        (
            await session.execute(
                select(func.count(PlateObservation.id)).where(PlateObservation.plate_id == plate_id)
            )
        ).scalar()
        or 0
    )
    rows = (await session.execute(base.offset(page.offset).limit(page.page_size))).all()

    return Paginated[PlateObservationOut](
        items=[_observation_out(o, f, c) for o, f, c in rows],
        total=total,
        page=page.page,
        page_size=page.page_size,
        pages=page.pages(total),
    )


@router.patch("/plates/{plate_id}", response_model=PlateOut)
async def patch_plate(plate_id: int, body: PlatePatch, session: SessionDep):
    plate = await session.get(Plate, plate_id)
    if plate is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Plate not found")
    if body.flagged is not None:
        plate.flagged = body.flagged
    if body.notes is not None:
        plate.notes = body.notes
    await session.flush()
    return await _attach_representative(session, plate)


# --------------------------------------------------------------------------------------
# Vehicles
# --------------------------------------------------------------------------------------


@router.get("/vehicles", response_model=Paginated[VehicleOut])
async def list_vehicles(session: SessionDep, page: PaginationDep, class_label: str | None = None):
    stmt = select(Vehicle)
    if class_label:
        stmt = stmt.where(Vehicle.class_label == class_label)
    return await _paginate(
        session, stmt.order_by(Vehicle.last_seen_at.desc().nullslast()), page, VehicleOut
    )


@router.get("/vehicles/{vehicle_id}", response_model=VehicleOut)
async def get_vehicle(vehicle_id: int, session: SessionDep):
    vehicle = await session.get(Vehicle, vehicle_id)
    if vehicle is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Vehicle not found")
    return VehicleOut.model_validate(vehicle)


# --------------------------------------------------------------------------------------
# Jobs
# --------------------------------------------------------------------------------------


@router.get("/jobs", response_model=Paginated[JobOut])
async def list_jobs(session: SessionDep, page: PaginationDep, state: str | None = None):
    stmt = select(ProcessingJob, Recording.filename).outerjoin(
        Recording, Recording.id == ProcessingJob.recording_id
    )
    if state:
        stmt = stmt.where(ProcessingJob.state == state)
    stmt = stmt.order_by(ProcessingJob.queued_at.desc())

    count_stmt = select(func.count(ProcessingJob.id))
    if state:
        count_stmt = count_stmt.where(ProcessingJob.state == state)
    total = int((await session.execute(count_stmt)).scalar() or 0)

    rows = (await session.execute(stmt.offset(page.offset).limit(page.page_size))).all()
    items = []
    for job, filename in rows:
        out = JobOut.model_validate(job)
        out.recording_filename = filename
        items.append(out)

    return Paginated[JobOut](
        items=items,
        total=total,
        page=page.page,
        page_size=page.page_size,
        pages=page.pages(total),
    )


@router.get("/jobs/stats", response_model=QueueStatsOut)
async def job_stats(session: SessionDep):
    return QueueStatsOut(**await queue.stats(session))


@router.post("/jobs/pause", response_model=QueueStatsOut)
async def pause_queue(session: SessionDep):
    queue.pause()
    return QueueStatsOut(**await queue.stats(session))


@router.post("/jobs/resume", response_model=QueueStatsOut)
async def resume_queue(session: SessionDep):
    queue.resume()
    return QueueStatsOut(**await queue.stats(session))


@router.post("/jobs/retry-failed")
async def retry_failed_jobs(session: SessionDep):
    return {"retried": await queue.retry_failed(session)}


@router.post("/jobs/{job_id}/retry", response_model=JobOut)
async def retry_job(job_id: int, session: SessionDep):
    job = await session.get(ProcessingJob, job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found")
    job.state = "queued"
    job.attempts = 0
    job.not_before = None
    job.error_message = None
    await session.flush()
    return JobOut.model_validate(job)


# `response_model=None` is load-bearing, not decoration. FastAPI infers the response model
# from the return annotation, and a bare `-> None` yields `NoneType` -- a class object, so
# truthy -- which trips its "204 must not have a response body" assertion *at import time*
# and takes the whole app down before it serves anything. Returning an explicit Response
# and suppressing the inferred model keeps this working across FastAPI versions.
@router.delete(
    "/jobs/{job_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
async def cancel_job(job_id: int, session: SessionDep) -> Response:
    if not await queue.cancel(session, job_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found or already finished")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------------------


@router.get("/search", response_model=SearchResults)
async def search(session: SessionDep, q: str = Query(..., min_length=1)):
    term = q.strip()
    results = SearchResults()

    plate_pattern = f"%{term.upper().replace(' ', '')}%"
    results.plates = [
        PlateOut.model_validate(p)
        for p in (
            await session.execute(
                select(Plate)
                .where(
                    or_(
                        Plate.normalised_text.like(plate_pattern),
                        Plate.display_text.like(plate_pattern),
                    )
                )
                .order_by(Plate.last_seen_at.desc().nullslast())
                .limit(20)
            )
        ).scalars()
    ]

    results.recordings = [
        RecordingOut.model_validate(r)
        for r in (
            await session.execute(
                select(Recording)
                .options(selectinload(Recording.camera))
                .where(Recording.filename.ilike(f"%{term}%"))
                .order_by(Recording.started_at.desc().nullslast())
                .limit(20)
            )
        ).scalars()
    ]

    # A bare date is the most natural way to look for a drive, so try to read the term as
    # one before falling back to nothing.
    journey_stmt = select(Journey).order_by(Journey.started_at.desc()).limit(20)
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y%m%d"):
        try:
            day = datetime.strptime(term, fmt)
        except ValueError:
            continue
        journey_stmt = (
            select(Journey)
            .where(func.date(Journey.started_at) == day.date())
            .order_by(Journey.started_at.desc())
            .limit(20)
        )
        break
    else:
        journey_stmt = journey_stmt.where(Journey.title.ilike(f"%{term}%"))

    results.journeys = [
        JourneyOut.model_validate(j) for j in (await session.execute(journey_stmt)).scalars()
    ]
    return results
