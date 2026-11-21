"""Full-recording sample coverage by the logger's last observed accessory-power state."""

from bisect import bisect_right
from dataclasses import asdict, dataclass
from datetime import timedelta

from sqlalchemy import and_, or_, select

from app.db.models import OBDDrive, OBDLoggerEvent, OBDSample
from app.ingest.obd_reconciliation import specs_for_poll_plan
from app.obd.timing import pid_is_missing, response_time, row_is_live


@dataclass
class Coverage:
    rows: int = 0
    speed_rows: int = 0
    rpm_rows: int = 0
    speed_and_rpm_rows: int = 0
    voltage_rows: int = 0
    voltage_only_rows: int = 0

    def observe(self, speed: bool, rpm: bool, voltage: bool, voltage_only: bool) -> None:
        self.rows += 1
        self.speed_rows += speed
        self.rpm_rows += rpm
        self.speed_and_rpm_rows += speed and rpm
        self.voltage_rows += voltage
        self.voltage_only_rows += voltage_only


async def _acc_transitions(session, drive):
    # ACC broadcasts usually lack a drive ID. Anchor to the recording session.
    # Export or recovery after a restart can reference that drive from a different session.
    event = OBDLoggerEvent
    anchors = (
        await session.execute(
            select(event.source_id_hash, event.session_id_hash)
            .where(
                event.drive_id == drive.drive_id,
                or_(
                    event.kind == "obd.poll_health",
                    and_(event.kind == "drive.lifecycle", event.reason_code == "engine_detected"),
                ),
            )
            .distinct()
            .limit(2)
        )
    ).all()
    if len(anchors) != 1:
        return []
    source, app_session = anchors[0]
    query = select(
        event.occurred_at, event.sequence, event.kind, event.outcome, event.reason_code
    ).where(
        event.source_id_hash == source,
        event.session_id_hash == app_session,
    )
    previous = (
        await session.execute(
            query.where(
                event.occurred_at < drive.started_at,
                event.kind == "app.service",
                event.outcome == "observed",
                event.reason_code.in_(("acc_on", "acc_off")),
            )
            .order_by(event.occurred_at.desc(), event.sequence.desc())
            .limit(1)
        )
    ).first()
    latest = max(drive.last_sample_at or drive.finished_at, drive.finished_at)
    earliest = previous.occurred_at if previous else drive.started_at
    ordered = query.order_by(event.occurred_at, event.sequence)
    within = (
        await session.execute(ordered.where(event.occurred_at.between(earliest, latest)))
    ).all()
    following = (await session.execute(ordered.where(event.occurred_at > latest).limit(1))).first()
    if following:
        within.append(following)
    transitions = []
    prior = None
    for row in within:
        if prior and row.sequence != prior.sequence + 1:
            # An omitted event may be an ACC change. Keep its interval unknown until
            # the journal actually reports ACC again, including gaps before drive start.
            transitions.append(
                (min(prior.occurred_at + timedelta(microseconds=1), row.occurred_at), "unknown")
            )
        if (
            row.kind == "app.service"
            and row.outcome == "observed"
            and row.reason_code in ("acc_on", "acc_off")
        ):
            transitions.append((row.occurred_at, row.reason_code))
        prior = row
    return transitions


async def ignition_coverage(session, drive: OBDDrive) -> dict:
    transitions = await _acc_transitions(session, drive)
    times = [at for at, _ in transitions]
    phases = {key: Coverage() for key in ("acc_on", "acc_off", "unknown", "total")}
    plan, specs = specs_for_poll_plan((drive.manifest_json or {}).get("poll_plan_version"))
    dynamic = [spec for spec in specs if spec.pid is not None and spec.provenance == "measured"]
    query = select(
        OBDSample.captured_at,
        OBDSample.ecu_data_status,
        OBDSample.quality_json,
        OBDSample.adapter_voltage_v,
        *(getattr(OBDSample, spec.attribute) for spec in dynamic),
    ).where(OBDSample.drive_db_id == drive.id)
    result = await session.stream(query.execution_options(yield_per=512))
    try:
        async for row in result:
            at, status, quality, battery, *values = row
            index = bisect_right(times, at) - 1
            phase = transitions[index][1] if index >= 0 else "unknown"
            live = row_is_live(status, quality)
            speed = (
                live
                and row.vehicle_speed_kmh is not None
                and not pid_is_missing(quality, 0x0D)
                and response_time(at, quality, 0x0D, require_timing=plan == 8) is not None
            )
            rpm = (
                live
                and row.engine_rpm is not None
                and not pid_is_missing(quality, 0x0C)
                and response_time(at, quality, 0x0C, require_timing=plan == 8) is not None
            )
            voltage = live and battery is not None
            # A legacy row can contain an optional ECU value without a response time.
            # It still disqualifies the row from the stricter voltage-only category.
            fields = (speed, rpm, voltage, voltage and all(value is None for value in values))
            phases[phase].observe(*fields)
            phases["total"].observe(*fields)
    finally:
        await result.close()
    return {key: asdict(value) for key, value in phases.items()}
