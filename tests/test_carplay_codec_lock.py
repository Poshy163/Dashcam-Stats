"""Execute the real codec guard on POSIX and Android's mksh without a device."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.ingest import carplay_timing

SOURCE = (Path(__file__).resolve().parents[1] / "backend/app/ingest/carplay_codec.sh").read_text()


def wait_for(predicate, seconds=5):
    deadline = time.monotonic() + seconds
    while not predicate():
        assert time.monotonic() < deadline, "fixture process did not reach its bounded checkpoint"
        time.sleep(0.01)


def start_identity(pid):
    return Path(f"/proc/{pid}/stat").read_text().rpartition(") ")[2].split()[19]


def process_exited(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().rpartition(") ")[2].split()[0] == "Z"
    except FileNotFoundError:
        return True


@pytest.fixture(params=["sh", "mksh"])
def capture(tmp_path, request):
    if os.name == "nt":
        pytest.skip("Linux /proc and kernel flock fixture is unavailable on Windows")
    shell = shutil.which(request.param)
    assert shell is not None, f"{request.param} is required for executable codec safety checks"
    assert shutil.which("flock"), "kernel flock utility is required on Linux CI"
    trace = tmp_path / "trace"
    trace.mkdir()
    (trace / "tracing_on").write_text("0\n")
    (trace / "current_tracer").write_text("nop\n")
    lock = tmp_path / "lock"
    guard = tmp_path / "guard"
    calls = tmp_path / "calls"
    logfile = tmp_path / "log"
    checkpoint = tmp_path / "checkpoint"
    script = tmp_path / "codec.sh"
    processes = []
    text = SOURCE.replace(
        "LOCK=/data/local/tmp/.dashcam_cpt_codec_capture.lock", f"LOCK='{lock}'"
    ).replace("GUARD=/data/local/tmp/.dashcam_cpt_codec_capture.guard", f"GUARD='{guard}'")
    text = text.replace(
        "for path in /sys/kernel/tracing /sys/kernel/debug/tracing; do",
        f"for path in '{trace}'; do",
    ).replace("timeout -s KILL 10 sh", "timeout -s KILL ${PARSER_TIMEOUT:-10} sh")
    prelude = f"""
pidof() {{ printf '%s\\n' "$$"; }}
log() {{ :; }}
atrace() {{
  if [ "$1" = --async_stop ]; then printf 'stop\\n' >> '{calls}'; return 0; fi
  printf 'start\\n' >> '{calls}'
  printf '# entries-in-buffer/entries-written: 4/4\\n'
  printf 'trace-11 ( %s) [000] .... 1.000000: tracing_mark_write: B|%s|CCodecBufferChannel::queue(c2.unisoc.avc.decoder#1@ts=1000)\\n' "$ZPID" "$ZPID"
  printf 'trace-11 ( %s) [000] .... 1.000100: tracing_mark_write: E\\n' "$ZPID"
  printf 'trace-11 ( %s) [000] .... 1.050000: tracing_mark_write: B|%s|CCodecBufferChannel::onWorkDone(c2.unisoc.avc.decoder#1@ts=1000)\\n' "$ZPID" "$ZPID"
  printf 'trace-11 ( %s) [000] .... 1.050100: tracing_mark_write: E\\n' "$ZPID"
  if [ -n "${{PRODUCER_GATE:-}}" ]; then
    : > "$PRODUCER_READY"
    attempt=0
    while [ -e "$PRODUCER_GATE" ] && [ "$attempt" -lt 1000 ]; do
      attempt=$((attempt+1)); sleep 0.01
    done
    [ ! -e "$PRODUCER_GATE" ] || return 1
  else
    sleep 0.05
  fi
}}
"""

    class Capture:
        def __init__(self):
            self.lock, self.guard, self.calls = lock, guard, calls
            self.trace, self.log, self.script = trace, logfile, script
            self.checkpoint = checkpoint

        def write(self, altered=None, prefix=""):
            script.write_text("#!/bin/sh\n" + prelude + prefix + (altered or text))

        def launch(self, sequence=1, env=None):
            process = subprocess.Popen(
                [shell, str(script), "fixture", str(sequence), str(logfile)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env={**os.environ, **(env or {})},
            )
            processes.append(process)
            return process

        def run(self, sequence=1, env=None):
            process = self.launch(sequence, env)
            _out, error = process.communicate(timeout=5)
            assert process.returncode == 0, error
            return self.rows()[-1]

        def rows(self):
            return [
                dict(piece.split("=", 1) for piece in row.split() if "=" in piece)
                for row in logfile.read_text().splitlines()
            ]

        def stale(self, owner=None):
            lock.mkdir(mode=0o700)
            if owner is not None:
                (lock / "owner").write_text(owner)
                (lock / "owner").chmod(0o600)
            os.utime(lock, (time.time() - 120, time.time() - 120))

        def stop_before(self, stage):
            anchor = 'mkfifo "$PIPE"' if stage == "fifo" else "AWKPID=$!"
            pause = f"""if [ "${{PAUSE_CAPTURE:-0}}" = 1 ]; then
  printf '%s %s\\n' "$$" "${{AWKPID:-0}}" > '{checkpoint}'
  kill -STOP "$$"
fi
"""
            altered = (
                text.replace(anchor, pause + anchor)
                if stage == "fifo"
                else text.replace(anchor, anchor + "\n" + pause)
            )
            self.write(altered)

    case = Capture()
    case.write()
    yield case
    for process in processes:
        if process.poll() is None:
            process.kill()
        try:
            process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=3)


@pytest.mark.parametrize(
    ("owner", "reason"),
    [
        (None, "recovered_missing_owner"),
        ("garbage\n", "recovered_malformed_owner"),
        ("", "recovered_malformed_owner"),
        ("2147483647:1:old:1\n", "recovered_dead_owner"),
    ],
)
def test_stale_workspace_recovers_without_stopping_trace(capture, owner, reason):
    capture.stale(owner)
    row = capture.run()
    assert row["codec_capture_status"] == "ok"
    assert row["codec_lock_status"] == reason
    assert row["codec_matched_n"] == "1"
    assert row["codec_trace_off"] == "1"
    assert capture.calls.read_text() == "start\n"
    assert not capture.lock.exists()
    assert capture.guard.is_file()  # The stable exclusion inode is never unlinked.


def test_young_unpublished_owner_is_not_stolen(capture):
    capture.lock.mkdir(mode=0o700)
    row = capture.run()
    assert row["codec_lock_status"] == "initializing"
    assert capture.lock.is_dir() and not capture.calls.exists()


@pytest.mark.parametrize("reused", [False, True])
def test_pid_start_identity_preserves_live_owner_but_allows_reused_pid(capture, reused):
    process = subprocess.Popen(["sleep", "30"])
    try:
        start = int(start_identity(process.pid))
        token = f"{process.pid}:{start + int(reused)}:old:1\n"
        capture.stale(token)
        row = capture.run()
        assert process.poll() is None  # Recovery never signals the other process.
        if reused:
            assert row["codec_lock_status"] == "recovered_dead_owner"
            assert row["codec_capture_status"] == "ok"
        else:
            assert row["codec_lock_status"] == "live_owner"
            assert (capture.lock / "owner").read_text() == token
            assert not capture.calls.exists()
    finally:
        process.terminate()
        process.wait(timeout=3)


def test_partial_owner_naming_a_live_process_remains_unknown(capture):
    capture.stale(f"{os.getpid()}:incomplete\n")
    row = capture.run()
    assert row["codec_lock_status"] == "owner_unknown"
    assert capture.lock.exists() and not capture.calls.exists()


@pytest.mark.parametrize("mode", ["symlink", "permissions"])
def test_unsafe_guard_is_preserved_without_capture(capture, mode):
    if mode == "symlink":
        target = capture.guard.with_name("unrelated")
        target.write_text("must survive")
        capture.guard.symlink_to(target)
    else:
        capture.guard.write_text("")
        capture.guard.chmod(0o000)
    row = capture.run()
    assert row["codec_lock_status"] == "unsafe_guard"
    assert row["codec_capture_status"] == "lock_unavailable"
    assert not capture.calls.exists()
    if mode == "symlink":
        assert target.read_text() == "must survive"


def test_unreadable_owner_and_unknown_members_fail_closed(capture):
    capture.stale("2147483647:1:old:1\n")
    (capture.lock / "owner").chmod(0o000)
    row = capture.run()
    assert row["codec_lock_status"] == "owner_unreadable"
    (capture.lock / "owner").chmod(0o600)
    (capture.lock / "unrelated").write_text("must survive")
    row = capture.run(2)
    assert row["codec_lock_status"] == "unsafe_workspace"
    assert (capture.lock / "unrelated").read_text() == "must survive"
    assert not capture.calls.exists()


def test_active_or_foreign_tracer_is_never_changed(capture):
    capture.stale("2147483647:1:old:1\n")
    (capture.trace / "tracing_on").write_text("1\n")
    row = capture.run()
    assert row["codec_capture_status"] == "trace_busy_or_unavailable"
    assert row["codec_lock_status"] == "not_attempted"
    assert not capture.calls.exists()
    assert (capture.trace / "tracing_on").read_text() == "1\n"
    assert (capture.lock / "owner").exists()


def test_live_kernel_guard_wins_even_with_malformed_owner(capture):
    capture.stale("garbage\n")
    capture.guard.write_text("")
    capture.guard.chmod(0o600)
    import fcntl

    with capture.guard.open("rb") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        row = capture.run()
    assert row["codec_lock_status"] == "guard_busy"
    assert (capture.lock / "owner").read_text() == "garbage\n"
    assert not capture.calls.exists()


@pytest.mark.parametrize("owner", [None, "2147483647:1:old:1\n"])
def test_legacy_initializer_or_child_is_not_proven_dead_by_age_or_parent_token(capture, owner):
    process = subprocess.Popen(["sh", "-c", "while :; do sleep 0.05; done", str(capture.script)])
    try:
        capture.stale(owner)
        row = capture.run()
        assert row["codec_lock_status"] == "legacy_helper_active"
        assert capture.lock.exists() and not capture.calls.exists()
        assert process.poll() is None
    finally:
        process.terminate()
        process.wait(timeout=3)


def test_dead_parent_token_does_not_authorize_recovery_over_pending_legacy_atrace(capture):
    ready = capture.script.parent / "atrace-ready"
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import ctypes,pathlib,time; ctypes.CDLL(None).prctl(15,b'atrace',0,0,0); "
            f"pathlib.Path({str(ready)!r}).touch(); time.sleep(30)",
        ]
    )
    try:
        wait_for(ready.exists)
        capture.stale("2147483647:1:old:1\n")
        row = capture.run()
        assert row["codec_lock_status"] == "legacy_atrace_active"
        assert process.poll() is None
        assert capture.lock.exists() and not capture.calls.exists()
    finally:
        process.terminate()
        process.wait(timeout=3)


def test_two_reclaimers_cannot_remove_each_others_live_workspace(capture):
    capture.stale("2147483647:1:old:1\n")
    gate = capture.script.parent / "recovery-gate"
    ready = capture.script.parent / "recovery-ready"
    gate.touch()
    # Stop the first reclaimer after stale-owner validation but before removal,
    # exactly where two unguarded reclaimers previously raced a fresh owner.
    capture.script.write_text(
        capture.script.read_text().replace(
            'remove_members && rmdir "$LOCK"',
            f"if [ \"${{PAUSE_RECOVERY:-0}}\" = 1 ]; then : > '{ready}'; "
            f"while [ -e '{gate}' ]; do sleep 0.01; done; fi\n"
            '    remove_members && rmdir "$LOCK"',
        )
    )
    first = capture.launch(1, {"PAUSE_RECOVERY": "1"})
    try:
        wait_for(ready.exists)
        row = capture.run(2)
        assert row["codec_lock_status"] == "guard_busy"
        assert not capture.calls.exists()
    finally:
        gate.unlink(missing_ok=True)
    _out, error = first.communicate(timeout=5)
    assert first.returncode == 0, error
    rows = capture.rows()
    assert {row["codec_lock_status"] for row in rows} == {"guard_busy", "recovered_dead_owner"}
    assert capture.calls.read_text() == "start\n"
    assert not capture.lock.exists()


@pytest.mark.parametrize("stage", ["fifo", "parser"])
def test_parent_crash_releases_guard_and_orphan_parser_is_bounded(capture, stage):
    capture.stop_before(stage)
    process = capture.launch(1, {"PAUSE_CAPTURE": "1", "PARSER_TIMEOUT": "1"})
    wait_for(capture.checkpoint.exists)
    _pid, parser_pid = map(int, capture.checkpoint.read_text().split())
    process.kill()
    process.wait(timeout=3)
    row = capture.run(2)
    assert row["codec_lock_status"] == "recovered_dead_owner"
    assert row["codec_capture_status"] == "ok"
    if parser_pid:
        wait_for(lambda: process_exited(parser_pid), seconds=3)


def test_delayed_orphan_parser_cannot_open_the_next_captures_fifo(capture):
    capture.stop_before("parser")
    binary = capture.script.parent / "bin"
    binary.mkdir()
    pause = binary / "pause"
    pause.touch()
    entered = binary / "entered"
    real_timeout = shutil.which("timeout")
    wrapper = binary / "timeout"
    wrapper.write_text(
        f'#!/bin/sh\nif [ "${{DELAY_PARSER:-0}}" = 1 ]; then\n'
        f"  : > '{entered}'\n  while [ -e '{pause}' ]; do sleep 0.01; done\nfi\n"
        f"exec '{real_timeout}' \"$@\"\n"
    )
    wrapper.chmod(0o700)
    first = capture.launch(
        1,
        {
            "PAUSE_CAPTURE": "1",
            "DELAY_PARSER": "1",
            "PARSER_TIMEOUT": "1",
            "PATH": f"{binary}:{os.environ['PATH']}",
        },
    )
    producer_gate = binary / "producer-gate"
    producer_ready = binary / "producer-ready"
    producer_gate.touch()
    try:
        wait_for(lambda: capture.checkpoint.exists() and entered.exists())
        _pid, parser_pid = map(int, capture.checkpoint.read_text().split())
        first.kill()
        first.wait(timeout=3)
        second = capture.launch(
            2, {"PRODUCER_GATE": str(producer_gate), "PRODUCER_READY": str(producer_ready)}
        )
        wait_for(producer_ready.exists)
        pause.unlink()
        wait_for(lambda: process_exited(parser_pid), seconds=3)
        producer_gate.unlink()
        _out, error = second.communicate(timeout=5)
        assert second.returncode == 0, error
        row = capture.rows()[-1]
        assert row["codec_capture_status"] == "ok"
        assert row["codec_matched_n"] == "1"
        assert capture.calls.read_text() == "start\n"
    finally:
        pause.unlink(missing_ok=True)
        producer_gate.unlink(missing_ok=True)


def test_parent_crash_cannot_release_guard_while_owned_producer_is_alive(capture):
    gate = capture.script.parent / "producer-gate"
    ready = capture.script.parent / "producer-ready"
    watchdog_ready = capture.script.parent / "watchdog-ready"
    gate.touch()
    text = (
        capture.script.read_text()
        .replace(
            "sleep 8 &",
            'if [ -n "${PRODUCER_GATE:-}" ]; then\n'
            '    while [ -e "$PRODUCER_GATE" ]; do sleep 0.01; done\n'
            "    sleep 0.01 &\n  else\n    sleep 8 &\n  fi",
        )
        .replace("WATCHPID=$!", f"WATCHPID=$!\n: > '{watchdog_ready}'")
    )
    capture.script.write_text(text)
    first = capture.launch(1, {"PRODUCER_GATE": str(gate), "PRODUCER_READY": str(ready)})
    try:
        wait_for(lambda: ready.exists() and watchdog_ready.exists())
        first.kill()
        first.wait(timeout=3)
        row = capture.run(2)
        assert row["codec_lock_status"] == "guard_busy"
        assert capture.calls.read_text() == "start\n"
    finally:
        gate.unlink(missing_ok=True)

    import fcntl

    def guard_released():
        with capture.guard.open("rb") as descriptor:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except BlockingIOError:
                return False

    wait_for(guard_released)
    row = capture.run(3)
    assert row["codec_lock_status"] == "recovered_dead_owner"
    assert row["codec_capture_status"] == "ok"


def test_partial_owner_publication_failure_cleans_only_its_workspace(capture):
    altered = SOURCE.replace(
        'printf \'%s\\n\' "$TOKEN" > "$LOCK/owner" || {',
        'printf partial > "$LOCK/owner"; false || {',
    )
    # Reuse the fixture's complete path substitutions and only replace this one statement.
    text = capture.script.read_text().replace(
        'printf \'%s\\n\' "$TOKEN" > "$LOCK/owner" || {',
        'printf partial > "$LOCK/owner"; false || {',
    )
    assert altered != SOURCE
    capture.script.write_text(text)
    row = capture.run()
    assert row["codec_lock_status"] == "initialization_failed"
    assert not capture.lock.exists() and capture.guard.exists()
    assert not capture.calls.exists()


@pytest.mark.parametrize(
    "status", ["recovered_missing_owner", "owner_unreadable", "legacy_helper_active"]
)
def test_codec_lock_health_survives_api_parser_without_raw_owner_data(status):
    row = carplay_timing.parse_event(
        datetime.now(UTC),
        f"schema=6 | event=codec_capture codec_capture_status=lock_owner_unavailable codec_lock_status={status}",
    )
    assert row["codec_lock_status"] == status
    assert row["codec_capture_status"] == "lock_owner_unavailable"
    unsafe = carplay_timing.parse_event(
        datetime.now(UTC), "schema=6 | event=codec_capture codec_lock_status=PRIVATE_PID_OR_SESSION"
    )
    # Rejecting the entire malformed event is as safe as retaining a null field.
    assert unsafe is None or unsafe["codec_lock_status"] is None
