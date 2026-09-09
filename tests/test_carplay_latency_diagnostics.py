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


@pytest.mark.parametrize("acc", [0, 1])
def test_full_sampler_pass_without_network_or_surface(tmp_path, acc):
    """Execute one actual shell pass: missing neighbours must not suppress diagnostics."""
    bash = r"C:\Program Files\Git\bin\bash.exe" if os.name == "nt" else shutil.which("bash")
    if not bash:
        pytest.skip("Bash is unavailable")
    if not Path(bash).exists():
        pytest.skip("Bash is unavailable")
    commands = {
        "settings": f'case "$*" in *acc_status*) echo {acc};; *bluetooth_on*) echo 1;; esac',
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
    kind = "diagnostic_context" if acc else "sampler_started"
    context = next(e for e in events if e and e["kind"] == kind)
    assert context["hotspot_neighbour_count"] == 0
    assert context["diagnostic_schema"] == 3
    assert context["zlink_tcp_sockets"] is None
    assert any(e and e["kind"] == "surface_unavailable" for e in events) == bool(acc)


def diagnostic_awk(name, source):
    match = re.search(rf"{name}\(\) \{{\n  awk '(.*?)'\n\}}", carplay_timing.script(), re.S)
    assert match
    return run_awk(match[1], source)


def codec_record(owner="com.zjinnova.zlink", encoder="0", mime="video/avc", avg="156056"):
    properties = {
        "encoder": encoder,
        "mime": mime,
        "latency.avg": avg,
        "latency.max": "640748",
        "latency.min": "14008",
        "latency.n": "25262",
        "lifetimeMs": "859413",
        "low-latency.on": "0",
        "low-latency.off": "0",
        "private": "SECRET",
    }
    body = ", ".join(f"android.media.mediacodec.{k}={v}" for k, v in properties.items())
    return f"  123: {{codec, (09-09 10:53:13.378), ({owner}, 0, 10077), ({body})}}\n"


def test_codec_summary_uses_only_zlink_video_decoder_and_preserves_source_time():
    raw = (
        codec_record()
        + codec_record(owner="camera.recorder")
        + codec_record(encoder="1")
        + codec_record(mime="audio/aac")
        + codec_record(owner="com.zjinnova.zlink.fake")
    )
    result = diagnostic_awk("codec_summary", raw)
    assert len(result.splitlines()) == 1
    assert "SECRET" not in result and "10077" not in result
    event = carplay_timing.parse_event(datetime.now(UTC), "schema=3 | " + result.strip())
    assert event["codec_reported_local"] == "09-09_10:53:13.378"
    assert event["codec_latency_avg_us"] == 156056
    assert event["codec_latency_max_us"] == 640748
    assert event["codec_latency_n"] == 25262
    assert event["codec_lifetime_ms"] == 859413
    assert event["codec_low_latency_on"] == 0
    assert event["gfx_frames"] is None


def test_codec_missing_or_nonnumeric_metrics_do_not_become_zero_or_private_text():
    result = diagnostic_awk("codec_summary", codec_record(avg="SECRET"))
    assert "codec_latency_avg_us=na" in result and "SECRET" not in result
    assert diagnostic_awk("codec_summary", "Permission Denial\n") == ""
    assert (
        carplay_timing.parse_event(datetime.now(UTC), "schema=3 | event=codec_summary")[
            "codec_latency_avg_us"
        ]
        is None
    )


def test_gfx_summary_preserves_reset_epoch_and_excludes_other_apps_and_view_titles():
    result = diagnostic_awk(
        "graphics_summary",
        """** Graphics info for pid 42 [com.zjinnova.zlink] **
Stats since: 35373206938382ns
Total frames rendered: 69154
Janky frames: 1170 (1.69%)
95th percentile: 21ms
Number High input latency: 14394
Number Slow UI thread: 588
View title SECRET
** Graphics info for pid 99 [other.app] **
Total frames rendered: 999999
""",
    )
    event = carplay_timing.parse_event(datetime.now(UTC), "schema=3 | " + result.strip())
    assert event["gfx_frames"] == 69154
    assert event["gfx_since_ns"] == 35373206938382
    assert event["gfx_p95_ms"] == 21
    assert event["gfx_high_input_latency"] == 14394
    assert "SECRET" not in result and "999999" not in result
    assert "gfx_frames=na" in diagnostic_awk("graphics_summary", "Permission Denial\n")


def test_device_network_counters_follow_headers_and_missing_is_unavailable():
    result = diagnostic_awk(
        "network_summary",
        """Tcp: OutSegs RetransSegs InSegs
Tcp: 200 7 100
Udp: SndbufErrors RcvbufErrors
Udp: 0 3
""",
    )
    assert result.strip() == (
        "event=network_summary device_tcp_retrans_segs=7 "
        "device_udp_rcvbuf_errors=3 device_udp_sndbuf_errors=0"
    )
    assert "device_tcp_retrans_segs=na" in diagnostic_awk("network_summary", "")
