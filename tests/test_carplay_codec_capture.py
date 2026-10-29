"""Exercise the exact bounded, privacy-filtering on-unit codec trace parser."""

import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "backend/app/ingest/carplay_codec.sh"
SOURCE = SCRIPT.read_text(encoding="utf-8")
PROGRAM = SOURCE.split("# BEGIN_CODEC_AWK\n", 1)[1].split("# END_CODEC_AWK", 1)[0]


def run_parser(trace, zpid=40):
    awk = shutil.which("awk") or r"C:\Program Files\Git\usr\bin\awk.exe"
    if not Path(awk).is_file():
        pytest.skip("AWK unavailable")
    result = subprocess.run(
        [awk, "-v", f"zpid={zpid}", PROGRAM], input=trace, text=True, capture_output=True, timeout=5
    )
    assert result.returncode == 0, result.stderr
    return dict(piece.split("=", 1) for piece in result.stdout.split())


def marker(timestamp, payload, tid=41):
    return f"PRIVATE_THREAD-{tid} ( 40) [000] .... {timestamp:.6f}: tracing_mark_write: {payload}\n"


def codec(timestamp, kind, pts, instance=1, pid=40):
    return marker(
        timestamp, f"B|{pid}|CCodecBufferChannel::{kind}(c2.unisoc.avc.decoder#{instance}@ts={pts})"
    ) + marker(timestamp + 0.0001, "E")


HEADER = "# entries-in-buffer/entries-written: 100/100   #P:8\n"


def test_pairs_are_per_instance_with_linear_percentiles_and_capture_boundaries():
    trace = HEADER
    trace += codec(1, "onWorkDone", -1000)
    trace += codec(1.1, "queue", 1000)
    trace += codec(1.15, "onWorkDone", 1000)
    trace += codec(1.2, "queue", 2000)
    trace += codec(1.3, "onWorkDone", 2000)
    trace += codec(1.35, "queue", 3000)
    result = run_parser(trace)
    assert result["codec_stats_status"] == "ok"
    assert result["codec_matched_n"] == "2"
    assert result["codec_latency_med_ms"] == "75.000"
    assert result["codec_latency_p95_ms"] == "97.500"
    assert result["codec_latency_max_ms"] == "100.000"
    assert result["codec_input_tail_n"] == result["codec_output_head_n"] == "1"
    assert result["codec_unmatched_input_n"] == result["codec_unmatched_output_n"] == "1"


def test_never_pairs_different_codec_instances_or_other_processes():
    trace = HEADER + codec(1, "queue", 1000, instance=1)
    trace += codec(1.1, "onWorkDone", 1000, instance=2)
    trace += codec(1.2, "onWorkDone", 1000, instance=1, pid=99)
    result = run_parser(trace)
    assert result["codec_instances"] == "2"
    assert result["codec_matched_n"] == "0"
    assert result["codec_latency_max_ms"] == "na"
    assert result["codec_output_n"] == "1"


def test_duplicate_pts_is_not_an_arbitrary_latency_pair():
    trace = (
        HEADER
        + codec(1, "queue", 1000)
        + codec(1.01, "queue", 1000)
        + codec(1.1, "onWorkDone", 1000)
    )
    result = run_parser(trace)
    assert result["codec_duplicate_input_n"] == "1"
    assert result["codec_matched_n"] == "0"
    assert result["codec_latency_med_ms"] == "na"


def test_pts_reset_invalidates_latencies_and_state_never_survives_next_capture():
    trace = (
        HEADER
        + codec(1, "queue", 2000)
        + codec(1.1, "onWorkDone", 2000)
        + codec(1.2, "queue", 1000)
    )
    result = run_parser(trace)
    assert result["codec_stats_status"] == "pts_reset"
    assert result["codec_latency_max_ms"] == "na"
    assert result["codec_pts_reset_n"] == "1"
    fresh = run_parser(HEADER + codec(2, "queue", 1000) + codec(2.05, "onWorkDone", 1000))
    assert fresh["codec_stats_status"] == "ok"
    assert fresh["codec_latency_max_ms"] == "50.000"


def test_texture_progress_requires_nested_slot_and_filters_camera_process():
    trace = HEADER
    trace += marker(1, "B|40|updateTexImage") + marker(1.001, "E")
    trace += marker(1.1, "B|40|acquireBuffer")
    trace += marker(1.101, "B|40|SurfaceTexture-PRIVATE-MAC: 2")
    trace += marker(1.102, "E") + marker(1.103, "E")
    trace += marker(1.2, "C|40|SurfaceTexture-PRIVATE-MAC|3")
    trace += marker(1.3, "C|40|com.zjinnova.zlink/PRIVATE_TITLE|1")
    trace += marker(1.4, "C|99|SurfaceTexture-CAMERA|99")
    result = run_parser(trace)
    assert result["codec_texture_update_n"] == "1"
    assert result["codec_texture_acquire_n"] == "1"
    assert result["codec_texture_queue_max"] == "3"
    assert result["codec_window_queue_max"] == "1"
    assert "PRIVATE" not in str(result) and "CAMERA" not in str(result)


@pytest.mark.parametrize(
    "header,status",
    [("", "header_missing"), ("# entries-in-buffer/entries-written: 10/100\n", "trace_overwrite")],
)
def test_missing_or_overwritten_trace_never_reports_healthy_latency(header, status):
    result = run_parser(header + codec(1, "queue", 1000) + codec(1.1, "onWorkDone", 1000))
    assert result["codec_stats_status"] == status
    assert result["codec_latency_p95_ms"] == "na"


def test_finite_marker_budget_rejects_partial_results():
    trace = HEADER + "".join(codec(1 + i / 1000, "queue", i) for i in range(2050))
    result = run_parser(trace)
    assert result["codec_stats_status"] == "limit_exceeded"
    assert result["codec_latency_max_ms"] == "na"


def test_empty_codec_coverage_is_not_reported_as_healthy():
    result = run_parser(HEADER + marker(1, "B|40|updateTexImage") + marker(1.001, "E"))
    assert result["codec_stats_status"] == "no_markers"
    assert result["codec_matched_n"] == "0"


def shell_path(path):
    value = path.as_posix()
    return "/" + value[0].lower() + value[2:] if re.match(r"^[A-Za-z]:/", value) else value


def test_active_tracer_is_untouched_and_produces_only_skipped_summary(tmp_path):
    shell = shutil.which("sh") or r"C:\Program Files\Git\bin\sh.exe"
    if not Path(shell).is_file():
        pytest.skip("POSIX shell unavailable")
    trace = tmp_path / "tracing"
    trace.mkdir()
    (trace / "tracing_on").write_bytes(b"1\n")
    (trace / "current_tracer").write_bytes(b"nop\n")
    altered = SOURCE.replace(
        "for path in /sys/kernel/tracing /sys/kernel/debug/tracing; do",
        f"for path in '{shell_path(trace)}'; do",
    )
    altered = altered.replace(
        "LOCK=/data/local/tmp/.dashcam_cpt_codec_capture.lock",
        f"LOCK='{shell_path(tmp_path / 'lock')}'",
    )
    script = tmp_path / "capture.sh"
    script.write_bytes(altered.encode("utf-8"))
    logfile = tmp_path / "sanitized.log"
    subprocess.run(
        [shell, shell_path(script), "session-1", "2", shell_path(logfile), "w"],
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    output = logfile.read_text(encoding="utf-8")
    assert "codec_capture_status=trace_busy_or_unavailable" in output
    assert "codec_trace_off=0" in output
    assert "codec_watchdog_fired=0" in output
    assert (trace / "tracing_on").read_text().strip() == "1"
    assert not (tmp_path / "lock").exists()


def test_production_capture_uses_fifo_and_fixed_limits_not_raw_files():
    assert 'atrace -t 5 -b 2048 gfx video 8<&8 > "$PIPE"' in SOURCE
    assert 'mkfifo "$PIPE" 8<&8' in SOURCE
    assert "sleep 8" in SOURCE
    assert "NR>200000 || bytes>16777216" in PROGRAM
    assert "codec_markers>2048" in PROGRAM
    assert 'wait "$ATRACEPID"' in SOURCE
    assert 'STARTED" = 1' in SOURCE and "owns_lock &&" in SOURCE


def test_watchdog_cancels_promptly_without_later_signalling_capture():
    shell = shutil.which("sh") or r"C:\Program Files\Git\bin\sh.exe"
    if not Path(shell).is_file():
        pytest.skip("POSIX shell unavailable")
    watcher = SOURCE.split("(\n  sleep_pid=", 1)[1].split("WATCHPID=$!", 1)[0]
    reaper = (
        "reap_watchdog() {" + SOURCE.split("reap_watchdog() {", 1)[1].split("\nfinish() {", 1)[0]
    )
    command = (
        reaper
        + "\nWATCHDOGFIRED=0\nexec 8</dev/null\nsleep 30 &\nATRACEPID=$!\n(\n  sleep_pid="
        + watcher
        + 'WATCHPID=$!\nsleep 0.1\nreap_watchdog\nkill -0 "$ATRACEPID"\nalive=$?\nprintf "fired=%s watcher=%s\\n" "$WATCHDOGFIRED" "$WATCHPID"\nkill "$ATRACEPID"\nwait "$ATRACEPID" 2>/dev/null\nexit "$alive"\n'
    )
    began = time.monotonic()
    result = subprocess.run([shell, "-c", command], capture_output=True, text=True, timeout=3)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "fired=0 watcher="
    assert time.monotonic() - began < 2


@pytest.mark.parametrize("shell_name", ["sh", "mksh"])
@pytest.mark.parametrize("identity_matches", [True, False])
def test_watchdog_reports_its_deadline_without_signalling_a_changed_identity(
    shell_name, identity_matches, tmp_path
):
    if os.name == "nt":
        pytest.skip("Linux process identities are required for executable watchdog checks")
    shell = shutil.which(shell_name)
    assert shell is not None, f"{shell_name} is required for executable watchdog checks"
    watcher = SOURCE.split("(\n  sleep_pid=", 1)[1].split("WATCHPID=$!", 1)[0]
    watcher = watcher.replace("sleep 8 8<&8", "sleep 0.1 8<&8")
    checkpoint = tmp_path / "deadline-fired"
    watcher = watcher.replace("  exit 124", f"  : > '{checkpoint}'\n  exit 124")
    reaper = (
        "reap_watchdog() {" + SOURCE.split("reap_watchdog() {", 1)[1].split("\nfinish() {", 1)[0]
    )
    identity = (
        'ATRACESTART=$(awk \'{sub(/^.*\\) /,"");print $20}\' "/proc/$ATRACEPID/stat")'
        if identity_matches
        else "ATRACESTART=not-the-owned-lifetime"
    )
    command = (
        reaper
        + "\nWATCHDOGFIRED=0\nexec 8</dev/null\nsleep 30 &\nATRACEPID=$!\n"
        + identity
        + "\n(\n  sleep_pid="
        + watcher
        # Cancel after the deadline has committed, potentially before its final
        # exit. The marker must survive this parent/child cancellation race.
        + f"WATCHPID=$!\nwhile [ ! -f '{checkpoint}' ]; do sleep 0.01; done\n"
        + 'reap_watchdog\nprintf "fired=%s watcher=%s\\n" "$WATCHDOGFIRED" "$WATCHPID"\n'
        + (
            'wait "$ATRACEPID" 2>/dev/null\nprintf "producer=%s\\n" "$?"\n'
            if identity_matches
            else 'kill -0 "$ATRACEPID"\nprintf "alive=%s\\n" "$?"\nkill "$ATRACEPID"\nwait "$ATRACEPID" 2>/dev/null\n'
        )
        + "exit 0\n"
    )
    result = subprocess.run([shell, "-c", command], capture_output=True, text=True, timeout=3)
    assert result.returncode == 0, result.stderr
    assert "fired=1 watcher=\n" in result.stdout
    assert ("producer=137" if identity_matches else "alive=0") in result.stdout
