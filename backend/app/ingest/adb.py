"""The ADB control channel — and only the control channel.

ADB is used here for four tiny things: connect, ask the unit's state, resolve where the TF
card is mounted, and list what is on it. The recordings themselves never travel through it.

That division is the entire reason this feature is viable. Measured against the live unit:

    adb pull, 4 parallel streams      ~4.0 MB/s
    adb exec-out, single              ~8.3 MB/s
    adb exec-out -P8                 ~10.0 MB/s     <- adbd's ceiling, whatever you do
    tar | nc over a plain socket     ~34.3 MB/s     <- what transport.py uses

The card reads at 60 MB/s and the WiFi link is good for ~50 MB/s, so ``adbd`` was the only
thing in the way: everything routed through that one daemon funnels into it. The unit has
no battery, so it is only powered while the car is running in the driveway -- a window of
roughly one to two minutes. At 10 MB/s that window moves 1.2 GB; at 34 MB/s it moves 4 GB,
which is the difference between keeping up and never catching up.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import shutil
from dataclasses import dataclass

from app.core.logging import get_logger
from app.ingest.models import RemoteFile, UnitInfo, UnitState

log = get_logger(__name__)

#: The port adb registers a network device on when the address does not name one.
DEFAULT_ADB_PORT = 5555

#: Control calls are all sub-second against a healthy unit; this only bounds a hung link.
CONTROL_TIMEOUT_S = 20.0

#: Resolution order for the footage directory.
#:
#: The TF card's volume id changes whenever the card is reformatted -- the manual asks for
#: that monthly, and it has already rolled EBDF-E4DD -> 0726-1708 once. ``/storage/Tfcard``
#: is a symlink the platform recreates at every mount, so it survives the reformat; the
#: glob is the fallback for a unit that does not create it.
#: Internal shared storage is last on purpose. The removable card is where this camera
#: records by default and is what the probe should find on an unmodified unit; the internal
#: path is the fallback for one that has been pointed at its own flash instead. It needs
#: naming explicitly because the glob above it cannot reach it -- the user directory is
#: ``/storage/emulated/0/DCIM/Video``, and ``/storage/*/DCIM/Video`` only descends one
#: level, matching ``/storage/emulated/DCIM/Video``, which does not exist.
SOURCE_PROBE = (
    "for d in /storage/Tfcard/DCIM/Video /storage/sdcard0/DCIM/Video /storage/*/DCIM/Video "
    "/storage/emulated/0/DCIM/Video /sdcard/DCIM/Video; "
    'do [ -d "$d" ] && { echo "$d"; break; }; done'
)


class AdbError(RuntimeError):
    """An ADB control call failed. Never raised for "the unit is simply not here"."""


class CardNotFound(AdbError):
    """The unit answered, but no DCIM/Video directory could be located on it.

    Its own class because the operator needs to be told something quite different from
    "the car is not here": the unit is present and authorised, and the card is missing,
    unmounted or freshly reformatted.
    """


#: Filenames the camera actually produces, and the only ones ever put into a shell command.
#:
#: Names from the card are attacker-adjacent input as far as this code is concerned -- they
#: are whatever happens to be on a removable card -- and they end up as argv inside
#: ``sh -c``. Anything with a space, a quote or a glob character in it would word-split or
#: expand: ``rm -f a b.ts`` deletes two files, and a single ``*`` deletes the card. So they
#: are validated against the camera's own format and quoted, rather than either alone.
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


#: A plain web address, and the only thing ever put into ``am start -d``.
#:
#: This one is the operator's own setting rather than whatever is on a removable card, which
#: makes it *less* hostile and not safe to leave unchecked: it still ends up inside a
#: single-quoted string that the unit's shell parses, and one stray quote there would close
#: the string and hand the remainder to ``sh``. Checking it against the shape a LAN address
#: actually has is cheaper than escaping, and it fails loudly instead of quietly starting
#: something else.
_SAFE_URL = re.compile(
    r"^https?://[A-Za-z0-9._~-]+(:\d{1,5})?(/[A-Za-z0-9._~/%-]*)?(\?[A-Za-z0-9._~/%=&-]*)?$"
)


def is_safe_name(name: str) -> bool:
    return bool(_SAFE_NAME.match(name)) and ".." not in name


def is_safe_url(url: str) -> bool:
    return len(url) <= 300 and bool(_SAFE_URL.match(url))


def _checked(name: str) -> str:
    if not is_safe_name(name):
        raise AdbError(f"refusing to put an unexpected filename into a shell command: {name!r}")
    return name


def _bare_list(names: list[str]) -> str:
    """Validated names for use *inside* an already single-quoted ``sh -c`` string.

    Quoting here would be worse than useless: the surrounding string is already
    single-quoted, so an inner ``'`` would close it rather than protect anything. The
    allowlist is what makes this safe, which is why it raises rather than escaping.
    """
    return " ".join(_checked(name) for name in names)


def _quoted_list(names: list[str]) -> str:
    """Validated *and* quoted names, for a top-level command."""
    return " ".join(f"'{_checked(name)}'" for name in names)


@dataclass(slots=True)
class AdbResult:
    returncode: int
    stdout: str
    stderr: str


def adb_path() -> str:
    found = shutil.which("adb")
    if not found:
        raise AdbError("adb is not installed in this image")
    return found


async def _adb(*args: str, timeout: float = CONTROL_TIMEOUT_S) -> AdbResult:
    cmd = [adb_path(), *args]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        # The adb client can exit in the instant between wait_for timing out and
        # kill(). uvloop reports that ordinary race as ProcessLookupError; never let it
        # replace the bounded AdbError (or, during application startup, abort lifespan).
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(TimeoutError, ProcessLookupError):
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        raise AdbError(f"adb {' '.join(args[:2])} timed out after {timeout:.0f}s") from None
    except asyncio.CancelledError:
        # Cancelling only stops this side *waiting*. The adb client keeps running and the
        # command it carries still lands on the unit, which for anything that has to be
        # undone is the worst possible outcome: a cancelled `disable` can arrive after the
        # `enable` meant to reverse it, leaving the radio off while this side believes it
        # restored. Killing the client hangs up the remote shell with it, so a command
        # that has not run yet never does -- and one that already ran necessarily ran
        # before whatever is issued next.
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        raise
    return AdbResult(
        proc.returncode or 0,
        out.decode("utf-8", "replace").strip(),
        err.decode("utf-8", "replace").strip(),
    )


async def shell(address: str, command: str, *, timeout: float = CONTROL_TIMEOUT_S) -> str:
    result = await _adb("-s", address, "shell", command, timeout=timeout)
    if result.returncode != 0:
        raise AdbError(result.stderr or f"adb shell failed ({result.returncode})")
    # The unit's shell is Android's: every line arrives with a carriage return attached.
    return result.stdout.replace("\r", "")


#: How long to wait for the unit's ADB port to answer during the presence tick.
#:
#: Short on purpose. On the home LAN a unit that is there answers in single-digit
#: milliseconds, and one that is not is an ARP miss -- so this is really "how long to wait
#: before concluding the car is out".
PRESENCE_TIMEOUT_S = 0.4


def normalised_address(address: str) -> str:
    """An adb address with its port, which is the only form adb itself will match.

    ``is_listening`` supplies a default port when the setting has none, so
    ``192.168.1.122`` passes the cheap presence check -- while every other call hands the
    raw string to adb, which registers the device as ``192.168.1.122:5555`` and then
    answers ``device '192.168.1.122' not found`` for ``-s 192.168.1.122``. The poller
    therefore took the expensive branch on every tick, spent four process spawns, concluded
    OFFLINE and repeated, for as long as the setting stayed that way -- behind a UI that
    said "Car not here". One spelling everywhere removes the disagreement.
    """
    address = (address or "").strip()
    if not address or ":" in address:
        return address
    return f"{address}:{DEFAULT_ADB_PORT}"


async def is_listening(address: str) -> bool:
    """Whether anything answers on the unit's ADB port, without involving adb at all.

    The presence tick used to be three ``adb`` process spawns -- disconnect, connect,
    get-state -- every few seconds, all day, whether or not the car was within a kilometre.
    This is one socket, and making the tick cheap is what makes it affordable to look often
    enough to matter: the whole window is sixty to a hundred and twenty seconds, and at
    34 MB/s every second spent not noticing the car is ~34 MB of footage that stays on the
    card until tomorrow.

    Deliberately not a substitute for :func:`describe`. An open port says something is
    listening, not that it is authorised or that its card is mounted, so this only decides
    whether asking the expensive question is worth it.
    """
    host, _, port = address.partition(":")
    # An empty host is not "localhost" here, whatever asyncio would make of it. An
    # unconfigured address must read as "no unit", not as a connection to this container.
    if not host.strip():
        return False
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, int(port or "5555")),
            timeout=PRESENCE_TIMEOUT_S,
        )
    except (TimeoutError, OSError, ValueError):
        return False
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()
    return True


async def reconnect(address: str, *, timeout: float = CONTROL_TIMEOUT_S) -> None:
    """Re-establish the control channel before every cycle.

    Not optional and not paranoia. The unit reboots with the car, and the transport on this
    side goes stale while still *looking* connected -- ``get-state`` answers ``device`` for
    a socket that no longer reaches anything. Disconnecting first costs one round trip and
    removes a whole class of "it worked yesterday" failure.

    ``timeout`` is exposed for callers that are retrying against a budget of their own. The
    default is the full control timeout, which is right for the once-per-cycle use; a caller
    polling for a restarted daemon needs each attempt to fail *fast* enough that it gets more
    than one, and inheriting a twenty-second ceiling would spend an entire driveway window
    inside a single attempt.
    """
    await _adb("disconnect", address, timeout=timeout)
    await _adb("connect", address, timeout=timeout)


#: How long adbd is given to answer a root request.
#:
#: A production build refuses in milliseconds; a debuggable one restarts, and the restart
#: is what takes the time. Generous, because this is issued once per run and the answer
#: decides whether the hotspot can be quieted for the whole window.
ROOT_TIMEOUT_S = 10.0


async def root(address: str) -> str:
    """Ask adbd to restart as root. Returns what it said, and never raises for a refusal.

    A refusal is the *expected* answer on most units and is emphatically not an error:
    ``adb root`` exits non-zero on a production build, and letting that surface as a
    failed control call would turn "this unit does not allow it" -- the ordinary case --
    into a failed transfer. Both streams are returned because the client writes the
    interesting sentence to whichever it feels like depending on version.
    """
    result = await _adb("-s", address, "root", timeout=ROOT_TIMEOUT_S)
    return f"{result.stdout} {result.stderr}".strip()


async def state(address: str) -> UnitState:
    result = await _adb("-s", address, "get-state")
    text = f"{result.stdout} {result.stderr}".lower()
    if result.returncode == 0 and "device" in result.stdout:
        return UnitState.DEVICE
    if "unauthorized" in text:
        return UnitState.UNAUTHORIZED
    if "offline" in text or "not found" in text or "cannot connect" in text:
        return UnitState.OFFLINE
    return UnitState.UNKNOWN


async def resolve_source(address: str, override: str = "") -> str:
    if override.strip():
        return override.strip()
    found = (await shell(address, SOURCE_PROBE)).strip().splitlines()
    for line in found:
        if line.strip():
            return line.strip()
    raise CardNotFound("could not find DCIM/Video on any mounted volume")


async def unit_clock(address: str) -> int | None:
    """The unit's own idea of the time, in Unix seconds, or None if it will not say.

    Needed because the only evidence that a recording is still being written is its
    mtime, and that timestamp is stamped by *this* clock while the code comparing it was
    using the container's. They are not the same clock and nothing holds them together:
    the unit has no battery, no cellular radio and a hand-set clock -- the whole reason
    ``general.timezone`` exists is that its overlay prints local time with no zone -- so
    drift of minutes is ordinary. Fifteen seconds of it is enough that "skip the segment
    the camera is writing right now" never fires once.

    None rather than a guess when it cannot be read. The caller falls back to its own
    clock, which is what the comparison did unconditionally before, so an unreadable
    clock is no worse than not asking.
    """
    try:
        reply = (await shell(address, "date +%s", timeout=CONTROL_TIMEOUT_S)).strip()
    except AdbError as exc:
        log.debug("could not read the head unit's clock", error=str(exc))
        return None
    for line in reply.splitlines():
        try:
            return int(line.strip())
        except ValueError:
            continue
    return None


async def uptime(address: str) -> float | None:
    """Seconds since the unit last booted, or None when it will not say.

    The unit has no battery, so it powers up when the engine starts and its uptime *is*
    the length of the current drive. That is the one thing that tells an arrival from a
    departure without any location sense: a car pulling back onto the driveway has been
    running for the whole trip (uptime of minutes to hours), while one pulling off it has
    just booted (uptime of seconds). ``/proc/uptime``'s first field is exactly that, in
    seconds; the second is idle time and is ignored.

    None rather than a guess when it cannot be read, so a caller gating on it can choose
    to proceed rather than strand a backup on an unreadable answer.
    """
    try:
        reply = (await shell(address, "cat /proc/uptime", timeout=CONTROL_TIMEOUT_S)).strip()
    except AdbError as exc:
        log.debug("could not read the head unit's uptime", error=str(exc))
        return None
    for line in reply.splitlines():
        field = line.strip().split(" ", 1)[0]
        try:
            return float(field)
        except ValueError:
            continue
    return None


async def inventory(address: str, source: str) -> list[RemoteFile]:
    """List the card's recordings as ``size|name|mtime``.

    ``stat`` rather than ``ls -l`` because the output is unambiguous to parse and the field
    order is ours to choose. This is the only listing of the card we take, and it is a few
    kilobytes for a full card.

    One ``stat`` for the whole card, not one per file. The loop this replaced spawned a
    process per recording -- around 140 on a full card -- on a SoC that is not fast at
    spawning processes, and it ran inside the window rather than before it. ``stat`` takes
    as many operands as you give it, and a full card's worth of names is roughly 4 KB of
    argv, nowhere near ``ARG_MAX``. Word splitting is not a risk here even though the glob
    is unquoted: the results of pathname expansion are not field-split, so a name with a
    space in it still arrives as one operand -- and anything that does not look like one of
    the camera's own filenames is dropped below regardless.
    """
    # The trailing `exit 0` is not decoration. On an empty card `*.ts` matches nothing, so
    # the guard fails and the shell would exit non-zero -- turning "there is nothing to
    # copy" into a control-channel failure. An empty card is the *expected* steady state
    # once delete-after-verify is on, so every run after the first would have failed.
    command = f"cd '{source}' && set -- *.ts && [ -e \"$1\" ] && stat -c '%s|%n|%Y' *.ts; exit 0"
    files: list[RemoteFile] = []
    for line in (await shell(address, command)).splitlines():
        parts = line.strip().split("|")
        if len(parts) != 3:
            continue
        name = parts[1].strip()
        if not is_safe_name(name):
            log.warning("ignoring a card file with an unexpected name", name=name[:80])
            continue
        try:
            files.append(
                RemoteFile(name=name, size=int(parts[0]), mtime=int(parts[2]), directory=source)
            )
        except ValueError:
            continue
    return files


#: Where the camera puts a recording somebody protected, relative to ``DCIM/Video``.
#:
#: A locked clip is *moved* here rather than copied, so it disappears from the ordinary
#: listing entirely -- which meant the one recording anybody deliberately marked as worth
#: keeping was the one recording that never got backed up. It sits beside ``Video`` under
#: ``DCIM``, so it is derived from the resolved source rather than probed for separately.
LOCKED_DIRNAME = "LockVideo"


async def resolve_locked(address: str, source: str) -> str:
    """The unit's locked-recording directory, or "" if it has none.

    Never raises for absence. A card with no protected clips on it is the ordinary case,
    and it must not be able to fail a window that would otherwise have copied footage.
    """
    parent, _, leaf = source.rstrip("/").rpartition("/")
    if not parent or not leaf:
        return ""
    candidate = f"{parent}/{LOCKED_DIRNAME}"
    try:
        found = (await shell(address, f"[ -d '{candidate}' ] && echo yes; exit 0")).strip()
    except AdbError as exc:
        log.debug("could not check for locked recordings", error=str(exc))
        return ""
    return candidate if found == "yes" else ""


#: How the camera names the segment it is still writing, and where it leaves it.
#:
#: The recorder writes the in-progress segment as ``pre_<start>_camera_N.ts`` in ``DCIM``
#: itself — one level above ``Video`` — pre-creates the next one as a zero-byte
#: placeholder, and finalises into ``DCIM/Video`` on close. When the power dies mid-
#: segment the finalise loses a race with the unit's three-second shutdown countdown
#: (``persist.sys.shutdown.countdown.time=3`` on this unit): the front camera usually
#: makes it, the rear often does not, and its partial stays stranded at the DCIM root —
#: valid, playable MPEG-TS (verified on the card: 0x47 sync packets and a clean PAT), of
#: exactly the footage most worth having, the last minute before the car shut off.
PARTIAL_PREFIX = "pre_"


async def list_orphan_partials(
    address: str, source: str, *, unit_now: int, min_age_s: int = 180
) -> list[RemoteFile]:
    """Cut-short recordings stranded beside ``source``'s parent, oldest first.

    Only files that are demonstrably orphans: non-zero (a zero-byte partial is a
    placeholder or a loss with nothing in it), older than *min_age_s* by the unit's own
    clock (the one that stamped them — the live segment is written continuously, so age is
    what separates the stranded from the in-flight), and named exactly the way the camera
    names its in-progress files. Absence and errors return an empty list: a card with no
    orphans is the healthy case, and rescue must never be able to fail a window.
    """
    parent, _, leaf = source.rstrip("/").rpartition("/")
    if not parent or not leaf:
        return []
    command = (
        f"cd '{parent}' && set -- {PARTIAL_PREFIX}*.ts && [ -e \"$1\" ] && "
        f"stat -c '%s|%n|%Y' {PARTIAL_PREFIX}*.ts; exit 0"
    )
    try:
        reply = await shell(address, command)
    except AdbError as exc:
        log.debug("could not look for cut-short recordings", error=str(exc))
        return []
    orphans: list[RemoteFile] = []
    for line in reply.splitlines():
        parts = line.strip().split("|")
        if len(parts) != 3:
            continue
        name = parts[1].strip()
        if not name.startswith(PARTIAL_PREFIX) or not is_safe_name(name):
            continue
        # The rescued file is committed under the name the camera *would* have given it,
        # so that name has to be safe too — and non-empty, which a bare "pre_.ts" is not.
        if not is_safe_name(name[len(PARTIAL_PREFIX) :]):
            continue
        try:
            size, mtime = int(parts[0]), int(parts[2])
        except ValueError:
            continue
        if size <= 0 or unit_now - mtime < min_age_s:
            continue
        orphans.append(RemoteFile(name=name, size=size, mtime=mtime, directory=parent))
    orphans.sort(key=lambda f: f.mtime)
    return orphans


async def inventory_all(address: str, sources: list[str]) -> list[RemoteFile]:
    """List several directories, first listing winning any name that appears twice.

    The duplicate rule is not hypothetical housekeeping. Every downstream step keys on the
    bare filename -- the delta compares it against the footage directory, the commit maps
    it to an expected size, the delete names it back to the unit -- so two files sharing a
    name in different directories would silently become one, and whichever arrived second
    would be committed under the other's size check. This camera moves a locked clip rather
    than copying it, so it should never happen; that is exactly why it is worth refusing
    rather than trusting.
    """
    seen: set[str] = set()
    files: list[RemoteFile] = []
    for source in sources:
        for item in await inventory(address, source):
            if item.name in seen:
                log.warning(
                    "ignoring a duplicate recording name on the card",
                    name=item.name,
                    directory=source,
                )
                continue
            seen.add(item.name)
            files.append(item)
    return files


async def clear_listener(address: str) -> None:
    """Kill any listener left over from a window that closed mid-transfer.

    A cut mid-transfer is the normal ending here rather than the exceptional one, so an
    orphan still holding the port is an ordinary thing to find.
    """
    with contextlib.suppress(AdbError):
        await shell(
            address,
            "for p in $(pidof nc 2>/dev/null); do kill $p 2>/dev/null; done; exit 0",
        )


async def launch_listener(
    address: str,
    source: str,
    names: list[str],
    *,
    port: int,
    timeout_s: int,
) -> asyncio.subprocess.Process | None:
    """Start ``tar c <files> | nc -l -p PORT`` and return the adb child *without waiting*.

    The obvious shape -- background it on the unit with ``setsid ... &`` and let the shell
    call return -- does not work here, and the way it fails is worth recording because it
    looks like the command failing when it is doing nothing of the sort. On this unit
    ``adb shell`` simply does not return for a backgrounded command, redirections and
    ``setsid`` notwithstanding. Measured against the live head unit: the call had not
    returned after twenty seconds, while a connection to port 9000 was answered
    immediately with a valid tar header. The listener was up and serving the whole time;
    only the local wait was stuck.

    So nothing is backgrounded remotely and nothing is awaited locally. The adb session
    stays open for the life of the transfer -- which is exactly as long as it is wanted --
    and the caller kills this process when the stream ends. ``nc -w`` bounds only the
    wait for this side to connect. The old outer ``timeout`` bounded the entire stream,
    which cut large or temporarily-slow backups off even while bytes were still moving.
    Once connected, the adb session and TCP connection are the lifetime guards instead.

    Nothing is installed on the unit for any of it. It is not rooted and does not need to
    be: Android's toybox already provides ``tar`` and ``nc``.

    The file list goes as argv. A full card is around 140 names of 30 characters, roughly
    4 KB, far inside ARG_MAX, and the camera's filenames carry no shell metacharacters --
    which :func:`_bare_list` insists on rather than assumes.
    """
    if not names:
        return None
    command = (
        f"cd '{source}' && tar c {_bare_list(names)} | nc -l -p {int(port)} -w {int(timeout_s)}"
    )
    return await asyncio.create_subprocess_exec(
        adb_path(),
        "-s",
        address,
        "shell",
        command,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )


async def stop_listener(proc: asyncio.subprocess.Process | None) -> bool:
    """End the adb session carrying the listener. True if it was still running.

    The return value is free diagnostics, and it answers a question the logs could not
    previously answer at all. The adb session *is* the listener's lifetime, so finding it
    still alive means the unit was still serving when this side stopped reading -- the car
    pulled away, the ordinary ending this whole design expects. Finding it already exited
    means the **unit** stopped first: ``tar`` failed, ``nc`` died, or the remote ``timeout``
    fired. Both used to surface as the identical "the stream ended with N file(s) still to
    come", which is the one message that gives an operator nowhere to look.
    """
    if proc is None or proc.returncode is not None:
        return False
    with contextlib.suppress(ProcessLookupError):
        proc.kill()
    with contextlib.suppress(TimeoutError, Exception):
        await asyncio.wait_for(proc.wait(), timeout=5.0)
    return True


#: Tells the browser which application asked, so it reuses that application's tab.
#:
#: Without it every transfer opened another tab, and they accumulate for as long as the
#: unit is up -- three pulls left three tabs of the same page on a screen nobody is going
#: to tidy up. Passing a stable id makes the browser keep one tab per application and
#: navigate it instead, which is both the fix and the refresh: measured on the unit, three
#: intents produced three tabs without it and one with it, and re-firing the *same* URL
#: reloaded that tab rather than merely focusing it.
#:
#: The extra is ``android.provider.Browser.EXTRA_APPLICATION_ID``, spelled out because this
#: is a shell command rather than a linked constant. Chrome honours it; a vendor browser
#: that does not will ignore an unknown extra rather than fail.
BROWSER_APPLICATION_ID_EXTRA = "com.android.browser.application_id"

#: The id itself. Any stable string works -- it is a grouping key, not a real package -- and
#: this one is a plain identifier so it carries no shell metacharacters into the command.
APPLICATION_ID = "com.dashcam.analyser"

# The production unit resolves web VIEW intents to Chrome.  The display path is deliberately
# package-specific at verification time: a successful ``am start`` only means Android accepted
# the intent, not that the parked car's screen actually changed.
CHROME_PACKAGE = "com.android.chrome"


async def show_url(address: str, url: str) -> str:
    """Open a web page on the unit's own screen. Returns "" on success, or why not.

    The return value is for the Test button on the Backup page and nothing else. The
    transfer path ignores it, and must: see below.

    This is how the car shows what is being copied, and without installing anything it is
    the only way to show a real progress bar. AOSP's ``cmd notification post`` cannot: its
    shell command has no ``setProgress``, no ``setOngoing`` and no ``setOnlyAlertOnce``, so
    a notification could only ever be plain text that re-alerts on every update. The app
    already serves a live Backup page, so pointing the unit's browser at it costs nothing
    and stays in step with the dashboard by construction.

    Never raises. A unit with no browser answers "Error: Activity not started, unable to
    resolve Intent", and a courtesy on the car's screen has no business turning a working
    transfer into a failed one -- which is why the transfer path discards what comes back
    rather than checking it.
    """
    if not is_safe_url(url):
        log.warning("refusing to open a URL that is not a plain web address", url=url[:120])
        return "That is not a plain web address."
    try:
        # `-d` rather than a component: an implicit VIEW intent lets whatever browser the
        # vendor shipped answer, instead of this needing to know its package name.
        reply = await shell(
            address,
            f"am start -a android.intent.action.VIEW -d '{url}' "
            f"--es {BROWSER_APPLICATION_ID_EXTRA} '{APPLICATION_ID}'",
            timeout=10.0,
        )
    except AdbError as exc:
        log.warning("could not open the backup page on the head unit", error=str(exc))
        return f"Could not reach the head unit: {exc}"
    # `am` reports "no browser" on stdout with a zero exit status, so the return code alone
    # would call that a success and leave nobody any the wiser about a blank screen.
    if "Error" in reply:
        log.warning("the head unit would not open the backup page", reply=reply[:200])
        return f"The head unit refused to open it: {reply[:200]}"
    return ""


async def chrome_is_foreground(address: str) -> bool:
    """Whether Chrome is the visible activity on the head unit.

    ``am start`` can succeed while a launcher, vendor overlay or stale task remains in front.
    This uses Android's foreground-window report rather than treating intent acceptance as a
    visual result.  It is best-effort because the caller must never hold up footage for a
    courtesy display.
    """

    try:
        reply = await shell(
            address,
            "dumpsys window windows | sed -n '/mCurrentFocus=/p;/mFocusedApp=/p'",
            timeout=6.0,
        )
    except AdbError as exc:
        log.warning("could not verify Chrome on the head unit", error=str(exc))
        return False
    return CHROME_PACKAGE in reply


#: The unit's own ignition line, as ``settings global acc_status`` reports it.
#:
#: Measured on the live unit: ``1`` with the engine on, flipping to ``0`` within a second
#: of it going off while the unit is still awake and on the network. That window -- awake,
#: reachable, engine off -- is the only moment it can be observed, and it is what makes
#: touching the unit's power state safe to automate.
PARKED = "0"

#: How long the unit stays awake after the ignition goes off, in seconds.
#:
#: The single most valuable thing found on this device. It drives an on-screen countdown
#: and **the radio stays up for the whole of it**, so it is the only lever that changes how
#: much footage a park is worth: at the shipped value of 3 the unit was gone before
#: anything could connect, at 60 it stayed reachable for the full minute.
SLEEP_COUNTDOWN_PROP = "persist.sys.sleep.countdown.time"


async def is_parked(address: str) -> bool:
    """Whether the ignition is off, according to the unit's own ACC line.

    Conservative by construction: anything other than a clear ``0`` reads as *not* parked.
    Its callers suspend the head unit, and the cost of being wrong in that direction is
    blanking the screen -- and possibly the recorder -- on somebody who is driving. A
    failed read, an unexpected value, or a control channel that has gone away all mean
    "leave it alone".
    """
    try:
        answer = (await shell(address, "settings get global acc_status", timeout=10.0)).strip()
    except AdbError as exc:
        log.debug("could not read the ignition state", error=str(exc))
        return False
    return answer == PARKED


async def sleep_countdown(address: str) -> int:
    """How many seconds the unit currently stays awake after ignition-off. 0 if unknown."""
    try:
        answer = (await shell(address, f"getprop {SLEEP_COUNTDOWN_PROP}", timeout=10.0)).strip()
        return int(answer)
    except (AdbError, ValueError):
        return 0


async def set_sleep_countdown(address: str, seconds: int) -> bool:
    """Set how long the unit stays awake after the ignition goes off.

    Writable from an ordinary shell because this unit runs SELinux ``Permissive``; there is
    no root involved. Verified by reading it back rather than trusting the exit status,
    because ``setprop`` says nothing at all when it is refused.
    """
    wanted = max(1, int(seconds))
    try:
        await shell(address, f"setprop {SLEEP_COUNTDOWN_PROP} {wanted}", timeout=10.0)
    except AdbError as exc:
        log.debug("could not set the sleep countdown", error=str(exc))
        return False
    return await sleep_countdown(address) == wanted


async def sleep_unit(address: str) -> str:
    """Suspend the head unit now. Returns "" on success, or why not.

    ``svc power forcesuspend`` rather than a vendor broadcast: it is documented, it is what
    the platform itself uses, and it does not depend on guessing at an unexported receiver.

    Never raises. Failing to sleep the unit costs a little of the car's battery; it must
    not be able to turn a completed transfer into a failed run, and the countdown expiring
    is the backstop for everything this misses.
    """
    try:
        reply = await shell(address, "svc power forcesuspend", timeout=10.0)
    except AdbError as exc:
        # The ordinary outcome: suspending takes down the link the command arrived on, so
        # the call dies rather than answering. That is it working.
        log.debug("the link dropped while suspending the head unit", error=str(exc))
        return ""
    if "Error" in reply or "Exception" in reply:
        return reply[:200]
    return ""


async def delete(address: str, source: str, names: list[str]) -> int:
    """Remove files from the unit. Only ever called for verified, committed copies.

    Every name is quoted and format-checked first. Unquoted, one unexpected filename on
    the card turns a targeted delete into a broad one -- ``a b.ts`` removes two files, and
    a name containing ``*`` removes everything matching it.
    """
    if not names:
        return 0
    await shell(address, f"cd '{source}' && rm -f {_quoted_list(names)}")
    return len(names)


async def describe(address: str, override: str = "") -> UnitInfo:
    """One control round trip that answers "is it here, and where is the footage"."""
    info = UnitInfo(address=address)
    await reconnect(address)
    info.state = await state(address)
    if info.online:
        try:
            info.source = await resolve_source(address, override)
        except AdbError as exc:
            # Recorded rather than swallowed. Reporting a present, authorised unit with an
            # unmounted or reformatted card as "not on the network" points the operator at
            # the wiring when the problem is the card.
            info.card_error = str(exc)
            log.warning("unit is online but its card could not be located", error=str(exc))
    return info
