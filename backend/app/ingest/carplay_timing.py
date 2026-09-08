"""Sampling CarPlay's own frame timing on the head unit, and reading it back.

The operator's complaint was that Zlink -- the unit's CarPlay app -- lags while the rest of
the Android UI stays smooth. SurfaceFlinger observed candidate ``SurfaceView[](BLAST)``
layers while CarPlay was visible; those names do not identify their owning app, so their
timing cannot on its own be attributed to Zlink or to all of CarPlay. The sampler preserves
the observed layer timing for comparable drive-to-drive evidence.

That was measured over adb on the driveway. The question that matters is what happens on a
long drive, where the unit is hot, the recorder has been running for an hour, and -- this
is the part the driveway cannot show -- there is no home network for the single radio to
hop to. There is also no adb. So the sampling runs on the unit itself: a detached toybox
shell script, armed on every visit the way the recording watcher is, that every few seconds
while a non-expired WLAN2 neighbour is present reads each candidate surface's frame timing
from SurfaceFlinger and the things that could be starving it -- load, SoC temperature,
Zlink's own CPU, the hotspot's incoming bitrate, and which channel each radio role is on.

**How it gets home.** Each observation is one line, written to bounded, rotated files on
the unit and emitted
into logcat under the tag ``CarPlayTiming`` at *error* priority. Error priority is not a
statement about severity: the unit-log collector (:mod:`app.ingest.unit_logs`) keeps only
``*:E``, so anything quieter would never ship. The collector then carries the lines into
the database with everything else, and :func:`parse_sample` turns them back into numbers
for the API and the Logs page. No new transport, no new table.

**What it costs.** The expensive reads -- ``dumpsys wifi``, the thermal zones, the CPU
counters -- stay on the fifteen-second cadence, because load and temperature do not move
faster than that. Only the two cheap SurfaceFlinger calls run every four seconds, which is
what it takes to see every frame: the ring holds 127 of them, about 5.3 seconds, so the
original single read per interval observed roughly a third of the drive and missed the
rest. Nothing at all runs while no WLAN2 neighbour is present beyond a heartbeat a minute. It never
changes a setting, a radio or a process.
"""

from __future__ import annotations

import asyncio
import base64
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.core.logging import get_logger
from app.core.settings_service import get_settings_service
from app.ingest import adb
from app.ingest.status import get_status

if TYPE_CHECKING:
    from app.ingest.unit_logs import ParsedLine

log = get_logger(__name__)

#: Where the sampler lives on the unit, and the pid file it keeps itself single-instance
#: with. Deleting these three files removes every trace.
REMOTE_SCRIPT = "/data/local/tmp/dashcam_carplay_timing.sh"
REMOTE_PID = "/data/local/tmp/.dashcam_carplay_timing.pid"
REMOTE_LOG = "/data/local/tmp/dashcam_carplay_timing.log"

# The timing sampler has its own small, rotated file as well as logcat.  Logcat is a
# useful transport when it is quiet, but a noisy tag can evict an entire drive before the
# unit next reaches home.  Keeping several modest files bounds card use and lets the next
# successful presence poll recover observations that logcat no longer contains.
REMOTE_LOG_KIB = 512
REMOTE_LOG_ROTATIONS = 6
MAX_RECOVERY_BYTES_PER_FILE = REMOTE_LOG_KIB * 1024
MAX_RECOVERY_LINES = 20_000

#: The logcat tag every sample carries. The unit-log collector's allow-list must name it.
TAG = "CarPlayTiming"

ENABLED_KEY = "ingest.carplay_timing"
INTERVAL_KEY = "ingest.carplay_timing_interval_s"
DEFAULT_INTERVAL_S = 15
MIN_INTERVAL_S = 5

#: How often the video surfaces are read, as against the context around them.
#:
#: Not a preference, so not a setting: it is derived from the ring SurfaceFlinger keeps,
#: which is 127 frames -- 5.3 s at the 24 fps this link runs at, 4.5 s at 28. Reading it
#: once per ``INTERVAL_KEY`` observed about a third of the drive; at four seconds the
#: windows overlap instead of leaving gaps, and the sampler removes the overlap so nothing
#: is counted twice. Raising it past 4 reopens the gap; lowering it buys nothing, because
#: the frames are already all seen.
FRAME_INTERVAL_S = 4
MAX_INTERVAL_S = 120

ARM_TIMEOUT_S = 20.0

#: Re-arming replaces the running sampler, which throws away the sample it was in the
#: middle of and the CPU baseline it needs for the next one. The presence poll would do
#: that every couple of seconds; once every few minutes is plenty to catch a reboot.
ARM_DEBOUNCE_S = 300.0

_SCRIPT_PATH = Path(__file__).with_name("carplay_timing.sh")
_last_armed: dict[str, float] = {}
_last_recovered: dict[str, float] = {}
_tasks: set[asyncio.Task[None]] = set()


def script() -> str:
    """The sampler, in the unit's own toybox sh. Shipped beside this module."""
    return _SCRIPT_PATH.read_text(encoding="utf-8")


def _enabled() -> bool:
    try:
        return bool(get_settings_service().get_nowait(ENABLED_KEY))
    except Exception:
        return False


def interval_s() -> int:
    try:
        raw = int(get_settings_service().get_nowait(INTERVAL_KEY) or DEFAULT_INTERVAL_S)
    except Exception:
        raw = DEFAULT_INTERVAL_S
    return max(MIN_INTERVAL_S, min(MAX_INTERVAL_S, raw))


async def arm(address: str) -> bool:
    """Deploy the sampler and start it under its own session. True when the launch landed.

    Always re-deploys, for the same reason the recording watcher does: the script is four
    kilobytes and one control call, and an updated app then never has to reason about which
    version a unit is carrying. The script's own pid file makes the restart idempotent.
    """
    if not _enabled():
        return False
    encoded = base64.b64encode(script().encode()).decode()
    every = interval_s()
    try:
        await adb.shell(
            address,
            f"echo {encoded} | base64 -d > {REMOTE_SCRIPT}",
            timeout=ARM_TIMEOUT_S,
        )
        await adb.shell(
            address,
            # `e` is the logcat priority the lines are emitted at -- see the module note.
            f"setsid sh {REMOTE_SCRIPT} {every} e {FRAME_INTERVAL_S} </dev/null >/dev/null 2>&1 &",
            timeout=ARM_TIMEOUT_S,
        )
    except adb.AdbError as exc:
        log.warning("could not arm the CarPlay timing sampler", error=str(exc))
        return False
    log.info(
        "armed the CarPlay timing sampler on the unit",
        interval_s=every,
        frame_interval_s=FRAME_INTERVAL_S,
    )
    return True


def on_unit_present(address: str) -> None:
    """Arm cheaply whenever present; recover retained diagnostics only when parked."""
    if not _enabled():
        return
    now = time.monotonic()
    # File recovery can read several MiB.  The sampler is also armed at departure, where
    # this work would delay the first observation and compete with the reported lag.  The
    # ingest status receives an exact read-only ACC verdict elsewhere; fail closed until it
    # positively says off, then recover on a later presence tick.
    if get_status().ignition_state == "off":
        recovered = _last_recovered.get(address)
        if recovered is None or now - recovered >= ARM_DEBOUNCE_S:
            _last_recovered[address] = now
            task = asyncio.create_task(
                _recover_when_parked(address), name="ingest-carplay-timing-recover"
            )
            _tasks.add(task)
            task.add_done_callback(_tasks.discard)
    last = _last_armed.get(address)
    if last is not None and now - last < ARM_DEBOUNCE_S:
        return
    _last_armed[address] = now
    task = asyncio.create_task(arm(address), name="ingest-carplay-timing")
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def shutdown() -> None:
    for task in list(_tasks):
        task.cancel()
    for task in list(_tasks):
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    _tasks.clear()


def reset_for_tests() -> None:
    _last_armed.clear()
    _last_recovered.clear()


# ----------------------------------------------------------------------------------------
# Reading the samples back.
# ----------------------------------------------------------------------------------------

_KV = re.compile(r"(\w+)=(\S+)")
_STA = re.compile(r"RSSI:(-?\d+)|Frequency:(\d+)MHz")
_FILE_LINE = re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z) (?P<message>.*)$")


def _number(value: str | None) -> float | None:
    if value is None or value == "na":
        return None
    try:
        return float(value.rstrip("%"))
    except ValueError:
        return None


def _int_or_none(value: str | None) -> int | None:
    """A count, or None when the sampler that produced this line did not emit it.

    Distinct from ``int(_number(...) or 0)``: zero hitches and a sampler too old to have
    counted them are different facts, and folding them together would show a day of
    pre-upgrade samples as a day with no holds in it.
    """
    number = _number(value)
    return None if number is None else int(number)


def _neighbour_states(value: str | None) -> dict[str, int] | None:
    """Parse the sampler's aggregate neighbour states without retaining addresses."""
    if value in (None, "na"):
        return None
    states: dict[str, int] = {}
    for item in value.split(","):
        state, separator, count = item.partition(":")
        if not separator or not state:
            return None
        parsed = _int_or_none(count)
        if parsed is None or parsed < 0:
            return None
        states[state] = parsed
    return states or None


def parse_sample(occurred_at: datetime, message: str) -> dict[str, Any] | None:
    """One ``CarPlayTiming`` line as numbers, or None for a heartbeat or anything else.

    A video sample looks like::

        acc=1 phone=1 load=21.8 soc=74.6 zlink_cpu=55 rx_kbit=1631
        sta=RSSI:-33/Frequency:5520MHz/ ap=5180 | layer=#104 fps=23.4 med=35.3
        p95=70.6 max=88.2 late=38% n=126 period=17.5

    Heartbeats (``| no phone on hotspot``) are deliberately dropped: they say the sampler
    is alive, which the operator can see in the raw unit log, but they carry no timing.
    """
    if " | " not in message:
        return None
    head, tail = message.split(" | ", 1)
    if not tail.startswith("layer="):
        return None
    fields = dict(_KV.findall(head))
    fields.update(dict(_KV.findall(tail)))
    fps = _number(fields.get("fps"))
    if fps is None:
        return None
    sta_rssi: int | None = None
    sta_mhz: int | None = None
    for rssi, mhz in _STA.findall(fields.get("sta", "")):
        if rssi:
            sta_rssi = int(rssi)
        if mhz:
            sta_mhz = int(mhz)
    return {
        "occurred_at": occurred_at,
        "session_id": fields.get("session") or None,
        "acc_on": fields.get("acc") == "1",
        # Legacy name retained for existing clients.  It only means a wlan2 neighbour
        # was observed; it does not identify a phone or prove an active CarPlay session.
        "phone_attached": _number(fields.get("phone")) not in (None, 0.0),
        "hotspot_neighbour_count": _int_or_none(fields.get("neigh_count")),
        "hotspot_neighbour_states": _neighbour_states(fields.get("neigh")),
        # Process presence is an observation of /proc, not a decoder health verdict.
        "zlink_process_present": (
            None if fields.get("zlink_proc") in (None, "na") else fields.get("zlink_proc") == "1"
        ),
        "load": _number(fields.get("load")),
        "soc_c": _number(fields.get("soc")),
        "zlink_cpu_pct": _number(fields.get("zlink_cpu")),
        "hotspot_rx_kbit": _number(fields.get("rx_kbit")),
        # What the AP lost in the interval (tx_dropped + tx_errors + rx_dropped), which is
        # the stall that average bitrate cannot show.
        "ap_drops": _number(fields.get("ap_drops")),
        # The OBD logger's CPU, and whether Bluetooth was up at all. Together these are the
        # coexistence question: the logger polls the car over BLE while driving, and BLE
        # shares this unit's one radio with the hotspot CarPlay runs over.
        "obd_cpu_pct": _number(fields.get("obd_cpu")),
        "bluetooth_on": None if fields.get("bt") in (None, "na") else fields.get("bt") == "1",
        "sta_mhz": sta_mhz,
        "sta_rssi": sta_rssi,
        "ap_mhz": int(fields["ap"]) if fields.get("ap", "na").isdigit() else None,
        "layer": fields.get("layer", ""),
        "surface_kind": fields.get("surface_kind") or None,
        # Which of the surfaces this was, in the order SurfaceFlinger listed them. The
        # layer's own `#N` is a sequence number that is reassigned between sessions -- it
        # has been observed as #99/#104 one session and #100/#103 the next, with the fast
        # and slow surfaces swapping which number they carried -- so it cannot be used to
        # follow one surface over time, and neither can the name, which this build prints
        # as a bare `SurfaceView[](BLAST)` with no package. The index at least keeps the
        # surfaces of a single sample apart.
        "layer_index": int(_number(fields.get("idx")) or 0),
        "late_threshold_ms": _number(fields.get("thr")),
        "fps": fps,
        "median_ms": _number(fields.get("med")),
        "p95_ms": _number(fields.get("p95")),
        "max_ms": _number(fields.get("max")),
        "late_pct": _number(fields.get("late")),
        # Holds long enough to see, which is a different question from `late_pct` and the
        # one that matches what a person in the car actually notices. Across a day of
        # driving late_pct sat at a median of 11% while the worst single hold reached
        # 265 ms -- a quarter-second of frozen picture that late_pct scored 26%, because
        # one long hold among many even ones barely moves a rate. The two correlate at
        # r=+0.34: they are not measuring the same thing.
        "hitches": _int_or_none(fields.get("hitch")),
        "frames": int(_number(fields.get("n")) or 0),
        # How many intervals this window actually contributed, and how long they covered.
        # Consecutive reads of the ring overlap, so `new_frames` is below `frames` by the
        # overlap; `span_s` summed across a drive is the coverage the old single-read
        # cadence could not reach. Null on samples from before the sampler counted them.
        "new_frames": _int_or_none(fields.get("new")),
        "span_s": _number(fields.get("span")),
        "period_ms": _number(fields.get("period")),
    }


def parse_event(occurred_at: datetime, message: str) -> dict[str, Any] | None:
    """Return a non-frame sampler observation, without turning absent timing into healthy timing."""
    if " | " not in message:
        return None
    head, tail = message.split(" | ", 1)
    fields = dict(_KV.findall(head))
    event_fields = dict(_KV.findall(tail))
    fields.update(event_fields)
    kind: str | None = None
    if tail.startswith("event="):
        kind = fields.get("event")
    elif tail == "no video surface":
        kind = "surface_unavailable"
    elif tail.startswith("layer=") and "new=0" in tail and "no new frames" in tail:
        kind = "surface_no_new_frames"
    if not kind:
        return None
    sta_mhz: int | None = None
    for _rssi, mhz in _STA.findall(fields.get("sta", "")):
        if mhz:
            sta_mhz = int(mhz)
    return {
        "occurred_at": occurred_at,
        "session_id": fields.get("session") or None,
        "kind": kind,
        "hotspot_neighbour_count": _int_or_none(fields.get("neigh_count")),
        "hotspot_neighbour_states": _neighbour_states(fields.get("neigh")),
        "zlink_process_present": (
            None if fields.get("zlink_proc") in (None, "na") else fields.get("zlink_proc") == "1"
        ),
        "sta_mhz": sta_mhz,
        "ap_mhz": int(fields["ap"]) if fields.get("ap", "na").isdigit() else None,
        "layer": fields.get("layer") or None,
        "surface_kind": fields.get("surface_kind") or None,
        "layer_index": _int_or_none(fields.get("idx")),
    }


def parse_sampler_file(raw: str) -> list[ParsedLine]:
    """Convert the sampler's ISO file lines into UnitLogEntry-compatible records.

    Importing ``unit_logs`` here avoids a module cycle during ordinary startup.  The file
    never contains neighbour addresses, SSIDs, or other device identifiers; only the
    sampler's aggregate diagnostic fields are accepted.
    """
    from app.ingest.unit_logs import MAX_MESSAGE_CHARS, ParsedLine

    entries: list[ParsedLine] = []
    for line in raw.splitlines():
        match = _FILE_LINE.match(line)
        if not match:
            continue
        # Pre-session files cannot be safely deduplicated against logcat because their
        # direct-file timestamp has second precision and no emitting pid.  Skip them
        # rather than doubling historical observations.
        if "sample=" not in match["message"]:
            continue
        try:
            occurred_at = datetime.strptime(match["ts"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        except ValueError:
            continue
        entries.append(
            ParsedLine(
                occurred_at=occurred_at,
                # These direct-file records did not travel through logcat, so there is no
                # trustworthy emitting pid/tid.  Zero is explicit provenance, not a guess.
                pid=0,
                tid=0,
                level="E",
                tag=TAG,
                message=match["message"][:MAX_MESSAGE_CHARS],
            )
        )
        if len(entries) >= MAX_RECOVERY_LINES:
            break
    return entries


def sampler_file_read_command() -> str:
    """Tail every generation oldest first; fixed paths need no shell interpolation."""
    generations = " ".join(f"{REMOTE_LOG}.{index}" for index in range(REMOTE_LOG_ROTATIONS, 0, -1))
    return (
        f"for f in {generations} {REMOTE_LOG}; do "
        f'[ -f "$f" ] && tail -c {MAX_RECOVERY_BYTES_PER_FILE} "$f"; '
        "done; exit 0"
    )


async def recover_sampler_file(address: str) -> tuple[int, int]:
    """Recover retained direct-file observations, returning ``(new, duplicate)``."""
    raw = await adb.shell(address, sampler_file_read_command(), timeout=ARM_TIMEOUT_S)
    # Test fakes from older callers sometimes return the lower-level result object.
    if isinstance(raw, adb.AdbResult):
        raw = raw.stdout
    entries = parse_sampler_file(raw)
    if not entries:
        return 0, 0
    from app.ingest.unit_logs import store

    return await store(entries)


async def _recover_when_parked(address: str) -> None:
    """Best-effort direct-file recovery after a known ignition-off transition."""
    try:
        await recover_sampler_file(address)
    except Exception as exc:
        # Recovery is diagnostic only.  A full database or malformed historical file must
        # not stop the later sampling/ingest work.
        log.warning("could not recover the CarPlay timing sampler file", error=str(exc))


def sessions(samples: list[dict[str, Any]], events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Summarise sampler lifetimes where the script emitted a session marker.

    Older rows remain unassigned instead of being inferred into a session from a time gap.
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in [*samples, *events]:
        session_id = row.get("session_id")
        if session_id:
            grouped.setdefault(str(session_id), []).append(row)
    result: list[dict[str, Any]] = []
    for session_id, rows in grouped.items():
        ordered = sorted(rows, key=lambda row: row["occurred_at"])
        frame_rows = [row for row in ordered if "fps" in row]
        result.append(
            {
                "session_id": session_id,
                "started_at": ordered[0]["occurred_at"],
                "ended_at": ordered[-1]["occurred_at"],
                "sample_count": len(frame_rows),
                "event_count": len(ordered) - len(frame_rows),
                "surface_no_new_frames": sum(
                    1 for row in ordered if row.get("kind") == "surface_no_new_frames"
                ),
                "surface_unavailable": sum(
                    1 for row in ordered if row.get("kind") == "surface_unavailable"
                ),
            }
        )
    return result


def summarise(samples: list[dict[str, Any]], bucket_s: int = 60) -> list[dict[str, Any]]:
    """Per-bucket figures, **one row per surface**, in time order.

    fps and late% are averaged (they are already per-sample rates); p95, max, temperature,
    load, Zlink CPU and bitrate take the worst seen in the bucket, because the question a
    bucket answers is "how bad did it get", not "what was typical".

    Buckets are per surface because there is always more than one. Pooling them produced a
    figure that described neither: measured over 234 samples, one surface ran a 35 ms
    cadence and the other 53 ms in the same minute, and the mean of the two was a number no
    surface ever achieved. They cannot be told apart from the sampler's output -- see
    ``layer_index`` -- so they are kept apart rather than blended, and the caller chooses.
    """
    buckets: dict[tuple[int, str, str | None], list[dict[str, Any]]] = {}
    for sample in samples:
        key = int(sample["occurred_at"].timestamp()) // bucket_s * bucket_s
        # Surface sequence numbers are reused after each sampler re-arm.  Newer records
        # carry a session marker, so never average two lifetimes simply because both had
        # a `#104` layer in the same minute.  Legacy rows intentionally remain together
        # under None because their session cannot be reconstructed safely.
        buckets.setdefault(
            (key, str(sample.get("layer", "")), sample.get("session_id")), []
        ).append(sample)

    def worst(rows: list[dict[str, Any]], field: str) -> float | None:
        values = [r[field] for r in rows if r.get(field) is not None]
        return max(values) if values else None

    def total(rows: list[dict[str, Any]], field: str) -> float | None:
        """Summed, not worst -- and None rather than 0 when nothing reported it.

        Counts and durations add up across a bucket where rates do not. Returning 0 for a
        bucket of samples that predate the field would read as "a minute with no holds in
        it", which is the opposite of "we were not counting".
        """
        values = [r[field] for r in rows if r.get(field) is not None]
        return sum(values) if values else None

    out: list[dict[str, Any]] = []
    for key, layer, session_id in sorted(buckets, key=lambda k: (k[0], k[1], k[2] or "")):
        rows = buckets[(key, layer, session_id)]
        fps = [r["fps"] for r in rows if r.get("fps") is not None]
        late = [r["late_pct"] for r in rows if r.get("late_pct") is not None]
        out.append(
            {
                "bucket_start": datetime.fromtimestamp(key, tz=rows[0]["occurred_at"].tzinfo),
                "layer": layer,
                "session_id": session_id,
                "layer_index": rows[-1].get("layer_index") or 0,
                "samples": len(rows),
                "fps": sum(fps) / len(fps) if fps else None,
                "late_pct": sum(late) / len(late) if late else None,
                # Summed: how many visible holds this minute held, and how much of it was
                # actually observed. `span_s` against `bucket_s` is the coverage figure --
                # it is what says whether a quiet minute was smooth or merely unwatched.
                "hitches": total(rows, "hitches"),
                "span_s": total(rows, "span_s"),
                "p95_ms": worst(rows, "p95_ms"),
                "max_ms": worst(rows, "max_ms"),
                "soc_c": worst(rows, "soc_c"),
                "load": worst(rows, "load"),
                "zlink_cpu_pct": worst(rows, "zlink_cpu_pct"),
                "hotspot_rx_kbit": worst(rows, "hotspot_rx_kbit"),
                "ap_drops": worst(rows, "ap_drops"),
                "obd_cpu_pct": worst(rows, "obd_cpu_pct"),
                "sta_mhz": rows[-1].get("sta_mhz"),
                "ap_mhz": rows[-1].get("ap_mhz"),
            }
        )
    return out
