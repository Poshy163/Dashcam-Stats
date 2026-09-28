"""Exercise the on-unit GPS parser and its independent, owned worker offline."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "backend/app/ingest/carplay_timing.sh"
SOURCE = SCRIPT.read_text(encoding="utf-8")
PROGRAM = SOURCE.split("# BEGIN_GPS_AWK\n", 1)[1].split("# END_GPS_AWK", 1)[0]
FIXTURE = """Location Manager State:
  Location Settings:
    Location Setting: true
  Location Providers:
    gps provider:
      listeners:
        1000/com.private.recorder/PRIVATE Request[WorkSource{1000 com.zjinnova.zlink}]
      last location=Location[gps 12.123456,98.654321 hAcc=1.771084 et=+9d21h40m44s461ms alt=148.492 {Bundle[{satellites=32, maxCn0=44}]}]
      enabled=true
      mStarted=true   (changed +7m56s443ms ago)
      GNSS_KPI_START
        Number of location reports: 57209
        Number of TTFF reports: 41
        TTFF mean (sec): 45.11453658536585
        TTFF standard deviation (sec): 54.60063701678679
      GNSS_KPI_END
    network provider:
      last location=null
      enabled=false
    fused provider:
      last location=Location[fused 12.123456,98.654321 hAcc=2.2 et=+9d21h40m34s275ms]
      enabled=true
  Historical Aggregate Location Provider Data:
    gps:
      1000/com.zjinnova.zlink: locations = 999
  GNSS Manager:
    Hardware model name: PRIVATE_DEVICE
"""


def executable(name):
    path = shutil.which(name) or str(Path(r"C:\Program Files\Git\usr\bin") / f"{name}.exe")
    if not Path(path).is_file():
        pytest.skip(f"{name} unavailable")
    return path


def parse(text=FIXTURE, now=855645636, rc=0):
    if rc is not None:
        text += f"\n__GPS_DUMP_RC__={rc}\n"
    result = subprocess.run(
        [executable("awk"), "-v", f"now_ms={now}", PROGRAM],
        input=text,
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    assert not result.stderr
    for token in result.stdout.split():
        assert re.fullmatch(
            r"[a-z0-9_]+=(?:na|ok|unsupported|dump_error|limit|[0-9]+(?:\.[0-9]+)?)", token
        ), token
    assert all(
        secret not in result.stdout
        for secret in ("PRIVATE", "com.", "12.123456", "98.654321", "WorkSource", "alt=")
    )
    return dict(token.split("=", 1) for token in result.stdout.split())


def function(name, following):
    return (
        name
        + "() {"
        + SOURCE.split(name + "() {", 1)[1].split("\n" + following + "() {", 1)[0]
        + "\n"
    )


def shell(command, timeout=5):
    return subprocess.run(
        [executable("sh"), "-c", "PATH=/usr/bin:/bin:$PATH\n" + command],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def test_freshness_scope_cumulative_counters_and_privacy():
    row = parse()
    assert row["gps_capture_status"] == "ok"
    assert row["gps_uptime_ms"] == "855645636"
    assert row["loc_gps_fix_elapsed_ms"] == "855644461"
    assert row["loc_gps_age_ms"] == "1175"
    assert row["loc_fused_age_ms"] == "11361"
    assert row["loc_gps_hacc_m"] == "1.771084"
    assert row["loc_gps_satellites"] == "32"
    assert row["gnss_reports"] == "57209"
    assert row["gnss_ttff_reports"] == "41"
    assert row["loc_network_present"] == "1" and row["loc_network_age_ms"] == "na"
    assert row["loc_network_enabled"] == "0"
    assert row["loc_passive_present"] == "0" and row["loc_passive_zlink_listener"] == "na"
    # A WorkSource reference and historic registration are not a current listener.
    assert row["loc_gps_zlink_listener"] == "0"
    direct = FIXTURE.replace("1000/com.private.recorder/PRIVATE", "1000/com.zjinnova.zlink/PRIVATE")
    assert parse(direct)["loc_gps_zlink_listener"] == "1"
    assert parse(FIXTURE.replace("\n", "\r\n")) == row


@pytest.mark.parametrize(
    "duration,expected",
    [
        ("+1d2h3m4s5ms", "93784005"),
        ("+12ms", "12"),
        ("+2s0ms", "2000"),
        ("+0ms", "0"),
        ("0", "0"),
        ("+", "na"),
        ("+BADPRIVATE", "na"),
        ("+1s2s", "na"),
        ("+1ms2s", "na"),
        ("+9007199254740992ms", "na"),
    ],
)
def test_elapsed_fix_duration_parser(duration, expected):
    raw = re.sub(r"et=\+[^ ]+", "et=" + duration, FIXTURE)
    assert parse(raw)["loc_gps_fix_elapsed_ms"] == expected


def test_future_fix_unknown_fields_and_unknown_provider_do_not_look_healthy():
    assert parse(now=1)["loc_gps_age_ms"] == "na"
    sparse = "Location Manager State:\n  Location Providers:\n    gps provider:\n"
    row = parse(sparse)
    assert row["gps_capture_status"] == "ok" and row["loc_gps_present"] == "1"
    assert all(
        row[key] == "na"
        for key in (
            "location_enabled",
            "gps_started",
            "loc_gps_age_ms",
            "loc_gps_hacc_m",
            "gnss_reports",
        )
    )
    foreign = (
        sparse
        + "    private provider:\n      enabled=true\n      last location=Location[gps PRIVATE hAcc=1 et=+1ms]\n"
    )
    assert parse(foreign)["loc_gps_fix_elapsed_ms"] == "na"


@pytest.mark.parametrize("rc", [124, 137, None])
def test_failed_or_incomplete_dump_discards_partial_fields(rc):
    row = parse(rc=rc)
    assert row == {
        "gps_capture_status": "dump_error",
        "gps_dump_rc": "na" if rc is None else str(rc),
        "gps_uptime_ms": "855645636",
    }


@pytest.mark.parametrize(
    "footer",
    [
        "*** SERVICE location DUMP TIMEOUT (3000ms) EXPIRED ***",
        "Error dumping service info: PRIVATE",
        "Permission Denial: PRIVATE",
        "Can't find service: location",
    ],
)
def test_zero_exit_service_failure_discards_full_looking_prefix(footer):
    row = parse(FIXTURE + footer + "\n", rc=0)
    assert row == {
        "gps_capture_status": "dump_error",
        "gps_dump_rc": "0",
        "gps_uptime_ms": "855645636",
    }


@pytest.mark.parametrize(
    "raw",
    [
        "PRIVATE",
        "Location Manager State:\n",
        "PRIVATE" * 10000,
        "\n" * 50001,
        ("x" * 100 + "\n") * 42000,
    ],
    ids=["unknown", "header_only", "line_limit", "row_limit", "byte_limit"],
)
def test_unsupported_and_resource_limits(raw):
    row = parse(raw)
    assert row["gps_capture_status"] in {"unsupported", "limit"}
    assert not any(key.startswith("loc_") for key in row)


def test_successful_event_fits_existing_logcat_message_budget():
    row = parse()
    message = "sample=1790254799-54336041-32274-gps-855645000-11520 session=1790254799-54336041-32274 schema=7 acc=0 | event=gps_context gps_capture_start_ms=855645000 gps_capture_ms=636 gps_poll_gap_ms=15000 location_mode=3 gps_zlink_process_present=1 gps_native_process_present=1 "
    message += " ".join(f"{key}={value}" for key, value in row.items())
    assert len(message.encode()) < 2000


def test_actual_poll_reads_only_expected_commands_and_emits_private_safe_idle_event(tmp_path):
    dump = tmp_path / "location.txt"
    dump.write_text(FIXTURE, encoding="utf-8")
    path = dump.as_posix()
    if re.match(r"^[A-Za-z]:/", path):
        path = "/" + path[0].lower() + path[2:]
    command = function("gps_summary", "gps_process_present").replace("${1:-live}", "855645636")
    command += function("gps_process_present", "gps_poll") + function(
        "gps_poll", "gps_worker_cleanup"
    )
    command += f"""
clock_ms() {{ printf 855645000; }}
timeout() {{ shift; "$@"; }}
settings() {{
  case "$*" in 'get global acc_status') printf 0;; 'get secure location_mode') printf 3;; *) exit 99;; esac
}}
pidof() {{ case "$1" in com.zjinnova.zlink) printf '123 456';; z-link) return 1;; *) exit 99;; esac; }}
dumpsys() {{ [ "$*" = '-t 3 location' ] || exit 99; cat '{path}'; }}
save_message() {{ printf '%s\\n' "$1"; }}
gps_previous=0; gps_seq=0; gps_token=855645000; SESSION=session-1
acc=0; phone=0; FRAME_CONTEXT=/does/not/exist
gps_poll
"""
    result = shell(command)
    assert result.returncode == 0 and not result.stderr, result.stderr
    message = result.stdout.strip()
    assert "schema=7 acc=0 | event=gps_context" in message
    assert "gps_capture_status=ok" in message and "loc_gps_age_ms=1175" in message
    assert "gps_zlink_process_present=1 gps_native_process_present=0" in message
    assert "gps_poll_gap_ms=na" in message
    assert len(message.encode()) < 2000
    assert all(
        secret not in message for secret in ("PRIVATE", "com.", "12.123456", "98.654321", "123 456")
    )


def test_idle_loop_starts_immediately_and_skips_missed_deadlines(tmp_path):
    clock = str(tmp_path / "clock").replace("\\", "/")
    if re.match(r"^[A-Za-z]:/", clock):
        clock = "/" + clock[0].lower() + clock[2:]
    command = function("deadline_delay", "cleanup") + function("gps_loop", "ensure_gps_worker")
    command += f"""
clock_ms() {{ cat '{clock}'; }}
gps_worker_cleanup() {{ :; }}
process_start() {{ printf 1; }}
child_alive() {{ [ "$iterations" -lt 3 ]; }}
gps_poll() {{
  iterations=$((iterations+1)); stamp=$(clock_ms)
  printf 'sample:%s:acc=%s:phone=%s\\n' "$stamp" "$acc" "$phone"
  work=1000; [ "$iterations" -ne 1 ] || work=23000
  echo $((stamp+work)) > '{clock}'
}}
sleep() {{
  printf 'delay:%s\\n' "$1"
  increment=$(awk -v seconds="$1" 'BEGIN {{printf "%.0f",seconds*1000}}')
  echo $(( $(clock_ms)+increment )) > '{clock}'
}}
echo 1000 > '{clock}'
iterations=0; acc=0; phone=0; FRAME_CONTEXT=/does/not/exist
SAMPLER_PID=1; SAMPLER_START=1; gps_loop
"""
    result = shell(command)
    assert result.returncode == 0, result.stderr
    assert not result.stderr, result.stderr
    assert result.stdout.splitlines() == [
        "sample:1000:acc=0:phone=0",
        "delay:7.000",
        "sample:31000:acc=0:phone=0",
        "delay:14.000",
        "sample:46000:acc=0:phone=0",
        "delay:14.000",
    ]


def test_worker_term_interrupts_sleep_and_reaps_owned_child():
    command = function("child_alive", "codec_alive") + function("deadline_delay", "cleanup")
    command += function("gps_worker_cleanup", "gps_loop") + function(
        "gps_loop", "ensure_gps_worker"
    )
    # Stable test identities replace /proc, which Git for Windows does not expose
    # with Linux start ticks. kill -0, TERM delivery, wait and traps remain real.
    command += """
process_start() { printf 1; }
clock_ms() { printf 1000; }
gps_poll() { :; }
SAMPLER_PID=$$; SAMPLER_START=1
gps_loop &
worker=$!
sleep 0.2
kill "$worker"
wait "$worker"
"""
    result = shell(command, timeout=3)
    assert result.returncode == 0, result.stderr


def test_reused_sleep_pid_is_not_signalled():
    command = function("child_alive", "codec_alive") + function("gps_worker_cleanup", "gps_loop")
    command += """
process_start() { printf 222; }
kill() { printf 'UNEXPECTED_SIGNAL\\n'; }
wait() { :; }
gps_sleep_pid=123; gps_sleep_start=111
gps_worker_cleanup
"""
    result = shell(command)
    assert result.returncode == 0 and not result.stdout, result


def test_term_during_bounded_read_does_not_emit_or_start_another_capture():
    command = function("child_alive", "codec_alive") + function("deadline_delay", "cleanup")
    command += function("gps_worker_cleanup", "gps_loop") + function(
        "gps_loop", "ensure_gps_worker"
    )
    command += """
process_start() { printf 1; }
clock_ms() { printf 1000; }
gps_poll() { printf 'capture_started\\n'; timeout 1 sleep 5; printf 'UNEXPECTED_EMIT\\n'; }
SAMPLER_PID=$$; SAMPLER_START=1
gps_loop &
worker=$!
sleep 0.2
kill "$worker"
wait "$worker"
"""
    result = shell(command, timeout=3)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["capture_started"]


@pytest.mark.parametrize(
    "result,rc,expected",
    [
        ("123 456", 0, "1"),
        ("", 1, "0"),
        ("", 124, "na"),
        ("PRIVATE", 1, "na"),
        ("", 0, "na"),
        ("123 PRIVATE", 0, "na"),
    ],
)
def test_process_presence_is_tristate_and_never_emits_pid(result, rc, expected):
    command = function("gps_process_present", "gps_poll")
    command += f"""
pidof() {{ :; }}
timeout() {{ printf '%s' '{result}'; return {rc}; }}
gps_process_present com.zjinnova.zlink
"""
    parsed = shell(command)
    assert parsed.returncode == 0 and parsed.stdout == expected


def test_parent_cleanup_and_startup_include_gps_worker():
    cleanup = function("cleanup", "save_message")
    assert '"$gps_pid:$gps_worker_start"' in cleanup and '"$gps_pid"' in cleanup
    assert "ensure_gps_worker\nwhile :; do\n  ensure_gps_worker" in SOURCE
    assert "schema=6" not in SOURCE
    result = subprocess.run(
        [executable("sh"), "-n", str(SCRIPT)], capture_output=True, text=True, timeout=5
    )
    assert result.returncode == 0, result.stderr
