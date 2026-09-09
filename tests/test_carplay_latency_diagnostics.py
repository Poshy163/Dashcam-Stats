"""Exercise the shipped on-unit programs, including delay hidden by a steady frame rate."""

import os
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.ingest import carplay_timing


def run_awk(program, source, **variables):
    awk = shutil.which("awk") or r"C:\Program Files\Git\usr\bin\awk.exe"
    if not Path(awk).exists():
        pytest.skip("AWK is unavailable")
    args = [awk]
    for key, value in variables.items():
        args.extend(["-v", f"{key}={value}"])
    return subprocess.run(
        [*args, program], input=source, text=True, capture_output=True, check=True
    ).stdout


def surface_stats(rows, seen=0):
    match = re.search(
        r'-v mark="\$mark" \'\n(?P<program>.*?\n\s*})\s*\' \| while read -r stat',
        carplay_timing.script(),
        flags=re.DOTALL,
    )
    assert match
    return run_awk(
        match["program"],
        "16666667\n" + "\n".join(rows) + "\n",
        seen=seen,
        mark=os.devnull,
        layer="#1",
        kind="package_window",
        idx=1,
    )


def test_steady_thirty_fps_can_have_nine_hundred_ms_display_delay():
    times = [10_000_000_000 + i * 33_333_333 for i in range(5)]
    stats = surface_stats([f"0 {t} {t - 900_000_000}" for t in times])
    parsed = carplay_timing.parse_sample(datetime.now(UTC), "schema=2 | " + stats.strip())
    assert parsed["fps"] == 30.0
    assert parsed["ready_to_present_samples"] == 4
    assert parsed["ready_to_present_p95_ms"] == 900.0
    assert parsed["ready_to_present_max_ms"] == 900.0


def test_ready_delay_excludes_overlapping_and_unavailable_fences():
    stats = surface_stats(
        [
            "0 10000000000 9000000000",  # baseline, not a new interval
            "0 10033333333 9000000000",  # already observed long delay
            "0 10066666666 10056666666",  # new 10 ms
            "0 10100000000 9223372036854775807",  # unsignalled fence
            "0 10133333333 10143333333",  # inconsistent future-ready time
        ],
        seen=10033333333,
    )
    assert "ready_n=1 ready_p95=10.0 ready_max=10.0" in stats
    unavailable = surface_stats([f"0 {10_000_000_000 + i * 33_333_333} 0" for i in range(4)])
    assert "ready_n=0 ready_p95=na ready_max=na" in unavailable


def test_socket_summary_filters_uid_and_never_emits_addresses():
    match = re.search(r'awk -v uid="\$uid" \'(.*?)\'\)', carplay_timing.script(), re.S)
    assert match
    header = "  sl local_address rem_address st tx_queue rx_queue tr uid\n"
    source = (
        header
        + (
            "0: PRIVATE:1234 PEER:1111 01 0000000A:00000400 00:0 0 10123\n"
            "1: SECRET:0000 OTHER:1111 01 000000FF:000000FF 00:0 0 99999\n"
        )
        + header
        + "2: PRIVATE:5678 PEER:2222 01 00000014:00000800 00:0 0 10123\n"
    )
    assert run_awk(match[1], source, uid=10123) == (
        "zlink_tcp_sockets=2 zlink_rx_queue_bytes=3072 zlink_tx_queue_bytes=30"
    )
    assert "zlink_tcp_sockets=na" in run_awk(match[1], "", uid=10123)


def test_context_survives_missing_surface_and_direct_file_recovery():
    message = (
        "sample=s-1 session=s schema=2 acc=1 load=20 soc=65 cpu_pressure=2.5 "
        "io_pressure=na zlink_rx_queue_bytes=4096 decoder_cpu=30 cpu_min_khz=768000 "
        "mem_available_kib=123456 | event=diagnostic_context"
    )
    [record] = carplay_timing.parse_sampler_file("2026-09-09T01:00:00Z " + message)
    parsed = carplay_timing.parse_event(record.occurred_at, record.message)
    assert parsed["kind"] == "diagnostic_context"
    assert parsed["zlink_rx_queue_bytes"] == 4096
    assert parsed["cpu_pressure_avg10"] == 2.5
    assert parsed["io_pressure_avg10"] is None
    assert parsed["decoder_service_cpu_pct"] == 30
    assert parsed["acc_on"] is True
    assert parsed["ready_to_present_max_ms"] is None
    legacy = carplay_timing.parse_event(datetime.now(UTC), "session=old | no video surface")
    assert legacy["diagnostic_schema"] is None
    assert legacy["zlink_rx_queue_bytes"] is None
    assert carplay_timing._number("nan") is None


def test_capture_can_run_without_hotspot_neighbours_and_limits_match_recovery():
    script = carplay_timing.script()
    assert '|| { [ "$acc" = 1 ] && [ -n "$zpid" ]; }' in script
    assert f"LOG_KIB={carplay_timing.REMOTE_LOG_KIB}" in script
    assert f"LOG_ROTATIONS={carplay_timing.REMOTE_LOG_ROTATIONS}" in script


def test_recovery_line_cap_keeps_latest_drive(monkeypatch):
    monkeypatch.setattr(carplay_timing, "MAX_RECOVERY_LINES", 2)
    raw = "\n".join(
        f"2026-09-09T01:00:0{i}Z sample=s-{i} session=s | event=diagnostic_context"
        for i in range(4)
    )
    entries = carplay_timing.parse_sampler_file(raw)
    assert len(entries) == 2
    assert "sample=s-2" in entries[0].message
    assert "sample=s-3" in entries[1].message


def test_full_sampler_pass_without_network_or_surface(tmp_path):
    """Execute one actual shell pass: missing neighbours must not suppress diagnostics."""
    bash = r"C:\Program Files\Git\bin\bash.exe" if os.name == "nt" else shutil.which("bash")
    if not bash:
        pytest.skip("Bash is unavailable")
    if not Path(bash).exists():
        pytest.skip("Bash is unavailable")
    commands = {
        "settings": 'case "$*" in *acc_status*) echo 1;; *bluetooth_on*) echo 1;; esac',
        "pidof": 'case "$1" in com.zjinnova.zlink) echo 9999999;; esac',
        "ip": "exit 0",
        "cmd": "exit 0",
        "dumpsys": "exit 0",
        "log": "exit 0",
        "sleep": "exit 0",
    }
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in commands.items():
        path = bindir / name
        path.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8", newline="\n")
        path.chmod(0o755)
    script = carplay_timing.script().replace("/data/local/tmp", tmp_path.as_posix())
    script = script.replace("while :; do", "for cpt_test_pass in 1; do")
    source = tmp_path / "sampler.sh"
    source.write_text(script, encoding="utf-8", newline="\n")
    # Set PATH inside bash so Windows semicolon separators cannot turn into shell paths.
    subprocess.run(
        [
            bash,
            "-c",
            'bin="$1"; command -v cygpath >/dev/null && bin=$(cygpath -u "$1"); '
            'export PATH="$bin:$PATH"; sh "$2" 1 e 4',
            "sampler-test",
            bindir.as_posix(),
            source.as_posix(),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    raw = (tmp_path / "dashcam_carplay_timing.log").read_text()
    entries = carplay_timing.parse_sampler_file(raw)
    events = [carplay_timing.parse_event(e.occurred_at, e.message) for e in entries]
    context = next(e for e in events if e and e["kind"] == "diagnostic_context")
    assert context["hotspot_neighbour_count"] == 0
    assert context["diagnostic_schema"] == 2
    assert context["zlink_tcp_sockets"] is None
    assert any(e and e["kind"] == "surface_unavailable" for e in events)
