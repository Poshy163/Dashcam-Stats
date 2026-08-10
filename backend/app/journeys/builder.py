"""Grouping recordings into journeys.

The primary signal is the gap between the end of one recording and the start of the next.
On the real corpus a five-minute threshold turns 354 front-camera segments into about 45
journeys across twelve days, which matches the actual driving pattern.

Two details matter more than they look:

* Front and rear recordings of the same drive must land in the *same* journey. Their
  filenames differ by a few seconds and many front segments have no rear counterpart, so
  matching on filename equality would spawn a phantom journey for every rear file.
* Average speed is computed over moving samples only. Averaging every 1 Hz sample would
  report a handful of km/h for any drive that met a red light.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.settings_service import get_settings_service
from app.db.models import (
    Journey,
    PlateObservation,
    Recording,
    TelemetryPoint,
    TrackedObject,
)
from app.db.retry import commit_with_retry
from app.osd import haversine_m
from app.osd.outliers import (
    looks_like_sign_loss,
    plausible_radius_m,
    robust_centre,
    spatial_outliers,
)

log = get_logger(__name__)

#: Ceiling on a single leg of a journey's distance measurement.
#:
#: Independent of the outlier pass, which needs three points and a majority before it
#: will act and so cannot judge a short journey. 50 km between two consecutive 1 Hz
#: samples is not a leg, it is a misread digit, and admitting one put 154,701 km on an
#: eighteen-minute drive.
_MAX_LEG_M = 50_000.0

#: Metres a vehicle could cover in a second. Mirrors the per-point ceiling in
#: :mod:`app.osd.engine` so the two cannot disagree about what counts as travel.
_MAX_STEP_M_PER_S = 400.0 * 1000.0 / 3600.0

#: Average speed over a whole journey beyond which its distance is not believable. Well
#: above any real drive -- the fastest genuine journey in this corpus averages about 60 --
#: so it only ever catches a distance that arithmetic already rules out.
_IMPLAUSIBLE_M_PER_S = 200.0 * 1000.0 / 3600.0


def as_utc(value: datetime | None) -> datetime | None:
    """Force a datetime to timezone-aware UTC.

    SQLite has no timezone-aware column type, so a value written as aware comes back
    **naive** while one just computed in Python is aware. Comparing the two raises
    ``TypeError``, and since journey clustering compares stored timestamps against freshly
    derived ones constantly, every comparison goes through here first.
    """
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


@dataclass(slots=True)
class _Cluster:
    recordings: list[Recording]

    @property
    def started_at(self) -> datetime:
        return min(as_utc(r.started_at) for r in self.recordings if r.started_at)

    @property
    def ended_at(self) -> datetime:
        return max(as_utc(r.ended_at or r.started_at) for r in self.recordings if r.started_at)


class JourneyBuilder:
    """Builds and refreshes journeys from the recording index."""

    async def rebuild(self, session: AsyncSession, *, since: datetime | None = None) -> int:
        """Recluster recordings into journeys. Returns the number of journeys touched."""
        settings = get_settings_service()
        if not bool(settings.get_nowait("journeys.enabled")):
            return 0

        gap = timedelta(minutes=float(settings.get_nowait("journeys.gap_minutes")))
        min_recordings = int(settings.get_nowait("journeys.min_recordings"))
        use_gps = bool(settings.get_nowait("journeys.use_gps_continuity"))
        max_jump_m = float(settings.get_nowait("journeys.max_jump_km")) * 1000.0

        stmt = (
            select(Recording)
            .where(Recording.started_at.isnot(None), Recording.ignored.is_(False))
            .order_by(Recording.started_at.asc(), Recording.id.asc())
        )
        if since is not None:
            stmt = stmt.where(Recording.started_at >= since)

        recordings = list((await session.execute(stmt)).scalars())
        if not recordings:
            return 0

        # Journeys a user has merged or split by hand are authoritative; leave their
        # recordings exactly where they are.
        manual_ids = set(
            (await session.execute(select(Journey.id).where(Journey.manual.is_(True)))).scalars()
        )
        movable = [r for r in recordings if r.journey_id not in manual_ids]

        # Derive, then decide. Clustering reads `start_lat`/`start_lon`, and `refresh`
        # rewrites them from the surviving telemetry -- so clustering on the stored values
        # and correcting them afterwards means the next pass clusters differently and the
        # rebuild has to run again to settle. Recomputing first collapses that to one pass
        # and, more importantly, makes the result a function of the telemetry rather than
        # of whatever the last run happened to leave behind.
        await self._refresh_start_positions(session, movable)

        clusters = self._cluster(movable, gap, use_gps, max_jump_m)

        # Automatic journeys are rebuilt from scratch; the alternative is trying to
        # reconcile old and new boundaries, which produces stale half-journeys.
        stale_ids = {r.journey_id for r in movable if r.journey_id is not None} - manual_ids
        if stale_ids:
            await session.execute(
                delete(Journey).where(Journey.id.in_(stale_ids), Journey.manual.is_(False))
            )
            # Deliberately no ``expire_all()`` here. The DELETE has cascaded through
            # ``ondelete="SET NULL"`` and the in-memory ``journey_id`` values are now
            # stale, but expiring them makes the very next read of ``started_at`` a lazy
            # load, and the clusters are read after this point — under the async engine
            # that raises MissingGreenlet. Nothing reassigns those attributes in Python
            # any more, so leaving them stale is harmless: :meth:`_attach` writes through
            # a statement that owes nothing to what the ORM believes.

        created = 0
        for cluster in clusters:
            if len(cluster.recordings) < min_recordings:
                await self._attach(session, [r.id for r in cluster.recordings], None)
                continue
            journey = Journey(started_at=cluster.started_at, ended_at=cluster.ended_at)
            session.add(journey)
            await session.flush()
            await self._attach(session, [r.id for r in cluster.recordings], journey.id)
            await self.refresh(session, journey)
            created += 1
            # One journey, one transaction. Forty-five journeys' worth of refreshing in a
            # single transaction is minutes of held write lock, and everything else that
            # writes -- both workers, the scheduler, the log sink -- waits behind it and
            # then fails on its busy timeout. Committing per journey also means a rebuild
            # interrupted halfway leaves the journeys it finished intact rather than
            # discarding all of them.
            await commit_with_retry(session, what="rebuild journey")

        await session.flush()
        removed = await self._drop_empty(session)
        log.info("rebuilt journeys", journeys=created, recordings=len(movable), removed=removed)
        return created

    @staticmethod
    async def _refresh_start_positions(session: AsyncSession, recordings: list[Recording]) -> int:
        """Re-derive each recording's start position from its earliest surviving fix.

        ``recordings.start_lat`` is written once by the telemetry stage and never revisited,
        so a first fix that was a misread stays frozen there -- and clustering reads it. On
        the live library a rear segment whose latitude had lost its minus sign sat 7,700 km
        from the front segment recorded at the same instant, so the two were never put in
        the same journey: about 44 real drives became 85, 31 of them holding one recording.

        A window function rather than SQLite's bare-column ``MIN()`` trick, which would pick
        the right row here and silently pick an arbitrary one on any other database.
        """
        if not recordings:
            return 0
        ids = [r.id for r in recordings]

        ranked = (
            select(
                TelemetryPoint.recording_id,
                TelemetryPoint.lat,
                TelemetryPoint.lon,
                func.row_number()
                .over(
                    partition_by=TelemetryPoint.recording_id,
                    order_by=TelemetryPoint.t_offset_s.asc(),
                )
                .label("rank"),
            )
            .where(
                TelemetryPoint.recording_id.in_(ids),
                TelemetryPoint.has_fix.is_(True),
                TelemetryPoint.lat.is_not(None),
                TelemetryPoint.lon.is_not(None),
            )
            .subquery()
        )
        first = {
            row.recording_id: (row.lat, row.lon)
            for row in (await session.execute(select(ranked).where(ranked.c.rank == 1))).all()
        }

        changed = 0
        for recording in recordings:
            lat, lon = first.get(recording.id, (None, None))
            if (recording.start_lat, recording.start_lon) != (lat, lon):
                recording.start_lat, recording.start_lon = lat, lon
                changed += 1
        if changed:
            await session.flush()
            log.info("re-derived recording start positions", corrected=changed, of=len(recordings))
        return changed

    @staticmethod
    async def _attach(
        session: AsyncSession, recording_ids: list[int], journey_id: int | None
    ) -> None:
        """Point recordings at a journey with a statement, never by ORM assignment.

        ``recording.journey_id = journey.id`` looks equivalent and destroyed the entire
        journey index on a real library. The sequence: journeys are deleted, the foreign
        key's ``ON DELETE SET NULL`` nulls every ``recordings.journey_id`` in the database,
        a replacement journey is inserted and **SQLite reissues the row id that was just
        freed** — so the new journey is id 1 exactly as the old one was. The assignment
        then writes 1 over an in-memory 1, the unit of work sees no change, no UPDATE is
        emitted, and the database keeps its NULL. ``refresh`` finds a journey with no
        recordings, ``_drop_empty`` deletes it as an empty leftover, and the whole thing
        logs "rebuilt journeys" and returns a success count.

        The result was that one press of "Scan now" emptied all 45 journeys, and every
        signal said it had worked.

        An explicit UPDATE cannot be optimised away by change detection, because there is
        no in-memory state for it to compare against. The denormalised copies of the id go
        with it: they are what the per-journey map and heat map read, and leaving them
        behind outlives the rebuild that orphaned them.
        """
        if not recording_ids:
            return
        for model in (Recording, TelemetryPoint, TrackedObject, PlateObservation):
            column = Recording.id if model is Recording else model.recording_id
            await session.execute(
                update(model)
                .where(column.in_(recording_ids))
                .values(journey_id=journey_id)
                .execution_options(synchronize_session=False)
            )

    async def repair_stale(self, session: AsyncSession, *, limit: int = 200) -> int:
        """Recompute journeys whose rollups are unset but whose telemetry says otherwise.

        Rollups are derived, so anything that invalidates them -- a migration correcting
        stored positions, a decoder fix, a journey attached before its telemetry existed --
        leaves them stale until that journey happens to be refreshed again. Nothing
        guaranteed that: a rebuild only touches journeys it reclusters, and `refresh` runs
        for one journey at a time as recordings finish.

        The result was a library where five journeys had a distance and a route and a
        hundred and twenty-three had neither, while their telemetry held thousands of
        perfectly good fixes.

        The test is deliberately "claims GPS but has no distance", which is a state no
        correctly refreshed journey can be in: `refresh` sets both from the same rows, so
        either there are fixes and both are populated, or there are none and neither is.
        """
        # Two shapes of wrong, both self-evident from the row alone.
        never_computed = and_(Journey.has_gps.is_(True), Journey.distance_m.is_(None))
        # A distance no vehicle could cover in the time the journey lasted. Recomputing is
        # what applies the current guards to a number produced by an older, looser one --
        # without this, a journey that already holds a bad distance keeps it forever,
        # because nothing would ever ask it to try again.
        impossible = and_(
            Journey.distance_m.is_not(None),
            Journey.duration_s > 0,
            Journey.distance_m / Journey.duration_s > _IMPLAUSIBLE_M_PER_S,
        )

        stale = list(
            (
                await session.execute(
                    select(Journey)
                    .where(or_(never_computed, impossible))
                    .order_by(Journey.started_at.desc())
                    .limit(limit)
                )
            ).scalars()
        )
        for journey in stale:
            await self.refresh(session, journey)
        if stale:
            log.info("recomputed journeys with stale rollups", journeys=len(stale))
        return len(stale)

    async def needs_recluster(self, session: AsyncSession) -> bool:
        """Would a rebuild actually group these recordings differently?

        Journeys are built two ways. A rebuild clusters the whole library at once and gets
        the boundaries right; ``assign_recording`` attaches one recording as it finishes and
        creates a journey when it finds no neighbour to join. The queue does not process
        recordings in chronological order, so a segment routinely finishes before the ones
        either side of it and starts a journey of its own -- and once the import is done the
        scanner reports no new files, the rebuild never runs, and those fragments are
        permanent.

        This asks the question by *doing* the clustering and comparing it against what is
        stored, which is the only formulation that cannot disagree with the rebuild it
        triggers.

        The previous one did disagree, permanently. It looked for two automatic journeys
        closer together in time than the gap, and reasoned that a rebuild would have joined
        them -- but ``_cluster`` splits on GPS continuity as well as on time, so a pair the
        rebuild had deliberately separated over a 7,700 km jump looked, to a time-only test,
        exactly like a pair that should be joined. The result was a rebuild on *every*
        scan, for ever: 12,626 log entries on the live library, each one deleting and
        recreating all 85 journeys and holding SQLite's write lock while it did. That load
        is where the workers' "database is locked" failures came from, so this loop was not
        merely wasteful -- it was the thing breaking recordings.

        Comparing the grouping itself is immune to that by construction: whatever
        ``_cluster`` decides, and for whatever reason, the stored assignment matches it
        immediately after a rebuild.
        """
        settings = get_settings_service()
        gap = timedelta(minutes=float(settings.get_nowait("journeys.gap_minutes")))
        use_gps = bool(settings.get_nowait("journeys.use_gps_continuity"))
        max_jump_m = float(settings.get_nowait("journeys.max_jump_km")) * 1000.0
        min_recordings = int(settings.get_nowait("journeys.min_recordings"))

        manual_ids = set(
            (await session.execute(select(Journey.id).where(Journey.manual.is_(True)))).scalars()
        )
        recordings = list(
            (
                await session.execute(
                    select(Recording)
                    .where(Recording.started_at.isnot(None), Recording.ignored.is_(False))
                    .order_by(Recording.started_at.asc(), Recording.id.asc())
                )
            ).scalars()
        )
        movable = [r for r in recordings if r.journey_id not in manual_ids]
        if not movable:
            return False

        claimed: set[int] = set()
        for cluster in self._cluster(movable, gap, use_gps, max_jump_m):
            ids = {r.journey_id for r in cluster.recordings}
            if len(cluster.recordings) < min_recordings:
                # These would be detached; they are stale unless they already are.
                if ids != {None}:
                    return True
                continue
            # One cluster is one journey. Several ids means the drive is currently split
            # across journeys; an id already claimed by another cluster means two drives
            # are currently sharing one.
            if len(ids) != 1 or None in ids:
                return True
            journey_id = ids.pop()
            if journey_id in claimed:
                return True
            claimed.add(journey_id)
        return False

    @staticmethod
    async def _drop_empty(session: AsyncSession) -> int:
        """Delete journeys that no longer hold any recordings.

        A journey can be emptied from either side: ``assign_recording`` creates one for a
        single new segment, and a later rebuild reclusters that segment into a different
        journey. Without this sweep both survive and the journeys list fills with
        zero-recording entries.
        """
        orphan_ids = list(
            (
                await session.execute(
                    select(Journey.id)
                    .outerjoin(Recording, Recording.journey_id == Journey.id)
                    .group_by(Journey.id)
                    .having(func.count(Recording.id) == 0)
                )
            ).scalars()
        )
        if orphan_ids:
            await session.execute(delete(Journey).where(Journey.id.in_(orphan_ids)))
        return len(orphan_ids)

    def _cluster(
        self,
        recordings: list[Recording],
        gap: timedelta,
        use_gps: bool,
        max_jump_m: float,
    ) -> list[_Cluster]:
        clusters: list[_Cluster] = []
        current: list[Recording] = []
        previous: Recording | None = None

        for recording in recordings:
            if previous is None:
                current = [recording]
                previous = recording
                continue

            if self._same_journey(previous, recording, gap, use_gps, max_jump_m):
                current.append(recording)
            else:
                clusters.append(_Cluster(current))
                current = [recording]
            # Advance against whichever recording extends furthest in time, so a rear
            # segment starting a second earlier than its front pair does not reset the
            # comparison point backwards.
            if as_utc(recording.ended_at or recording.started_at) >= as_utc(
                previous.ended_at or previous.started_at
            ):
                previous = recording

        if current:
            clusters.append(_Cluster(current))
        return clusters

    @staticmethod
    def _same_journey(
        previous: Recording,
        candidate: Recording,
        gap: timedelta,
        use_gps: bool,
        max_jump_m: float,
    ) -> bool:
        prev_end = as_utc(previous.ended_at or previous.started_at)
        candidate_start = as_utc(candidate.started_at)
        if prev_end is None or candidate_start is None:
            return False

        if candidate_start - prev_end > gap:
            return False

        if use_gps and max_jump_m > 0:
            # Both endpoints must have a real fix; a missing fix is not evidence of a jump.
            if (
                previous.start_lat is not None
                and candidate.start_lat is not None
                and previous.start_lon is not None
                and candidate.start_lon is not None
            ):
                jump = haversine_m(
                    previous.start_lat,
                    previous.start_lon,
                    candidate.start_lat,
                    candidate.start_lon,
                )
                if jump > max_jump_m:
                    return False
        return True

    # -- rollups -----------------------------------------------------------------------

    async def refresh(self, session: AsyncSession, journey: Journey) -> Journey:
        """Recompute a journey's aggregates from its recordings and telemetry.

        Read everything, decide everything, *then* write. The order is load-bearing rather
        than tidy. Under SQLite a transaction holds the write lock from its first write
        until it ends, and this method used to open one with ``_attach`` and then spend the
        rest of its time reading -- every telemetry point of the journey, twice, plus a
        streamed pass for distance. On a library where a rebuild refreshes forty-five
        journeys inside one transaction that is minutes of held lock, and the workers'
        first write of a stage (``DELETE FROM telemetry_points``, ``DELETE FROM
        tracked_objects``) blew straight past its thirty second ``busy_timeout`` and failed
        the whole recording. Those two statements are exactly the ones that showed up in
        the failure log.

        One read of the journey's telemetry now serves the outlier pass, the bounds, the
        distance and the endpoints -- which is fewer queries than the four separate passes
        it replaces, as well as a far shorter write.
        """
        settings = get_settings_service()
        min_move_m = float(settings.get_nowait("telemetry.min_move_metres"))

        recordings = list(
            (
                await session.execute(
                    select(Recording)
                    .where(Recording.journey_id == journey.id)
                    .order_by(Recording.started_at.asc())
                )
            ).scalars()
        )
        journey.recording_count = len(recordings)
        if not recordings:
            return journey

        starts = [as_utc(r.started_at) for r in recordings if r.started_at]
        ends = [as_utc(r.ended_at or r.started_at) for r in recordings if r.started_at]
        if starts:
            journey.started_at = min(starts)
        if ends:
            journey.ended_at = max(ends)
        journey.duration_s = max(
            0.0,
            (as_utc(journey.ended_at) - as_utc(journey.started_at)).total_seconds(),
        )

        recording_ids = [r.id for r in recordings]

        # -- reads -----------------------------------------------------------------------
        points = list(
            (
                await session.execute(
                    select(
                        TelemetryPoint.id,
                        TelemetryPoint.recording_id,
                        TelemetryPoint.lat,
                        TelemetryPoint.lon,
                        TelemetryPoint.speed_kmh,
                        TelemetryPoint.captured_at,
                        TelemetryPoint.t_offset_s,
                    )
                    .where(
                        TelemetryPoint.recording_id.in_(recording_ids),
                        TelemetryPoint.has_fix.is_(True),
                    )
                    .order_by(
                        TelemetryPoint.captured_at.asc().nullslast(),
                        TelemetryPoint.recording_id.asc(),
                        TelemetryPoint.t_offset_s.asc(),
                    )
                )
            ).all()
        )

        # Which of those coordinates cannot belong to this drive at all. Computed here,
        # applied in the write phase below.
        rejected_ids, centre, radius_m = self._find_outliers(points, journey.duration_s)
        good = [p for p in points if p.id not in rejected_ids]

        # Positions copied onto sightings when they were processed are stale the moment a
        # telemetry point behind one is rejected -- and nothing used to go back for them,
        # which is why a coordinate cleared out of `telemetry_points` went on being drawn
        # on the map as a detection long afterwards.
        stale_tracks, stale_observations = await self._find_stale_sighting_positions(
            session, recording_ids, centre, radius_m
        )

        counts = (
            await session.execute(
                select(
                    select(func.count(TrackedObject.id))
                    .where(TrackedObject.recording_id.in_(recording_ids))
                    .scalar_subquery(),
                    select(func.count(func.distinct(PlateObservation.plate_id)))
                    .where(PlateObservation.recording_id.in_(recording_ids))
                    .scalar_subquery(),
                )
            )
        ).one()

        # -- derivations, in memory ------------------------------------------------------
        located = [p for p in good if p.lat is not None and p.lon is not None]
        speeds = [float(p.speed_kmh) for p in good if p.speed_kmh is not None]

        journey.has_gps = bool(located)
        journey.min_lat = min((p.lat for p in located), default=None)
        journey.max_lat = max((p.lat for p in located), default=None)
        journey.min_lon = min((p.lon for p in located), default=None)
        journey.max_lon = max((p.lon for p in located), default=None)
        journey.max_speed_kmh = max(speeds, default=None)

        if located:
            journey.start_lat, journey.start_lon = located[0].lat, located[0].lon
            journey.end_lat, journey.end_lon = located[-1].lat, located[-1].lon

        # Each recording's own start position, re-derived from the fixes that survived.
        #
        # This is the third copy of a coordinate that nothing ever revisited, and it is the
        # most expensive of the three. `recordings.start_lat` is written once, in the
        # telemetry stage, from that recording's first fix -- and when the first fix is a
        # misread the value is frozen there forever, because cleaning `telemetry_points`
        # updates the journey's rollups and never the recording's.
        #
        # Clustering reads it. A rear segment whose first fix lost its minus sign sits
        # 7,700 km from the front segment recorded at the same instant, so `_same_journey`
        # refuses to put the pair together and the drive is split in half. On the live
        # library that turned about 44 real drives into 85 journeys, 31 of them holding a
        # single recording.
        # Earliest surviving fix *within each recording*, by its own offset -- not by the
        # journey-wide ordering above, which puts rows with no readable clock at the end.
        first_by_recording: dict[int, object] = {}
        for point in located:
            current = first_by_recording.get(point.recording_id)
            if current is None or (point.t_offset_s or 0.0) < (current.t_offset_s or 0.0):
                first_by_recording[point.recording_id] = point
        for recording in recordings:
            point = first_by_recording.get(recording.id)
            recording.start_lat = point.lat if point else None
            recording.start_lon = point.lon if point else None

        journey.distance_m, journey.avg_speed_kmh = self._measure(good, min_move_m)
        journey.vehicle_count = int(counts[0] or 0)
        journey.unique_plate_count = int(counts[1] or 0)

        # -- writes ----------------------------------------------------------------------
        if rejected_ids:
            await self._clear_positions(session, rejected_ids)
        if stale_tracks:
            await session.execute(
                update(TrackedObject)
                .where(TrackedObject.id.in_(stale_tracks))
                .values(lat=None, lon=None)
                .execution_options(synchronize_session=False)
            )
        if stale_observations:
            await session.execute(
                update(PlateObservation)
                .where(PlateObservation.id.in_(stale_observations))
                .values(lat=None, lon=None)
                .execution_options(synchronize_session=False)
            )
        if rejected_ids or stale_tracks or stale_observations:
            log.info(
                "discarded positions that cannot belong to this journey",
                journey_id=journey.id,
                telemetry=len(rejected_ids),
                of=len(points),
                sightings=len(stale_tracks),
                plate_observations=len(stale_observations),
                radius_km=round(radius_m / 1000.0, 1) if radius_m else None,
            )

        # Re-point the denormalised journey ids every time, not only when a recording is
        # first attached. The route map and the heat map's journey filter read
        # telemetry_points.journey_id, so a journey whose telemetry was written before it
        # existed -- or attached by a path that only set recordings.journey_id -- draws
        # nothing at all. Idempotent, four statements, and it makes refresh the one place
        # that puts a journey right.
        await self._attach(session, recording_ids, journey.id)

        await session.flush()
        return journey

    @staticmethod
    def _find_outliers(
        rows: list, span_s: float
    ) -> tuple[set[int], tuple[float, float] | None, float]:
        """Which stored positions cannot belong to this journey, and where its centre is.

        The journey is the smallest scope at which the worst failure mode is visible. When
        the rear camera loses the minus sign in front of a latitude it usually loses it for
        the whole clip, so every fix in that recording agrees with every other one at high
        confidence while sitting 7,700 km away. Nothing inside the recording contradicts it;
        only its neighbours in the drive do.

        Pure, and returns the centre and radius as well as the verdict, because the same
        two numbers judge the positions copied onto sightings — one definition of "could
        the vehicle have been here", applied to every table that stores a coordinate.
        """
        located = [r for r in rows if r.lat is not None and r.lon is not None]
        if len(located) < 3:
            return set(), None, plausible_radius_m(span_s)

        points = [(float(r.lat), float(r.lon)) for r in located]
        outliers = spatial_outliers(points, span_s=span_s)
        radius = plausible_radius_m(span_s)
        centre = robust_centre([p for i, p in enumerate(points) if i not in outliers])

        if outliers and centre is not None:
            sign_losses = sum(
                1 for i in outliers if looks_like_sign_loss(points[i], centre, radius)
            )
            if sign_losses:
                # Worth separating: a dropped minus sign points at the overlay region or
                # the glyph templates, where a mangled digit points at the weather.
                log.info("some rejected positions look like a lost minus sign", count=sign_losses)

        return {located[i].id for i in outliers}, centre, radius

    @staticmethod
    async def _clear_positions(session: AsyncSession, ids: set[int]) -> None:
        """Strip the coordinates off telemetry rows, keeping everything else.

        Rejected rows keep their timestamp, speed and raw text and lose only the position,
        because the rest of the reading was never in doubt — the speed on a sign-flipped
        line is perfectly good, and discarding it would trade one wrong number for a
        missing one.
        """
        await session.execute(
            update(TelemetryPoint)
            .where(TelemetryPoint.id.in_(ids))
            .values(has_fix=False, lat=None, lon=None, heading_deg=None)
            .execution_options(synchronize_session=False)
        )

    @staticmethod
    async def _find_stale_sighting_positions(
        session: AsyncSession,
        recording_ids: list[int],
        centre: tuple[float, float] | None,
        radius_m: float,
    ) -> tuple[list[int], list[int]]:
        """Sightings holding a coordinate this journey says the vehicle was never at.

        ``tracked_objects`` and ``plate_observations`` each keep their own copy of the
        position, stamped when the recording was processed. Cleaning ``telemetry_points``
        never touched them, so a coordinate that had been recognised as impossible and
        cleared at the source went on being drawn on the map as a detection — which is
        exactly how one sighting kept reporting a longitude eleven thousand kilometres from
        the drive it belonged to, long after the telemetry point behind it was gone.

        Judged by the same centre and radius as the telemetry, so the two cannot disagree.
        """
        if centre is None:
            return [], []

        stale: dict[str, list[int]] = {"tracks": [], "observations": []}
        for model, key in ((TrackedObject, "tracks"), (PlateObservation, "observations")):
            rows = (
                await session.execute(
                    select(model.id, model.lat, model.lon).where(
                        model.recording_id.in_(recording_ids),
                        model.lat.is_not(None),
                        model.lon.is_not(None),
                    )
                )
            ).all()
            stale[key] = [
                row.id
                for row in rows
                if haversine_m(centre[0], centre[1], float(row.lat), float(row.lon)) > radius_m
            ]
        return stale["tracks"], stale["observations"]

    @staticmethod
    def _measure(rows: list, min_move_m: float) -> tuple[float | None, float | None]:
        """Distance and moving average over already-cleaned fixes, in journey order.

        Takes the rows rather than fetching them: they have already been read once for the
        outlier pass, and reading them again inside the write transaction is what made this
        method the longest-held lock in the process.
        """
        distance = 0.0
        skipped = 0
        anchor: tuple[float, float] | None = None
        anchor_time: datetime | None = None
        moving_sum = 0.0
        moving_count = 0
        any_point = False

        for row in rows:
            lat, lon, speed, captured = row.lat, row.lon, row.speed_kmh, row.captured_at
            any_point = True
            if speed is not None and speed > 1.0:
                moving_sum += speed
                moving_count += 1
            if lat is None or lon is None:
                continue
            if anchor is None:
                anchor = (lat, lon)
                anchor_time = captured
                continue
            step = haversine_m(anchor[0], anchor[1], lat, lon)

            # A ceiling on a single leg, independent of the outlier pass above. That pass
            # needs three points and a majority to act, so a short journey can still carry
            # a coordinate it could not judge -- and without this, one such point put
            # 154,701 km on an eighteen-minute drive and 21 of 45 journeys reported a
            # distance no car could cover.
            #
            # The ceiling is how far the vehicle could have travelled in the time that
            # actually elapsed, not a flat distance. A flat one has to be set loose enough
            # for a legitimate leg across a long dropout, which leaves it far too loose for
            # two fixes a second apart: a journey of exactly two fixes, 36 km apart because
            # one longitude digit was misread, reported 36.7 km covered in 120 seconds and
            # slipped under a 50 km cap untouched.
            #
            # Skip the leg rather than the point: the anchor is the last position still
            # trusted, and moving it to a coordinate this one disagrees with would corrupt
            # every leg after it too.
            gap_s = None
            if captured is not None and anchor_time is not None:
                gap_s = abs((as_utc(captured) - as_utc(anchor_time)).total_seconds())
            # Without both timestamps there is no elapsed time to reason from, so fall back
            # to the flat cap rather than inventing one.
            limit = _MAX_STEP_M_PER_S * max(1.0, gap_s) if gap_s is not None else _MAX_LEG_M
            if step > limit:
                skipped += 1
                continue

            # The overlay quantises coordinates to about 11 m, so smaller hops are noise.
            # Advancing the anchor on every sample would sum that noise into hundreds of
            # phantom metres for a parked car.
            if step >= min_move_m:
                distance += step
                anchor = (lat, lon)
                anchor_time = captured

        if skipped:
            log.warning(
                "skipped implausible legs while measuring a journey",
                skipped=skipped,
                max_leg_km=round(_MAX_LEG_M / 1000.0, 1),
            )

        if not any_point:
            return None, None
        avg = round(moving_sum / moving_count, 1) if moving_count else 0.0
        return round(distance, 1), avg

    async def assign_recording(self, session: AsyncSession, recording: Recording) -> Journey | None:
        """Attach one freshly processed recording to a journey.

        Cheaper than a full rebuild for the common case of a single new segment arriving.
        """
        settings = get_settings_service()
        if not bool(settings.get_nowait("journeys.enabled")) or recording.started_at is None:
            return None

        gap = timedelta(minutes=float(settings.get_nowait("journeys.gap_minutes")))

        journey = (
            await session.execute(
                select(Journey)
                .where(
                    Journey.started_at <= recording.started_at + gap,
                    Journey.ended_at >= recording.started_at - gap,
                )
                .order_by(Journey.started_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

        if journey is None:
            journey = Journey(
                started_at=as_utc(recording.started_at),
                ended_at=as_utc(recording.ended_at or recording.started_at),
            )
            session.add(journey)
            await session.flush()

        # Through _attach, not a bare assignment: the route map and the heat map's journey
        # filter read telemetry_points.journey_id, not recordings.journey_id. Setting only
        # the latter attached the recording while leaving its telemetry orphaned, so an
        # incrementally built journey had a correct recording list and an empty route.
        await self._attach(session, [recording.id], journey.id)
        await session.flush()
        await self.refresh(session, journey)
        return journey

    # -- manual edits ------------------------------------------------------------------

    async def merge(self, session: AsyncSession, journey_ids: list[int]) -> Journey | None:
        """Merge journeys into the earliest one and mark the result user-managed."""
        if len(journey_ids) < 2:
            return None
        journeys = list(
            (
                await session.execute(
                    select(Journey)
                    .where(Journey.id.in_(journey_ids))
                    .order_by(Journey.started_at.asc())
                )
            ).scalars()
        )
        if len(journeys) < 2:
            return None

        target, *rest = journeys
        rest_ids = [j.id for j in rest]
        for recording in (
            await session.execute(select(Recording).where(Recording.journey_id.in_(rest_ids)))
        ).scalars():
            recording.journey_id = target.id

        await session.execute(delete(Journey).where(Journey.id.in_(rest_ids)))
        target.manual = True
        await session.flush()
        await self.refresh(session, target)
        return target

    async def split(
        self, session: AsyncSession, journey_id: int, at_recording_id: int
    ) -> tuple[Journey, Journey] | None:
        """Split a journey so *at_recording_id* becomes the first recording of a new one."""
        journey = await session.get(Journey, journey_id)
        if journey is None:
            return None

        recordings = list(
            (
                await session.execute(
                    select(Recording)
                    .where(Recording.journey_id == journey_id)
                    .order_by(Recording.started_at.asc(), Recording.id.asc())
                )
            ).scalars()
        )
        index = next((i for i, r in enumerate(recordings) if r.id == at_recording_id), None)
        if index is None or index == 0:
            return None

        tail = recordings[index:]
        new_journey = Journey(
            started_at=as_utc(tail[0].started_at),
            ended_at=max(as_utc(r.ended_at or r.started_at) for r in tail),
            manual=True,
        )
        session.add(new_journey)
        await session.flush()
        for recording in tail:
            recording.journey_id = new_journey.id

        journey.manual = True
        await session.flush()
        await self.refresh(session, journey)
        await self.refresh(session, new_journey)
        return journey, new_journey
