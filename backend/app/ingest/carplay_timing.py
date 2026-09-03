"""Sampling CarPlay's own frame timing on the head unit, and reading it back.

The operator's complaint was that Zlink -- the unit's CarPlay app -- lags while the rest of
the Android UI stays smooth. Measured on the unit with CarPlay in use, that is exactly what
the numbers say: Zlink's own views render cleanly (``gfxinfo``: under one percent janky),
while the *video surface* CarPlay is drawn on -- a ``SurfaceView[](BLAST)`` layer, which
``gfxinfo`` does not cover -- delivers 23-26 frames a second with a quarter to a third of
them landing two or more frames late (SurfaceFlinger: median interval 35 ms, p95 70 ms,
worst 106 ms). The lag lives in the video path.

That was measured over adb on the driveway. The question that matters is what happens on a
long drive, where the unit is hot, the recorder has been running for an hour, and -- this
is the part the driveway cannot show -- there is no home network for the single radio to
hop to. There is also no adb. So the sampling runs on the unit itself: a detached toybox
shell script, armed on every visit the way the recording watcher is, that every few seconds
while a phone is attached to the CarPlay hotspot reads each video surface's frame timing
from SurfaceFlinger and the things that could be starving it -- load, SoC temperature,
Zlink's own CPU, the hotspot's incoming bitrate, and which channel each radio role is on.

**How it gets home.** Each sample is one line, written to a file on the unit and emitted
into logcat under the tag ``CarPlayTiming`` at *error* priority. Error priority is not a
statement about severity: the unit-log collector (:mod:`app.ingest.unit_logs`) keeps only
``*:E``, so anything quieter would never ship. The collector then carries the lines into
the database with everything else, and :func:`parse_sample` turns them back into numbers
for the API and the Logs page. No new transport, no new table.

**What it costs.** Four ``dumpsys`` calls and a few file reads every sample -- about a
fifth of a second of work every fifteen seconds -- and nothing at all while no phone is
attached beyond a heartbeat a minute. It never changes a setting, a radio or a process.
"""

from __future__ import annotations

import asyncio
import base64
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from app.core.logging import get_logger
from app.core.settings_service import get_settings_service
from app.ingest import adb

log = get_logger(__name__)

#: Where the sampler lives on the unit, and the pid file it keeps itself single-instance
#: with. Deleting these three files removes every trace.
REMOTE_SCRIPT = "/data/local/tmp/dashcam_carplay_timing.sh"
REMOTE_PID = "/data/local/tmp/.dashcam_carplay_timing.pid"
REMOTE_LOG = "/data/local/tmp/dashcam_carplay_timing.log"

#: The logcat tag every sample carries. The unit-log collector's allow-list must name it.
TAG = "CarPlayTiming"

ENABLED_KEY = "ingest.carplay_timing"
INTERVAL_KEY = "ingest.carplay_timing_interval_s"
DEFAULT_INTERVAL_S = 15
MIN_INTERVAL_S = 5
MAX_INTERVAL_S = 120

ARM_TIMEOUT_S = 20.0

#: Re-arming replaces the running sampler, which throws away the sample it was in the
#: middle of and the CPU baseline it needs for the next one. The presence poll would do
#: that every couple of seconds; once every few minutes is plenty to catch a reboot.
ARM_DEBOUNCE_S = 300.0

_SCRIPT_PATH = Path(__file__).with_name("carplay_timing.sh")
_last_armed: dict[str, float] = {}
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
            f"setsid sh {REMOTE_SCRIPT} {every} e </dev/null >/dev/null 2>&1 &",
            timeout=ARM_TIMEOUT_S,
        )
    except adb.AdbError as exc:
        log.warning("could not arm the CarPlay timing sampler", error=str(exc))
        return False
    log.info("armed the CarPlay timing sampler on the unit", interval_s=every)
    return True


def on_unit_present(address: str) -> None:
    """Called from the presence poll. Arms in the background, at most once per debounce."""
    if not _enabled():
        return
    now = time.monotonic()
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


# ----------------------------------------------------------------------------------------
# Reading the samples back.
# ----------------------------------------------------------------------------------------

_KV = re.compile(r"(\w+)=(\S+)")
_STA = re.compile(r"RSSI:(-?\d+)|Frequency:(\d+)MHz")


def _number(value: str | None) -> float | None:
    if value is None or value == "na":
        return None
    try:
        return float(value.rstrip("%"))
    except ValueError:
        return None


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
        "acc_on": fields.get("acc") == "1",
        "phone_attached": _number(fields.get("phone")) not in (None, 0.0),
        "load": _number(fields.get("load")),
        "soc_c": _number(fields.get("soc")),
        "zlink_cpu_pct": _number(fields.get("zlink_cpu")),
        "hotspot_rx_kbit": _number(fields.get("rx_kbit")),
        "sta_mhz": sta_mhz,
        "sta_rssi": sta_rssi,
        "ap_mhz": int(fields["ap"]) if fields.get("ap", "na").isdigit() else None,
        "layer": fields.get("layer", ""),
        "fps": fps,
        "median_ms": _number(fields.get("med")),
        "p95_ms": _number(fields.get("p95")),
        "max_ms": _number(fields.get("max")),
        "late_pct": _number(fields.get("late")),
        "frames": int(_number(fields.get("n")) or 0),
        "period_ms": _number(fields.get("period")),
    }


def summarise(samples: list[dict[str, Any]], bucket_s: int = 60) -> list[dict[str, Any]]:
    """Per-bucket figures across every surface, in time order.

    fps and late% are averaged (they are already per-sample rates); p95, max, temperature,
    load, Zlink CPU and bitrate take the worst seen in the bucket, because the question a
    bucket answers is "how bad did it get", not "what was typical".
    """
    buckets: dict[int, list[dict[str, Any]]] = {}
    for sample in samples:
        key = int(sample["occurred_at"].timestamp()) // bucket_s * bucket_s
        buckets.setdefault(key, []).append(sample)

    def worst(rows: list[dict[str, Any]], field: str) -> float | None:
        values = [r[field] for r in rows if r.get(field) is not None]
        return max(values) if values else None

    out: list[dict[str, Any]] = []
    for key in sorted(buckets):
        rows = buckets[key]
        fps = [r["fps"] for r in rows if r.get("fps") is not None]
        late = [r["late_pct"] for r in rows if r.get("late_pct") is not None]
        out.append(
            {
                "bucket_start": datetime.fromtimestamp(key, tz=rows[0]["occurred_at"].tzinfo),
                "samples": len(rows),
                "fps": sum(fps) / len(fps) if fps else None,
                "late_pct": sum(late) / len(late) if late else None,
                "p95_ms": worst(rows, "p95_ms"),
                "max_ms": worst(rows, "max_ms"),
                "soc_c": worst(rows, "soc_c"),
                "load": worst(rows, "load"),
                "zlink_cpu_pct": worst(rows, "zlink_cpu_pct"),
                "hotspot_rx_kbit": worst(rows, "hotspot_rx_kbit"),
                "sta_mhz": rows[-1].get("sta_mhz"),
                "ap_mhz": rows[-1].get("ap_mhz"),
            }
        )
    return out
