"""Read-only evidence at adjacent recording boundaries; never integrate through a gap.

A short interruption and moving observations on either side suggest a reconnect, but
cannot prove uninterrupted ignition or motion. Unknown identity, overlapping captures,
deliberate stops and gaps longer than two minutes are deliberately not associated.
"""

from __future__ import annotations

import math
from datetime import timedelta

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import aliased, load_only

from app.db.models import OBDDrive, OBDSample
from app.obd.timing import (
    has_response_times,
    nominal_cycle_s,
    pid_is_missing,
    response_time,
    row_is_live,
)

MAX_BOUNDARY_INTERVAL_S = 120.0
_INTERRUPTIONS = {"command_timeout", "connection_lost", "device_restart", "poll_trial_fallback"}


def missing_duration_metadata(drive: OBDDrive) -> dict[str, object]:
    """Keep producer and derived definitions separate, including old unknown values."""
    analysis = drive.gap_analysis_json if isinstance(drive.gap_analysis_json, dict) else {}
    producer = analysis.get("producer_summary", {})
    if not isinstance(producer, dict):
        producer = {}
    value = producer.get("missing_data_duration_s")
    if drive.summary_source == "producer" and isinstance(drive.summary_json, dict):
        value = drive.summary_json.get("missing_data_duration_s")
    if type(value) not in {int, float} or not math.isfinite(value) or value < 0:
        value = None
    return {
        "gap_scope": "within_segment",
        "missing_data_duration_definition": (
            "row_interval_excess_only_when_interval_exceeds_1.5x_nominal"
            if drive.summary_source == "derived"
            else "producer_reported"
        ),
        "producer_missing_data_duration_s": value,
        "producer_missing_data_duration_definition": "producer_reported; version-dependent",
    }


def _compatible(previous: OBDDrive, following: OBDDrive) -> bool:
    a, b = previous.manifest_json, following.manifest_json
    if not isinstance(a, dict) or not isinstance(b, dict):
        return False
    logger = a.get("logger_id")
    if not isinstance(logger, str) or not logger.strip() or logger != b.get("logger_id"):
        return False
    if previous.vehicle_id != following.vehicle_id or previous.clean_end:
        return False
    if previous.lifecycle_status not in {"interrupted", "recovered"}:
        return False
    if previous.stop_reason not in _INTERRUPTIONS:
        return False
    # An intervening power-off has no reconstructable continuity, even if close in time.
    if following.start_reason in {"ignition_on", "accessory_power_on", "manual_start"}:
        return False
    separation = (following.started_at - previous.finished_at).total_seconds()
    if not 0 <= separation <= MAX_BOUNDARY_INTERVAL_S:
        return False
    # Out-of-order rows cannot establish trustworthy edges from an older projection.
    for drive in (previous, following):
        analysis = drive.gap_analysis_json or {}
        if isinstance(analysis, dict):
            transport = analysis.get("transport", {})
            if isinstance(transport, dict) and transport.get("out_of_order_count", 0):
                return False
    return True


def _observation(sample: OBDSample, drive: OBDDrive):
    speed = sample.vehicle_speed_kmh
    if (
        speed is None
        or not math.isfinite(speed)
        or speed < 0
        or not row_is_live(sample.ecu_data_status, sample.quality_json)
        or pid_is_missing(sample.quality_json, 0x0D)
    ):
        return None
    timed = drive.manifest_json.get("poll_plan_version") == 8
    at = response_time(sample.captured_at, sample.quality_json, 0x0D, require_timing=timed)
    if at is None or not drive.started_at <= at <= drive.finished_at:
        return None
    return at, speed, has_response_times(sample.quality_json), sample.captured_at


def _boundary(previous, following, before, after):
    if before is None or after is None:
        return None
    # A mixed legacy/timed boundary must not silently label mixed timestamps as
    # row observations. Use row times on both sides unless both completions exist.
    response_based = before[2] and after[2]
    before_at = before[0] if response_based else before[3]
    after_at = after[0] if response_based else after[3]
    interval = (after_at - before_at).total_seconds()
    nominal = max(
        nominal_cycle_s(previous.manifest_json.get("poll_plan_version")),
        nominal_cycle_s(following.manifest_json.get("poll_plan_version")),
    )
    if not nominal * 1.5 < interval <= MAX_BOUNDARY_INTERVAL_S:
        return None
    moving_before, moving_after = before[1] > 0, after[1] > 0
    return {
        "previous_drive_id": previous.drive_id,
        "next_drive_id": following.drive_id,
        "previous_observation_at": before_at.isoformat(),
        "next_observation_at": after_at.isoformat(),
        "observation_interval_s": interval,
        "nominal_interval_s": nominal,
        "unobserved_excess_s": interval - nominal,
        "classification": (
            "likely_reconnect" if moving_before and moving_after else "unconfirmed_interruption"
        ),
        "interruption_reason": previous.interruption_reason or previous.stop_reason,
        "moving_before": moving_before,
        "moving_after": moving_after,
        "timestamp_basis": "response_completion" if response_based else "row_observation",
    }


async def recording_gap_context(session, drives, *, include_after=False):
    """Batch adjacent identities and bounded edge observations, independent of pagination.

    Neighbor order uses capture time, never database/import order. A mismatched logger
    or overlapping immediate neighbor cannot be skipped to create a convenient match.
    At most two minutes of edge samples per eligible pair are read.
    """
    result = {drive.id: {"recording_gap_before": None} for drive in drives}
    if include_after:
        for item in result.values():
            item["recording_gap_after"] = None
    if not drives:
        return result
    neighbor = aliased(OBDDrive)

    def adjacent(after=False):
        ordered_after = or_(
            neighbor.started_at > OBDDrive.started_at,
            and_(neighbor.started_at == OBDDrive.started_at, neighbor.id > OBDDrive.id),
        )
        return (
            select(neighbor.id)
            .where(
                neighbor.vehicle_id == OBDDrive.vehicle_id,
                ordered_after
                if after
                else or_(
                    neighbor.started_at < OBDDrive.started_at,
                    and_(neighbor.started_at == OBDDrive.started_at, neighbor.id < OBDDrive.id),
                ),
            )
            .order_by(
                neighbor.started_at.asc() if after else neighbor.started_at.desc(),
                neighbor.id.asc() if after else neighbor.id.desc(),
            )
            .limit(1)
            .correlate(OBDDrive)
            .scalar_subquery()
        )

    columns = [OBDDrive.id, adjacent()]
    if include_after:
        columns.append(adjacent(after=True))
    links = (await session.execute(select(*columns).where(OBDDrive.id.in_(result)))).all()
    neighbors = {value for link in links for value in link[1:] if value is not None}
    rows = {drive.id: drive for drive in drives}
    if neighbors - rows.keys():
        rows.update(
            (drive.id, drive)
            for drive in (
                await session.scalars(
                    select(OBDDrive).where(OBDDrive.id.in_(neighbors - rows.keys()))
                )
            ).all()
        )
    pairs = set()
    for link in links:
        if link[1] is not None and _compatible(rows[link[1]], rows[link[0]]):
            pairs.add((link[1], link[0]))
        if include_after and link[2] is not None and _compatible(rows[link[0]], rows[link[2]]):
            pairs.add((link[0], link[2]))
    filters = []
    for before, after in pairs:
        filters.extend(
            (
                and_(
                    OBDSample.drive_db_id == before,
                    OBDSample.captured_at >= rows[before].finished_at - timedelta(seconds=120),
                    OBDSample.captured_at <= rows[before].finished_at,
                ),
                and_(
                    OBDSample.drive_db_id == after,
                    OBDSample.captured_at >= rows[after].started_at,
                    OBDSample.captured_at <= rows[after].started_at + timedelta(seconds=120),
                ),
            )
        )
    if not filters:
        return result
    query = (
        select(OBDSample)
        .options(
            load_only(
                OBDSample.drive_db_id,
                OBDSample.captured_at,
                OBDSample.vehicle_speed_kmh,
                OBDSample.ecu_data_status,
                OBDSample.quality_json,
            )
        )
        .where(or_(*filters), OBDSample.vehicle_speed_kmh.is_not(None))
    )
    edges = {}
    for sample in (await session.scalars(query)).all():
        observation = _observation(sample, rows[sample.drive_db_id])
        if observation is None:
            continue
        first, last = edges.get(sample.drive_db_id, (observation, observation))
        edges[sample.drive_db_id] = (
            min(first, observation, key=lambda item: item[0]),
            max(last, observation, key=lambda item: item[0]),
        )
    for before, after in pairs:
        gap = _boundary(
            rows[before],
            rows[after],
            edges.get(before, (None, None))[1],
            edges.get(after, (None, None))[0],
        )
        if after in result:
            result[after]["recording_gap_before"] = gap
        if include_after and before in result:
            result[before]["recording_gap_after"] = gap
    return result
