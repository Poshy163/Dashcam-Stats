"""Execute the real codec guard on POSIX and Android's mksh without a device."""

from __future__ import annotations

import errno
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.ingest import carplay_timing

SOURCE = (Path(__file__).resolve().parents[1] / "backend/app/ingest/carplay_codec.sh").read_text()


def wait_for(predicate, seconds=20):
    # Returns as soon as the checkpoint appears; the bound only decides when a busy
    # shared runner counts as a hung fixture. 5 s failed CI (mksh-directory) on 30 Oct.
    deadline = time.monotonic() + seconds
    while True:
        value = predicate()
        if value:
            return value
        assert time.monotonic() < deadline, "fixture process did not reach its bounded checkpoint"
        time.sleep(0.01)


def numeric_record(path, fields):
    """A created/truncated file is not a published PID checkpoint yet."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    values = text.split()
    if (
        not text.endswith("\n")
        or len(values) != fields
        or not all(value.isascii() and value.isdigit() for value in values)
    ):
        return None
    numbers = tuple(map(int, values))
    return numbers if numbers[0] > 0 else None


def start_identity(pid):
    return Path(f"/proc/{pid}/stat").read_text().rpartition(") ")[2].split()[19]


def process_exited(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().rpartition(") ")[2].split()[0] == "Z"
    except (FileNotFoundError, ProcessLookupError):
        return True


@pytest.mark.parametrize("error_number", [errno.ENOENT, errno.ESRCH], ids=["enoent", "esrch"])
def test_process_exited_accepts_process_disappearance_during_stat_read(monkeypatch, error_number):
    """A proc entry may vanish before open or while its contents are being read."""
    original = Path.read_text
    target = Path("/proc/12345/stat")

    def disappeared(path, *args, **kwargs):
        if path == target:
            raise OSError(error_number, os.strerror(error_number), str(path))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", disappeared)
    assert process_exited(12345) is True


def test_process_exited_does_not_treat_permission_failure_as_process_exit(monkeypatch):
    original = Path.read_text
    target = Path("/proc/12345/stat")

    def denied(path, *args, **kwargs):
        if path == target:
            raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), str(path))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", denied)
    with pytest.raises(PermissionError):
        process_exited(12345)


@pytest.fixture(params=["sh", "mksh"])
def capture(tmp_path, request, codec_process_contract):
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
    tools = tmp_path / "tools"
    tools.mkdir()
    inherited = tmp_path / "inherited-fds"
    producer_pids = tmp_path / "producer-pids"
    processes = []
    text = SOURCE.replace(
        "LOCK=/data/local/tmp/.dashcam_cpt_codec_capture.lock", f"LOCK='{lock}'"
    ).replace("GUARD=/data/local/tmp/.dashcam_cpt_codec_capture.guard", f"GUARD='{guard}'")
    text = text.replace(
        "for path in /sys/kernel/tracing /sys/kernel/debug/tracing; do",
        f"for path in '{trace}'; do",
    ).replace("timeout -s KILL 10 sh", "timeout -s KILL ${PARSER_TIMEOUT:-10} sh")
    text = text.replace("timeout -s KILL 4 sh -c", f"timeout -s KILL 4 '{shell}' -c")
    text = text.replace("timeout -s KILL 1 sh -c", f"timeout -s KILL 1 '{shell}' -c")
    prelude = """
pidof() {{ printf '%s\\n' "$$"; }}
log() {{ :; }}
""".replace("{{", "{").replace("}}", "}")
    for command in ("flock", "mkdir", "rmdir", "rm", "mkfifo", "chmod"):
        executable = shutil.which(command)
        assert executable, f"{command} required for executable codec safety checks"
        stub = tools / command
        stub.write_text(
            f"#!/bin/sh\n[ /proc/$$/fd/8 -ef '{guard}' ] || exit 98\n"
            f"printf '{command}\\n' >> '{inherited}'\nexec '{executable}' \"$@\" 8<&8\n"
        )
        stub.chmod(0o700)
    # This must be an executable, not a shell function: mksh closes an exec-opened
    # fd unless the actual external command receives explicit passthrough.
    atrace = tools / "atrace"
    atrace.write_text(f"""#!/bin/sh
  [ /proc/$$/fd/8 -ef '{guard}' ] || exit 98
  if [ "$1" = --async_stop ]; then
    printf 'stop\\n' >> '{calls}'
    if [ -n "${{STOP_GATE:-}}" ]; then
      : > "$STOP_READY"
      while [ -e "$STOP_GATE" ]; do sleep 0.01 8<&8; done
    fi
    printf '0\\n' > '{trace / "tracing_on"}'
    exit 0
  fi
  ZPID=$PPID
  printf '%s\\n' "$$" >> '{producer_pids}'
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
      attempt=$((attempt+1)); sleep 0.01 8<&8
    done
    [ ! -e "$PRODUCER_GATE" ] || exit 1
  else
    sleep 0.05 8<&8
  fi
  if [ "${{LEAVE_TRACE_ON:-0}}" = 1 ]; then printf '1\\n' > '{trace / "tracing_on"}'; fi
""")
    atrace.chmod(0o700)

    class Capture:
        def __init__(self):
            self.lock, self.guard, self.calls = lock, guard, calls
            self.trace, self.log, self.script = trace, logfile, script
            self.checkpoint = checkpoint
            self.inherited, self.producer_pids = inherited, producer_pids
            self.tools = tools
            self.process_contract = codec_process_contract

        def write(self, altered=None, prefix=""):
            script.write_text("#!/bin/sh\n" + prelude + prefix + (altered or text))

        def launch(self, sequence=1, env=None):
            process = subprocess.Popen(
                [shell, str(script), "fixture", str(sequence), str(logfile)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env={
                    **os.environ,
                    **(env or {}),
                    "PATH": f"{tools}:{(env or {}).get('PATH', os.environ['PATH'])}",
                },
            )
            processes.append(process)
            return process

        def run(self, sequence=1, env=None):
            process = self.launch(sequence, env)
            _out, error = process.communicate(timeout=10)
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

        def legacy(self, owner=b"\0" * 38, directory_mode=0o777, owner_mode=0o666):
            self.stale()
            (lock / "owner").write_bytes(owner)
            (lock / "summary").write_bytes(b"old bounded summary\n")
            os.mkfifo(lock / "pipe")
            for child in lock.iterdir():
                child.chmod(0o666)
            (lock / "owner").chmod(owner_mode)
            lock.chmod(directory_mode)
            os.utime(lock, (time.time() - 120, time.time() - 120))

        def synthetic_proc(
            self, count=600, same_uid=False, arguments=b"/bin/sleep\0", comm="sleep"
        ):
            proc = script.parent / "proc"
            proc.mkdir()
            uid = os.geteuid() if same_uid else os.geteuid() + 1
            for index in range(count):
                process = proc / str(100_000 + index)
                process.mkdir()
                (process / "status").write_text(
                    f"Name:\tfixture\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n"
                )
                if same_uid:
                    (process / "comm").write_text(comm + "\n")
                    fields = ["S", *["0"] * 18, str(100 + index)]
                    (process / "stat").write_text(
                        f"{process.name} (fixture) " + " ".join(fields) + "\n"
                    )
                    (process / "cmdline").write_bytes(arguments)
            script.write_text(
                script.read_text().replace(
                    "for process in /proc/[0-9]*; do", f"for process in '{proc}'/[0-9]*; do"
                )
            )
            return proc

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
    try:
        yield case
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            try:
                process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=3)
        if producer_pids.exists():
            for pid in producer_pids.read_text().splitlines():
                wait_for(lambda pid=int(pid): process_exited(pid))


@pytest.fixture
def codec_process_contract(tmp_path_factory, request):
    if os.name == "nt":
        pytest.skip("Linux /proc and kernel flock fixture is unavailable on Windows")
    import fcntl

    # Private trace directories still share this UID and /proc with other xdist
    # workers. Their intentional atrace actors must not invalidate another case's
    # real process-absence proof. Hold one per-run lease through child teardown.
    directory = tmp_path_factory.getbasetemp()
    if hasattr(request.config, "workerinput"):
        directory = directory.parent
    path = directory / "codec-process-contract.lock"
    with path.open("a+b") as descriptor:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield path


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


def test_real_external_commands_receive_the_same_guard_descriptor(capture):
    row = capture.run()
    assert row["codec_capture_status"] == "ok"
    assert numeric_record(capture.producer_pids, 1)
    assert {"flock", "mkdir", "mkfifo", "rm", "rmdir"} <= set(
        capture.inherited.read_text().splitlines()
    )


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


def test_global_atrace_starting_between_migration_proofs_refuses_then_recovers(capture):
    capture.legacy()
    before = (capture.lock / "owner").read_bytes()
    gate = capture.script.parent / "directory-gate"
    entered = capture.script.parent / "directory-entered"
    ready = capture.script.parent / "foreign-atrace-ready"
    gate.touch()
    real_chmod = shutil.which("chmod")
    chmod = capture.tools / "chmod"
    chmod.write_text(
        f"#!/bin/sh\n'{real_chmod}' \"$@\" 8<&8 || exit $?\n"
        f"if [ \"$2\" = /proc/self/fd/7 ] && [ -e '{gate}' ]; then\n"
        f" : > '{entered}'; while [ -e '{gate}' ]; do sleep .01 8<&8; done\nfi\n"
    )
    actor = None
    first = capture.launch(1)
    try:
        wait_for(entered.exists)
        actor = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import ctypes,pathlib,time; ctypes.CDLL(None).prctl(15,b'atrace',0,0,0); "
                f"pathlib.Path({str(ready)!r}).touch(); time.sleep(30)",
            ]
        )
        wait_for(ready.exists)
        gate.unlink()
        _out, error = first.communicate(timeout=10)
        assert first.returncode == 0, error
        row = capture.rows()[-1]
        assert row["codec_capture_status"] == "lock_recovery_failed"
        assert row["codec_lock_status"] == "recovery_failed"
        assert (capture.lock / "owner").read_bytes() == before
        assert (capture.lock / "owner").stat().st_mode & 0o777 == 0o666
        assert not capture.calls.exists() and actor.poll() is None
    finally:
        gate.unlink(missing_ok=True)
        if actor is not None:
            actor.terminate()
            actor.wait(timeout=3)
    assert capture.run(2)["codec_capture_status"] == "ok"


def test_fixture_process_contract_is_held_without_inheriting_it_into_children(capture):
    script = """import fcntl,os,pathlib,sys
path=sys.argv[1]
for descriptor in pathlib.Path('/proc/self/fd').iterdir():
    try:
        assert os.readlink(descriptor) != path
    except FileNotFoundError:
        pass
with open(path,'rb') as stream:
    try:
        fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        print('fixture_contract_held')
    else:
        raise AssertionError('fixture contract was not held')
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(capture.process_contract)],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "fixture_contract_held"


@pytest.mark.parametrize(
    ("comm", "arguments", "reason"),
    [
        ("rm", ["OWNER"], "legacy_workspace_active"),
        ("toybox", ["rm", "OWNER"], "legacy_workspace_active"),
        ("timeout", ["1", "atrace", "--async_stop"], "legacy_atrace_active"),
    ],
)
def test_dead_parent_does_not_hide_already_execed_legacy_mutation_children(
    capture, comm, arguments, reason
):
    ready = capture.script.parent / "legacy-child-ready"
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            f"import ctypes,pathlib,time; ctypes.CDLL(None).prctl(15,{comm.encode()!r},0,0,0); "
            f"pathlib.Path({str(ready)!r}).touch(); time.sleep(30)",
            *[str(capture.lock / "owner") if arg == "OWNER" else arg for arg in arguments],
        ]
    )
    try:
        wait_for(ready.exists)
        capture.stale("2147483647:1:old:1\n")
        assert capture.run()["codec_lock_status"] == reason
        assert process.poll() is None and not capture.calls.exists()
    finally:
        process.terminate()
        process.wait(timeout=3)


def test_exec_during_legacy_argv_read_is_ambiguous_even_with_unchanged_start_ticks(capture):
    ready = capture.script.parent / "comm-ready"
    change = capture.script.parent / "comm-change"
    changed = capture.script.parent / "comm-changed"
    entered = capture.script.parent / "head-entered"
    gate = capture.script.parent / "head-gate"
    gate.touch()
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import ctypes,pathlib,time; libc=ctypes.CDLL(None); libc.prctl(15,b'sh',0,0,0); "
            f"pathlib.Path({str(ready)!r}).touch(); "
            f"\nwhile not pathlib.Path({str(change)!r}).exists(): time.sleep(.01)\n"
            f"libc.prctl(15,b'rm',0,0,0); pathlib.Path({str(changed)!r}).touch(); time.sleep(30)",
            str(capture.lock / "owner"),
        ]
    )
    actual_head = shutil.which("head")
    head = capture.tools / "head"
    head.write_text(
        f'#!/bin/sh\nif [ "$3" = /proc/{child.pid}/cmdline ]; then\n'
        f"  : > '{entered}'\n  while [ -e '{gate}' ]; do sleep 0.01; done\nfi\n"
        f"exec '{actual_head}' \"$@\"\n"
    )
    head.chmod(0o700)
    capture.stale("2147483647:1:old:1\n")
    try:
        wait_for(ready.exists)
        started = start_identity(child.pid)
        process = capture.launch()
        wait_for(entered.exists)
        change.touch()
        wait_for(changed.exists)
        gate.unlink()
        _out, error = process.communicate(timeout=5)
        assert process.returncode == 0, error
        assert start_identity(child.pid) == started
        assert capture.rows()[-1]["codec_lock_status"] == "legacy_owner_unknown"
        assert capture.lock.exists() and not capture.calls.exists()
    finally:
        gate.unlink(missing_ok=True)
        child.terminate()
        child.wait(timeout=3)


def test_more_than_512_unrelated_processes_use_builtin_probes(capture):
    capture.stale("2147483647:1:old:1\n")
    proc = capture.synthetic_proc(600)
    # These tasks intentionally have no comm/stat/cmdline. Their different UID is
    # sufficient, and no external per-PID parser may be used for their status.
    awk = capture.tools / "awk"
    real_awk = shutil.which("awk")
    awk.write_text(
        f'#!/bin/sh\nfor arg in "$@"; do case "$arg" in "{proc}/"*) exit 98;; esac; done\n'
        f"exec '{real_awk}' \"$@\"\n"
    )
    awk.chmod(0o700)
    row = capture.run()
    assert row["codec_lock_status"] == "recovered_dead_owner"
    assert row["codec_capture_status"] == "ok"


@pytest.mark.parametrize(
    "case", ["unreadable_uid", "malformed_stat", "unknown_comm_helper", "oversized_utf8"]
)
def test_synthetic_process_evidence_never_becomes_an_absence_proof(capture, case):
    capture.stale("2147483647:1:old:1\n")
    arguments = b"/bin/sleep\0"
    if case == "unknown_comm_helper":
        arguments = b"/data/local/tmp/dashcam_carplay_codec.sh\0"
    elif case == "oversized_utf8":
        arguments = ("\u20ac" * 23_000).encode() + b"\0/data/local/tmp/dashcam_carplay_codec.sh\0"
    proc = capture.synthetic_proc(1, same_uid=True, arguments=arguments, comm="python")
    process = next(proc.iterdir())
    if case == "unreadable_uid":
        (process / "status").unlink()
    elif case == "malformed_stat":
        (process / "stat").write_text("malformed\n")
    row = capture.run()
    expected = "legacy_helper_active" if case == "unknown_comm_helper" else "legacy_owner_unknown"
    assert row["codec_lock_status"] == expected
    assert capture.lock.exists() and not capture.calls.exists()


@pytest.mark.parametrize("final_uptime", ["14.01", "9.99"])
def test_final_process_read_cannot_spend_more_than_the_fixed_budget(capture, final_uptime):
    capture.stale("2147483647:1:old:1\n")
    proc = capture.synthetic_proc(1, same_uid=True)
    clock = capture.script.parent / "uptime"
    clock.write_text("10.00 0.00\n")
    capture.script.write_text(
        capture.script.read_text().replace(
            "read uptime_value ignored < /proc/uptime", f"read uptime_value ignored < '{clock}'"
        )
    )
    real_head = shutil.which("head")
    head = capture.tools / "head"
    head.write_text(
        f"#!/bin/sh\n'{real_head}' \"$@\"\n"
        f"case \"$3\" in '{proc}/'*) printf '{final_uptime} 0.00\\n' > '{clock}';; esac\n"
    )
    head.chmod(0o700)
    assert capture.run()["codec_lock_status"] == "legacy_scan_timeout"
    assert capture.lock.exists() and not capture.calls.exists()


@pytest.mark.parametrize("case,count", [("inventory", 4097), ("candidates", 129)])
def test_finite_inventory_and_candidate_caps_are_distinct(capture, case, count):
    capture.stale("2147483647:1:old:1\n")
    capture.synthetic_proc(count, same_uid=case == "candidates")
    # Freeze only the fixture clock to prove count guards independently of CPU load.
    clock = capture.script.parent / "uptime"
    clock.write_text("10.00 0.00\n")
    capture.script.write_text(
        capture.script.read_text().replace(
            "read uptime_value ignored < /proc/uptime", f"read uptime_value ignored < '{clock}'"
        )
    )
    expected = "legacy_scan_limit" if case == "inventory" else "legacy_candidate_limit"
    assert capture.run()["codec_lock_status"] == expected
    assert capture.lock.exists() and not capture.calls.exists()


def test_same_comm_exec_with_changed_argv_is_not_an_absence_proof(capture):
    capture.stale("2147483647:1:old:1\n")
    proc = capture.synthetic_proc(1, same_uid=True, comm="sh")
    cmdline = next(proc.iterdir()) / "cmdline"
    first = capture.script.parent / "first-argv"
    real_head = shutil.which("head")
    head = capture.tools / "head"
    head.write_text(
        f"#!/bin/sh\n'{real_head}' \"$@\"\n"
        f"if [ \"$3\" = '{cmdline}' ] && [ ! -e '{first}' ]; then\n"
        f" : > '{first}'; printf '/data/local/tmp/dashcam_carplay_codec.sh\\000' > '{cmdline}'\nfi\n"
    )
    head.chmod(0o700)
    assert capture.run()["codec_lock_status"] == "legacy_owner_unknown"
    assert capture.lock.exists() and not capture.calls.exists()


@pytest.mark.parametrize("directory_mode", [0o777, 0o700])
def test_real_legacy_modes_and_interrupted_tightening_recover(capture, directory_mode):
    capture.legacy(directory_mode=directory_mode)
    row = capture.run()
    assert row["codec_lock_status"] == "recovered_malformed_owner"
    assert row["codec_capture_status"] == "ok"
    assert capture.calls.read_text() == "start\n" and not capture.lock.exists()
    assert capture.inherited.read_text().splitlines().count("chmod") == 2


@pytest.mark.parametrize(
    "case", ["unexpected_dir", "unexpected_owner", "hardlink", "symlink", "unknown", "many_members"]
)
def test_legacy_migration_never_normalizes_an_unsafe_tree(capture, case):
    capture.legacy(
        directory_mode=0o755 if case == "unexpected_dir" else 0o777,
        owner_mode=0o600 if case == "unexpected_owner" else 0o666,
    )
    owner = capture.lock / "owner"
    if case == "hardlink":
        os.link(owner, capture.script.parent / "outside")
    elif case == "symlink":
        outside = capture.script.parent / "outside"
        outside.write_text("preserve")
        owner.unlink()
        owner.symlink_to(outside)
    elif case == "unknown":
        (capture.lock / "unknown").write_text("preserve")
    elif case == "many_members":
        for index in range(32):
            (capture.lock / f"summary-1-{index}").write_text("preserve")
    before = capture.lock.stat().st_mode
    row = capture.run()
    assert row["codec_lock_status"] in {"unsafe_workspace", "recovery_failed"}
    assert capture.lock.stat().st_mode == before
    assert capture.lock.exists() and not capture.calls.exists()
    assert "chmod" not in capture.inherited.read_text().splitlines()


def test_live_legacy_owner_preserves_world_writable_modes_and_is_not_signalled(capture):
    child = subprocess.Popen(["sleep", "30"])
    try:
        capture.legacy(f"{child.pid}:{start_identity(child.pid)}:old:1\n".encode())
        assert capture.run()["codec_lock_status"] == "live_owner"
        assert child.poll() is None
        assert capture.lock.stat().st_mode & 0o777 == 0o777
        assert (capture.lock / "owner").stat().st_mode & 0o777 == 0o666
        assert not capture.calls.exists()
    finally:
        child.terminate()
        child.wait(timeout=3)


def test_hardlinked_guard_is_never_trusted(capture):
    capture.guard.write_text("")
    capture.guard.chmod(0o600)
    os.link(capture.guard, capture.script.parent / "outside-guard")
    assert capture.run()["codec_lock_status"] == "unsafe_guard"
    assert not capture.calls.exists()


@pytest.mark.parametrize(
    "race",
    [
        "owner_at_directory_chmod",
        "owner_at_owner_chmod",
        "changed_bytes",
        "same_size_restored_mtime",
        "trace_started",
    ],
)
def test_pinned_legacy_chmod_refuses_races_without_changing_outside_file(capture, race):
    capture.legacy()
    owner = capture.lock / "owner"
    outside = capture.script.parent / "outside"
    outside.write_text("preserve outside")
    outside.chmod(0o644)
    saved = capture.script.parent / "saved-owner"
    reference = capture.script.parent / "timestamp-reference"
    reference.write_text("")
    os.utime(reference, ns=(owner.stat().st_atime_ns, owner.stat().st_mtime_ns))
    real_chmod = shutil.which("chmod")
    target = "/proc/self/fd/9" if race == "owner_at_owner_chmod" else "/proc/self/fd/7"
    action = {
        "owner_at_directory_chmod": f"mv '{owner}' '{saved}'; ln -s '{outside}' '{owner}'",
        "owner_at_owner_chmod": f"mv '{owner}' '{saved}'; ln -s '{outside}' '{owner}'",
        "changed_bytes": f"printf changed > '{owner}'",
        "same_size_restored_mtime": f"printf '%038d' 0 > '{owner}'; touch -r '{reference}' '{owner}'",
        "trace_started": f"printf '1\\n' > '{capture.trace / 'tracing_on'}'",
    }[race]
    wrapper = capture.tools / "chmod"
    wrapper.write_text(
        f"#!/bin/sh\n[ /proc/$$/fd/8 -ef '{capture.guard}' ] || exit 98\n"
        f'if [ "$2" = {target} ]; then {action}; fi\n'
        f"exec '{real_chmod}' \"$@\" 8<&8\n"
    )
    row = capture.run()
    assert row["codec_lock_status"] == "recovery_failed"
    assert outside.read_text() == "preserve outside" and outside.stat().st_mode & 0o777 == 0o644
    assert capture.lock.exists() and not capture.calls.exists()


@pytest.mark.parametrize("phase", ["directory", "owner"])
def test_parent_crash_during_pinned_chmod_keeps_guard_and_allows_partial_retry(capture, phase):
    capture.legacy()
    before = {
        p.name: (p.stat().st_ino, p.read_bytes() if p.is_file() else None)
        for p in capture.lock.iterdir()
    }
    gate = capture.script.parent / "chmod-gate"
    ready = capture.script.parent / "chmod-ready"
    gate.touch()
    real_chmod = shutil.which("chmod")
    target = "/proc/self/fd/7" if phase == "directory" else "/proc/self/fd/9"
    wrapper = capture.tools / "chmod"
    wrapper.write_text(
        f"#!/bin/sh\n[ /proc/$$/fd/8 -ef '{capture.guard}' ] || exit 98\n"
        f"'{real_chmod}' \"$@\" 8<&8 || exit $?\n"
        f"if [ \"$2\" = {target} ] && [ -e '{gate}' ]; then\n"
        f" printf '%s\\n' \"$$\" > '{ready}'; while [ -e '{gate}' ]; do sleep .01 8<&8; done\nfi\n"
    )
    first = capture.launch(1)
    try:
        child = wait_for(lambda: numeric_record(ready, 1))[0]
        first.kill()
        first.wait(timeout=3)
        assert capture.lock.stat().st_mode & 0o777 == 0o700
        assert (capture.lock / "owner").stat().st_mode & 0o777 == (
            0o666 if phase == "directory" else 0o600
        )
        assert {
            p.name: (p.stat().st_ino, p.read_bytes() if p.is_file() else None)
            for p in capture.lock.iterdir()
        } == before
        assert capture.run(2)["codec_lock_status"] == "guard_busy"
    finally:
        gate.unlink(missing_ok=True)
    wait_for(lambda: process_exited(child))
    import fcntl

    def guard_released():
        with capture.guard.open("rb") as descriptor:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except BlockingIOError:
                return False

    wait_for(guard_released)
    assert capture.run(3)["codec_capture_status"] == "ok"
    assert not capture.lock.exists()


def test_fifo_substituted_at_member_open_is_bounded_and_preserved(capture):
    capture.legacy()
    owner = capture.lock / "owner"
    saved = capture.script.parent / "saved-owner"
    real_timeout = shutil.which("timeout")
    wrapper = capture.tools / "timeout"
    wrapper.write_text(
        '#!/bin/sh\nif [ "$3" = 4 ]; then\n'
        f" mv '{owner}' '{saved}'; mkfifo '{owner}' 8<&8\n"
        f" exec '{real_timeout}' \"$@\" 7<&7 8<&8\nfi\n"
        f"exec '{real_timeout}' \"$@\"\n"
    )
    wrapper.chmod(0o700)
    row = capture.run()
    assert row["codec_lock_status"] == "recovery_failed"
    assert saved.read_bytes() == b"\0" * 38 and owner.is_fifo()
    assert capture.lock.exists() and not capture.calls.exists()
    import fcntl

    with capture.guard.open("rb") as descriptor:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)


@pytest.mark.parametrize("case", ["read_error", "timeout"])
def test_member_hash_failure_is_bounded_and_never_permits_recovery(capture, case):
    capture.legacy()
    sha = capture.tools / "sha256sum"
    sha.write_text(
        "#!/bin/sh\n" + ("exit 99\n" if case == "read_error" else "exec sleep 30 8<&8\n")
    )
    sha.chmod(0o700)
    row = capture.run()
    assert row["codec_lock_status"] == "recovery_failed"
    assert (capture.lock / "owner").read_bytes() == b"\0" * 38
    assert (capture.lock / "owner").stat().st_mode & 0o777 == 0o666
    assert not capture.calls.exists()


def test_fifo_substituted_at_initial_owner_parse_is_bounded(capture):
    capture.legacy()
    owner = capture.lock / "owner"
    saved = capture.script.parent / "saved-owner"
    actual_timeout = shutil.which("timeout")
    wrapper = capture.tools / "timeout"
    wrapper.write_text(
        '#!/bin/sh\nfor arg in "$@"; do\n'
        f" if [ \"$arg\" = codec_owner ]; then mv '{owner}' '{saved}'; mkfifo '{owner}' 8<&8; "
        f"exec '{actual_timeout}' \"$@\" 8<&8; fi\n"
        "done\n"
        f"exec '{actual_timeout}' \"$@\"\n"
    )
    wrapper.chmod(0o700)
    assert capture.run()["codec_lock_status"] == "owner_unreadable"
    assert saved.read_bytes() == b"\0" * 38 and owner.is_fifo()
    assert capture.lock.stat().st_mode & 0o777 == 0o777
    assert not capture.calls.exists()


@pytest.mark.parametrize("case", ["oversized", "read_error"])
def test_owner_read_limit_and_failed_read_never_authorize_empty_owner_recovery(capture, case):
    capture.legacy(owner=b"X" * 16_384 if case == "oversized" else b"\0" * 38)
    before = (capture.lock / "owner").read_bytes()
    if case == "read_error":
        real_head = shutil.which("head")
        head = capture.tools / "head"
        head.write_text(f'#!/bin/sh\n[ "$2" != 201 ] || exit 99\nexec \'{real_head}\' "$@"\n')
        head.chmod(0o700)
    assert capture.run()["codec_lock_status"] == "owner_unreadable"
    assert (capture.lock / "owner").read_bytes() == before
    assert capture.lock.stat().st_mode & 0o777 == 0o777
    assert not capture.calls.exists()


@pytest.mark.parametrize("newline", [b"", b"\n"])
def test_young_mixed_nul_owner_remains_malformed_and_keeps_initialization_grace(capture, newline):
    capture.stale()
    raw = b"2147483647:1:old:1\0" + newline
    owner = capture.lock / "owner"
    owner.write_bytes(raw)
    owner.chmod(0o600)
    os.utime(capture.lock, None)
    inode = owner.stat().st_ino
    assert capture.run()["codec_lock_status"] == "initializing"
    assert owner.read_bytes() == raw and owner.stat().st_ino == inode
    assert not capture.calls.exists()


def test_owner_raw_length_is_the_same_snapshot_that_the_pinned_fd_validates(capture):
    capture.stale()
    owner = capture.lock / "owner"
    text = b"2147483647:1:old:1"
    owner.write_bytes(text + b"\0")
    owner.chmod(0o600)
    os.utime(capture.lock, None)
    actual_stat = shutil.which("stat")
    wrapper = capture.tools / "stat"
    # Simulate the earlier length-only stat preceding a NUL append, while the
    # later full metadata and pinned inode correctly report the post-append size.
    wrapper.write_text(
        f'#!/bin/sh\nif [ "$1" = -c ] && [ "$2" = %s ] && [ "$3" = \'{owner}\' ]; then\n'
        f" printf '{len(text)}\\n'; exit 0\nfi\n"
        f"exec '{actual_stat}' \"$@\"\n"
    )
    wrapper.chmod(0o700)
    assert capture.run()["codec_lock_status"] == "initializing"
    assert owner.read_bytes() == text + b"\0" and not capture.calls.exists()


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
    _pid, parser_pid = wait_for(lambda: numeric_record(capture.checkpoint, 2))
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
        wait_for(entered.exists)
        _pid, parser_pid = wait_for(lambda: numeric_record(capture.checkpoint, 2))
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
            "sleep 8 8<&8 &",
            'if [ -n "${PRODUCER_GATE:-}" ]; then\n'
            '    while [ -e "$PRODUCER_GATE" ]; do sleep 0.01; done\n'
            "    sleep 0.01 8<&8 &\n  else\n    sleep 8 8<&8 &\n  fi",
        )
        .replace("WATCHPID=$!", f"WATCHPID=$!\nprintf '%s\\n' \"$WATCHPID\" > '{watchdog_ready}'")
    )
    capture.script.write_text(text)
    first = capture.launch(1, {"PRODUCER_GATE": str(gate), "PRODUCER_READY": str(ready)})
    try:
        wait_for(ready.exists)
        (watchdog_pid,) = wait_for(lambda: numeric_record(watchdog_ready, 1))
        os.kill(watchdog_pid, signal.SIGTERM)
        wait_for(lambda: process_exited(watchdog_pid))
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


def test_async_stop_child_keeps_guard_after_parent_crash(capture):
    capture.script.write_text(
        capture.script.read_text().replace(
            "timeout 1 atrace --async_stop", "timeout 10 atrace --async_stop"
        )
    )
    gate = capture.script.parent / "stop-gate"
    ready = capture.script.parent / "stop-ready"
    gate.touch()
    first = capture.launch(
        1, {"LEAVE_TRACE_ON": "1", "STOP_GATE": str(gate), "STOP_READY": str(ready)}
    )
    try:
        wait_for(ready.exists)
        first.kill()
        first.wait(timeout=3)
        # The old stop can still complete later even after tracing became idle.
        (capture.trace / "tracing_on").write_text("0\n")
        assert capture.run(2)["codec_lock_status"] == "guard_busy"
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
    assert capture.run(3)["codec_capture_status"] == "ok"


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
