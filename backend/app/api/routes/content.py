"""Recordings, journeys, plates, vehicles, jobs and search."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, Query, Response, status
from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import aliased, selectinload

from app.api.deps import PaginationDep, RowId, RowIdFilter, SessionDep
from app.api.geometry import build_polyline_indices
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
from app.core.settings_service import get_settings_service, local_zone
from app.db.models import (
    Camera,
    CameraRole,
    JobKind,
    JobState,
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
from app.journeys.track import single_track
from app.workers import queue

log = get_logger(__name__)

router = APIRouter(prefix="/api", tags=["content"])


def _is_date_only(value: datetime) -> bool:
    return (value.hour, value.minute, value.second, value.microsecond) == (0, 0, 0, 0)


def _day_start(value: datetime) -> datetime:
    """Interpret a bare date as local midnight, in UTC.

    ``started_at`` is stored in UTC while the picker speaks the user's local time, and for
    Adelaide those differ by nine and a half hours. Comparing the raw value against UTC
    would slide every boundary by that much, so a date is anchored in the configured zone
    before being converted.

    The conversion is guarded because it is arithmetic on a calendar with ends. Stamping
    ``0001-01-01`` with a +9:30 zone and converting to UTC lands before ``datetime.min``
    and raises ``OverflowError`` -- which is an ``ArithmeticError``, so it slipped past the
    ``ValueError`` handler and returned 500. ``date_from=0001-01-01`` is not an attack; it
    is what a date picker sends when someone holds the up arrow.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=local_zone() if _is_date_only(value) else UTC)
    try:
        return value.astimezone(UTC)
    except (OverflowError, OSError, ValueError):
        raise ValueError(
            f"{value.date().isoformat()} is outside the range of dates this "
            "application can represent."
        ) from None


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
        try:
            return column < _day_start(value) + timedelta(days=1)
        except OverflowError:
            # The other end of the calendar: 9999-12-31 has no day after it.
            raise ValueError(
                f"{value.date().isoformat()} is outside the range of dates this "
                "application can represent."
            ) from None
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
    camera_id: RowIdFilter = None,
    journey_id: RowIdFilter = None,
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
async def get_recording(recording_id: RowId, session: SessionDep):
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
async def get_telemetry(recording_id: RowId, session: SessionDep):
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
async def get_detections(recording_id: RowId, session: SessionDep):
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
async def get_recording_plates(recording_id: RowId, session: SessionDep):
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
async def reprocess_recording(recording_id: RowId, body: ReprocessRequest, session: SessionDep):
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


@router.get("/journeys/{journey_id}", response_model=JourneyDetailOut)
async def get_journey(journey_id: RowId, session: SessionDep):
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

    # The drive's path, assembled exactly the way the journey's own distance and start/end
    # markers are — one position per second, in time order, from whichever camera saw it.
    #
    # It used to be its own third answer: front camera only, ordered by clip. That drew
    # nothing at all for a stretch the front camera could not read, while the distance
    # counted both cameras and the markers came from a `captured_at` ordering, so the line,
    # the pins and the number beside them were each describing a different set of rows.
    # Sharing `single_track` is what stops them disagreeing.
    fixes = (
        await session.execute(
            select(
                TelemetryPoint.lat,
                TelemetryPoint.lon,
                TelemetryPoint.speed_kmh,
                TelemetryPoint.captured_at,
                TelemetryPoint.recording_id,
                TelemetryPoint.t_offset_s,
            ).where(
                TelemetryPoint.journey_id == journey_id,
                TelemetryPoint.has_fix.is_(True),
                TelemetryPoint.lat.is_not(None),
                TelemetryPoint.lon.is_not(None),
            )
        )
    ).all()

    starts = {r.id: r.started_at for r in recordings}
    front_ids = {
        r.id for r in recordings if r.camera is not None and r.camera.role is CameraRole.FRONT
    }
    path = single_track(fixes, starts, front_ids)

    tolerance = float(get_settings_service().get_nowait("maps.route_simplify_m"))
    samples = [(float(p.lat), float(p.lon), p.at.timestamp()) for p in path]
    # Segments, not one line: the shared helper breaks the route wherever joining two
    # fixes would draw a road that was never taken. /api/map/routes has always done this;
    # the journey view had its own copy of a simplifier and none of the splitting, so the
    # same drive was drawn differently depending on which page asked for it.
    route = [
        [
            (
                round(path[i].lat, 6),
                round(path[i].lon, 6),
                path[i].recording_id,
                round(path[i].t_offset_s, 2),
            )
            for i in run
        ]
        for run in build_polyline_indices(samples, tolerance_m=tolerance)
    ]

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
async def split_journey(journey_id: RowId, body: SplitRequest, session: SessionDep):
    result = await JourneyBuilder().split(session, journey_id, body.at_recording_id)
    if result is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Cannot split there — the recording must be in this journey and not its first",
        )
    return [JourneyOut.model_validate(j) for j in result]


@router.post("/journeys/{journey_id}/reprocess")
async def reprocess_journey(journey_id: RowId, body: ReprocessRequest, session: SessionDep):
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

    page_out = await _paginate(session, stmt.order_by(order), page, PlateOut)
    # The crops live on the observations, not on the plate, so the generic paginator
    # cannot know about them: it validates the ORM row and stops. Without this the Plates
    # grid showed "no vehicle image" under every card while the images sat on disk and
    # served perfectly — the detail route filled them in, the list route never did, and
    # the list is the page with the pictures on it.
    page_out.items = await _with_representative(session, page_out.items)
    return page_out


async def _best_observations(session, plate_ids: list[int]) -> dict[int, PlateObservation]:
    """The highest-confidence observation for each plate, in one query.

    A window function rather than a query per plate. A page holds two dozen cards and this
    runs on every load, so the obvious loop is two dozen round trips for something the
    database can rank in a single pass.
    """
    if not plate_ids:
        return {}

    ranked = (
        select(
            PlateObservation,
            func.row_number()
            .over(
                partition_by=PlateObservation.plate_id,
                order_by=PlateObservation.ocr_confidence.desc(),
            )
            .label("rank"),
        )
        .where(PlateObservation.plate_id.in_(plate_ids))
        .subquery()
    )
    observations = (
        (await session.execute(select(aliased(PlateObservation, ranked)).where(ranked.c.rank == 1)))
        .scalars()
        .all()
    )
    return {obs.plate_id: obs for obs in observations}


async def _with_representative(session, items: list[PlateOut]) -> list[PlateOut]:
    """Attach each plate's representative crops to an already-built page of results."""
    best = await _best_observations(session, [item.id for item in items])
    for item in items:
        observation = best.get(item.id)
        if observation is not None:
            item.representative_crop_path = observation.plate_crop_path
            item.representative_vehicle_path = observation.vehicle_crop_path
    return items


async def _attach_representative(session, plate: Plate) -> PlateOut:
    """Pick the highest-confidence observation's crops to represent one plate."""
    return (await _with_representative(session, [PlateOut.model_validate(plate)]))[0]


@router.get("/plates/{plate_id}", response_model=PlateOut)
async def get_plate(plate_id: RowId, session: SessionDep):
    plate = await session.get(Plate, plate_id)
    if plate is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Plate not found")
    return await _attach_representative(session, plate)


@router.get("/plates/{plate_id}/observations", response_model=Paginated[PlateObservationOut])
async def get_plate_observations(plate_id: RowId, session: SessionDep, page: PaginationDep):
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
async def patch_plate(plate_id: RowId, body: PlatePatch, session: SessionDep):
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


#: Classes shown on the Vehicles page by default. Tracking follows people and bicycles too,
#: and they are legitimate detections, but a page called Vehicles should not open on a list
#: of pedestrians.
_VEHICLE_CLASSES = ("car", "truck", "bus", "motorcycle")


def _sighting_out(track: TrackedObject, plate: Plate | None) -> VehicleOut:
    """One tracked object rendered as a vehicle sighting."""
    return VehicleOut(
        id=track.id,
        class_label=track.class_label,
        primary_plate=PlateOut.model_validate(plate) if plate is not None else None,
        # make/model/colour stay null: nothing in this pipeline classifies them, and
        # inventing them would be worse than leaving the fields empty.
        recording_id=track.recording_id,
        first_seen_offset_s=track.first_seen_offset_s,
        lat=track.lat,
        lon=track.lon,
        first_seen_at=track.first_seen_at,
        last_seen_at=track.last_seen_at,
        observation_count=track.frame_count,
        representative_crop_path=track.crop_path,
    )


async def _plates_for_tracks(session, track_ids: list[int]) -> dict[int, Plate]:
    """The plate read on each track, where one was."""
    if not track_ids:
        return {}
    rows = (
        await session.execute(
            select(PlateObservation.tracked_object_id, Plate)
            .join(Plate, Plate.id == PlateObservation.plate_id)
            .where(PlateObservation.tracked_object_id.in_(track_ids))
            .order_by(PlateObservation.ocr_confidence.desc())
        )
    ).all()
    # Highest confidence first, so the first row seen for a track is the one to keep.
    found: dict[int, Plate] = {}
    for track_id, plate in rows:
        found.setdefault(track_id, plate)
    return found


@router.get("/vehicles", response_model=Paginated[VehicleOut])
async def list_vehicles(
    session: SessionDep,
    page: PaginationDep,
    class_label: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    has_plate: bool | None = None,
):
    """Vehicles actually seen, which live in ``tracked_objects``.

    This used to select from ``vehicles``, a table for *identity across sightings* that
    nothing in the pipeline has ever written a row to — re-identifying a car between drives
    is not implemented, and faking it would be worse than leaving it empty. So the page was
    structurally empty: it queried a table with no writer while 1,071 real sightings sat
    one table over, and the empty state told the user detection had not run.

    The table stays, because it is where re-identification would go. The page shows what
    exists today: one row per physical vehicle followed across a recording, which is what
    the UI already described itself as showing.
    """
    stmt = select(TrackedObject).where(TrackedObject.crop_path.is_not(None))
    stmt = stmt.where(
        TrackedObject.class_label == class_label
        if class_label
        else TrackedObject.class_label.in_(_VEHICLE_CLASSES)
    )
    # Filters, because ten thousand sightings at 24 to a page is 420 pages of Next.
    if date_from:
        stmt = stmt.where(TrackedObject.first_seen_at >= _day_start(date_from))
    if date_to:
        stmt = stmt.where(_before_end_of(TrackedObject.first_seen_at, date_to))
    if has_plate is not None:
        read = (
            select(PlateObservation.id)
            .where(PlateObservation.tracked_object_id == TrackedObject.id)
            .exists()
        )
        stmt = stmt.where(read if has_plate else ~read)

    total = int(
        (
            await session.execute(select(func.count()).select_from(stmt.order_by(None).subquery()))
        ).scalar()
        or 0
    )
    tracks = list(
        (
            await session.execute(
                stmt.order_by(
                    TrackedObject.first_seen_at.desc().nullslast(), TrackedObject.id.desc()
                )
                .offset(page.offset)
                .limit(page.page_size)
            )
        )
        .scalars()
        .all()
    )
    plates = await _plates_for_tracks(session, [t.id for t in tracks])
    return Paginated[VehicleOut](
        items=[_sighting_out(t, plates.get(t.id)) for t in tracks],
        total=total,
        page=page.page,
        page_size=page.page_size,
        pages=page.pages(total),
    )


@router.get("/vehicles/{vehicle_id}", response_model=VehicleOut)
async def get_vehicle(vehicle_id: RowId, session: SessionDep):
    """One sighting. Ids here are ``tracked_objects`` ids, matching the list above."""
    track = await session.get(TrackedObject, vehicle_id)
    if track is not None:
        plates = await _plates_for_tracks(session, [track.id])
        return _sighting_out(track, plates.get(track.id))

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

    # Newest-queued-first put the running job at the bottom of a 2,000-row list.
    #
    # The queue claims work oldest-first, so ordering the list newest-first placed the job
    # actually running as far from page one as it is possible to be -- structurally, not by
    # coincidence. With a backlog of any size the Queue page showed fifty identical jobs
    # that had never started, the "Currently processing" panel rendered nothing, and the
    # Running tile directly above it said 2.
    recency = func.coalesce(
        ProcessingJob.finished_at, ProcessingJob.started_at, ProcessingJob.queued_at
    )
    state_rank = case(
        (ProcessingJob.state == JobState.RUNNING, 0),
        (ProcessingJob.state == JobState.FAILED, 1),
        (ProcessingJob.state == JobState.QUEUED, 3),
        else_=2,
    )
    stmt = stmt.order_by(
        state_rank,
        # Pending work reads in the order it will be claimed, so the top of that section
        # is what runs next; everything else reads newest first.
        case((ProcessingJob.state == JobState.QUEUED, ProcessingJob.priority), else_=None).asc(),
        case((ProcessingJob.state == JobState.QUEUED, ProcessingJob.queued_at), else_=None).asc(),
        recency.desc(),
        ProcessingJob.id.desc(),
    )

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
async def retry_job(job_id: RowId, session: SessionDep):
    job = await session.get(ProcessingJob, job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found")
    job.state = "queued"
    job.attempts = 0
    job.not_before = None
    job.error_message = None
    job.worker_id = None
    # A clean slate includes the free-retry budget the queue grants for database
    # contention. Leaving it behind meant a job that had once been unlucky started its
    # manual retry with fewer of them than a job that had not.
    job.result = None
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
async def cancel_job(job_id: RowId, session: SessionDep) -> Response:
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
