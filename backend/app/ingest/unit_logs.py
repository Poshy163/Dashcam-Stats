"""Bringing the head unit's own system log back, without drowning in it.

The unit ships with logging switched off — ``persist.log.tag=S`` and no ``logd`` process at
all — so the vendor recorder fails silently and leaves nothing behind to read.  That is
comfortable for the vendor and useless for anyone trying to work out why an hour of footage
is missing.  This module turns the useful part of the log back on and carries it home.

**Why the writers are silenced, not only the reader.**  The first version cleared
``persist.log.tag`` outright and filtered on the reading side.  That unleashed every
chatty native component -- the media codec and camera server alone wrote hundreds of
kilobytes a second -- and the cost is paid by the *writers* and by ``logd`` whether or
not anyone reads the result.  Measured back to back on the unit: ``logd`` at 23.6% of a
core with the flood on, 14.9% with it off, and the main buffer rolling over more than a
thousand lines in ten seconds versus none at all -- on a chip where CarPlay is starving
for exactly that kind of headroom.  So the vendor's blanket ``persist.log.tag=S`` is
kept, and only an allow-list of tags is raised to error level with ``log.tag.<tag>``:
the crash reporters, the process killers, the thermal service, the recorder's own
liveness line, and the CarPlay timing sampler.  Everything else is never written.  The
reading-side filter below still applies, because ``log.tag`` does not gate the kernel
buffer and a raised tag can still be noisy.

**Why a filter is the whole design.**  Measured on the live unit: at warning level and
above the log runs at roughly 22 KiB/s — about 80 MB an hour — and 79% of it is two tags,
``ParamSet`` and ``isp_alg_fw``, which are the camera ISP printing its tuning parameters
frame by frame.  Shipping that over the car's link and into the database would cost more
than it could ever explain.  Silencing the known-noisy tags and keeping error level and
above elsewhere measured at 5 KiB per 15 seconds — a 66x reduction, about 1.2 MB an hour —
and what survives is exactly the evidence worth having:

``ZQC-CamSubStream0``/``1``
    The built-in recorder reporting its real per-camera frame rate (``ObtainYuvRate:16/s,
    cameraId 0``).  This is a far better liveness signal than the file-age heuristic in
    :mod:`app.ingest.health`, because it distinguishes "this camera is producing frames"
    from "some camera wrote a file recently".
``UnisocWatchdog``, ``lowmemorykiller``, ``ActivityManager``
    The recorder being killed rather than crashing — the failure mode that leaves no
    crash log and no footage.
``ThermalManagerService``
    The unit sits at 64-68°C on a desk; in a sunlit car it is worse, and heat is the
    prime suspect for both card wear and the observed recorder stalls.
kernel ``mmc``/``FAT-fs``/``blk_update_request``
    The card developing errors, which is what ``errors=remount-ro`` turns into a silent
    stop.

The deny list is a setting, not a constant, because the right list is a property of the
vendor firmware rather than of this code — a different unit will be noisy in different
places, and the person holding it should be able to say so without a redeploy.

**Why no script this time.**  :mod:`app.ingest.health` deploys a shell script because it
samples state that no existing tool reports.  Here ``logcat`` already does the whole job:
``-f`` writes to a file, ``-r``/``-n`` rotate it with a hard ceiling, and ``-v year -v UTC``
stamps every line with an unambiguous absolute timestamp, so nothing has to infer the year
or reason about the unit's timezone.  One detached process, no script to version, and the
size bound is enforced by the tool rather than by our arithmetic.

**Why collect-then-rearm rather than truncate.**  The capture holds the current file open.
Truncating underneath a writer that is not in append mode leaves it writing at its old
offset, which pads the file with NULs and corrupts exactly the evidence we came for.  So a
consuming read kills the capture, drains every rotation, deletes them, and starts a fresh
one — which is what re-arming does anyway.  The non-destructive glance used while the car
is parked at home reads without deleting, and the unique constraint on the line hash means
re-reading the same lines is free.

Nothing is installed.  Deleting the log files from ``/data/local/tmp`` removes every trace,
and ``persist.log.tag`` can be put back to ``S`` to return the unit exactly as found.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.core.logging import get_logger
from app.core.settings_service import get_settings_service
from app.db.models import UnitLogEntry, utcnow
from app.db.session import session_scope
from app.ingest import adb

log = get_logger(__name__)

#: Where the capture writes on the unit.  ``/data/local/tmp`` is the one place the shell
#: user can write, and it is the same directory the recording watcher already uses.
REMOTE_LOG = "/data/local/tmp/dashcam_unitlog.log"
REMOTE_PID = "/data/local/tmp/dashcam_unitlog.pid"

#: Rotation ceiling on the unit: ``ROTATE_KIB`` per file, ``ROTATE_COUNT`` rotations kept,
#: so the capture can never occupy more than roughly (count + 1) x size on the card.
ROTATE_KIB = 256
ROTATE_COUNT = 4

#: Buffers worth reading.  ``crash`` carries FATAL/ANR, ``kernel`` carries the card errors.
BUFFERS = ("main", "system", "crash", "kernel")

#: Minimum level kept for tags that are not explicitly silenced.
CAPTURE_LEVEL = "E"

UNIT_LOG_TIMEOUT_S = 20.0
REFRESH_S = 60.0

ENABLED_KEY = "ingest.unit_logs"
DENY_KEY = "ingest.unit_log_silenced_tags"

#: Tags allowed to write at all.  Everything not named here stays under the vendor's
#: blanket ``persist.log.tag=S`` and is never written -- see the module note for the
#: measured cost of doing otherwise.  Each is raised to error level on every arming,
#: because ``log.tag.<tag>`` does not persist across a reboot.
ALLOW_KEY = "ingest.unit_log_allowed_tags"
DEFAULT_ALLOW_TAGS: tuple[str, ...] = (
    "AndroidRuntime",
    "DEBUG",
    "ActivityManager",
    "WindowManager",
    "lowmemorykiller",
    "UnisocWatchdog",
    "ThermalManagerService",
    "ZQC-CamSubStream0",
    "ZQC-CamSubStream1",
    "CarPlayTiming",
    "zj",
    "System.err",
)
STATUS_KEY = "ingest.unit_log_status"

#: Tags silenced by default.  Every entry was measured on this firmware rather than
#: guessed, and each is here for one of two reasons.
#:
#: **Privacy.**  The networking tags print the hostname of every connection the unit makes
#: — a live capture contained ``pull-flv-f11-gcp01.tiktokcdn.com`` — so collecting them
#: would build a browsing and streaming history of the car in the footage database.  This
#: project hashes app identifiers and refuses to store hardware ones; a DNS log must not
#: arrive through the back door.  These stay silenced on the unit, so the line is never
#: written, let alone transferred.
#:
#: **Volume.**  ``ParamSet`` and ``isp_alg_fw`` are the camera ISP printing tuning
#: parameters frame by frame — 79% of all warn-level output.  The networking tags were a
#: further 87% of a live error-level capture.  Filtering on the unit is the difference
#: between roughly one megabyte an hour and eighty.
DEFAULT_DENY_TAGS = (
    # Privacy: these name the hosts the unit talks to.
    "dips_net",
    "resolv",
    "NETD_SEND_DNS_SOCK",
    "NETD_CREATE_SOCK",
    "dnsmasq2.89",
    "mDNSResponder",
    # Volume: camera ISP tuning, printed per frame.
    "ParamSet",
    "isp_alg_fw",
    "easypusher_jni",
    # Volume: chatty platform services with nothing to say about the recorder.
    "ApplicationPackageManager",
    "LIBGPS_LTE",
    "BatteryService",
    "ProcessStats",
    "GNSSMGT",
    "SprdActivityDebugConfigsUtilImpl",
    "vendor.sprd.modules.thm@2.0-impl",
)

#: How much history the server keeps.  Unit logs are diagnostic breadcrumbs, not evidence:
#: they exist to explain a fault someone noticed recently, so they age out quickly.
SERVER_RETENTION_DAYS = 14
MAX_SERVER_ROWS = 50_000

#: One capture line in ``threadtime`` with the ``year`` and ``UTC`` modifiers applied:
#: ``2026-09-01 13:19:41.979 +0000   971  1476 E BatteryService: message``.  The tag runs
#: up to the first ``": "`` because vendor tags contain spaces, dots and ``@``.
_LINE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}) "
    r"(?P<offset>[+-]\d{4})\s+"
    r"(?P<pid>\d+)\s+(?P<tid>\d+) "
    r"(?P<level>[VDIWEFS]) "
    r"(?P<tag>.*?):\s?(?P<message>.*)$"
)

#: A tag safe to hand to the unit's shell inside a filter spec.  Vendor tags legitimately
#: contain dots, dashes, underscores, ``@`` and digits; anything else is refused rather
#: than quoted, because a tag list is configuration and never needs shell metacharacters.
_SAFE_TAG = re.compile(r"^[A-Za-z0-9_.@:+-]{1,64}$")

#: Longest message stored.  Vendor stack dumps can run to kilobytes per line and the tail
#: is never the informative part.
MAX_MESSAGE_CHARS = 2048

_tasks: set[asyncio.Task] = set()
_last_refresh: dict[str, float] = {}


@dataclass(frozen=True)
class ParsedLine:
    """One captured log line, already absolute in time."""

    occurred_at: datetime
    pid: int
    tid: int
    level: str
    tag: str
    message: str

    @property
    def line_hash(self) -> str:
        """Identity for deduplication.

        A refresh re-reads lines a previous refresh already stored, so the same line must
        land once.  Hashing the whole tuple (not just the message) keeps two different
        processes emitting an identical string at the same millisecond distinguishable.
        """
        material = f"{self.occurred_at.isoformat()}|{self.pid}|{self.tid}|{self.tag}|{self.message}"
        return hashlib.sha256(material.encode("utf-8", "replace")).hexdigest()


def _enabled() -> bool:
    try:
        return bool(get_settings_service().get_nowait(ENABLED_KEY))
    except Exception:
        return False


def silenced_tags() -> tuple[str, ...]:
    """The deny list, from settings, falling back to the measured defaults.

    Unsafe entries are dropped rather than escaped: this string is interpolated into a
    remote shell command, and a tag list has no legitimate need for a metacharacter.
    """
    try:
        raw = get_settings_service().get_nowait(DENY_KEY)
    except Exception:
        raw = None
    if not raw or not str(raw).strip():
        return DEFAULT_DENY_TAGS
    tags: list[str] = []
    for candidate in str(raw).split(","):
        tag = candidate.strip()
        if not tag:
            continue
        if not _SAFE_TAG.match(tag):
            log.warning("ignoring an unsafe unit-log tag", tag=tag[:40])
            continue
        tags.append(tag)
    return tuple(tags) or DEFAULT_DENY_TAGS


def allowed_tags() -> tuple[str, ...]:
    """The allow list, from settings, falling back to the curated defaults.

    Validated the same way as the deny list and for the same reason: every entry ends up
    inside a remote ``setprop``.
    """
    try:
        raw = get_settings_service().get_nowait(ALLOW_KEY)
    except Exception:
        raw = None
    if not raw or not str(raw).strip():
        return DEFAULT_ALLOW_TAGS
    tags: list[str] = []
    for candidate in str(raw).split(","):
        tag = candidate.strip()
        if not tag:
            continue
        if not _SAFE_TAG.match(tag):
            log.warning("ignoring an unsafe unit-log allow tag", tag=tag[:40])
            continue
        tags.append(tag)
    return tuple(tags) or DEFAULT_ALLOW_TAGS


def capture_command() -> str:
    """The detached logcat invocation, bounded in size and filtered to what matters.

    ``-T 1`` starts at the present rather than replaying the ring buffer, so re-arming
    after every arrival does not re-import the backlog it already consumed.
    """
    buffers = " ".join(f"-b {name}" for name in BUFFERS)
    silenced = " ".join(f"{tag}:S" for tag in silenced_tags())
    return (
        f"logcat {buffers} -v threadtime -v year -v UTC -T 1 "
        f"-f {REMOTE_LOG} -r {ROTATE_KIB} -n {ROTATE_COUNT} "
        f"{silenced} '*:{CAPTURE_LEVEL}'"
    )


async def ensure_logging(address: str) -> bool:
    """Raise the allowed tags under the vendor's blanket silence, and start ``logd``.

    True when ``logd`` is running afterwards.  ``persist.log.tag=S`` is the vendor's own
    default and is re-asserted here so a unit that was ever left unsuppressed is put
    back; the per-tag ``log.tag.<tag>`` levels do not persist, and neither does
    ``ctl.start``, so both are applied on every arming rather than once.  ``start logd``
    proper is refused ("Must be root"); setting ``ctl.start`` works because the shell
    user holds that permission for this service.
    """
    raise_tags = " ".join(f"setprop log.tag.{tag} E;" for tag in allowed_tags())
    try:
        await adb.shell(
            address,
            f"setprop persist.log.tag S; {raise_tags} setprop ctl.start logd; sleep 1; "
            "getprop init.svc.logd",
            timeout=UNIT_LOG_TIMEOUT_S,
        )
        state = await adb.shell(address, "getprop init.svc.logd", timeout=UNIT_LOG_TIMEOUT_S)
    except adb.AdbError as exc:
        log.warning("could not re-enable logging on the unit", error=str(exc))
        return False
    running = (state or "").strip() == "running"
    if not running:
        log.warning("logd did not come up on the unit", state=(state or "").strip()[:40])
    return running


async def arm(address: str) -> bool:
    """Start a fresh bounded capture, replacing any capture already running.

    Idempotent by pid file: re-arming on every arrival replaces the capture rather than
    stacking one per visit.  ``setsid`` is load-bearing for the same reason it is in the
    recording watcher — a plainly backgrounded remote command dies when adbd SIGHUPs the
    process group on transport drop, which is precisely when the car is driving away.
    ``exec`` makes the recorded pid the logcat process itself rather than a wrapper shell.
    """
    if not await ensure_logging(address):
        return False
    command = capture_command()
    launch = (
        f"[ -f {REMOTE_PID} ] && kill $(cat {REMOTE_PID}) 2>/dev/null; "
        f"rm -f {REMOTE_LOG} {REMOTE_LOG}.* {REMOTE_PID} 2>/dev/null; "
        f"setsid sh -c 'echo $$ > {REMOTE_PID}; exec {command}' </dev/null >/dev/null 2>&1 &"
    )
    try:
        await adb.shell(address, launch, timeout=UNIT_LOG_TIMEOUT_S)
    except adb.AdbError as exc:
        log.warning("could not arm the unit log capture", error=str(exc))
        return False
    log.info("armed the unit log capture", silenced=len(silenced_tags()))
    return True


async def _read_capture(address: str, *, consume: bool) -> str | None:
    """Every rotation oldest-first, optionally stopping the capture and clearing it.

    logcat rotates by renaming the current file to ``.1`` and shifting existing rotations
    up, so the highest suffix is the oldest and the unsuffixed file is newest.  Reading in
    that order keeps the lines in time order, which matters because the parser trusts the
    timestamps but the operator reads the tail.

    A consuming read must kill the capture before deleting: the writer holds the current
    file open, and clearing it underneath a non-append writer pads the file with NULs.
    """
    rotations = " ".join(f"{REMOTE_LOG}.{index}" for index in range(ROTATE_COUNT, 0, -1))
    command = f"cat {rotations} {REMOTE_LOG} 2>/dev/null"
    if consume:
        command = (
            f"[ -f {REMOTE_PID} ] && kill $(cat {REMOTE_PID}) 2>/dev/null; "
            + command
            + f"; rm -f {REMOTE_LOG} {REMOTE_LOG}.* {REMOTE_PID} 2>/dev/null"
        )
    command += "; exit 0"
    try:
        return await adb.shell(address, command, timeout=UNIT_LOG_TIMEOUT_S)
    except adb.AdbError as exc:
        log.debug("could not read the unit log capture", error=str(exc))
        return None


def parse(raw: str) -> list[ParsedLine]:
    """Turn captured text into rows, skipping logcat's own banners and any partial line.

    Anything unparseable is dropped rather than stored as a mystery: a rotation boundary
    can cut a line in half, and half a line is not evidence.
    """
    entries: list[ParsedLine] = []
    for line in raw.splitlines():
        if not line or line.startswith("---------"):
            continue
        match = _LINE.match(line.rstrip("\r"))
        if match is None:
            continue
        try:
            stamp = datetime.strptime(
                f"{match['ts']} {match['offset']}", "%Y-%m-%d %H:%M:%S.%f %z"
            ).astimezone(UTC)
        except ValueError:
            continue
        tag = match["tag"].strip()
        if not tag:
            continue
        entries.append(
            ParsedLine(
                occurred_at=stamp,
                pid=int(match["pid"]),
                tid=int(match["tid"]),
                level=match["level"],
                tag=tag[:64],
                message=match["message"].strip()[:MAX_MESSAGE_CHARS],
            )
        )
    return entries


async def _prune() -> None:
    """Age out old rows and cap the table, oldest first."""
    cutoff = utcnow() - timedelta(days=SERVER_RETENTION_DAYS)
    async with session_scope() as session:
        await session.execute(delete(UnitLogEntry).where(UnitLogEntry.occurred_at < cutoff))
        total = int((await session.execute(select(func.count(UnitLogEntry.id)))).scalar() or 0)
        excess = total - MAX_SERVER_ROWS
        if excess <= 0:
            return
        victims = (
            (
                await session.execute(
                    select(UnitLogEntry.id)
                    .order_by(UnitLogEntry.occurred_at.asc(), UnitLogEntry.id.asc())
                    .limit(excess)
                )
            )
            .scalars()
            .all()
        )
        if victims:
            await session.execute(delete(UnitLogEntry).where(UnitLogEntry.id.in_(victims)))


async def store(entries: list[ParsedLine]) -> tuple[int, int]:
    """Insert unseen lines.  Returns (accepted, duplicate).

    Deduplication is left to the unique index rather than a read-then-write check: the
    parked refresh re-reads the same tail every minute, so duplicates are the common case
    and a conflict-ignoring insert makes that free instead of quadratic.
    """
    if not entries:
        await _prune()
        return 0, 0
    received = utcnow()
    rows = [
        {
            "occurred_at": entry.occurred_at,
            "received_at": received,
            "pid": entry.pid,
            "tid": entry.tid,
            "level": entry.level,
            "tag": entry.tag,
            "message": entry.message,
            "line_hash": entry.line_hash,
        }
        for entry in entries
    ]
    accepted = 0
    async with session_scope() as session:
        for row in rows:
            statement = sqlite_insert(UnitLogEntry).values(**row)
            result = await session.execute(
                statement.on_conflict_do_nothing(index_elements=["line_hash"])
            )
            accepted += int(result.rowcount or 0)
    await _prune()
    return accepted, len(rows) - accepted


async def _publish_status(accepted: int, total: int) -> None:
    summary = (
        f"{total} lines read from the unit, {accepted} new"
        if total
        else "nothing new from the unit"
    )
    try:
        await get_settings_service().set(STATUS_KEY, summary)
    except Exception:  # pragma: no cover - the status line is never worth failing over
        log.debug("could not publish the unit log status")


#: The unit's filterspec silently ignores tags this long.  Measured: every deny entry of
#: 25 characters or fewer is honoured, while both 32-character entries were not, so a
#: ``TAG:S`` for one of those is accepted on the command line and then does nothing.
MAX_UNIT_FILTER_TAG = 25


def drop_silenced(entries: list[ParsedLine]) -> list[ParsedLine]:
    """Enforce the deny list here as well as on the unit.

    The unit-side filter is still the primary one and does the work that matters — a line
    silenced there costs no card write, no transfer, and for the networking tags never
    exists off the device at all.  But it cannot be relied on alone: tags beyond
    :data:`MAX_UNIT_FILTER_TAG` are accepted by the filterspec and then ignored, so
    without this a long deny entry would look configured and do nothing.  Dropping them
    again here makes the setting mean what it says whatever the firmware does with it.
    """
    denied = set(silenced_tags())
    if not denied:
        return entries
    return [entry for entry in entries if entry.tag not in denied]


async def _ingest(address: str, *, consume: bool) -> tuple[int, int]:
    raw = await _read_capture(address, consume=consume)
    if not raw:
        return 0, 0
    entries = drop_silenced(parse(raw))
    accepted, duplicate = await store(entries)
    if entries:
        await _publish_status(accepted, len(entries))
    return accepted, duplicate


async def refresh(address: str) -> None:
    """The live glance while the car is parked: read without consuming."""
    await _ingest(address, consume=False)


async def collect(address: str) -> tuple[int, int]:
    """The authoritative read on arrival: drain the capture and clear it."""
    accepted, duplicate = await _ingest(address, consume=True)
    if accepted:
        log.info("collected unit logs", accepted=accepted, duplicate=duplicate)
    return accepted, duplicate


async def _collect_then_arm(address: str) -> None:
    # Collect first for the same reason the recording watcher does: arming destroys the
    # capture, and the story of the last drive lives in the file it is about to replace.
    await collect(address)
    await arm(address)


def on_unit_seen(address: str) -> None:
    """The car has just arrived: drain what it recorded away, then start a fresh capture."""
    if not _enabled():
        return
    task = asyncio.create_task(_collect_then_arm(address), name="ingest-unit-logs-arrival")
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def on_unit_present(address: str) -> None:
    """The car is sitting here: keep the view current without consuming the capture."""
    if not _enabled():
        return
    now = time.monotonic()
    if now - _last_refresh.get(address, 0.0) < REFRESH_S:
        return
    _last_refresh[address] = now
    task = asyncio.create_task(refresh(address), name="ingest-unit-logs-refresh")
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def shutdown() -> None:
    """Cancel in-flight work and wait for it, so no adb subprocess is left unreaped.

    The remote capture keeps running by design: the app being down is exactly the absence
    it exists to cover, and it is detached from this process's adb sessions anyway.
    """
    tasks = list(_tasks)
    for task in tasks:
        task.cancel()
    for task in tasks:
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    _tasks.clear()
