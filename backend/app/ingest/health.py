"""Watching the recorder while nobody can — and saying what it saw when the car comes home.

The failures that lose footage are silent and they happen *away from home*: the card's FAT
develops an error and the kernel remounts it read-only (``errors=remount-ro`` is in the
mount options), the recorder stalls under memory pressure, the card fills because recycling
broke. By the time the car is back on the driveway the unit has usually rebooted and the
evidence is gone — all that is left is a gap in the footage, noticed days later.

So a watcher rides along. It is not an app: it is a ~1 KB shell script running under the
unit's own toybox, deployed to ``/data/local/tmp`` (the one place the shell user can write)
and started under ``setsid`` so it gets its own session. That last part is load-bearing and
was confirmed on the unit: a plainly-backgrounded remote command dies the moment its adb
transport drops — adbd SIGHUPs the shell's process group — so it would not survive the car
driving out of range, which is the one thing it must do. ``setsid`` puts it in a session of
its own, and it was verified to outlive both the launching client exiting and a full
``disconnect``/``connect`` bounce of the kind the presence poll does every tick. The engine
starting powers the unit up; the app arms the watcher the moment the unit is seen; the car
drives off; the session drops; the script keeps sampling. Every 20 seconds
it appends one line — unit clock, card writable or read-only, age of the newest recording,
free space, free memory — and when the car is next seen, the app collects the log, reads
the drive's story out of it, and reports anything that went wrong where a person will see
it: the log, the Settings page, and the Home Assistant webhook that reaches a phone.

**The stall rule needs its grace period explained.** The age of the newest ``.ts`` is the
liveness signal — this camera closes a segment roughly every minute, so an age beyond
:data:`STALL_AGE_S` means the recorder has stopped writing. But the first samples of every
trip would trip it falsely: at power-on the newest recording is the last one of the
*previous* drive, hours old, and it stays the newest until the camera closes its first new
segment. So a stall only counts once the trip is at least :data:`STALL_AGE_S` old — by
which time a working recorder has closed several segments — and the trips themselves are
found by splitting the log wherever consecutive samples are further apart than a few
missed beats, which is what an engine-off period looks like.

**What this deliberately is not.** Not an APK (a sideloaded app is *less* privileged than
the shell, gets killed by the unit's power management, and needs reinstalling after every
firmware update), not a daemon with persistence tricks (nothing survives the unit's reboot
except the script file itself, and the next arming replaces it anyway), and not a second
transfer channel (one ``cat`` of a few kilobytes per window, on the control channel, after
the log has already been read). Deleting the two files from ``/data/local/tmp`` removes
every trace; the unit is left exactly as found, which is the standing rule here.
"""

from __future__ import annotations

import asyncio
import base64
import re
import time
from dataclasses import dataclass, field

from app.core.logging import get_logger
from app.core.settings_service import get_settings_service
from app.ingest import adb

log = get_logger(__name__)

#: Where the watcher lives on the unit. ``/data/local/tmp`` because it is the one place
#: uid 2000 can write; both files survive a reboot, which is harmless — the script only
#: runs when armed, and the log is collected and truncated at every arrival.
REMOTE_SCRIPT = "/data/local/tmp/dashcam_health.sh"
REMOTE_LOG = "/data/local/tmp/dashcam_health.log"
REMOTE_PID = "/data/local/tmp/.dashcam_health.pid"

#: How often the watcher samples. Twenty seconds is three lines a minute — ~35 bytes each,
#: a few KB per hour of driving — and comfortably inside the roughly-a-minute cadence the
#: camera closes segments at, so a stall is seen within one segment of it starting.
SAMPLE_INTERVAL_S = 20

#: A recording older than this, well into a trip, means the recorder has stopped writing.
#: The camera closes a segment about every 60 seconds; three misses is a stall, not jitter.
STALL_AGE_S = 180

#: Free space below this is an incident: the camera recycles old footage by itself, so a
#: card that gets this full has recycling broken, and the next stop is "card full, not
#: recording".
LOW_SPACE_KB = 1_048_576  # 1 GiB

#: Consecutive samples further apart than this belong to different trips. Generous — three
#: missed samples — so scheduler jitter on a busy SoC never splits a real trip in two.
TRIP_GAP_S = SAMPLE_INTERVAL_S * 3 + 5

#: Settings keys: the switch, the self-heal switch, and where the last verdict is shown.
ENABLED_KEY = "ingest.unit_health_watch"
FIX_KEY = "ingest.unit_health_fix"
REPORT_KEY = "ingest.unit_health"

#: Ceiling on each control call here, same reasoning as the radio quieting's.
HEALTH_TIMEOUT_S = 6.0

#: How long after arming to leave the unit alone before arming again. The arrival branch
#: of the poll re-runs every tick while the arrival gate holds, and re-arming on each would
#: kill and restart the watcher over and over for nothing.
ARM_DEBOUNCE_S = 120.0

#: What may be passed as the footage directory. It comes from ``resolve_source`` — the
#: unit's own mount table — but it ends up inside a quoted shell argument on the unit, so
#: the same policy as card filenames applies: validated against a conservative shape and
#: refused otherwise, never escaped.
_SAFE_DIR = re.compile(r"^/[A-Za-z0-9/_.-]{1,200}$")

#: One log line: ``epoch|card|age|avail_kb|memavail_kb|fix``. The sixth field is optional so
#: logs written by an older watcher (five fields, no self-heal) still parse. Any field may be
#: ``na``/``nodir``; ``fix`` is ``-`` normally and ``fixtry`` on a cycle that remounted.
_LINE = re.compile(r"^(\d{9,11})\|(\w+)\|(\w+)\|(\w+)\|(\w+)(?:\|([\w-]+))?$")

#: How many self-heal remounts the watcher will attempt in one drive, and how far apart.
#: A card that does not come back after a few tries is failing, not stuck, and wants
#: replacing rather than remounting in a loop — so the watcher gives up and keeps flagging
#: it instead. Both are baked into the on-unit script; the constants exist so the analysis
#: and the tests can speak of the same numbers.
FIX_MAX_ATTEMPTS = 3
FIX_COOLDOWN_S = 90


def script(sample_interval_s: int = SAMPLE_INTERVAL_S) -> str:
    """The watcher itself, in the unit's own toybox sh.

    Single-instance by pid file: each start kills the previous instance, so re-arming on
    every arrival is idempotent rather than an accumulation. The card's writability is
    read from ``/proc/mounts`` rather than probed with a test write — this card is already
    suspected of failing, and 4,000 extra metadata writes a day is not a diagnostic, it is
    an accelerant. A missing vfat line is recorded as unknown rather than as an incident,
    because a unit recording to internal storage has no vfat card and nothing is wrong.
    The log is rotated by line count so it can never grow past a few hundred KB even if
    the car does not come home for weeks.

    **Self-heal.** When ``$2`` is ``1`` and the card has flipped read-only or dropped its
    mount, the watcher remounts it in place — ``sm unmount`` then ``sm mount``, which routes
    through vold, runs ``fsck_msdos`` on the way back up, and returns the card read-write.
    Verified on the unit: the camera resumes recording on its own afterwards. It fires only
    on an already-broken card, so a working recording is never disturbed; it is throttled to
    :data:`FIX_MAX_ATTEMPTS` a drive, :data:`FIX_COOLDOWN_S` apart, because a card that will
    not come back is failing and wants replacing, not looping. ``com.android.shell`` holds
    ``MOUNT_UNMOUNT_FILESYSTEMS`` on this unit, which is what lets an unrooted watcher do it
    at all. Every remount is marked ``fixtry`` in the log so the app can report it.
    """
    return f"""#!/system/bin/sh
# dashcam-stats recording watcher. Deleting this file and its .log removes every trace.
DIR="$1"
FIX="$2"
LOG={REMOTE_LOG}
PIDF={REMOTE_PID}
[ -f "$PIDF" ] && kill "$(cat "$PIDF")" 2>/dev/null
echo $$ > "$PIDF"
fixes=0
lastfix=0
while :; do
  now=$(date +%s)
  card=na; age=na; avail=na; mem=na; fixed=-
  line=$(grep " vfat " /proc/mounts | grep /mnt/media_rw | head -n 1)
  if [ -n "$line" ]; then
    set -- $line
    case "$4" in ro|ro,*) card=ro;; rw|rw,*) card=rw;; esac
  fi
  if [ -d "$DIR" ]; then
    f=$(ls -t "$DIR"/*.ts 2>/dev/null | head -n 1)
    if [ -n "$f" ]; then
      m=$(stat -c %Y "$f" 2>/dev/null)
      [ -n "$m" ] && age=$((now-m))
    fi
    set -- $(df -k "$DIR" 2>/dev/null | tail -n 1)
    [ -n "$4" ] && avail=$4
  else
    age=nodir
  fi
  set -- $(grep MemAvailable /proc/meminfo)
  [ -n "$2" ] && mem=$2
  if [ "$FIX" = "1" ] && {{ [ "$card" = "ro" ] || [ "$age" = "nodir" ]; }}; then
    if [ "$fixes" -lt {int(FIX_MAX_ATTEMPTS)} ] && [ $((now-lastfix)) -gt {int(FIX_COOLDOWN_S)} ]; then
      vol=$(sm list-volumes 2>/dev/null | grep "^public" | head -n 1 | cut -d" " -f1)
      if [ -n "$vol" ]; then
        sm unmount "$vol" >/dev/null 2>&1
        sleep 3
        sm mount "$vol" >/dev/null 2>&1
        fixes=$((fixes+1)); lastfix=$now; fixed=fixtry
      fi
    fi
  fi
  echo "$now|$card|$age|$avail|$mem|$fixed" >> "$LOG"
  n=$(wc -l < "$LOG")
  [ "$n" -gt 4000 ] && {{ tail -n 2000 "$LOG" > "$LOG.t" && mv "$LOG.t" "$LOG"; }}
  sleep {int(sample_interval_s)}
done
"""


@dataclass
class Sample:
    ts: int
    card: str  # "rw", "ro", "na"
    age: int | None  # seconds since the newest recording, None when unreadable
    nodir: bool  # the footage directory itself was missing
    avail_kb: int | None
    mem_kb: int | None
    fix: str = "-"  # "fixtry" on a cycle where the watcher remounted the card


@dataclass
class Report:
    """One collection's verdict: what the watcher saw since the last one."""

    samples: int = 0
    trips: int = 0
    watched_s: int = 0
    incidents: list[str] = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        return not self.incidents

    def summary(self) -> str:
        if not self.samples:
            return "nothing recorded since the last check"
        watched = (
            f"watched {max(1, round(self.watched_s / 60))} min of running time "
            f"across {self.trips} trip(s)"
        )
        if self.healthy:
            return f"{watched}; the recorder looked healthy throughout"
        return f"{watched}; PROBLEMS: " + "; ".join(self.incidents)


def parse(raw: str) -> list[Sample]:
    samples: list[Sample] = []
    for line in raw.splitlines():
        match = _LINE.match(line.strip())
        if not match:
            continue
        ts, card, age, avail, mem, fix = match.groups()
        samples.append(
            Sample(
                ts=int(ts),
                card=card if card in ("rw", "ro") else "na",
                age=int(age) if age.isdigit() else None,
                nodir=age == "nodir",
                avail_kb=int(avail) if avail.isdigit() else None,
                mem_kb=int(mem) if mem.isdigit() else None,
                fix=fix or "-",
            )
        )
    samples.sort(key=lambda s: s.ts)
    return samples


def _trips(samples: list[Sample]) -> list[list[Sample]]:
    trips: list[list[Sample]] = []
    for sample in samples:
        if trips and sample.ts - trips[-1][-1].ts <= TRIP_GAP_S:
            trips[-1].append(sample)
        else:
            trips.append([sample])
    return trips


def _when(ts: int) -> str:
    """The unit's own clock rendered readably; it is the only clock the log has."""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def analyze(samples: list[Sample]) -> Report:
    """Read the drives' story out of the samples. Pure, so the rules are testable.

    Incidents are deduplicated to one line per kind per trip: a stall that lasted ten
    minutes is one problem with a duration, not thirty problems.
    """
    report = Report(samples=len(samples))
    if not samples:
        return report
    trips = _trips(samples)
    report.trips = len(trips)
    for trip in trips:
        start, end = trip[0].ts, trip[-1].ts
        report.watched_s += max(int(SAMPLE_INTERVAL_S), end - start)

        fixes = [s for s in trip if s.fix == "fixtry"]
        # Whether the card was writable again by the end tells "auto-recovered" from "gave
        # up": both are worth surfacing, but only the second is footage lost.
        ended_ro = trip[-1].card == "ro"

        ro = [s for s in trip if s.card == "ro"]
        if ro and fixes and not ended_ro:
            report.incidents.append(
                f"the card went READ-ONLY at {_when(ro[0].ts)} and was automatically "
                f"remounted ({len(fixes)}×), so recording continued — but a card that does "
                f"this is failing and should be replaced soon"
            )
        elif ro:
            tail = (
                f" the watcher tried to remount it {len(fixes)}× and it did not come back;"
                if fixes
                else ""
            )
            report.incidents.append(
                f"the card went READ-ONLY at {_when(ro[0].ts)} — a filesystem error made "
                f"the kernel stop all writes, and recording with it;{tail} the card needs "
                f"replacing"
            )

        # The grace period: at power-on the newest recording is the previous drive's last,
        # so age is meaningless until the trip is old enough to have closed new segments.
        stalls = [
            s
            for s in trip
            if s.age is not None and s.age > STALL_AGE_S and s.ts - start > STALL_AGE_S
        ]
        if stalls:
            span = max(1, round((stalls[-1].ts - stalls[0].ts + SAMPLE_INTERVAL_S) / 60))
            report.incidents.append(
                f"the recorder STOPPED WRITING around {_when(stalls[0].ts)} for ~{span} "
                f"min while the unit was running — that footage does not exist"
            )

        gone = [s for s in trip if s.nodir and s.ts - start > 60]
        if gone:
            recovered = " it was automatically remounted," if fixes else ""
            report.incidents.append(
                f"the recording folder went MISSING at {_when(gone[0].ts)} — the card "
                f"unmounted or was reformatted mid-drive;{recovered} check the card"
            )

    last = samples[-1]
    if last.avail_kb is not None and last.avail_kb < LOW_SPACE_KB:
        report.incidents.append(
            f"the card is nearly full ({last.avail_kb // 1024} MB free) — the camera's "
            f"own recycling should prevent this, so it may be broken"
        )
    return report


def _wanted() -> bool:
    try:
        return bool(get_settings_service().get_nowait(ENABLED_KEY))
    except Exception:
        return False


def _fix_wanted() -> bool:
    """Whether the watcher should remount a broken card, not only report it. Both switches
    must be on: the self-heal is meaningless without the watcher that detects the fault."""
    try:
        settings = get_settings_service()
        return bool(settings.get_nowait(ENABLED_KEY)) and bool(settings.get_nowait(FIX_KEY))
    except Exception:
        return False


async def arm(address: str, source_dir: str) -> bool:
    """Deploy the script and start it under its own session. True when the launch landed.

    Always re-deploys — the script is a kilobyte and one control call, and it means an
    updated app never has to reason about which version a unit is carrying. The launch is
    ``setsid ... </dev/null >/dev/null 2>&1 &``: ``setsid`` detaches it from the adb
    session so it survives the car leaving, and the full redirection is what lets ``adb
    shell`` return promptly rather than hanging the way it does for the ``tar | nc``
    listener (whose stdout is the transfer). The script's own pid file makes the restart
    idempotent, so re-arming on every arrival replaces the watcher rather than stacking
    watchers.
    """
    if not _SAFE_DIR.match(source_dir):
        log.warning("refusing to arm the health watcher on an odd directory", dir=source_dir[:80])
        return False
    fix = "1" if _fix_wanted() else "0"
    encoded = base64.b64encode(script().encode()).decode()
    try:
        await adb.shell(
            address,
            f"echo {encoded} | base64 -d > {REMOTE_SCRIPT}",
            timeout=HEALTH_TIMEOUT_S,
        )
        await adb.shell(
            address,
            f"setsid sh {REMOTE_SCRIPT} '{source_dir}' {fix} </dev/null >/dev/null 2>&1 &",
            timeout=HEALTH_TIMEOUT_S,
        )
    except adb.AdbError as exc:
        log.warning("could not arm the health watcher", error=str(exc))
        return False
    log.info("armed the recording watcher on the unit", dir=source_dir)
    return True


async def _read_log(address: str, *, truncate: bool) -> str | None:
    """The unit's log, optionally emptying it in the same call. None if it cannot be read.

    Truncation is what makes a line reported at most once, so it belongs to :func:`collect`
    (the authoritative per-drive read) and never to :func:`refresh` (the live glance while
    parked, which must not consume what the next arrival will report on). The append/read
    race can drop at most one 35-byte sample, nothing against re-reporting whole drives.
    """
    command = f"cat {REMOTE_LOG} 2>/dev/null"
    if truncate:
        command += f"; : > {REMOTE_LOG} 2>/dev/null"
    command += "; exit 0"
    try:
        return await adb.shell(address, command, timeout=HEALTH_TIMEOUT_S)
    except adb.AdbError as exc:
        log.debug("could not read the health log", error=str(exc))
        return None


async def _publish_report(report: Report) -> None:
    """Put a verdict where a person can see it: the live status and the Settings page.

    The live status first, so the Backup page shows the verdict whether or not the setting
    write succeeds. Import-local to avoid a cycle. This is the part shared by the live
    refresh and the per-drive collect; the webhook is not here because only the arrival
    collect should be able to page a phone.
    """
    from app.ingest.status import get_status

    get_status().set_recorder_health(report.summary(), ok=report.healthy)
    try:
        await get_settings_service().set(
            REPORT_KEY,
            f"as of {time.strftime('%Y-%m-%d %H:%M')}: {report.summary()}",
            internal=True,
        )
    except Exception as exc:
        log.debug("could not persist the health report", error=str(exc))


async def refresh(address: str) -> None:
    """Update the live card from the log so far, without consuming it or paging anyone.

    The glance while parked: on a unit that stays online the arrival collect never fires a
    second time, so this is what keeps the Backup page's recorder-health card current. It
    reads non-destructively and stops at the status/Settings surface — no truncation, no
    webhook — so the next real arrival still gets the whole drive to report on.
    """
    raw = await _read_log(address, truncate=False)
    if not raw:
        return
    report = analyze(parse(raw))
    if report.samples:
        await _publish_report(report)


async def collect(address: str) -> Report | None:
    """Read and truncate the unit's log, and say what the drives looked like.

    The authoritative per-drive read, fired on arrival: it consumes the log so a line is
    reported once, updates the card and Settings page, and — only when something went
    wrong — fires the webhook that reaches a phone.
    """
    raw = await _read_log(address, truncate=True)
    if raw is None:
        return None
    report = analyze(parse(raw))
    if not report.samples:
        return report
    await _publish_report(report)
    if report.healthy:
        log.info("the recording watcher saw no problems", **_fields(report))
    else:
        for incident in report.incidents:
            log.warning("the recording watcher caught a problem", incident=incident)
        # The one channel that reaches a phone. Fired with the incidents attached; the
        # reporter never raises, so this cannot cost the window that is starting.
        try:
            from app.ingest.reporter import publish

            await publish("health", extra={"health_incidents": report.incidents})
        except Exception as exc:
            log.debug("could not publish the health event", error=str(exc))
    return report


def _fields(report: Report) -> dict[str, object]:
    return {
        "trips": report.trips,
        "watched_min": round(report.watched_s / 60),
        "samples": report.samples,
    }


#: How often the live card is refreshed from the log while a unit stays present. Frequent
#: enough that a card is current within a glance of opening the page, rare enough to be one
#: small read on the idle control channel; the arm debounce is longer because arming is the
#: heavier act.
REFRESH_S = 30.0

#: The last time each unit was armed / refreshed, for the two throttles, and the tasks in
#: flight.
_last_armed: dict[str, float] = {}
_last_refresh: dict[str, float] = {}
_tasks: set[asyncio.Task] = set()


def on_unit_seen(address: str, source_dir: str) -> None:
    """Collect the last drives' log and re-arm the watcher. Never blocks the caller.

    Fired by the poller on the arrival transition — which re-runs every tick while the
    arrival gate holds, hence the debounce. Deliberately fired *before* any pull gating: a
    departure window whose pull is held is exactly the drive the watcher must be armed for.
    A watcher needs a directory to watch, so a unit whose card could not even be located is
    left to the pull's own card-error reporting rather than armed against nothing.
    """
    if not _wanted() or not source_dir:
        return
    now = time.monotonic()
    if now - _last_armed.get(address, 0.0) < ARM_DEBOUNCE_S:
        return
    _last_armed[address] = now
    task = asyncio.create_task(_collect_then_arm(address, source_dir), name="ingest-health")
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def on_unit_present(address: str) -> None:
    """Keep the live card current while the unit stays online. Never blocks the caller.

    Fired by the poller on every tick a unit is present, and throttled to :data:`REFRESH_S`
    so it is one small non-destructive read now and then rather than a read per tick. This
    is what makes the recorder-health card mean something on a unit that never leaves — a
    car parked at home, or a bench unit — where the arrival collect fires only once.
    """
    if not _wanted():
        return
    now = time.monotonic()
    if now - _last_refresh.get(address, 0.0) < REFRESH_S:
        return
    _last_refresh[address] = now
    task = asyncio.create_task(refresh(address), name="ingest-health-refresh")
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _collect_then_arm(address: str, source_dir: str) -> None:
    # Collect first: arming restarts the script, and the story of the last drives should
    # be read before anything touches the file it is written in.
    await collect(address)
    await arm(address, source_dir)


async def shutdown() -> None:
    """Cancel in-flight collect/arm tasks. The remote watcher keeps running, by design —
    the app going down is exactly the kind of absence it exists to cover, and it is
    detached from this process's adb sessions anyway."""
    for task in list(_tasks):
        task.cancel()


def reset_for_tests() -> None:
    _last_armed.clear()
    _last_refresh.clear()
    _tasks.clear()
