"""Reconnect evidence crosses recording boundaries without filling unknown travel."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from test_obd_server_import import _sample, make_bundle

from app.db.models import OBDDiagnostic, OBDDrive, OBDSample
from app.db.session import session_scope
from app.ingest.obd_bundle import store_validated_bundle, validate_bundle
from app.ingest.obd_reconciliation import reconcile_drive_projection
from app.obd.recording_gaps import missing_duration_metadata, recording_gap_context

BASE = datetime(2026, 10, 23, tzinfo=UTC)


async def _segment(
    directory,
    name,
    first,
    last,
    *,
    stop="command_timeout",
    clean=False,
    plan=7,
    start=None,
    speed=60,
    logger="synthetic-logger",
    producer_missing=4.25,
):
    samples = [
        _sample(name, i, at=BASE + timedelta(seconds=t)) for i, t in enumerate((first, last))
    ]
    for sample in samples:
        sample["vehicle_speed"] = speed
        if plan == 8:
            sample["quality"]["pid_response_age_ms"] = {"0D": 0, "0C": 0}
            sample.pop("estimated_fuel_rate")
    started = BASE + timedelta(seconds=first if start is None else start)
    finished = BASE + timedelta(seconds=last)
    lifecycle = {
        "clean_end": clean,
        "last_sample_at_utc": finished.isoformat(),
        "termination_noticed_at_utc": finished.isoformat(),
        "finalised_at_utc": finished.isoformat(),
        "completion_status": "complete" if clean else "interrupted",
        "interruption_reason": None if clean else stop,
    }
    path = make_bundle(
        directory,
        name,
        samples=samples,
        manifest_patch={
            **lifecycle,
            "logger_id": logger,
            "logger_version": "0.3.14",
            "poll_plan_version": plan,
            "last_successful_obd_response_at_utc": finished.isoformat(),
            "start_time_utc": started.isoformat(),
            "completion_status": "complete" if clean else "interrupted",
            "stop_reason": stop,
        },
        summary_patch={
            **lifecycle,
            "start_time_utc": started.isoformat(),
            "duration_s": last - (first if start is None else start),
            "missing_data_duration_s": producer_missing,
        },
    )
    async with session_scope() as session:
        await store_validated_bundle(session, validate_bundle(path))


async def _pair(directory, *, gap=35.068787, plan_before=7, plan_after=7, speed_after=60):
    await _segment(directory, "before", 0, 5, plan=plan_before)
    await _segment(
        directory,
        "after",
        5 + gap,
        10 + gap,
        plan=plan_after,
        speed=speed_after,
        start=5 + gap - 4,
    )


async def _stored_evidence():
    async with session_scope() as session:
        return {
            "drives": [
                dict(row)
                for row in (
                    await session.execute(select(OBDDrive.__table__).order_by(OBDDrive.id))
                ).mappings()
            ],
            "samples": [
                dict(row)
                for row in (
                    await session.execute(select(OBDSample.__table__).order_by(OBDSample.id))
                ).mappings()
            ],
        }


@pytest.mark.parametrize("interval", [35.068787, 24.601125])
async def test_retained_timeout_intervals_visible_outside_page_without_filling_totals(
    db_session, client, tmp_path, interval
):
    # Anonymous boundary metadata replay of the two moving 0.3.14 engine-load
    # timeouts retained on 23 Oct 2026. The actual row-to-row intervals are unchanged.
    await _pair(tmp_path, gap=interval)
    evidence_before = await _stored_evidence()
    assert len(evidence_before["drives"]) == 2
    assert all(
        row["processing_status"] == "ready"
        and row["gap_count"] == row["missing_data_duration_s"] == 0
        for row in evidence_before["drives"]
    ), evidence_before["drives"]
    before = (await client.get("/api/obd/drives/summary")).json()
    response = await client.get("/api/obd/drives?page_size=1")
    assert response.status_code == 200
    current = response.json()["items"][0]
    assert current["drive_id"] == "after"
    gap = current["recording_gap_before"]
    assert gap["previous_drive_id"] == "before"
    assert gap["next_drive_id"] == "after"
    assert gap["observation_interval_s"] == pytest.approx(interval)
    assert gap["unobserved_excess_s"] == pytest.approx(interval - 5)
    assert gap["classification"] == "likely_reconnect"
    assert gap["timestamp_basis"] == "row_observation"
    assert gap["interruption_reason"] == "command_timeout"
    assert gap["moving_before"] and gap["moving_after"]
    assert "logger_id" not in str(gap)
    assert current["gap_scope"] == "within_segment"
    assert current["gap_count"] == current["missing_data_duration_s"] == 0
    assert current["producer_missing_data_duration_s"] == 4.25
    assert current["missing_data_duration_definition"] == (
        "row_interval_excess_only_when_interval_exceeds_1.5x_nominal"
    )
    assert current["producer_missing_data_duration_definition"] == (
        "producer_reported; version-dependent"
    )
    series = (await client.get("/api/obd/drives/before/series")).json()
    assert series["drive"]["recording_gap_before"] is None
    assert series["drive"]["recording_gap_after"] == gap
    latest = (await client.get("/api/obd/drives/after/series")).json()
    assert latest["drive"]["recording_gap_before"] == gap
    assert latest["drive"]["recording_gap_after"] is None
    assert (await client.get("/api/obd/drives/summary")).json() == before
    # Merely reading boundary context never mutates historical projections/raw rows.
    evidence_after = await _stored_evidence()
    assert evidence_after == evidence_before, {
        "before": evidence_before["drives"],
        "after": evidence_after["drives"],
    }
    assert len(evidence_after["samples"]) == 4


@pytest.mark.parametrize(
    "interval, expected",
    [
        (7.5, False),
        (7.500001, True),
        (120, True),
        (120.000001, False),
    ],
)
async def test_threshold_and_bound_are_explicit(db_session, tmp_path, interval, expected):
    await _pair(tmp_path, gap=interval)
    async with session_scope() as session:
        rows = (await session.scalars(select(OBDDrive))).all()
        after = next(row for row in rows if row.drive_id == "after")
        context = await recording_gap_context(session, [after])
        assert (context[after.id]["recording_gap_before"] is not None) is expected


@pytest.mark.parametrize("interval", [4, 5, 7.5])
async def test_normal_trial_to_baseline_boundary_not_flagged(db_session, tmp_path, interval):
    await _pair(tmp_path, gap=interval, plan_before=8, plan_after=7)
    async with session_scope() as session:
        after = await session.scalar(select(OBDDrive).where(OBDDrive.drive_id == "after"))
        assert (await recording_gap_context(session, [after]))[after.id][
            "recording_gap_before"
        ] is None


@pytest.mark.parametrize(
    "mutation",
    [
        "clean",
        "shutdown",
        "engine_stopped",
        "backup",
        "identity",
        "unknown_identity",
        "vehicle",
        "overlap",
        "out_of_order",
        "power_on",
        "missing_pid",
        "nonlive",
        "no_speed",
    ],
)
async def test_non_reconnect_or_ambiguous_identity_not_associated(db_session, tmp_path, mutation):
    await _pair(tmp_path)
    async with session_scope() as session:
        rows = (await session.scalars(select(OBDDrive))).all()
        before, after = sorted(rows, key=lambda row: row.started_at)
        if mutation == "clean":
            before.clean_end = True
        elif mutation == "shutdown":
            before.lifecycle_status = "shutdown_detected"
        elif mutation == "engine_stopped":
            before.stop_reason = "engine_stopped"
        elif mutation == "backup":
            before.stop_reason = "ingestion_requested"
            before.lifecycle_status = "saved_for_backup"
        elif mutation in {"identity", "unknown_identity"}:
            after.manifest_json = {
                **after.manifest_json,
                "logger_id": ("another-logger" if mutation == "identity" else ""),
            }
        elif mutation == "vehicle":
            after.vehicle_id = "another-vehicle"
        elif mutation == "overlap":
            before.finished_at = after.started_at + timedelta(microseconds=1)
        elif mutation == "out_of_order":
            before.gap_analysis_json = {"transport": {"out_of_order_count": 1}}
        elif mutation == "power_on":
            after.start_reason = "accessory_power_on"
        else:
            samples = (
                await session.scalars(select(OBDSample).where(OBDSample.drive_db_id == after.id))
            ).all()
            for sample in samples:
                if mutation == "missing_pid":
                    sample.quality_json = {"missing_pids": [0x0D]}
                elif mutation == "nonlive":
                    sample.ecu_data_status = "stale"
                else:
                    sample.vehicle_speed_kmh = None
        await session.flush()
        assert (await recording_gap_context(session, [after]))[after.id][
            "recording_gap_before"
        ] is None


async def test_stationary_endpoint_is_uncertain_not_proven_continuous_drive(db_session, tmp_path):
    await _pair(tmp_path, speed_after=0)
    async with session_scope() as session:
        after = await session.scalar(select(OBDDrive).where(OBDDrive.drive_id == "after"))
        gap = (await recording_gap_context(session, [after]))[after.id]["recording_gap_before"]
        assert gap["classification"] == "unconfirmed_interruption"
        assert not gap["moving_after"]


async def test_arrival_order_and_later_backfill_use_immediate_chronological_neighbor(
    db_session, tmp_path
):
    await _segment(tmp_path, "after", 40, 45)
    await _segment(tmp_path, "before", 0, 5)
    async with session_scope() as session:
        after = await session.scalar(select(OBDDrive).where(OBDDrive.drive_id == "after"))
        assert (await recording_gap_context(session, [after]))[after.id]["recording_gap_before"]
    # Never leap over this newly imported, different-logger recording to retain a
    # previously plausible association. Neither source projection needs rewriting.
    await _segment(tmp_path, "middle", 15, 20, logger="different-logger")
    async with session_scope() as session:
        after = await session.scalar(select(OBDDrive).where(OBDDrive.drive_id == "after"))
        assert (await recording_gap_context(session, [after]))[after.id][
            "recording_gap_before"
        ] is None


async def test_response_completion_and_invalid_required_timing(db_session, tmp_path):
    await _pair(tmp_path, plan_before=8, plan_after=8)
    async with session_scope() as session:
        rows = (await session.scalars(select(OBDDrive))).all()
        before, after = sorted(rows, key=lambda row: row.started_at)
        gap = (await recording_gap_context(session, [after]))[after.id]["recording_gap_before"]
        assert gap["timestamp_basis"] == "response_completion"
        for sample in (
            await session.scalars(select(OBDSample).where(OBDSample.drive_db_id == after.id))
        ).all():
            sample.quality_json = {"transport": "ok"}
        await session.flush()
        assert (await recording_gap_context(session, [after]))[after.id][
            "recording_gap_before"
        ] is None
        assert before.gap_count == 1  # Timed 5s fixture rows exceed this plan's 4.5s threshold.


async def test_mixed_timing_boundary_uses_row_times_consistently(db_session, tmp_path):
    await _pair(tmp_path, plan_before=8)
    async with session_scope() as session:
        drives = (await session.scalars(select(OBDDrive))).all()
        before, after = sorted(drives, key=lambda row: row.started_at)
        sample = await session.scalar(
            select(OBDSample).where(OBDSample.drive_db_id == before.id, OBDSample.sequence == 1)
        )
        sample.quality_json = {"pid_response_age_ms": {"0D": 1500}}
        await session.flush()
        gap = (await recording_gap_context(session, [after]))[after.id]["recording_gap_before"]
        assert gap["timestamp_basis"] == "row_observation"
        assert gap["previous_observation_at"] == (BASE + timedelta(seconds=5)).isoformat()
        assert gap["observation_interval_s"] == pytest.approx(35.068787)


async def test_producer_duration_survives_reprocessing_old_unknown_stays_unknown(
    db_session, tmp_path
):
    await _segment(tmp_path, "old", 0, 5)
    async with session_scope() as session:
        drive = await session.scalar(select(OBDDrive))
        assert missing_duration_metadata(drive)["producer_missing_data_duration_s"] == 4.25
        assert not (await reconcile_drive_projection(session, drive))["changed"]
        assert missing_duration_metadata(drive)["producer_missing_data_duration_s"] == 4.25
        old_analysis = dict(drive.gap_analysis_json)
        old_analysis.pop("producer_summary")
        drive.gap_analysis_json = old_analysis
        assert missing_duration_metadata(drive)["producer_missing_data_duration_s"] is None
        await reconcile_drive_projection(session, drive)
        assert missing_duration_metadata(drive)["producer_missing_data_duration_s"] is None


async def test_empty_context_does_not_query_for_neighbors(db_session):
    assert await recording_gap_context(db_session, []) == {}


@pytest.mark.parametrize(
    "category, expected",
    [
        ("optional_pid_04_timeout_recovered", 0),
        ("optional_pid_0E_timeout_recovered", 0),
        ("command_timeout", 1),
        ("connection_lost", 1),
        ("optional_pid_0C_timeout_recovered", 1),
        ("optional_pid_0D_timeout_recovered", 1),
        ("optional_pid_10_timeout_recovered", 1),
        ("optional_pid_FF_timeout_recovered", 1),
        ("optional_pid_0e_timeout_recovered", 1),
        ("optional_pid_04_timeout_recovered_extra", 1),
    ],
)
async def test_recovered_optional_timeout_is_not_a_connection_loss(
    db_session, tmp_path, category, expected
):
    await _segment(tmp_path, "loss", 0, 5)
    async with session_scope() as session:
        drive = await session.scalar(select(OBDDrive))
        session.add(
            OBDDiagnostic(
                drive_db_id=drive.id,
                event_hash="a" * 64,
                observed_at=BASE,
                kind="connection_failure",
                payload_json={"category": category, "message": "test"},
            )
        )
        await session.flush()
        await reconcile_drive_projection(session, drive)
        assert drive.connection_loss_count == expected
        assert drive.lifecycle_status == "interrupted"
        assert not drive.gap_analysis_json["lifecycle_evidence"]["shutdown_inferred"]
        # A real connection-lost ending remains positive evidence even when the
        # only retained diagnostic describes a separate optional recovery.
        drive.stop_reason = "connection_lost"
        await reconcile_drive_projection(session, drive)
        assert drive.connection_loss_count == max(1, expected)
        assert drive.lifecycle_status == "interrupted"
