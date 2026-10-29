"""Expensive codec tracing needs an explicit, non-renewing diagnostic lease."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.ingest import carplay_timing as timing


@pytest.fixture(autouse=True)
async def isolated_policy(monkeypatch):
    await timing.shutdown()
    timing.reset_for_tests()
    monkeypatch.setattr(timing, "_enabled", lambda: True)
    monkeypatch.setattr(timing, "recover_on_unit_present", lambda _address: None)
    yield
    await timing.shutdown()
    timing.reset_for_tests()


def settings(monkeypatch, values):
    values.setdefault("ingest.unit_logs", True)
    monkeypatch.setattr(
        timing, "get_settings_service", lambda: SimpleNamespace(get_nowait=values.get)
    )


async def settle():
    for _ in range(8):
        await asyncio.sleep(0)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, 0),
        (0, 0),
        (1000, 0),
        (999, 0),
        (True, 0),
        ("1900", 0),
        (1900.5, 0),
        (1900, 1900),
        (2800, 2800),
        (2801, 0),
    ],
)
def test_only_an_explicit_bounded_unexpired_integer_lease_is_accepted(monkeypatch, value, expected):
    settings(monkeypatch, {timing.CODEC_TRACE_UNTIL_KEY: value})
    monkeypatch.setattr(timing.time, "time", lambda: 1000)
    assert timing.codec_trace_until_unix_s() == expected


def test_server_restart_does_not_extend_a_saved_lease(monkeypatch):
    settings(monkeypatch, {timing.CODEC_TRACE_UNTIL_KEY: 1900})
    monkeypatch.setattr(timing.time, "time", lambda: 1000)
    assert timing.codec_trace_until_unix_s() == 1900
    timing.reset_for_tests()
    monkeypatch.setattr(timing.time, "time", lambda: 1900)
    assert timing.codec_trace_until_unix_s() == 0


@pytest.mark.parametrize("deadline", [0, 1900])
async def test_lease_is_part_of_launch_and_verified_runtime_fingerprint(monkeypatch, deadline):
    settings(monkeypatch, {timing.CODEC_TRACE_UNTIL_KEY: deadline})
    monkeypatch.setattr(timing.time, "time", lambda: 1000)
    sent = []

    async def shell(_address, command, **_kwargs):
        sent.append(command)
        return "sampler_verified\n" if "echo sampler_verified" in command else ""

    monkeypatch.setattr(timing.adb, "shell", shell)
    assert await timing.arm("unit")
    launch = next(c for c in sent if "setsid" in c)
    assert f" 15 e 3 {deadline} </dev/null" in launch
    verify = next(c for c in sent if "echo sampler_verified" in c)
    assert f"{timing.bundle_id()}:15:3:{deadline}" in verify


async def test_changed_policy_bypasses_debounce_without_rearming_identical_backup_polls(
    monkeypatch,
):
    values = {timing.CODEC_TRACE_UNTIL_KEY: 0}
    settings(monkeypatch, values)
    now = [1000.0]
    monkeypatch.setattr(
        timing, "time", SimpleNamespace(time=lambda: 1000, monotonic=lambda: now[0])
    )
    calls = []

    async def arm(_address):
        calls.append(timing.codec_trace_until_unix_s())
        return True

    monkeypatch.setattr(timing, "arm", arm)
    timing.reconcile_on_unit_present("unit", config_only=True)
    await settle()
    for expiry in (1900, 0):
        values[timing.CODEC_TRACE_UNTIL_KEY] = expiry
        timing.reconcile_on_unit_present("unit", config_only=True)
        await settle()
    assert calls == [0, 1900, 0]
    now[0] += 10000
    timing.reconcile_on_unit_present("unit", config_only=True)
    await settle()
    assert calls == [0, 1900, 0], "backup polls do not periodically replace a healthy sampler"


async def test_failed_config_reconciliation_retries_after_30_seconds_during_backup(monkeypatch):
    settings(monkeypatch, {})
    now = [1000.0]
    monkeypatch.setattr(
        timing, "time", SimpleNamespace(time=lambda: 1000, monotonic=lambda: now[0])
    )
    calls = []

    async def arm(_address):
        calls.append(now[0])
        return len(calls) > 1

    monkeypatch.setattr(timing, "arm", arm)
    timing.reconcile_on_unit_present("unit", config_only=True)
    await settle()
    now[0] += 29
    timing.reconcile_on_unit_present("unit", config_only=True)
    await settle()
    assert len(calls) == 1
    now[0] += 1
    timing.reconcile_on_unit_present("unit", config_only=True)
    await settle()
    assert len(calls) == 2
    now[0] += 10000
    timing.reconcile_on_unit_present("unit", config_only=True)
    await settle()
    assert len(calls) == 2


async def test_policy_changed_during_an_upload_is_reconciled_on_the_next_presence(monkeypatch):
    values = {timing.CODEC_TRACE_UNTIL_KEY: 0}
    settings(monkeypatch, values)
    monkeypatch.setattr(timing.time, "time", lambda: 1000)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def arm(_address):
        calls.append(timing.codec_trace_until_unix_s())
        entered.set()
        await release.wait()
        return True

    monkeypatch.setattr(timing, "arm", arm)
    timing.reconcile_on_unit_present("unit", config_only=True)
    await entered.wait()
    values[timing.CODEC_TRACE_UNTIL_KEY] = 1900
    timing.reconcile_on_unit_present("unit", config_only=True)
    assert calls == [0]
    release.set()
    await settle()
    timing.reconcile_on_unit_present("unit", config_only=True)
    await settle()
    assert calls == [0, 1900]


async def test_explicit_stop_still_runs_when_passive_diagnostics_were_disabled(monkeypatch):
    values = {timing.CODEC_TRACE_UNTIL_KEY: 1900}
    settings(monkeypatch, values)
    monkeypatch.setattr(timing.time, "time", lambda: 1000)
    enabled = [True]
    monkeypatch.setattr(timing, "_enabled", lambda: enabled[0])
    calls = []

    async def arm(_address):
        calls.append("arm")
        return True

    async def stop(_address):
        calls.append("stop")
        return True

    monkeypatch.setattr(timing, "arm", arm)
    monkeypatch.setattr(timing, "stop", stop)
    timing.reconcile_on_unit_present("unit", config_only=True)
    await settle()
    enabled[0] = False
    values[timing.CODEC_TRACE_UNTIL_KEY] = 0
    timing.reconcile_on_unit_present("unit", config_only=True, force_off=True)
    await settle()
    assert calls == ["arm", "stop"]


async def test_unit_log_disable_removes_trace_eligibility_without_hiding_persisted_lease(
    monkeypatch,
):
    values = {timing.CODEC_TRACE_UNTIL_KEY: 1900, "ingest.unit_logs": False}
    settings(monkeypatch, values)
    monkeypatch.setattr(timing.time, "time", lambda: 1000)
    sent = []

    async def shell(_address, command, **_kwargs):
        sent.append(command)
        return "sampler_verified\n" if "echo sampler_verified" in command else ""

    monkeypatch.setattr(timing.adb, "shell", shell)
    assert timing.codec_trace_until_unix_s() == 1900
    assert await timing.arm("unit")
    assert " 15 e 3 0 </dev/null" in next(c for c in sent if "setsid" in c)


async def test_offline_delete_then_disabled_online_presence_stops_only_and_retries(
    client, monkeypatch
):
    from app.api.routes import ingest_carplay_trace as routes
    from app.core.settings_service import get_settings_service

    service = get_settings_service()
    await service.set_many(
        {"ingest.unit_adb_address": "192.0.2.10:5555", timing.ENABLED_KEY: False}
    )
    await service.set(timing.CODEC_TRACE_UNTIL_KEY, int(time.time()) + 900, internal=True)
    monkeypatch.setattr(routes, "get_status", lambda: SimpleNamespace(unit_online=False))
    monkeypatch.setattr(timing, "_enabled", lambda: service.get_nowait(timing.ENABLED_KEY))
    now = [1000.0]
    monkeypatch.setattr(timing, "time", SimpleNamespace(monotonic=lambda: now[0], time=time.time))
    calls = []

    async def stop(address):
        calls.append(address)
        return len(calls) > 1

    async def never_arm(_address):
        raise AssertionError("disabled passive sampling must not be restarted")

    monkeypatch.setattr(timing, "stop", stop)
    monkeypatch.setattr(timing, "arm", never_arm)
    assert (await client.delete("/api/ingest/carplay-trace")).status_code == 200
    assert service.get_nowait(timing.CODEC_TRACE_UNTIL_KEY) == 0
    assert calls == []
    timing.on_unit_present("192.0.2.10:5555")
    await settle()
    assert len(calls) == 1
    now[0] += 29
    timing.reconcile_on_unit_present("192.0.2.10:5555", config_only=True)
    await settle()
    assert len(calls) == 1
    now[0] += 1
    timing.reconcile_on_unit_present("192.0.2.10:5555", config_only=True)
    await settle()
    assert len(calls) == 2
    now[0] += 1000
    timing.reconcile_on_unit_present("192.0.2.10:5555", config_only=True)
    await settle()
    assert len(calls) == 2


@pytest.mark.parametrize(
    "flag", ["codec_watchdog_fired", "codec_trace_requested", "codec_trace_active"]
)
@pytest.mark.parametrize("raw", [None, "0", "1", "2", "NaN", "PRIVATE"])
def test_policy_and_watchdog_flags_remain_nullable_and_allowlisted(flag, raw):
    message = "schema=9 | event=codec_trace_policy"
    if raw is not None:
        message += f" {flag}={raw}"
    row = timing.parse_event(datetime.now(UTC), message)
    assert row[flag] == (int(raw) if raw in {"0", "1"} else None)


@pytest.mark.parametrize("raw", ["-1", "1.5", "NaN", "PRIVATE"])
def test_invalid_expiry_observations_are_unavailable(raw):
    row = timing.parse_event(
        datetime.now(UTC),
        f"schema=9 | event=codec_trace_policy codec_trace_until_epoch_s={raw} "
        f"codec_trace_remaining_s={raw}",
    )
    assert row["codec_trace_until_epoch_s"] is None
    assert row["codec_trace_remaining_s"] is None


async def test_retained_policy_and_watchdog_observations_round_trip_through_http_api(client):
    from app.ingest import unit_logs

    stamp = datetime.now(UTC).replace(microsecond=0)
    messages = [
        "sample=policy-http-1 session=policy-http schema=9 acc=1 "
        "codec_trace_requested=1 codec_trace_active=1 codec_trace_until_epoch_s=1900 "
        "codec_trace_remaining_s=120 | event=codec_trace_policy codec_watchdog_fired=1",
        "sample=policy-http-legacy session=policy-http schema=6 "
        "| event=codec_capture codec_capture_status=ok codec_trace_off=1",
    ]
    records = timing.parse_sampler_file(
        "\n".join(f"{stamp:%Y-%m-%dT%H:%M:%SZ} {message}" for message in messages)
    )
    assert await unit_logs.store(records) == (2, 0)
    assert await unit_logs.store(records) == (0, 2)
    response = await client.get("/api/unit-logs/carplay-timing", params={"hours": 1})
    assert response.status_code == 200
    data = response.json()
    assert data["sampler_schema"] == timing.SAMPLER_SCHEMA
    assert data["samples"] == []
    policy = next(row for row in data["events"] if row["kind"] == "codec_trace_policy")
    expected = {
        "codec_trace_requested": 1,
        "codec_trace_active": 1,
        "codec_trace_until_epoch_s": 1900,
        "codec_trace_remaining_s": 120,
        "codec_watchdog_fired": 1,
    }
    assert {key: policy[key] for key in expected} == expected
    legacy = next(row for row in data["events"] if row["kind"] == "codec_capture")
    assert all(legacy[key] is None for key in expected)


@pytest.fixture(params=["bash", "mksh"])
def shell(request):
    if request.param == "bash" and os.name == "nt":
        found = r"C:\Program Files\Git\bin\bash.exe"
        found = found if Path(found).exists() else None
    else:
        found = shutil.which(request.param)
    if not found:
        pytest.skip(f"{request.param} is unavailable")
    return found


def policy_functions():
    source = timing.script()
    alive_start = source.index("codec_alive() {")
    alive = source[alive_start : source.index("\n}", alive_start) + 3]
    return (
        alive
        + "\n"
        + source[source.index("stop_codec_worker() {") : source.index("# Deadline scheduling")]
    )


def run_shell(shell, tmp_path, source):
    script = tmp_path / "policy.sh"
    script.write_text(source, encoding="utf-8", newline="\n")
    return subprocess.run(
        [shell, str(script)], cwd=tmp_path, text=True, capture_output=True, timeout=10, check=True
    ).stdout.splitlines()


@pytest.mark.parametrize("deadline,expected", [(0, 0), (999, 0), (1900, 1), (2801, 0)])
def test_device_refuses_default_expired_or_excessive_lease(shell, tmp_path, deadline, expected):
    rows = run_shell(
        shell,
        tmp_path,
        f"""
CODEC_TRACE_UNTIL={deadline}; CODEC_TRACE_ACTIVE=0; CODEC_TRACE_EXPIRES_MS=0
date() {{ echo 1000; }}
clock_ms() {{ echo 500000; }}
{policy_functions()}
initialize_codec_trace
codec_trace_policy 1000 500000
printf '%s\\n' "$CODEC_TRACE_FIELDS"
""",
    )
    assert f"codec_trace_active={expected}" in rows[-1]


def test_device_monotonic_cap_prevents_wall_clock_rollback_from_extending_lease(shell, tmp_path):
    rows = run_shell(
        shell,
        tmp_path,
        f"""
CODEC_TRACE_UNTIL=1100; CODEC_TRACE_ACTIVE=0; CODEC_TRACE_EXPIRES_MS=0
date() {{ echo 1000; }}
clock_ms() {{ echo 500000; }}
child_alive() {{ return 1; }}
{policy_functions()}
initialize_codec_trace
codec_trace_policy 900 599000
printf '%s\\n' "$CODEC_TRACE_FIELDS"
codec_trace_policy 900 600000
printf '%s\\n' "$CODEC_TRACE_FIELDS"
""",
    )
    assert "codec_trace_active=1" in rows[0] and "codec_trace_remaining_s=1" in rows[0]
    assert "codec_trace_active=0" in rows[1] and "codec_trace_remaining_s=0" in rows[1]


def test_missing_device_clock_fails_closed(shell, tmp_path):
    rows = run_shell(
        shell,
        tmp_path,
        f"""
CODEC_TRACE_UNTIL=1100; CODEC_TRACE_ACTIVE=0; CODEC_TRACE_EXPIRES_MS=0
date() {{ :; }}
clock_ms() {{ echo 500000; }}
child_alive() {{ return 1; }}
{policy_functions()}
initialize_codec_trace
codec_trace_policy '' 500000
printf '%s\\n' "$CODEC_TRACE_FIELDS"
""",
    )
    assert "codec_trace_active=0" in rows[-1]


@pytest.mark.parametrize("deadline,expected_calls", [(0, 0), (999, 0), (1900, 1), (2801, 0)])
def test_real_capture_launch_path_requires_an_unexpired_explicit_lease(
    shell, tmp_path, deadline, expected_calls
):
    codec = tmp_path / "codec-fixture.sh"
    codec.write_text("printf 'capture\\n' >> captures\n", encoding="utf-8", newline="\n")
    source = timing.script()
    start = source.index("  # Independent five-second codec windows")
    launch = source[start : source.index('  if ! child_alive "$frame_pid"', start)]
    rows = run_shell(
        shell,
        tmp_path,
        f"""
CODEC_TRACE_UNTIL={deadline}; CODEC_TRACE_ACTIVE=0; CODEC_TRACE_EXPIRES_MS=0
codec_pid=; codec_start=; codec_last=0; codec_seq=0; SESSION=fixture; LOG=unused; PRIO=e
CODEC_SCRIPT='{codec.as_posix()}'
active=1; zpid=fixture; now=1000
date() {{ echo 1000; }}
clock_ms() {{ echo 500000; }}
process_start() {{ echo fixture; }}
child_alive() {{ return 1; }}
{policy_functions()}
initialize_codec_trace
{launch}
[ -z "$codec_pid" ] || wait "$codec_pid"
if [ -f captures ]; then wc -l < captures; else echo 0; fi
""",
    )
    assert int(rows[-1]) == expected_calls


@pytest.mark.parametrize("race", ["before_launch", "before_identity"])
def test_expiry_between_launch_gate_and_child_identity_cannot_leave_a_capture_running(
    shell, tmp_path, race
):
    codec = tmp_path / "codec-race.sh"
    # Keep the fixture in one owned PID. A bare shell waiting on sleep would leave
    # an unrelated fake grandchild holding pytest's stdout after TERM; production
    # codec workers have their own explicit descendant cleanup.
    codec.write_text("exec sleep 30\n", encoding="utf-8", newline="\n")
    source = timing.script()
    start = source.index("  # Independent five-second codec windows")
    launch = source[start : source.index('  if ! child_alive "$frame_pid"', start)]
    if race == "before_launch":
        launch = launch.replace(
            '      sh "$CODEC_SCRIPT"', '      kill -USR1 "$$"\n      sh "$CODEC_SCRIPT"'
        )
    else:
        # Capture the owned PID but expire before its start-time identity is read.
        launch = launch.replace(
            "      codec_start=$(", '      kill -USR1 "$$"\n      codec_start=$('
        )
    rows = run_shell(
        shell,
        tmp_path,
        f"""
CODEC_TRACE_UNTIL=1900; CODEC_TRACE_ACTIVE=0; CODEC_TRACE_EXPIRES_MS=0
codec_pid=; codec_start=; codec_last=0; codec_seq=0; SESSION=fixture; LOG=unused; PRIO=e
CODEC_SCRIPT='{codec.as_posix()}'
active=1; zpid=fixture; now=1000
date() {{ echo 1000; }}
clock_ms() {{ echo 500000; }}
process_start() {{ echo fixture; }}
child_alive() {{ [ -n "$1" ] && [ "$2" = fixture ] && kill -0 "$1" 2>/dev/null; }}
{policy_functions()}
initialize_codec_trace
trap expire_codec_trace USR1
{launch}
[ "$CODEC_TRACE_ACTIVE" = 0 ] && [ -z "$codec_pid" ] && [ -z "$codec_start" ] || exit 74
printf 'expired_owned_capture_reaped\\n'
""",
    )
    assert "expired_owned_capture_reaped" in rows


def test_expiry_interrupts_owned_capture_reaps_children_and_preserves_foreign_process(
    shell, tmp_path
):
    # Real child processes and real signals exercise expiry while the context worker
    # is waiting, rather than merely asserting the presence of a guard expression.
    rows = run_shell(
        shell,
        tmp_path,
        f"""
{policy_functions()}
process_start() {{ printf 'fixture'; }}
child_alive() {{ [ -n "$1" ] && [ "$2" = fixture ] && kill -0 "$1" 2>/dev/null; }}
SAMPLER_PID=$$; SAMPLER_START=fixture; CODEC_TRACE_ACTIVE=1
sleep 30 & foreign_pid=$!
sleep 30 & codec_pid=$!; codec_start=fixture; owned_pid=$codec_pid
sleep 30 & sleep_pid=$!; sleep_start=fixture; context_sleep=$sleep_pid
trap expire_codec_trace USR1
trap 'kill "$foreign_pid" 2>/dev/null; wait "$foreign_pid" 2>/dev/null' EXIT
codec_lease_loop 1 & lease_pid=$!
wait "$sleep_pid"
wait "$lease_pid"
kill -0 "$foreign_pid" || exit 71
kill -0 "$owned_pid" 2>/dev/null && exit 72
kill -0 "$context_sleep" 2>/dev/null && exit 73
printf 'active=%s passive_continues foreign_preserved\\n' "$CODEC_TRACE_ACTIVE"
""",
    )
    assert "active=0 passive_continues foreign_preserved" in rows


@pytest.mark.parametrize("owned", [True, False])
def test_disabled_policy_stop_verifies_target_and_waits_for_owned_worker_cleanup(
    shell, tmp_path, monkeypatch, owned
):
    if os.name == "nt":
        pytest.skip("real /proc process ownership and PID lifetimes require Linux")
    pidfile = tmp_path / "sampler.pid"
    sampler = tmp_path / "sampler.sh"
    workerfile = tmp_path / "worker.pid"
    cleaned = tmp_path / "cleaned"
    sampler.write_text(
        f"""sleep 30 &
worker=$!
echo $$ > '{pidfile}'
echo "$worker" > '{workerfile}'
cleanup() {{ kill "$worker" 2>/dev/null; wait "$worker" 2>/dev/null; echo complete > '{cleaned}'; }}
trap cleanup EXIT
trap 'exit 0' TERM INT
wait "$worker"
""",
        encoding="utf-8",
        newline="\n",
    )
    monkeypatch.setattr(timing, "REMOTE_PID", str(pidfile))
    monkeypatch.setattr(timing, "REMOTE_SCRIPT", str(sampler))
    foreign = subprocess.Popen(["sleep", "30"])
    target = subprocess.Popen(["/bin/sh", str(sampler)])
    try:
        for _ in range(100):
            if pidfile.exists() and workerfile.exists():
                break
            time.sleep(0.01)
        assert workerfile.exists()
        if not owned:
            pidfile.write_text(str(foreign.pid))
        result = subprocess.run(
            [shell, "-c", timing._stop_command()], text=True, capture_output=True, timeout=10
        )
        if owned:
            assert result.returncode == 0, result.stderr
            assert result.stdout.strip() == "sampler_stopped"
            target.wait(timeout=2)
            assert cleaned.read_text().strip() == "complete"
            assert not Path(f"/proc/{int(workerfile.read_text())}").exists()
        else:
            assert result.returncode != 0
            assert "sampler_stopped" not in result.stdout
            assert target.poll() is None
        assert foreign.poll() is None
    finally:
        for process in (target, foreign):
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=2)
