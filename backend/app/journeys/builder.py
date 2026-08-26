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
    Camera,
    CameraRole,
    Journey,
    PlateObservation,
    Recording,
    TelemetryPoint,
    TrackedObject,
)
from app.db.retry import commit_with_retry
from app.journeys.track import single_track
from app.osd import haversine_m
from app.osd.outliers import (
    looks_like_sign_loss,
    plausible_radius_m,
    robust_centre,
    spatial_outliers,
)
from app.osd.reasons import GpsQuality, GpsReason
from app.osd.track_quality import MAX_ROAD_SPEED_MS
from app.osd.validate import is_plausible_step

log = get_logger(__name__)

#: Ceiling on a single leg of a journey's distance measurement.
#:
#: Independent of the outlier pass, which needs three points and a majority before it
#: will act and so cannot judge a short journey. 50 km between two consecutive 1 Hz
#: samples is not a leg, it is a misread digit, and admitting one put 154,701 km on an
#: eighteen-minute drive.
_MAX_LEG_M = 50_000.0

#: How lopsided a journey has to be before its few "positions" are read as a misread
#: placeholder rather than as a place.
#:
#: The outlier pass either side of this is *relative* — it asks whether a fix disagrees
#: with its recording or with the rest of its drive. That question has no answer for a
#: session the camera spent parked with no satellite lock, because there is nothing to
#: disagree with: every located fix is the same corrupted ``00.0000`` marker, so the median
#: centre lands on it and the check reports a clean drive to the Gulf of Guinea.
#:
#: Ten no-fix samples for every position is deliberately lopsided. A parked car that really
#: does hold a lock reports thousands of positions and no no-fix samples at all, so it is
#: nowhere near this; a garage reports hundreds of no-fix samples and a handful of
#: corruptions, and lands well past it.
_NO_LOCK_FIX_RATIO = 10.0

#: Metres a vehicle could cover in a second, including the overlay's own quantisation.
#:
#: Taken from :mod:`app.osd.track_quality` rather than restated, so the journey's idea of
#: an impossible leg cannot drift away from the extractor's idea of an impossible step —
#: they were 400 km/h and 400 km/h by coincidence rather than by construction, and at 1 Hz
#: that licensed a 111 m hop every second, which is how a route could accumulate kilometres
#: of travel through places the vehicle never went.
_MAX_STEP_M_PER_S = MAX_ROAD_SPEED_MS

#: Average speed over a whole journey beyond which its distance is not believable. Well
#: above any real drive -- the fastest genuine journey in this corpus averages about 60 --
#: so it only ever catches a distance that arithmetic already rules out.
_IMPLAUSIBLE_M_PER_S = 200.0 * 1000.0 / 3600.0

#: Row ids per ``IN`` clause. SQLite refuses more than 32,766 bound parameters in one
#: statement, and the journey passes routinely hold the whole library; 900 is the classic
#: conservative bound and leaves room for the handful of other parameters in the same query.
_ID_CHUNK = 900


def _journey_ready_recording():
    """A recording with no retained stage waiting to be rebuilt."""
    # Imported lazily because ``app.pipeline.__init__`` imports the stages, which import
    # this builder. Importing the revision module while the builder itself is initialising
    # would therefore close a cycle through ``stages -> builder``.
    from app.pipeline.revisions import INVALIDATED_REVISION

    # Legacy databases predate revision columns and therefore contain NULL here; those
    # rows remain valid. During current reanalysis, however, every requested stage is
    # stamped ``invalidated`` until its replacement commits. Requiring every derived
    # revision to be usable excludes retained membership for both full and partial runs.
    return and_(
        *(
            or_(column.is_(None), column != INVALIDATED_REVISION)
            for column in (
                Recording.metadata_revision,
                Recording.telemetry_revision,
                Recording.detection_revision,
                Recording.plate_revision,
            )
        )
    )


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
            .where(
                Recording.started_at.isnot(None),
                Recording.ignored.is_(False),
                _journey_ready_recording(),
            )
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
            # Only the recordings: `refresh` finds its members through
            # `Recording.journey_id` and re-points every denormalised copy itself on the way
            # out, so writing them here as well rewrote `telemetry_points` twice per rebuild.
            await self._attach(
                session, [r.id for r in cluster.recordings], journey.id, recordings_only=True
            )
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

    async def repair_start_positions(self, session: AsyncSession) -> int:
        """Re-derive every recording's start position, whether or not anything reclusters.

        This has to be self-healing rather than only a step inside ``rebuild``, and the live
        server proved why within two minutes of the fix being deployed. ``needs_recluster``
        compares the stored grouping against what clustering would produce, so once the two
        agree it stops asking -- which is the whole point of it. But a library whose start
        positions are *wrong* is grouped consistently with those wrong values: 85 journeys,
        31 holding a single recording, and clustering agreeing with every one of them.
        Nothing was left to trigger the rebuild that would have corrected the positions, so
        the fragmentation froze the moment the loop was fixed.

        The fragmentation also hides its own cause. Rejecting an impossible coordinate is
        journey-scoped, because a whole clip can be sign-flipped and agree with itself -- so
        a recording marooned in a journey of one has no reference frame, and its sightings
        keep coordinates nothing is able to see are wrong. That is exactly what stopped
        migration 0005 clearing recording 268: its journey held one recording with no fixes,
        so the pass had nothing to judge against and skipped it.

        Correcting the positions first breaks the deadlock. The damaged clips lose their
        bogus start coordinate, cluster back with their neighbours on time alone, and the
        ordinary journey refresh then has the neighbours' telemetry to recognise their
        sightings as impossible.
        """
        # Keyset-paged, not loaded whole. This runs on every scheduled scan, and selecting
        # the entire library instantiated one full ORM object per recording -- sixty-odd
        # columns each, all of them resident at once, in a process that is also holding an
        # OpenVINO graph and an ffmpeg decode buffer. The correction itself is per
        # recording, so nothing here needs to see them together.
        corrected = 0
        after = 0
        while True:
            batch = list(
                (
                    await session.execute(
                        select(Recording)
                        .where(
                            Recording.started_at.isnot(None),
                            Recording.ignored.is_(False),
                            Recording.id > after,
                            _journey_ready_recording(),
                        )
                        .order_by(Recording.id.asc())
                        .limit(_ID_CHUNK)
                    )
                ).scalars()
            )
            if not batch:
                break
            after = batch[-1].id
            corrected += await self._refresh_start_positions(session, batch)
            # The flush is what makes it safe to let the batch go; without it the identity
            # map is the only thing holding the pending updates.
            await session.flush()
            session.expunge_all()
        return corrected

    @staticmethod
    async def _refresh_start_positions(session: AsyncSession, recordings: list[Recording]) -> int:
        """Re-derive the given recordings' start positions from their earliest surviving fix.

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

        # Chunked, because the callers pass the whole library.
        #
        # SQLAlchemy renders `IN` as one bind parameter per element and SQLite refuses more
        # than 32,766 of them in a statement -- measured, not assumed. Both callers select
        # every journey-ready recording with no limit, so past that count this raised
        # `OperationalError: too many SQL variables` on every scheduled scan, and the
        # scheduler's single try/except then abandoned everything after it: the recluster,
        # the stale-rollup repair and the duration repair all stopped running, silently,
        # with one "scheduled task failed" line to show for it. At two-minute clips from two
        # cameras that ceiling is about 550 hours of driving -- well inside the life of a
        # permanently installed dashcam.
        first: dict[int, tuple[float | None, float | None]] = {}
        for start in range(0, len(ids), _ID_CHUNK):
            chunk = ids[start : start + _ID_CHUNK]
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
                    TelemetryPoint.recording_id.in_(chunk),
                    TelemetryPoint.has_fix.is_(True),
                    TelemetryPoint.lat.is_not(None),
                    TelemetryPoint.lon.is_not(None),
                )
                .subquery()
            )
            first.update(
                {
                    row.recording_id: (row.lat, row.lon)
                    for row in (
                        await session.execute(select(ranked).where(ranked.c.rank == 1))
                    ).all()
                }
            )

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
        session: AsyncSession,
        recording_ids: list[int],
        journey_id: int | None,
        *,
        recordings_only: bool = False,
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

        ``recordings_only`` writes just ``recordings.journey_id``, which is all a caller
        needs when the full attach is about to happen anyway. The rebuild is that caller: it
        attached a cluster so ``refresh`` could find the members by ``Recording.journey_id``,
        and ``refresh`` then attached the same recordings again on its way out -- so the
        three denormalised copies were rewritten twice per rebuild, on top of the once the
        cascading DELETE had already nulled them. One of those copies is
        ``telemetry_points.journey_id``, a row per second of footage per camera, so dropping
        the duplicate takes a full rewrite of the largest table in the schema out of every
        scan that sees a new file.
        """
        if not recording_ids:
            return
        models = (
            (Recording,)
            if recordings_only
            else (Recording, TelemetryPoint, TrackedObject, PlateObservation)
        )
        for model in models:
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
                    .where(
                        Recording.started_at.isnot(None),
                        Recording.ignored.is_(False),
                        _journey_ready_recording(),
                    )
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
                elapsed = (candidate_start - prev_end).total_seconds()
                # A jump the vehicle could not physically have made is a misread
                # coordinate, not a new drive -- and cutting the drive there is the worse
                # of the two mistakes by far.
                #
                # This check exists for the case where the car really did move without
                # recording: parked in a garage, driven onto a ferry, GPS reacquired a
                # suburb away. All of those are *possible* journeys between the two
                # points. A latitude that lost its minus sign is 7,700 km away in under
                # two minutes, which is not a journey, it is a bad digit.
                #
                # On the live library two clips with a single misread fix each cut one
                # Aug 3 drive into five journeys, three of them holding one recording --
                # and one bad clip cuts twice, before it and after it, which is where the
                # "three journeys for one drive" came from.
                if jump > max_jump_m and is_plausible_step(jump, elapsed):
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
                    .where(
                        Recording.journey_id == journey.id,
                        _journey_ready_recording(),
                    )
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
                        # Selected so `_measure` can actually see it. Without the column the
                        # guard there was reading a `TrackPoint` attribute that did not
                        # exist and defaulting to False on every row.
                        TelemetryPoint.breaks_segment,
                    ).where(
                        TelemetryPoint.recording_id.in_(recording_ids),
                        TelemetryPoint.has_fix.is_(True),
                    )
                )
            ).all()
        )

        front_ids = set(
            (
                await session.execute(
                    select(Recording.id)
                    .join(Camera, Camera.id == Recording.camera_id)
                    .where(Recording.id.in_(recording_ids), Camera.role == CameraRole.FRONT)
                )
            ).scalars()
        )

        # Which of those coordinates cannot belong to this drive at all. Computed over
        # *every* point, so the write phase below clears every copy of a bad one; the
        # single track assembled next is only for measuring.
        rejected_ids, centre, radius_m = self._find_outliers(points, journey.duration_s)

        # The one shape the outlier pass above is structurally unable to judge: a journey
        # with no satellite lock anywhere in it, whose only "positions" are the placeholder
        # decoding badly. They agree with each other perfectly, so nothing disagrees.
        # Rejected for disagreeing with the drive around them, unless the whole session
        # turns out to be a garage below -- in which case the honest reading is the one the
        # camera gave: there was no satellite lock, and nothing was ever misread *into* a
        # place. The two sets are disjoint by construction, so one verdict covers the batch.
        reject_quality, reject_reason = GpsQuality.REJECTED, GpsReason.ISOLATED_POSITION_OUTLIER

        no_lock = self._is_no_lock_session(points, recordings)
        if no_lock:
            rejected_ids = {p.id for p in points if p.lat is not None and p.lon is not None}
            reject_quality, reject_reason = GpsQuality.NO_FIX, GpsReason.NO_FIX
            # No centre survives: there is no established position to judge anything
            # against, and inventing one out of the readings being rejected is how the
            # placeholder got believed in the first place.
            centre = None
            log.info(
                "journey has no satellite lock; its positions are a misread placeholder",
                journey_id=journey.id,
                telemetry=len(rejected_ids),
                no_fix_samples=sum(r.gps_no_fix_count or 0 for r in recordings),
            )

        good = [p for p in points if p.id not in rejected_ids]
        track = self._one_track(good, recordings, front_ids)

        # Positions copied onto sightings when they were processed are stale the moment a
        # telemetry point behind one is rejected -- and nothing used to go back for them,
        # which is why a coordinate cleared out of `telemetry_points` went on being drawn
        # on the map as a detection long afterwards.
        stale_tracks, stale_observations = await self._find_stale_sighting_positions(
            session, recording_ids, centre, radius_m, reject_all=no_lock
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
        # Bounds come from every surviving fix, because they only have to contain the drive
        # and a missing corner would crop the map. Everything else is measured along the
        # single deduplicated track, in time order.
        located = [p for p in good if p.lat is not None and p.lon is not None]
        on_track = [p for p in track if p.lat is not None and p.lon is not None]
        speeds = [float(p.speed_kmh) for p in track if p.speed_kmh is not None]

        journey.has_gps = bool(located)
        journey.min_lat = min((p.lat for p in located), default=None)
        journey.max_lat = max((p.lat for p in located), default=None)
        journey.min_lon = min((p.lon for p in located), default=None)
        journey.max_lon = max((p.lon for p in located), default=None)
        journey.max_speed_kmh = max(speeds, default=None)

        # The first and last position of the drive itself. Null when the journey has no
        # valid fix at all, which the UI shows as unknown rather than guessing.
        journey.start_lat = on_track[0].lat if on_track else None
        journey.start_lon = on_track[0].lon if on_track else None
        journey.end_lat = on_track[-1].lat if on_track else None
        journey.end_lon = on_track[-1].lon if on_track else None

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
        # The recording-level GPS counters go with it, for exactly the same reason.
        #
        # `has_gps`, `gps_point_count` and `gps_rejected_count` are written once by the
        # telemetry stage and never revisited, and this method is the thing that revisits
        # the data behind them: `_clear_positions` below has just thrown out every fix the
        # journey decided could not belong to this drive. Left alone, a recording whose
        # entire GPS was repudiated still reported `has_gps = true` with N fixes -- so it
        # kept its green badge in the Recordings grid, answered `?has_gps=true`, and was
        # classified "healthy" on the Telemetry Health page while its map drew nothing at
        # all. `retention.idle` reads the same stale numbers to decide whether a clip is
        # empty enough to delete on its own.
        rejected_by_recording: dict[int, int] = {}
        for point in points:
            if point.id in rejected_ids:
                rejected_by_recording[point.recording_id] = (
                    rejected_by_recording.get(point.recording_id, 0) + 1
                )
        surviving_by_recording: dict[int, int] = {}
        for point in located:
            surviving_by_recording[point.recording_id] = (
                surviving_by_recording.get(point.recording_id, 0) + 1
            )

        for recording in recordings:
            point = first_by_recording.get(recording.id)
            recording.start_lat = point.lat if point else None
            recording.start_lon = point.lon if point else None
            newly_rejected = rejected_by_recording.get(recording.id, 0)
            if not newly_rejected:
                # Nothing this journey decided changes what the stage recorded, so the
                # stage's numbers stand. Recomputing them here would overwrite counts the
                # stage derived from evidence this method never sees -- the OCR statuses
                # behind `gps_ocr_gap_count`, for instance.
                continue
            surviving = surviving_by_recording.get(recording.id, 0)
            recording.gps_point_count = surviving
            recording.has_gps = surviving > 0
            recording.gps_rejected_count = (recording.gps_rejected_count or 0) + newly_rejected

        journey.distance_m, journey.avg_speed_kmh = self._measure(track, min_move_m)
        journey.vehicle_count = int(counts[0] or 0)
        journey.unique_plate_count = int(counts[1] or 0)

        # -- writes ----------------------------------------------------------------------
        if rejected_ids:
            await self._clear_positions(
                session, rejected_ids, quality=reject_quality, reason=reject_reason
            )
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
    def _one_track(points: list, recordings: list[Recording], front_ids: set[int]) -> list:
        """The drive's path, deduplicated to one position per second. See :mod:`.track`.

        Shared with the API rather than reimplemented there, so the distance, the markers
        and the line drawn on the map are all measuring the same thing. They were three
        different populations before, and disagreed accordingly.
        """
        return single_track(points, {r.id: as_utc(r.started_at) for r in recordings}, front_ids)

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
    def _is_no_lock_session(rows: list, recordings: list[Recording]) -> bool:
        """Is this a parked session whose "positions" are the no-fix marker misread?

        Two conditions, and both are needed. The camera has to have reported no lock
        overwhelmingly more often than it reported a position — see
        :data:`_NO_LOCK_FIX_RATIO` — and every position it did report has to be the *same*
        coordinate, to the last printed digit.

        The second is what separates this from a genuinely stationary drive. A real lock
        wanders: the overlay prints four decimals, about 11 m, and a stationary receiver
        drifts further than that within a minute, so a parked hour is dozens of distinct
        cells. A constant repeated to the fourth decimal across every fix in a journey is
        not a receiver, it is one misread rendered the same way every time.

        A journey holding a single position is caught by the same rule, vacuously — and
        that is the intent rather than an oversight. One unrepeatable fix in a session the
        camera otherwise spent reporting no lock has nothing to corroborate it, and the
        live library's example was a longitude of 161.0 in the Pacific.
        """
        located = [row for row in rows if row.lat is not None and row.lon is not None]
        if not located:
            return False
        no_fix_samples = sum(recording.gps_no_fix_count or 0 for recording in recordings)
        if no_fix_samples < len(located) * _NO_LOCK_FIX_RATIO:
            return False
        return len({(float(row.lat), float(row.lon)) for row in located}) == 1

    @staticmethod
    async def _clear_positions(
        session: AsyncSession,
        ids: set[int],
        *,
        quality: GpsQuality,
        reason: GpsReason,
    ) -> None:
        """Strip the coordinates off telemetry rows, keeping everything else.

        Rejected rows keep their timestamp, speed and raw text and lose only the position,
        because the rest of the reading was never in doubt — the speed on a sign-flipped
        line is perfectly good, and discarding it would trade one wrong number for a
        missing one.

        The quality columns move with the coordinate, and they were the one thing this used
        to leave behind: a row cleared here kept saying ``valid`` while carrying no
        position. Nothing drew it — the map tests ``lat IS NOT NULL`` as well — but
        :mod:`app.osd.reasons` exists so that "which check rejected this, and why" is a
        query rather than an afternoon, and a rejected row reporting ``valid`` is precisely
        the answer that module was built to stop being wrong.
        """
        await session.execute(
            update(TelemetryPoint)
            .where(TelemetryPoint.id.in_(ids))
            .values(
                has_fix=False,
                lat=None,
                lon=None,
                heading_deg=None,
                gps_quality=str(quality),
                gps_reason=str(reason),
            )
            .execution_options(synchronize_session=False)
        )

    @staticmethod
    async def _find_stale_sighting_positions(
        session: AsyncSession,
        recording_ids: list[int],
        centre: tuple[float, float] | None,
        radius_m: float,
        *,
        reject_all: bool = False,
    ) -> tuple[list[int], list[int]]:
        """Sightings holding a coordinate this journey says the vehicle was never at.

        ``tracked_objects`` and ``plate_observations`` each keep their own copy of the
        position, stamped when the recording was processed. Cleaning ``telemetry_points``
        never touched them, so a coordinate that had been recognised as impossible and
        cleared at the source went on being drawn on the map as a detection — which is
        exactly how one sighting kept reporting a longitude eleven thousand kilometres from
        the drive it belonged to, long after the telemetry point behind it was gone.

        Judged by the same centre and radius as the telemetry, so the two cannot disagree.
        ``reject_all`` is the case where there is no centre to judge against because every
        telemetry position was thrown out: a sighting stamped from those is no better than
        the fix it was copied from.
        """
        if centre is None and not reject_all:
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
                if centre is None
                or haversine_m(centre[0], centre[1], float(row.lat), float(row.lon)) > radius_m
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
            # `at` rather than the raw overlay clock: it has already been checked against
            # the recording the sample came from, so a misread digit cannot stretch the
            # elapsed time the leg ceiling below is measured against.
            lat, lon, speed, captured = row.lat, row.lon, row.speed_kmh, row.at
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

            # A break means nothing trustworthy was recorded between the previous position
            # and this one, so nothing is *known* to have been travelled. The route layer
            # already refuses to draw that leg; measuring it would put a distance on the
            # map's own admission that it does not know the way.
            if row.breaks_segment:
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
