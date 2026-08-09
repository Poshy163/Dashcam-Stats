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

from sqlalchemy import delete, func, select, update
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

        clusters = self._cluster(movable, gap, use_gps, max_jump_m)

        # Automatic journeys are rebuilt from scratch; the alternative is trying to
        # reconcile old and new boundaries, which produces stale half-journeys.
        stale_ids = {r.journey_id for r in movable if r.journey_id is not None} - manual_ids
        if stale_ids:
            await session.execute(
                delete(Journey).where(Journey.id.in_(stale_ids), Journey.manual.is_(False))
            )

        created = 0
        for cluster in clusters:
            if len(cluster.recordings) < min_recordings:
                for recording in cluster.recordings:
                    recording.journey_id = None
                continue
            journey = Journey(started_at=cluster.started_at, ended_at=cluster.ended_at)
            session.add(journey)
            await session.flush()
            for recording in cluster.recordings:
                recording.journey_id = journey.id
            await self.refresh(session, journey)
            created += 1

        await session.flush()
        removed = await self._drop_empty(session)
        log.info("rebuilt journeys", journeys=created, recordings=len(movable), removed=removed)
        return created

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
        """Recompute a journey's aggregates from its recordings and telemetry."""
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

        # Before anything is derived from the fixes, drop the ones that cannot belong to
        # this drive. Doing it here rather than in each consumer is the point: bounds,
        # start/end, distance, the map and the recording viewer all read the same rows, so
        # cleaning them once leaves every view agreeing rather than each filtering to its
        # own taste. It also self-heals -- a journey rebuilt after a decoder improvement
        # re-examines its own history.
        await self._reject_outliers(session, recording_ids, journey.duration_s)

        # Aggregate bounds and max speed in SQL rather than pulling every point back:
        # a long journey has tens of thousands of telemetry rows.
        bounds = (
            await session.execute(
                select(
                    func.min(TelemetryPoint.lat),
                    func.max(TelemetryPoint.lat),
                    func.min(TelemetryPoint.lon),
                    func.max(TelemetryPoint.lon),
                    func.max(TelemetryPoint.speed_kmh),
                ).where(
                    TelemetryPoint.recording_id.in_(recording_ids),
                    TelemetryPoint.has_fix.is_(True),
                )
            )
        ).one()
        journey.min_lat, journey.max_lat, journey.min_lon, journey.max_lon = bounds[:4]
        journey.max_speed_kmh = bounds[4]
        journey.has_gps = journey.min_lat is not None

        journey.distance_m, journey.avg_speed_kmh = await self._track_metrics(
            session, recording_ids, min_move_m
        )

        if journey.has_gps:
            first = (
                await session.execute(
                    select(TelemetryPoint.lat, TelemetryPoint.lon)
                    .where(
                        TelemetryPoint.recording_id.in_(recording_ids),
                        TelemetryPoint.has_fix.is_(True),
                    )
                    .order_by(TelemetryPoint.captured_at.asc())
                    .limit(1)
                )
            ).first()
            last = (
                await session.execute(
                    select(TelemetryPoint.lat, TelemetryPoint.lon)
                    .where(
                        TelemetryPoint.recording_id.in_(recording_ids),
                        TelemetryPoint.has_fix.is_(True),
                    )
                    .order_by(TelemetryPoint.captured_at.desc())
                    .limit(1)
                )
            ).first()
            if first:
                journey.start_lat, journey.start_lon = first
            if last:
                journey.end_lat, journey.end_lon = last

        journey.vehicle_count = int(
            (
                await session.execute(
                    select(func.count(TrackedObject.id)).where(
                        TrackedObject.recording_id.in_(recording_ids)
                    )
                )
            ).scalar()
            or 0
        )
        journey.unique_plate_count = int(
            (
                await session.execute(
                    select(func.count(func.distinct(PlateObservation.plate_id))).where(
                        PlateObservation.recording_id.in_(recording_ids)
                    )
                )
            ).scalar()
            or 0
        )

        await session.flush()
        return journey

    @staticmethod
    async def _reject_outliers(
        session: AsyncSession, recording_ids: list[int], span_s: float
    ) -> int:
        """Clear coordinates that cannot belong to this journey. Returns how many.

        The journey is the smallest scope at which the worst failure mode is visible. When
        the rear camera loses the minus sign in front of a latitude it usually loses it for
        the whole clip, so every fix in that recording agrees with every other one at high
        confidence while sitting 7,700 km away. Nothing inside the recording contradicts it;
        only its neighbours in the drive do.

        Rejected rows keep their timestamp, speed and raw text and lose only the position,
        because the rest of the reading was never in doubt — the speed on a sign-flipped
        line is perfectly good, and discarding it would trade one wrong number for a
        missing one.
        """
        rows = (
            await session.execute(
                select(TelemetryPoint.id, TelemetryPoint.lat, TelemetryPoint.lon).where(
                    TelemetryPoint.recording_id.in_(recording_ids),
                    TelemetryPoint.has_fix.is_(True),
                    TelemetryPoint.lat.is_not(None),
                    TelemetryPoint.lon.is_not(None),
                )
            )
        ).all()
        if len(rows) < 3:
            return 0

        points = [(float(r.lat), float(r.lon)) for r in rows]
        outliers = spatial_outliers(points, span_s=span_s)
        if not outliers:
            return 0

        centre = robust_centre([p for i, p in enumerate(points) if i not in outliers])
        radius = plausible_radius_m(span_s)
        sign_losses = (
            sum(1 for i in outliers if looks_like_sign_loss(points[i], centre, radius))
            if centre
            else 0
        )

        await session.execute(
            update(TelemetryPoint)
            .where(TelemetryPoint.id.in_([rows[i].id for i in outliers]))
            .values(has_fix=False, lat=None, lon=None, heading_deg=None)
        )
        log.info(
            "discarded positions that cannot belong to this journey",
            rejected=len(outliers),
            of=len(rows),
            # Worth separating: a dropped minus sign points at the overlay region or the
            # glyph templates, where a mangled digit points at the weather.
            sign_losses=sign_losses,
            radius_km=round(radius / 1000.0, 1),
        )
        return len(outliers)

    @staticmethod
    async def _track_metrics(
        session: AsyncSession, recording_ids: list[int], min_move_m: float
    ) -> tuple[float | None, float | None]:
        """Distance and moving average, streamed so memory stays bounded."""
        rows = await session.stream(
            select(TelemetryPoint.lat, TelemetryPoint.lon, TelemetryPoint.speed_kmh)
            .where(
                TelemetryPoint.recording_id.in_(recording_ids),
                TelemetryPoint.has_fix.is_(True),
            )
            .order_by(TelemetryPoint.captured_at.asc(), TelemetryPoint.t_offset_s.asc())
        )

        distance = 0.0
        skipped = 0
        anchor: tuple[float, float] | None = None
        moving_sum = 0.0
        moving_count = 0
        any_point = False

        async for lat, lon, speed in rows:
            any_point = True
            if speed is not None and speed > 1.0:
                moving_sum += speed
                moving_count += 1
            if lat is None or lon is None:
                continue
            if anchor is None:
                anchor = (lat, lon)
                continue
            step = haversine_m(anchor[0], anchor[1], lat, lon)

            # A ceiling on a single leg, independent of the outlier pass above. That pass
            # needs three points and a majority to act, so a short journey can still carry
            # a coordinate it could not judge -- and without this, one such point put
            # 154,701 km on an eighteen-minute drive and 21 of 45 journeys reported a
            # distance no car could cover. Skip the leg rather than the point: the anchor
            # is the last position still trusted, and moving it to a coordinate this one
            # disagrees with would corrupt every leg after it too.
            if step > _MAX_LEG_M:
                skipped += 1
                continue

            # The overlay quantises coordinates to about 11 m, so smaller hops are noise.
            # Advancing the anchor on every sample would sum that noise into hundreds of
            # phantom metres for a parked car.
            if step >= min_move_m:
                distance += step
                anchor = (lat, lon)

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

        recording.journey_id = journey.id
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
