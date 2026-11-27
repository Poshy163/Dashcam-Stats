from datetime import timedelta

from test_obd_server_import import BASE, _sample, make_bundle

from app.db.models import OBDLoggerEvent
from app.db.session import session_scope
from app.ingest.obd_bundle import store_validated_bundle, validate_bundle

DRIVE = "drive_ignition_coverage"
URL = f"/api/obd/drives/{DRIVE}/ignition-coverage"


def event(seq, at, reason, *, source="camera", session="boot", anchor=False):
    return OBDLoggerEvent(
        source_id_hash=source,
        session_id_hash=session,
        sequence=seq,
        occurred_at=BASE + timedelta(seconds=at),
        kind="drive.lifecycle" if anchor else "app.service",
        outcome="started" if anchor else "observed",
        reason_code=reason,
        drive_id=DRIVE if anchor else None,
        level="info",
        app_version_name="0.3.30",
        app_version_code=43,
        build_git_sha="unknown",
    )


async def store_samples(path, samples):
    finished = samples[-1]["timestamp_utc"]
    lifecycle = {
        "last_sample_at_utc": finished,
        "termination_noticed_at_utc": finished,
        "finalised_at_utc": finished,
        "interruption_reason": None,
        "completion_status": "complete",
    }
    bundle = make_bundle(
        path,
        DRIVE,
        samples=samples,
        manifest_patch={
            **lifecycle,
            "poll_plan_version": 7,
            "last_successful_obd_response_at_utc": finished,
        },
        summary_patch=lifecycle,
    )
    async with session_scope() as session:
        await store_validated_bundle(session, validate_bundle(bundle))


async def test_observed_shutdown_rows_and_late_journal(db_session, client, tmp_path):
    # Same shape as the short outbound trip. Static fields are carried into the
    # twelve shutdown rows, which only have a new adapter-voltage reading.
    samples = [_sample(DRIVE, index, telemetry=index < 58) for index in range(70)]
    for row in samples[58:]:
        row.update(adapter_voltage=12.7, obd_standard="EOBD", oxygen_sensors_present=[1, 2])
    await store_samples(tmp_path, samples)
    before = (await client.get(URL)).json()
    assert before["unknown"] == before["total"]
    assert before["startup"]["acc_to_first_speed_rpm_s"] is None
    assert before["startup"]["drive_to_first_speed_rpm_s"] == 0
    assert before["total"]["rows"] == 70
    async with session_scope() as session:
        later_export = event(11, 450, "export_completed", session="next-boot", anchor=True)
        later_export.kind = "bundle.export"
        session.add(later_export)
        session.add_all(
            [
                event(1, -22, "acc_on"),
                event(2, -18, "acc_on"),
                event(3, 0, "engine_detected", anchor=True),
                event(4, 58 * 5, "acc_off"),
                event(1, 60, "acc_off", source="another-car"),
                event(10, 40, "acc_off", session="another-boot"),
            ]
        )
    after = (await client.get(URL)).json()
    assert after["acc_on"] == {
        "rows": 58,
        "speed_rows": 58,
        "rpm_rows": 58,
        "speed_and_rpm_rows": 58,
        "voltage_rows": 58,
        "voltage_only_rows": 0,
    }
    assert after["acc_off"] == {
        "rows": 12,
        "speed_rows": 0,
        "rpm_rows": 0,
        "speed_and_rpm_rows": 0,
        "voltage_rows": 12,
        "voltage_only_rows": 12,
    }
    assert after["unknown"]["rows"] == 0
    assert after["total"] == before["total"]
    assert after["startup"]["acc_to_first_speed_rpm_s"] == 18


async def test_unobserved_prefix_ignition_restart_and_partial_ecu_rows(
    db_session, client, tmp_path
):
    samples = [_sample(DRIVE, index, telemetry=False) for index in range(6)]
    samples[0].update(engine_rpm=1500, vehicle_speed=30)
    samples[1].update(adapter_voltage=12.5)
    samples[2].update(adapter_voltage=12.5, coolant_temperature=85)
    samples[3].update(adapter_voltage=12.5, vehicle_speed=0)
    samples[4].update(adapter_voltage=13.9, vehicle_speed=0, engine_rpm=0)
    samples[5].update(ecu_data_status="last_known", vehicle_speed=25, engine_rpm=1250)
    await store_samples(tmp_path, samples)
    async with session_scope() as session:
        session.add_all(
            [
                event(1, 0, "engine_detected", anchor=True),
                event(2, 5, "acc_off"),
                event(3, 15, "acc_on"),
            ]
        )
    result = (await client.get(URL)).json()
    assert result["unknown"]["speed_and_rpm_rows"] == result["unknown"]["rows"] == 1
    assert result["startup"]["acc_to_first_speed_rpm_s"] is None
    assert result["acc_off"]["voltage_only_rows"] == 1
    assert result["acc_off"]["rows"] == 2
    assert result["acc_on"] == {
        "rows": 3,
        "speed_rows": 2,
        "rpm_rows": 1,
        "speed_and_rpm_rows": 1,
        "voltage_rows": 2,
        "voltage_only_rows": 0,
    }
    # A colliding drive identifier from a second source invalidates the journal link.
    async with session_scope() as session:
        session.add(event(2, 0, "engine_detected", source="another-car", anchor=True))
    ambiguous = (await client.get(URL)).json()
    assert ambiguous["unknown"] == ambiguous["total"]
    assert ambiguous["startup"]["stages"] == {}
    assert (await client.get("/api/obd/drives/no_such_drive/ignition-coverage")).status_code == 404


async def test_missing_journal_sequence_expires_previous_acc_state(db_session, client, tmp_path):
    await store_samples(tmp_path, [_sample(DRIVE, i) for i in range(4)])
    async with session_scope() as session:
        session.add_all(
            [
                event(1, -10, "acc_on"),
                event(2, 0, "engine_detected", anchor=True),
                event(5, 10, "acc_off"),
                event(6, 20, "engine_stopped", anchor=True),
            ]
        )
    result = (await client.get(URL)).json()
    assert result["acc_on"]["rows"] == 1
    assert result["unknown"]["rows"] == 1
    assert result["acc_off"]["rows"] == 2


async def test_startup_uses_first_valid_pair_and_only_final_connection(
    db_session, client, tmp_path
):
    samples = [_sample(DRIVE, i, telemetry=False) for i in range(5)]
    samples[0].update(adapter_voltage=13.8)
    samples[1].update(ecu_data_status="last_known", engine_rpm=800, vehicle_speed=10)
    samples[2].update(engine_rpm=800, vehicle_speed=0)
    samples[2]["quality"]["missing_pids"] = [13]
    samples[3].update(engine_rpm=800, vehicle_speed=0)
    samples[3]["quality"]["pid_response_age_ms"] = {"0C": 100}
    samples[4].update(engine_rpm=0, vehicle_speed=0)
    await store_samples(tmp_path, samples)

    def stage(seq, at, kind, outcome, reason):
        entry = event(seq, at, reason)
        entry.kind, entry.outcome = kind, outcome
        return entry

    async with session_scope() as session:
        session.add_all(
            [
                event(1, -27, "acc_on"),
                stage(2, -21, "obd.ble_connection", "succeeded", "notifications_ready"),
                stage(3, -20, "obd.elm_session", "succeeded", "elm_ready"),
                stage(4, -19, "obd.ecu_session", "skipped", "voltage_below_stop"),
                stage(5, -7, "obd.ble_connection", "succeeded", "notifications_ready"),
                stage(6, -5, "obd.elm_session", "succeeded", "elm_ready"),
                stage(7, -4, "obd.elm_session", "retrying", "voltage_below_stop"),
                stage(8, -1, "obd.ecu_session", "succeeded", "ecu_proof_valid"),
                event(9, 0, "engine_detected", anchor=True),
            ]
        )
    result = (await client.get(URL)).json()["startup"]
    assert result["drive_to_first_speed_rpm_s"] == 20
    assert result["acc_to_first_speed_rpm_s"] == 47
    assert result["low_voltage_parked_waits"] == result["startup_voltage_rechecks"] == 1
    assert result["stages"] == {"bluetooth_ready_s": 20, "adapter_ready_s": 22, "ecu_ready_s": 26}


async def test_gap_before_pair_and_restarted_recording_have_no_ignition_latency(
    db_session, client, tmp_path
):
    samples = [_sample(DRIVE, i, telemetry=i == 2) for i in range(3)]
    await store_samples(tmp_path, samples)
    async with session_scope() as session:
        session.add_all(
            [
                event(1, -20, "acc_on"),
                event(3, 0, "engine_detected", anchor=True),
            ]
        )
    startup = (await client.get(URL)).json()["startup"]
    assert startup["acc_to_first_speed_rpm_s"] is None
    assert startup["drive_to_first_speed_rpm_s"] == 10
    async with session_scope() as session:
        prior_drive = event(2, -10, "engine_detected", anchor=True)
        prior_drive.drive_id = "other_recording"
        session.add(prior_drive)
    startup = (await client.get(URL)).json()["startup"]
    assert startup["observed_acc_on_at"] is None
    assert startup["drive_to_first_speed_rpm_s"] == 10
