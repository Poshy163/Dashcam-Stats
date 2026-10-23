"""A proved native pre-sleep stop is distinct from a missing recovery watchdog."""

from __future__ import annotations

import asyncio
import subprocess
from dataclasses import replace
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from test_ingest_radio_coordinator import fake_controller as fake_controller
from test_ingest_radio_unit_report import posix_shell as posix_shell
from test_ingest_radios import _WATCHDOG_HANDLE, _arm_and_capture_watchdog

from app.db.models import IngestRadioTransition
from app.db.session import session_scope
from app.ingest import radio_coordinator, radios


async def _command(monkeypatch, operation, handle):
    if operation == "probe":
        return radios._watchdog_probe_command(handle)
    captured = []

    async def shell(_address, command, **_kwargs):
        captured.append(command)
        return ""

    monkeypatch.setattr(radios.adb, "shell", shell)
    assert await radios._renew_watchdog_lease("unit", 300, handle) is False
    return captured[0]


@pytest.mark.parametrize("operation", ["probe", "renew"])
@pytest.mark.parametrize(
    "scenario, expected",
    [
        ("armed", "armed"),
        ("preempt", "window_ending:deadline_approaching"),
        ("restoring", "window_ending:restoring"),
        ("dead", ""),
        ("wrong_token", ""),
        ("wrong_pid", ""),
        ("wrong_cmdline", ""),
        ("missing_flag", ""),
        ("malformed", ""),
        ("future_claim", ""),
        ("past_deadline", ""),
        ("changed_deadline", ""),
        ("lease_expired", ""),
        ("missing_readiness", ""),
        ("lease_restore", ""),
        ("unbounded_claim", ""),
        ("maintenance", "armed"),
        ("stale_maintenance", "window_ending:deadline_approaching"),
        ("unproven_maintenance", "window_ending:deadline_approaching"),
    ],
)
async def test_actual_generated_probe_and_renew_require_owned_bounded_window_evidence(
    monkeypatch, tmp_path, posix_shell, operation, scenario, expected
):
    handle = replace(_WATCHDOG_HANDLE, sleep_deadline_uptime_s=1200)
    command = await _command(monkeypatch, operation, handle)
    # Only platform inputs change: execute the actual production shell decisions,
    # with private paths, simulated /proc files and an explicit live/dead PID read.
    prefix = tmp_path.as_posix() + "/"
    command = command.replace("/data/local/tmp/", prefix)
    command = command.replace('"/proc/$pid/cmdline"', f"'{prefix}cmdline'")
    command = command.replace("/proc/uptime", f"'{prefix}uptime'")
    command = command.replace("/proc/sys/kernel/random/boot_id", f"'{prefix}boot'")

    def filename(value):
        return tmp_path / value.rsplit("/", 1)[1]

    filename(radios.FLAG_PATH).touch()
    filename(radios.WATCHDOG_ACTIVE_PATH).write_text(f"{handle.token} {handle.pid}")
    (tmp_path / "cmdline").write_text(handle.script_path.replace("/data/local/tmp/", prefix))
    now = (
        1124 if scenario in {"armed", "lease_expired"} else 1125 if scenario == "preempt" else 1140
    )
    (tmp_path / "uptime").write_text(f"{now}.00 0.00\n")
    maintenance = "maintenance" in scenario
    if scenario in {"armed", "preempt", "lease_expired"} or maintenance:
        filename(handle.ready_path).write_text("armed")
        filename(handle.lease_path).write_text("1123" if scenario == "lease_expired" else "1500")
    elif scenario != "missing_readiness":
        filename(handle.restoring_path).write_text(
            {
                "malformed": "pre_sleep nonsense 1200",
                "future_claim": "pre_sleep 1141 1200",
                "changed_deadline": "pre_sleep 1140 1201",
                "lease_restore": "lease_expired 1140 1200",
                "unbounded_claim": "pre_sleep 1 1200",
            }.get(scenario, "pre_sleep 1140 1200")
        )
    if scenario == "past_deadline":
        (tmp_path / "uptime").write_text("1200.0 0.0\n")
    elif scenario == "wrong_token":
        filename(radios.WATCHDOG_ACTIVE_PATH).write_text(f"{'f' * 32} {handle.pid}")
    elif scenario == "wrong_pid":
        filename(radios.WATCHDOG_ACTIVE_PATH).write_text(f"{handle.token} {handle.pid + 1}")
    elif scenario == "wrong_cmdline":
        (tmp_path / "cmdline").write_text("different-generation.sh")
    elif scenario == "missing_flag":
        filename(radios.FLAG_PATH).unlink()
    platform = ""
    if maintenance:
        boot = "01234567-89ab-cdef-0123-456789abcdef"
        (tmp_path / "boot").write_text(boot)
        heartbeat = now - 1 if scenario == "stale_maintenance" else now + 30
        filename("/data/local/tmp/.dashcam_stats_maintenance").write_text(
            f"{boot} {'different-boot' if scenario == 'unproven_maintenance' else boot} "
            f"{heartbeat} {now + 180}"
        )
        platform = "settings() { echo 0; }; getprop() { echo true; }; "
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    result = subprocess.run(
        [
            posix_shell,
            "-c",
            platform + f"kill() {{ return {1 if scenario == 'dead' else 0}; }}; " + command,
        ],
        text=True,
        capture_output=True,
        env={"PATH": "/usr/bin:/bin"},
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    if expected == "armed":
        assert result.stdout == (str(handle.pid) if operation == "probe" else "renewed"), (
            result.stderr
        )
        if operation == "renew":
            before[filename(handle.lease_path).name] = f"{now + 300}\n".encode()
    else:
        assert result.stdout == expected, result.stderr
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == before


async def test_actual_native_script_publishes_claim_before_readiness_disappears(
    monkeypatch, tmp_path, posix_shell
):
    report = radios.WatchdogReport(
        host="192.0.2.1",
        port=8199,
        token="a" * 48,
        transition_id="00000000-0000-0000-0000-000000000001",
        sleep_deadline_uptime_s=1200,
    )
    handle, captured = await _arm_and_capture_watchdog(monkeypatch, report=report)
    script = (
        captured["launcher"]
        .split("<<'DASHCAM_RADIO_WATCHDOG'\n", 1)[1]
        .split("\nDASHCAM_RADIO_WATCHDOG\n", 1)[0]
    )
    script = script.replace("/data/local/tmp/", tmp_path.as_posix() + "/")
    flag = tmp_path / radios.FLAG_PATH.rsplit("/", 1)[1]
    flag.touch()
    ready = tmp_path / handle.ready_path.rsplit("/", 1)[1]
    restoring = tmp_path / handle.restoring_path.rsplit("/", 1)[1]
    first_restore = "timeout 6 sh -c 'cmd bluetooth_manager enable || svc bluetooth enable'"
    # Stop at the first would-be hardware effect, after running all real arming,
    # claim/recheck and marker publication logic with a deterministic device clock.
    script = script.replace(
        first_restore, f"[ ! -e '{ready.as_posix()}' ] && cat '{restoring.as_posix()}'; exit 0"
    )
    generated = tmp_path / "generated.sh"
    generated.write_text("cut() { echo 1140; }; " + script, newline="\n")
    result = subprocess.run(
        [posix_shell, generated.as_posix()],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin"},
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "pre_sleep 1140 1200"
    assert not ready.exists()


@pytest.mark.parametrize("state", ["deadline_approaching", "restoring"])
@pytest.mark.parametrize("generation_changed", [False, True])
async def test_actual_arming_probe_attests_pid_and_refuses_a_generation_change(
    tmp_path, posix_shell, state, generation_changed
):
    candidate = replace(_WATCHDOG_HANDLE, pid=0, sleep_deadline_uptime_s=1200)
    prefix = tmp_path.as_posix() + "/"
    command = radios._watchdog_probe_command(candidate, include_ending_pid=True)
    command = command.replace("/data/local/tmp/", prefix)
    command = command.replace('"/proc/$pid/cmdline"', f"'{prefix}cmdline'")
    command = command.replace("/proc/uptime", f"'{prefix}uptime'")

    def filename(path):
        return tmp_path / path.rsplit("/", 1)[1]

    filename(radios.FLAG_PATH).touch()
    active = filename(radios.WATCHDOG_ACTIVE_PATH)
    active.write_text(f"{candidate.token} {_WATCHDOG_HANDLE.pid}")
    (tmp_path / "cmdline").write_text(candidate.script_path.replace("/data/local/tmp/", prefix))
    now = 1140 if state == "restoring" else 1125
    (tmp_path / "uptime").write_text(f"{now}.0 0.0\n")
    if state == "restoring":
        filename(candidate.restoring_path).write_text("pre_sleep 1140 1200")
    else:
        filename(candidate.ready_path).write_text("armed")
        filename(candidate.lease_path).write_text("1500")
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    # Change active ownership after the initial PID/cmdline read, during device-clock
    # observation. The production final generation check must reject this older proof.
    swap = (
        f"cut() {{ printf '{'f' * 32} 9999' > '{active.as_posix()}'; echo {now}; }}; "
        if generation_changed
        else ""
    )
    result = subprocess.run(
        [posix_shell, "-c", 'kill() { [ "$1" = -0 ]; }; ' + swap + command],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin"},
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == (
        "" if generation_changed else f"window_ending:{state}:{_WATCHDOG_HANDLE.pid}"
    ), result.stderr
    if generation_changed:
        before[active.name] = f"{'f' * 32} 9999".encode()
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == before


@pytest.mark.parametrize("state", ["deadline_approaching", "restoring"])
async def test_arming_window_end_retains_the_actual_pid_without_discard_or_renewal(
    monkeypatch, state
):
    candidate = replace(_WATCHDOG_HANDLE, pid=0)
    script = AsyncMock(return_value="")
    discard = AsyncMock()
    shell = AsyncMock(return_value=f"window_ending:{state}:{_WATCHDOG_HANDLE.pid}")
    monkeypatch.setattr(radios, "_new_watchdog_handle", lambda: candidate)
    monkeypatch.setattr(radios, "_shell_script", script)
    monkeypatch.setattr(radios, "_discard_watchdog_candidate", discard)
    monkeypatch.setattr(radios.adb, "shell", shell)
    with pytest.raises(radios.WatchdogWindowEnding) as caught:
        await radios._arm_watchdog("unit", 300)
    assert caught.value.recovery_state == state
    assert caught.value.watchdog_handle == _WATCHDOG_HANDLE
    assert shell.await_count == script.await_count == 1
    discard.assert_not_awaited()


@pytest.mark.parametrize("state", ["deadline_approaching", "restoring"])
async def test_controller_keeps_an_arming_window_end_until_explicit_verified_standdown(
    monkeypatch, state
):
    handle = replace(_WATCHDOG_HANDLE, sleep_deadline_uptime_s=1200)
    arm = AsyncMock(side_effect=radios.WatchdogWindowEnding(state, watchdog_handle=handle))
    shell = AsyncMock(return_value="cleared")
    monkeypatch.setattr(radios, "_arm_watchdog", arm)
    monkeypatch.setattr(radios, "_persist_marker", AsyncMock())
    monkeypatch.setattr(radios, "_remove_flag", AsyncMock())
    monkeypatch.setattr(radios.adb, "shell", shell)
    controller = radios.RadioController("unit", watchdog_deadline_s=300)
    controller.claim()
    try:
        with pytest.raises(radios.WatchdogWindowEnding):
            await controller._ensure_watchdog_locked(marker="bluetooth")
        assert controller._watchdog is handle
        assert controller._watchdog_lost.is_set()
        assert controller._watchdog_proof.window_ending_state == state
        assert controller._watchdog_proof.valid_until == 0
        assert controller._watchdog_renewal_task is None
        commands = [call.args[1] for call in shell.await_args_list]
        assert commands == [f"touch '{radios.FLAG_PATH}'"]
        with pytest.raises(radios.WatchdogWindowEnding):
            await controller._ensure_watchdog_locked(marker="bluetooth")
        assert arm.await_count == 1
        assert shell.await_count == 1
        # Coordinator calls this only after all baseline restoration checks pass.
        assert await controller.stand_down_watchdog()
        cleanup = shell.await_args.args[1]
        assert cleanup == radios._watchdog_owner_cleanup_command(handle, prove=True)
        assert f'[ "$state" = "{handle.token} {handle.pid}" ]' in cleanup
        assert '"/proc/$pid/cmdline"' in cleanup
        assert controller._watchdog is None
    finally:
        await controller.release()


@pytest.mark.parametrize("state", ["deadline_approaching", "restoring"])
async def test_legacy_quiet_retains_an_arming_stop_until_bluetooth_restore_is_verified(
    monkeypatch, state
):
    arm = AsyncMock(
        side_effect=radios.WatchdogWindowEnding(state, watchdog_handle=_WATCHDOG_HANDLE)
    )
    shell = AsyncMock(return_value="cleared")
    monkeypatch.setattr(radios, "_arm_watchdog", arm)
    monkeypatch.setattr(radios, "_persist_marker", AsyncMock())
    monkeypatch.setattr(radios, "_bluetooth_is_on", AsyncMock(return_value=True))
    monkeypatch.setattr(radios, "_set_bluetooth", AsyncMock(return_value=True))
    monkeypatch.setattr(radios, "_confirm_bluetooth_on", AsyncMock(side_effect=[False, True]))
    monkeypatch.setattr(radios, "new_quieting_allowed", lambda: True)
    monkeypatch.setattr(radios.adb, "shell", shell)
    quiet = radios.RadioQuiet("unit", online_for=60, watchdog_deadline_s=300)
    with pytest.raises(radios.WatchdogWindowEnding):
        await quiet._quiet_bluetooth()
    assert quiet._watchdog is _WATCHDOG_HANDLE
    assert quiet._watchdog_lost.is_set()
    assert quiet._watchdog_renewal_task is None
    assert quiet.bluetooth_off
    assert shell.await_count == 1  # Only the initial flag; no premature cleanup.
    await quiet.finish()
    assert quiet._watchdog is _WATCHDOG_HANDLE
    assert quiet.bluetooth_off
    assert shell.await_count == 1  # An unverified restore leaves recovery intact.
    await quiet.finish()
    assert shell.await_count == 2
    assert shell.await_args.args[1] == radios._watchdog_owner_cleanup_command(
        _WATCHDOG_HANDLE, prove=True
    )
    assert quiet._watchdog is None
    assert not quiet.bluetooth_off


@pytest.mark.parametrize(
    "reply",
    [
        "window_ending:restoring",
        "window_ending:restoring:0",
        "window_ending:restoring:-1",
        "window_ending:restoring:2147483648",
        "window_ending:restoring:4242 extra",
        "window_ending:unknown:4242",
    ],
)
def test_arming_window_end_requires_an_exact_positive_pid_reply(reply):
    candidate = replace(_WATCHDOG_HANDLE, pid=0)
    radios._watchdog_ending_reply(reply, watchdog_handle=candidate, require_pid=True)


@pytest.mark.parametrize("state", ["deadline_approaching", "restoring"])
async def test_positive_window_end_never_becomes_health_or_a_renewal(monkeypatch, state):
    async def shell(*_args, **_kwargs):
        return f"window_ending:{state}"

    monkeypatch.setattr(radios.adb, "shell", shell)
    with pytest.raises(radios.WatchdogWindowEnding) as caught:
        await radios._watchdog_is_armed("unit", _WATCHDOG_HANDLE)
    assert caught.value.recovery_state == state
    with pytest.raises(radios.WatchdogWindowEnding):
        await radios._renew_watchdog_lease("unit", 300, _WATCHDOG_HANDLE)
    proof = radios.WatchdogLeaseProof(valid_until=10000, confirmed_at=1)
    lost = asyncio.Event()
    monkeypatch.setattr(radios, "_watchdog_renew_interval", lambda _deadline: 0)
    await radios._watchdog_renewal_loop("unit", 300, _WATCHDOG_HANDLE, lost, proof)
    assert lost.is_set()
    assert proof.valid_until == 10000
    assert proof.confirmed_at == 1
    assert proof.window_ending_state == state
    assert not proof.permits_retry()


@pytest.mark.parametrize("state", ["deadline_approaching", "restoring"])
async def test_expected_heartbeat_stop_keeps_type_and_restoration_authority(
    db_session, fake_controller, monkeypatch, state
):
    async def ending(_self):
        raise radios.WatchdogWindowEnding(state)

    monkeypatch.setattr(fake_controller, "watchdog_healthy", ending)
    monkeypatch.setattr(radio_coordinator, "HEARTBEAT_INTERVAL_S", 0.001)
    cancelled = asyncio.Event()
    transition = await radio_coordinator.begin(
        trigger="auto",
        address="unit:5555",
        logger_status=None,
        logger_status_path=None,
        watchdog_deadline_s=300,
        lease_loss_callback=cancelled.set,
    )
    try:
        error = await asyncio.wait_for(transition.wait_for_lease_loss(), timeout=2)
        assert isinstance(error, radio_coordinator.RadioWindowEnding)
        assert error.reason_code == "native_sleep_deadline"
        assert error.recovery_state == state
        assert cancelled.is_set() and transition.process_fence.held
        with pytest.raises(radio_coordinator.RadioWindowEnding):
            transition.raise_if_lease_lost()
        assert await transition.restore()
        async with session_scope() as session:
            row = await session.get(IngestRadioTransition, transition.id)
            assert row.phase == "complete" and not row.active and not row.recovery_required
    finally:
        await transition.close()


@pytest.mark.parametrize("case", ["recent", "stale", "future", "wrong_source", "wrong_reason"])
async def test_completed_generation_report_race_requires_recent_authenticated_sleep_evidence(
    db_session, fake_controller, case
):
    transition = await radio_coordinator.begin(
        trigger="auto",
        address="unit:5555",
        logger_status=None,
        logger_status_path=None,
        watchdog_deadline_s=300,
    )
    try:
        now = radio_coordinator._now()
        await transition.checkpoint(
            restore_evidence_source="server" if case == "wrong_source" else "unit",
            unit_sleep_reported_at=(
                None
                if case == "wrong_reason"
                else now - timedelta(seconds=76)
                if case == "stale"
                else now + timedelta(seconds=10)
                if case == "future"
                else now
            ),
        )
        await transition._signal_watchdog_stop("detached watchdog missing")
        assert isinstance(transition.lease_loss_error, radio_coordinator.RadioWindowEnding) is (
            case == "recent"
        )
        if case == "recent":
            assert transition.lease_loss_error.recovery_state == "unit_report"
        assert await transition.restore()
        async with session_scope() as session:
            row = await session.get(IngestRadioTransition, transition.id)
            assert row.phase == ("complete" if case == "recent" else "failed")
    finally:
        await transition.close()


async def test_direct_native_preparation_stop_restores_without_a_false_failed_transition(
    db_session, fake_controller
):
    transition = await radio_coordinator.begin(
        trigger="auto",
        address="unit:5555",
        logger_status=None,
        logger_status_path=None,
        watchdog_deadline_s=300,
    )
    try:
        await transition.checkpoint(bluetooth_before="on", bluetooth_disable_attempted=True)
        assert await transition.restore(error=radios.WatchdogWindowEnding("deadline_approaching"))
        async with session_scope() as session:
            row = await session.get(IngestRadioTransition, transition.id)
            assert row.phase == "complete" and not row.active and not row.recovery_required
            assert row.bluetooth_restore_verified
    finally:
        await transition.close()


@pytest.mark.parametrize("failure", ["explicit_error", "restoration_failure"])
async def test_expected_window_end_cannot_hide_a_real_failure(db_session, fake_controller, failure):
    transition = await radio_coordinator.begin(
        trigger="auto",
        address="unit:5555",
        logger_status=None,
        logger_status_path=None,
        watchdog_deadline_s=300,
    )
    try:
        await transition.checkpoint(bluetooth_before="on", bluetooth_disable_attempted=True)
        transition._signal_lease_loss(radio_coordinator.RadioWindowEnding("restoring"))
        if failure == "restoration_failure":
            fake_controller.bluetooth_ok = False
            assert not await transition.restore()
        else:
            assert await transition.restore(error=RuntimeError("real transfer failure"))
        async with session_scope() as session:
            row = await session.get(IngestRadioTransition, transition.id)
            assert row.phase == (
                "recovery_required" if failure == "restoration_failure" else "failed"
            )
            assert row.last_error
            if failure == "explicit_error":
                assert "real transfer failure" in row.last_error
    finally:
        await transition.close()


@pytest.mark.parametrize(
    "error_type", [radios.WatchdogWindowEnding, radio_coordinator.RadioWindowEnding]
)
async def test_later_expected_window_end_cannot_erase_an_existing_real_lease_failure(
    db_session, fake_controller, error_type
):
    transition = await radio_coordinator.begin(
        trigger="auto",
        address="unit:5555",
        logger_status=None,
        logger_status_path=None,
        watchdog_deadline_s=300,
    )
    try:
        transition._signal_lease_loss("original dead watchdog")
        assert await transition.restore(error=error_type("restoring"))
        async with session_scope() as session:
            row = await session.get(IngestRadioTransition, transition.id)
            assert row.phase == "failed"
            assert "original dead watchdog" in row.last_error
    finally:
        await transition.close()
