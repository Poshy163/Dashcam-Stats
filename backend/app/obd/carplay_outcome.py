"""Per-ignition CarPlay milestones. Missing telemetry never establishes a failed connection."""

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import OBDCarPlayObservation as Observation
from app.db.models import OBDDrive, OBDLoggerEvent

_ACTIONS = {"early_retry", "waiting_connect", "waiting_cycle"}


async def _window(session: AsyncSession, drive: OBDDrive) -> tuple[str, datetime, datetime] | None:
    event = OBDLoggerEvent
    anchor = or_(
        event.kind == "obd.poll_health",
        and_(event.kind == "drive.lifecycle", event.reason_code == "engine_detected"),
    )
    anchors = (
        await session.execute(
            select(event.source_id_hash, event.session_id_hash)
            .where(event.drive_id == drive.drive_id, anchor)
            .distinct()
            .limit(2)
        )
    ).all()
    if len(anchors) != 1:
        return None
    source, boot = anchors[0]
    # Read only this logger session. Inspect sequence continuity around ACC broadcasts,
    # including before OBD starts, and collapse repeated ACC-on messages from one wake.
    session_filter = (event.source_id_hash == source, event.session_id_hash == boot)
    last_off = await session.scalar(
        select(event.occurred_at)
        .where(
            *session_filter,
            event.kind == "app.service",
            event.outcome == "observed",
            event.reason_code == "acc_off",
            event.occurred_at < drive.started_at,
        )
        .order_by(event.occurred_at.desc())
        .limit(1)
    )
    lower = drive.started_at - timedelta(days=1)
    if last_off is not None:
        lower = max(lower, last_off)
    # Inspect the first later row as well. A sequence gap there invalidates a tail
    # which would otherwise silently span a lost power transition.
    following = await session.scalar(
        select(event.occurred_at)
        .where(*session_filter, event.occurred_at > drive.finished_at)
        .order_by(event.occurred_at)
        .limit(1)
    )
    events = await session.stream(
        select(event.occurred_at, event.sequence, event.kind, event.outcome, event.reason_code)
        .where(
            *session_filter,
            event.occurred_at.between(lower, following or drive.finished_at),
        )
        .order_by(event.sequence)
        .execution_options(yield_per=512)
    )
    ignition = previous = None
    stop = drive.finished_at
    try:
        async for row in events:
            at = row.occurred_at
            if previous is not None and (
                row.sequence != previous.sequence + 1 or at < previous.occurred_at
            ):
                if previous.occurred_at >= drive.started_at:
                    stop = previous.occurred_at
                    break
                ignition = None
            if at > drive.finished_at:
                break
            if row.kind == "app.service" and row.outcome == "observed":
                if row.reason_code == "acc_off":
                    if at >= drive.started_at:
                        stop = at
                        break
                    ignition = None
                elif row.reason_code == "acc_on" and ignition is None and at <= drive.started_at:
                    ignition = at
            previous = row
    finally:
        await events.close()
    if ignition is None or stop <= drive.started_at:
        return None
    later_session = await session.scalar(
        select(event.occurred_at)
        .where(
            event.source_id_hash == source,
            event.session_id_hash != boot,
            event.occurred_at.between(ignition, stop),
        )
        .order_by(event.occurred_at)
        .limit(1)
    )
    if later_session is not None:
        if later_session <= drive.started_at:
            return None
        stop = later_session
    # A second recording beginning within this interval can represent a restart or
    # interrupted export. Never reuse an earlier recording's startup milestones.
    prior_drive = await session.scalar(
        select(OBDDrive.id)
        .where(
            OBDDrive.id != drive.id,
            OBDDrive.vehicle_id == drive.vehicle_id,
            OBDDrive.started_at < stop,
            OBDDrive.finished_at > ignition,
        )
        .limit(1)
    )
    return None if prior_drive else (source, ignition, stop)


async def drive_carplay_outcome(session: AsyncSession, drive: OBDDrive) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "unknown",
        "ignition_at": None,
        "observed_until": None,
        "hotspot": None,
        "saved_phone_lease": None,
        "phone_handshake": None,
        "video_session": None,
        "helper": None,
    }
    window = await _window(session, drive)
    if window is None:
        return result
    source, ignition, stop = window
    result.update(ignition_at=ignition, observed_until=stop)
    observations = list(
        (
            await session.execute(
                select(Observation.occurred_at, Observation.kind, Observation.duration_ms).where(
                    Observation.source_id_hash == source,
                    Observation.occurred_at >= ignition,
                    Observation.occurred_at < stop,
                )
            )
        ).all()
    )

    def milestone(at: datetime) -> dict[str, Any]:
        return {"at": at, "after_ignition_s": round((at - ignition).total_seconds(), 3)}

    names = {"iap": "phone_handshake", "lease": "saved_phone_lease", "hotspot": "hotspot"}
    video = []
    handshakes: list[datetime] = []
    handoffs = [at for at, kind, _ in observations if kind == "phone_handoff"]
    other_phones = [at for at, kind, _ in observations if kind == "other_iap"]
    leases = [at for at, kind, _ in observations if kind == "lease"]
    late_requests = [
        at for at, kind, _ in observations if kind in {"waiting_connect", "waiting_cycle"}
    ]
    # Android may publish a decoder a few seconds into shutdown. A lifetime spanning
    # another ignition, or much beyond the observed window, has an ambiguous frame count.
    output_limit = stop + timedelta(seconds=30)
    if any(
        duration is not None and at + timedelta(milliseconds=duration) > stop
        for at, _, duration in observations
    ):
        next_wake = await session.scalar(
            select(OBDLoggerEvent.occurred_at)
            .where(
                OBDLoggerEvent.source_id_hash == source,
                OBDLoggerEvent.kind == "app.service",
                OBDLoggerEvent.outcome == "observed",
                OBDLoggerEvent.reason_code == "acc_on",
                OBDLoggerEvent.occurred_at.between(stop, output_limit),
            )
            .order_by(OBDLoggerEvent.occurred_at)
            .limit(1)
        )
        if next_wake:
            output_limit = next_wake - timedelta(microseconds=1)
    for at, kind, duration in sorted(observations, key=lambda row: row[0]):
        if kind in names and result[names[kind]] is None:
            result[names[kind]] = milestone(at)
        if kind == "lease" and result["hotspot"] is None:
            result["hotspot"] = milestone(at)
        if kind == "iap":
            handshakes.append(at)
        elif kind in _ACTIONS and result["helper"] is None:
            result["helper"] = {**milestone(at), "action": kind}
        elif kind in {"decoder", "video_output"} and duration is not None:
            if at + timedelta(milliseconds=duration) > output_limit:
                kind = "decoder"
            video.append((at, kind, duration))
    # A codec may release as ACC switches off. Its stored time is the start calculated
    # from the completed lifetime, never the parked collection time or a displayed frame.
    if video:
        # Prefer the earliest Apple handoff with a decoder (the proven enrollment timing),
        # then a decoder with measured output, then a session lacking an output counter.
        def rank(row: tuple[datetime, str, int]) -> tuple[int, bool, datetime]:
            at, kind, _ = row
            nearby_iap = [peer for peer in handshakes if -1 <= (at - peer).total_seconds() <= 12]
            new_lease = any(-1 <= (at - lease).total_seconds() <= 90 for lease in leases)
            # A late helper can only act after the lease is 90 seconds old. Accept the
            # existing lease if the saved phone then handshakes, with no other Apple peer
            # between that grant and decoder startup. The request is not proof of recovery.
            late_chain = any(
                90 <= (request - lease).total_seconds() <= 200
                and any(request <= peer <= request + timedelta(seconds=30) for peer in nearby_iap)
                and not any(lease <= other <= at + timedelta(seconds=3) for other in other_phones)
                for request in late_requests
                for lease in leases
            )
            apple = (
                (new_lease or late_chain)
                and bool(nearby_iap)
                and any(-3 <= (at - handoff).total_seconds() <= 12 for handoff in handoffs)
                and not any(-3 <= (at - other).total_seconds() <= 12 for other in other_phones)
            )
            return (int(kind != "video_output"), not apple, at)

        at, kind, duration = min(video, key=rank)
        result["video_session"] = {
            **milestone(at),
            "output_confirmed": kind == "video_output",
            "duration_s": duration / 1000,
        }
        if kind == "video_output":
            result["status"] = "zlink_video" if rank((at, kind, duration))[1] else "carplay_video"
        else:
            result["status"] = (
                "decoder_observed" if rank((at, kind, duration))[1] else "carplay_session"
            )
    elif handshakes:
        result["status"] = "phone_handshake"
    elif result["saved_phone_lease"]:
        result["status"] = "saved_phone_joined"
    return result
