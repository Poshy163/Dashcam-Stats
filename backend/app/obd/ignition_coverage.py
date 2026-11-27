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


async def _ignition_journal(session, drive):
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
        return [], []
    source, app_session = anchors[0]
    query = select(
        event.occurred_at,
        event.sequence,
        event.kind,
        event.outcome,
        event.reason_code,
        event.drive_id,
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
    return transitions, within


_STAGE_EVENTS = {
    ("obd.ble_connection", "notifications_ready"): "bluetooth_ready_s",
    ("obd.elm_session", "elm_ready"): "adapter_ready_s",
    ("obd.ecu_session", "ecu_proof_valid"): "ecu_ready_s",
}


def _startup_timing(drive, transitions, events, first_pair):
    def seconds(start, end):
        return round((end - start).total_seconds(), 3)

    result = {
        "observed_acc_on_at": None,
        "first_speed_rpm_at": first_pair.isoformat() if first_pair else None,
        "acc_to_first_speed_rpm_s": None,
        "drive_to_first_speed_rpm_s": (
            seconds(drive.started_at, first_pair)
            if first_pair and first_pair >= drive.started_at
            else None
        ),
        "stages": {},
        "low_voltage_parked_waits": None,
        "startup_voltage_rechecks": None,
    }
    through = first_pair or drive.started_at
    # Only an uninterrupted observation spanning drive creation and the first recorded pair
    # can link the recording to this ignition. A later restart/edge cannot repair a missing one.
    states = [(at, state) for at, state in transitions if at <= through]
    if not states:
        return result
    edge, state = states[-1]
    if state != "acc_on" or edge > drive.started_at or through < drive.started_at:
        return result
    if any(
        row.kind == "drive.lifecycle"
        and row.outcome == "started"
        and row.reason_code == "engine_detected"
        and row.drive_id != drive.drive_id
        and edge <= row.occurred_at <= through
        for row in events
    ):
        # A recorder reconnect on a long-running ignition is not a new ignition startup.
        return result
    result["observed_acc_on_at"] = edge.isoformat()
    if first_pair:
        result["acc_to_first_speed_rpm_s"] = seconds(edge, first_pair)
    stages = result["stages"]
    parked_waits = rechecks = 0
    for row in events:
        if not edge <= row.occurred_at <= drive.started_at:
            continue
        low_voltage = row.reason_code in {"voltage_below_start", "voltage_below_stop"}
        if low_voltage and row.kind == "obd.ecu_session" and row.outcome == "skipped":
            parked_waits += 1
        if low_voltage and row.kind == "obd.elm_session" and row.outcome == "retrying":
            rechecks += 1
        stage = _STAGE_EVENTS.get((row.kind, row.reason_code))
        if stage and row.outcome == "succeeded":
            if stage == "bluetooth_ready_s":
                stages.clear()  # A later connection supersedes an earlier unsuccessful setup.
            stages[stage] = seconds(edge, row.occurred_at)
        elif row.kind in {"obd.ble_connection", "obd.elm_session", "obd.ecu_session"} and (
            row.outcome == "failed" or (row.outcome == "skipped" and low_voltage)
        ):
            stages.clear()
    result["low_voltage_parked_waits"] = parked_waits
    result["startup_voltage_rechecks"] = rechecks
    return result


async def ignition_coverage(session, drive: OBDDrive) -> dict:
    transitions, events = await _ignition_journal(session, drive)
    times = [at for at, _ in transitions]
    phases = {key: Coverage() for key in ("acc_on", "acc_off", "unknown", "total")}
    first_pair = None
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
            if speed and rpm and (first_pair is None or at < first_pair):
                first_pair = at
            # A legacy row can contain an optional ECU value without a response time.
            # It still disqualifies the row from the stricter voltage-only category.
            fields = (speed, rpm, voltage, voltage and all(value is None for value in values))
            phases[phase].observe(*fields)
            phases["total"].observe(*fields)
    finally:
        await result.close()
    return {
        **{key: asdict(value) for key, value in phases.items()},
        "startup": _startup_timing(drive, transitions, events, first_pair),
    }
