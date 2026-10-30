"""The on-unit sampler's per-pass helpers must cost no process starts.

Measured on the head unit, a trivial awk costs about 70 ms of CPU and the old helpers
made an idle sampler use about a fifth of a core. These tests run the shipped shell
functions (not reimplementations) under sh and, where available, mksh: the unit's
shell, whose arithmetic is 32-bit and whose echo interprets backslash escapes.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app.ingest import carplay_timing

SOURCE = carplay_timing.script()
SHELLS = ["sh", "mksh"]


def executable(name: str) -> str:
    path = shutil.which(name) or str(Path(r"C:\Program Files\Git\usr\bin") / f"{name}.exe")
    if not Path(path).is_file():
        pytest.skip(f"{name} unavailable")
    return path


def function(name: str, following: str) -> str:
    return (
        name
        + "() {"
        + SOURCE.split(name + "() {", 1)[1].split("\n" + following + "() {", 1)[0]
        + "\n"
    )


def posix(path: Path) -> str:
    text = path.as_posix()
    if re.match(r"^[A-Za-z]:/", text):
        text = "/" + text[0].lower() + text[2:]
    return text


def shell(command: str, shell_name: str = "sh") -> str:
    result = subprocess.run(
        [executable(shell_name), "-s"],
        input="PATH=/usr/bin:/bin:$PATH\n" + command,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.mark.parametrize("shell_name", SHELLS)
@pytest.mark.parametrize(
    ("deadline", "current", "step", "expected"),
    [
        (1000, 1400, 3000, "4000 2.600"),  # work time is subtracted from the period
        (4000, 7800, 3000, "10000 2.200"),  # an overrun skips the missed deadline
        (5000, 5000, 3000, "8000 3.000"),  # exactly on time waits one full period
        (9000, 1000, 15000, "9000 8.000"),  # a future deadline is kept
        (1000, 3995, 3000, "4000 0.005"),  # milliseconds are zero-padded
        (1000, 100000, 3000, "103000 3.000"),  # a long stall never bursts to catch up
    ],
)
def test_deadline_delay_keeps_the_awk_contract(shell_name, deadline, current, step, expected):
    out = shell(
        function("deadline_delay", "cleanup") + f"deadline_delay {deadline} {current} {step}\n",
        shell_name,
    )
    assert out.strip() == expected


@pytest.mark.parametrize("shell_name", SHELLS)
def test_deadline_delay_waits_correctly_across_the_32_bit_boundary(shell_name):
    """Uptime passes 2^31 ms after ~24.8 days; mksh wraps there, the wait must not."""
    command = function("deadline_delay", "cleanup")
    command += 'set -- $(deadline_delay 2147482000 2147483900 3000); echo "$2"\n'
    command += 'set -- $(deadline_delay "$1" 2147485300 3000); echo "$2"\n'
    assert shell(command, shell_name).split() == ["1.100", "2.700"]


@pytest.mark.parametrize("shell_name", SHELLS)
@pytest.mark.parametrize(
    ("uptime", "expected"),
    [
        ("271937.30 100294.41", "271937300"),
        ("0.05 0.00", "50"),
        ("0.00 0.00", "0"),  # leading zeros dropped without base# (not POSIX: dash)
        ("12.3 0", "12300"),
        ("5 0", "5000"),
        ("3000000.99 1.00", "3000000990"),  # beyond 32 bits: concatenated, not computed
        ("garbage 0", ""),
        ("", ""),
    ],
)
def test_clock_ms_reads_uptime_without_awk(tmp_path, shell_name, uptime, expected):
    fixture = tmp_path / "uptime"
    fixture.write_text(uptime + "\n" if uptime else "", encoding="ascii", newline="\n")
    body = function("clock_ms", "stop_codec_worker").replace("/proc/uptime", posix(fixture))
    assert "awk" not in body
    # An unreadable clock prints nothing and returns non-zero; callers only use stdout.
    assert shell(body + "clock_ms; :\n", shell_name).strip() == expected


def _stat_line(comm: str, utime: int, stime: int, start: int) -> str:
    # Fields after "pid (comm)": state is field 3, utime 14, stime 15, starttime 22.
    fields = ["S"] + [str(n) for n in range(4, 53)]
    fields[14 - 3], fields[15 - 3], fields[22 - 3] = str(utime), str(stime), str(start)
    return f"2565 ({comm}) " + " ".join(fields)


@pytest.mark.parametrize("shell_name", SHELLS)
@pytest.mark.parametrize("comm", [".zjinnova.zlink", "odd) name"])
def test_process_start_and_ticks_parse_after_the_command_name(tmp_path, shell_name, comm):
    stat = tmp_path / "2565"
    stat.mkdir()
    (stat / "stat").write_text(_stat_line(comm, 418178, 286498, 2079) + "\n", newline="\n")
    body = function("process_start", "process_ticks") + function("process_ticks", "child_alive")
    assert "awk" not in body
    body = body.replace('"/proc/$1/stat"', f'"{posix(tmp_path)}/$1/stat"')
    out = shell(
        body + "process_start 2565; process_ticks 2565; process_ticks 9999; echo end\n", shell_name
    )
    assert out.split() == ["2079", "704676", "end"]


@pytest.mark.parametrize("shell_name", SHELLS)
def test_pressure_reads_avg10_without_awk(tmp_path, shell_name):
    (tmp_path / "cpu").write_text(
        "some avg10=25.23 avg60=26.87 avg300=29.41 total=12954890147\n"
        "full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n",
        newline="\n",
    )
    body = function("pressure", "diagnostic_context").replace(
        "/proc/pressure/", posix(tmp_path) + "/"
    )
    out = shell(body + "pressure cpu; echo :; pressure missing; echo end\n", shell_name)
    assert out.split() == ["25.23", ":", "end"]


@pytest.mark.parametrize("shell_name", SHELLS)
def test_soc_zone_is_found_once_rounded_and_relocated(tmp_path, shell_name):
    for index, (kind, temp) in enumerate([("cputop0-tzone0", "86630"), ("soc-thmzone", "87130")]):
        zone = tmp_path / f"thermal_zone{index}"
        zone.mkdir()
        (zone / "type").write_text(kind + "\n", newline="\n")
        (zone / "temp").write_text(temp + "\n", newline="\n")
    soc = posix(tmp_path / "thermal_zone1")
    command = function("read_soc_temperature", "pressure")
    command += f"THERMAL_ROOT='{posix(tmp_path)}'\n"
    command += 'read_soc_temperature; echo "$soc $soc_zone"\n'
    # Cached: a renamed type file no longer matters, a new reading does.
    command += f"mv '{soc}/type' '{soc}/type.old'; echo 87150 > '{soc}/temp'\n"
    command += 'read_soc_temperature; echo "$soc"\n'
    # Unreadable zone is looked up again; nothing else is the SoC, so na.
    command += f"rm '{soc}/temp'; read_soc_temperature; echo \"$soc\"\n"
    lines = shell(command, shell_name).splitlines()
    assert lines == [f"87.1 {soc}", "87.2", "na"]


def test_station_summary_awk_matches_the_old_grep_pipeline():
    awk = executable("awk")
    program = SOURCE.split("cmd wifi status 2>/dev/null | awk '", 1)[1].split("')", 1)[0]
    status = (
        "Wifi is enabled\n"
        "==== Primary ClientModeManager instance ====\n"
        'Wifi is connected to "Home"\n'
        'WifiInfo: SSID: "Home", BSSID: 28:70:4e:d1:07:10, RSSI: -68, Link speed: 292Mbps, '
        "Frequency: 5500MHz, Net ID: 0, Frequency: 2412MHz\n"
    )

    def run(text: str) -> str:
        return subprocess.run(
            [awk, program], input=text, capture_output=True, text=True, check=True
        ).stdout

    assert run(status) == "RSSI:-68/Frequency:5500MHz/"
    assert run("Wifi is enabled\nWifi is not connected\n") == ""


@pytest.mark.parametrize("shell_name", SHELLS)
def test_surface_ids_are_taken_without_sed(tmp_path, shell_name):
    executable("awk")
    window = "com.zjinnova.zlink/com.zjinnova.android.zlink.features.main.MainActivity#255"
    body = function("sample_surfaces", "frame_loop")
    assert "| sed" not in body and "sed -n" not in body and 'cat "$mark"' not in body
    command = (
        f"""
dumpsys() {{
  case "$*" in
    *--list*) printf '%s\\n' '{window}' 'SurfaceView[](BLAST)#101' 'com.zjinnova.zlink/bad#x' ;;
    *--latency*) printf '17500000\\n0 1000000000 990000000\\n0 1033333333 1023333333\\n0 1066666666 1056666666\\n0 1100000000 1090000000\\n' ;;
  esac
}}
emit() {{ printf '%s\\n' "$1"; }}
MARKPFX='{posix(tmp_path)}/mark_'; poll_ms=5000; head=HEAD
"""
        + body
        + "sample_surfaces\nsample_surfaces\n"
    )
    rows = shell(command, shell_name).splitlines()
    assert len(rows) == 2
    assert rows[0].startswith("HEAD | layer=#255 ") and " new=3 " in rows[0]
    assert "layer=#255" in rows[1] and "new=0" in rows[1]  # the marker was read back
    assert (tmp_path / "mark_255").read_text().strip() == "1100000000"


def test_expensive_dumps_are_rate_limited_not_run_every_pass():
    # ~2 MB / ~0.5 s of system_server work, measured; only for the hotspot frequency.
    assert SOURCE.count("dumpsys -t 2 wifi") == 1
    wifi = SOURCE.index("dumpsys -t 2 wifi")
    guard = SOURCE.rindex("if [", 0, wifi)
    assert "ap_checked" in SOURCE[guard:wifi] and "-ge 300" in SOURCE[guard:wifi]
    assert '"$phone" != "$prev_neigh"' in SOURCE[guard:wifi]
    # The compositor-thread dump is likewise at most every five minutes.
    queue = SOURCE.index("dumpsys -t 2 SurfaceFlinger 2>/dev/null | surface_queue_summary")
    guard = SOURCE.rindex("if [", 0, queue)
    assert "queue_checked" in SOURCE[guard:queue] and "-ge 300" in SOURCE[guard:queue]
    # iw is absent on this firmware; it is probed once, not through timeout each pass.
    assert "command -v iw" in SOURCE
    assert "timeout 1 iw" in SOURCE.split('if [ "$link_iw" = 1 ]; then', 1)[1].split("fi", 1)[0]


def test_per_pass_reads_use_builtins():
    main = SOURCE.split("\nwhile :; do\n  ensure_gps_worker", 1)[1]
    for gone in (
        "cut -d' ' -f1 /proc/loadavg",
        "cat $z/type",
        "cat /sys/class/net/wlan2/statistics",
        "awk '{print $14+$15}'",
        "| cut -d' ' -f1)",
    ):
        assert gone not in main, gone
    link = SOURCE.split("link_loop() {", 1)[1].split("\n}\n", 1)[0]
    assert "timeout 1 cat /proc/net" not in link and "$(< /proc/net/tcp)" in link


@pytest.mark.parametrize("shell_name", SHELLS)
@pytest.mark.parametrize(
    ("neigh", "fresh"),
    [
        ("r:0,s:3,d:0,p:0,m:0", "0"),  # only leftovers: the phone has gone
        ("r:1,s:2,d:1,p:1,m:0", "3"),
        ("r:0,s:0,d:0,p:0,m:2", "0"),  # PERMANENT entries are not a phone
        ("r:4,s:1,d:0,p:0,m:0", "4"),
    ],
)
def test_only_recently_confirmed_neighbours_make_the_sampler_active(shell_name, neigh, fresh):
    start = SOURCE.index("  fresh=0\n")
    block = SOURCE[start : SOURCE.index("\n  done\n", start) + len("\n  done\n")]
    out = shell(f"neigh='{neigh}'\n" + block + 'echo "$fresh"\n', shell_name)
    assert out.strip() == fresh
    # The total is still reported; activity decisions use the fresh count only.
    assert SOURCE.count('[ "$phone" -gt 0 ] || { [ "$acc" = 1 ]') == 0
    assert SOURCE.count('[ "$fresh" -gt 0 ] || { [ "$acc" = 1 ] && [ -n "$zpid" ]; }') == 3
