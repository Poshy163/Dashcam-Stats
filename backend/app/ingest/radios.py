"""Quieting the unit's other radios while a transfer runs.

The transfer already runs at the radio's measured ceiling. The unit's WiFi is a
single-stream 1x1 chip — 433 Mbps PHY, ~35 MB/s of goodput, and the transport achieves
~32 of it (docs/dashcam-backup-investigation.md §15) — and the unit's Bluetooth and its
own hotspot share that one chip. Anything they spend — softap beaconing and serving on a
second channel, Bluetooth taking turns on the antenna — is paid for in footage left on
the card. So while a run is moving bytes, both are turned off, and turned back on when
the run ends.

Three rules shape everything here, in order of importance.

**Never leave the car worse than it was found.** Bluetooth is the driver's hands-free,
and the ordinary way a window ends is the engine stopping mid-transfer, after which this
app cannot reach the unit again that day. So restoring is not one ``finally``: it is the
run's own restore, plus a watchdog left running on the unit that re-enables Bluetooth on
a deadline whether or not this app still exists — the same trick the transfer's listener
already plays with ``timeout`` — plus a marker persisted on this side so that anything
still off is turned back on the moment the unit is next seen, before a single byte is
asked for. The watchdog gates on a flag file the restore removes, so a stale watchdog
whose run already restored does nothing.

**Only turn off what can be turned back on.** Bluetooth's state is readable
(``settings get global bluetooth_on``), so it is toggled freely and only when it was
actually on. The hotspot is restarted only when it was positively seen serving *and* its
SSID and passphrase were recovered from ``dumpsys wifi``, because a shell has nowhere
else to get them and restarting the wrong network — or an open one — is worse than
leaving it for the next engine start to re-arm.

**Believe the effect, never the exit status.** This is the rule the first version of this
module did not have, and the field found it inside a day: Bluetooth went off and the
hotspot did not. AOSP's ``WifiShellCommand`` gates every verb outside its
``NON_PRIVILEGED_COMMANDS`` allowlist on ``uid != Process.ROOT_UID``, and neither
``stop-softap`` nor ``start-softap`` is on that list — so on an unrooted unit the command
throws ``SecurityException: Uid 2000 does not have access to stop-softap`` and the
hotspot carries on beaconing through every transfer. ``svc bluetooth`` has no equivalent
gate, which is why exactly half the feature appeared to work. So a stop is believed only
when :func:`_serving_ap` can no longer find the AP — and when it cannot be done, that is
a warning naming the unit's own words, not silence. There is no unrooted way round it;
the honest outcome is to say so and leave the hotspot alone.

**Never delay the transfer.** Every call here rides the control channel, which is idle
while the bulk socket moves bytes. The quieting itself waits until the unit has been on
the network for :data:`QUIET_AFTER_ONLINE_S`, so a car that is merely turning around on
the driveway keeps its phone connection — one that is still here ten seconds in is
parked.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from dataclasses import dataclass

from app.core.logging import get_logger
from app.core.settings_service import get_settings_service
from app.ingest import adb

log = get_logger(__name__)

#: How long the unit must have been on the network before its radios are touched.
#:
#: The guard against the fleeting connection: a car that arrives and leaves inside this
#: is reversing out of the driveway with somebody on the phone, and cutting Bluetooth
#: under that call to speed up a transfer that is about to be severed anyway helps
#: nobody.
QUIET_AFTER_ONLINE_S = 10.0

#: The flag the on-unit watchdog gates on, removed by whichever restore happens first.
#:
#: ``/data/local/tmp`` because it is the one place the ``shell`` user can write on an
#: unrooted unit. It survives a reboot, which is harmless: the flag does nothing on its
#: own, and every watchdog that reads it was armed by a run in the current boot.
FLAG_PATH = "/data/local/tmp/.dashcam_analyser_radios"

#: Where "something is still turned off" is persisted on this side.
#:
#: A settings key rather than process memory, for the same reason the learned origin is:
#: the failure this exists for — the engine stopping mid-transfer — is also a moment
#: nothing can be written to the unit, and the app may well restart before the car is
#: next seen. Read-only in the UI, shown so a silent phone is never a mystery.
MARKER_KEY = "ingest.radios_pending_restore"

#: Serialises everything that changes a radio's state.
#:
#: Two things can be doing that at once, started seconds apart by different code paths:
#: the run's own quieting, and the arrival-time restore the poller fires when a previous
#: window left something off. Each is a multi-step sequence — read the state, write the
#: marker, touch the flag, issue the toggle — and interleaving them lets the slower one
#: undo the faster one's bookkeeping. The case that matters: a lagging restore deleting
#: the flag and the marker that the *current* window has just written, which takes out the
#: on-unit watchdog and the next-arrival repair together and leaves nothing at all holding
#: the promise that Bluetooth comes back.
_lock = asyncio.Lock()

#: How many quietings currently own the radios. An arrival restore that reaches the front
#: of the queue and finds one in progress stands down, rather than turning back on what
#: the transfer has just deliberately turned off.
_active = 0

#: Ceiling on every radio control call. These are sub-second against a healthy unit, and
#: against a unit that has just driven away the only wrong answer is a long one.
RADIO_TIMEOUT_S = 6.0

#: Interface names that can carry a soft AP. Matched rather than listed, because the
#: name is a vendor choice — ``ap0``, ``softap0``, ``swlan0`` and ``wlan1`` are all in the
#: wild — and which one matters is decided by what it is *doing*, in :func:`_serving_ap`.
_AP_NAME = re.compile(r"^(?:ap|softap|swlan|wlan|wl)\d*(?:\.\d+)?$")

#: One ``ip -o addr`` line: the interface and the IPv4 it holds. ``inet6`` lines cannot
#: match, which is deliberate — a link-local address says nothing about serving.
_IP_LINE = re.compile(r"^\d+:\s+(\S+)\s+inet\s+(\d{1,3}(?:\.\d{1,3}){3})")

#: Replies that mean the unit declined, whatever its exit status said.
#:
#: Both have to be checked. ``cmd`` prints a ``SecurityException`` and exits non-zero, but
#: an unknown *service* is reported on stdout with a zero exit status — so a return code
#: alone calls a refusal a success, which is precisely how the first version of this
#: reported a hotspot as stopped every window while it carried on beaconing.
_REFUSALS = (
    "error",
    "exception",
    "unknown",
    "not found",
    "does not have access",
    "usage:",
    # What a unit without the service at all answers: `cmd: Can't find service: tethering`.
    "can't find",
    "no such",
)


def _accepted(reply: str) -> bool:
    lowered = reply.lower()
    return not any(refusal in lowered for refusal in _REFUSALS)


#: What may be carried back into ``cmd wifi start-softap`` inside single quotes.
#:
#: The SSID and passphrase come out of ``dumpsys`` output, which makes them scraped text
#: rather than trusted configuration, and they end up as argv on the unit's shell. Same
#: policy as the card filenames: validated against a conservative shape and refused
#: otherwise, never escaped.
_SAFE_AP_TEXT = re.compile(r"^[A-Za-z0-9 ._@#%+=-]{1,63}$")


def _parse_softap_config(dump: str) -> tuple[str, str] | None:
    """(ssid, passphrase) recovered from ``dumpsys wifi``, or None rather than a guess.

    The dump is full of SSIDs — every network the client side has ever seen — so nothing
    is read outside a window anchored on the softap configuration's own markers. Newer
    builds render the SSID as ``WifiSsid{"name"}``, older ones bare; both are accepted.
    A redacted or oddly-shaped value returns None, which downstream means "stop it but
    do not promise to start it": restarting the wrong network, or an open one, is worse
    than leaving the hotspot for the next engine start to re-arm.
    """
    marker = re.search(r"WifiApConfigStore|mPersistentWifiApConfig|SoftApConfiguration", dump)
    if not marker:
        return None
    window = dump[marker.start() : marker.start() + 4000]

    ssid_match = re.search(
        r'ssid\s*[=:]\s*(?:WifiSsid\{)?"([^"\r\n]+)"', window, re.IGNORECASE
    ) or re.search(r"ssid\s*[=:]\s*([^\s\",}{]+)", window, re.IGNORECASE)
    pass_match = re.search(
        r'(?:passphrase|presharedkey)\s*[=:]\s*"?([^\s\",}{]+)"?', window, re.IGNORECASE
    )
    if not ssid_match or not pass_match:
        return None
    ssid = ssid_match.group(1).strip()
    passphrase = pass_match.group(1).strip()
    # WPA2 passphrases are 8..63 by definition; anything shorter is a parse artefact or a
    # redaction, and either way not something to type back at the unit.
    if len(passphrase) < 8 or "redact" in passphrase.lower():
        return None
    if not _SAFE_AP_TEXT.match(ssid) or not _SAFE_AP_TEXT.match(passphrase):
        return None
    return ssid, passphrase


async def _bluetooth_is_on(address: str) -> bool | None:
    """The unit's Bluetooth state, or None when it cannot be read.

    ``bluetooth_on`` is the setting BluetoothManagerService itself persists: 0 off, 1 on,
    2 on-during-airplane-mode. None matters and is not folded into False — a radio whose
    state is unknown is left alone entirely, because "restore" for it could just as
    easily mean turning on something the operator keeps off.
    """
    try:
        reply = (
            await adb.shell(address, "settings get global bluetooth_on", timeout=RADIO_TIMEOUT_S)
        ).strip()
    except adb.AdbError:
        return None
    if reply == "0":
        return False
    if reply in ("1", "2"):
        return True
    return None


async def _set_bluetooth(address: str, *, enable: bool) -> bool:
    """Issue the toggle, newest interface first. True once something accepted it.

    ``cmd bluetooth_manager`` is the Android 12+ door; ``svc bluetooth`` the one older
    builds answer. Both are tried because this feature is generic even though the unit it
    was written against is Android 15. ``cmd`` reports an unknown service on stdout with
    a zero exit status, so the reply text is checked rather than the return code alone.
    """
    verb = "enable" if enable else "disable"
    for command in (f"cmd bluetooth_manager {verb}", f"svc bluetooth {verb}"):
        try:
            reply = await adb.shell(address, command, timeout=RADIO_TIMEOUT_S)
        except adb.AdbError:
            continue
        if not _accepted(reply):
            continue
        return True
    return False


async def _serving_ap(address: str) -> str:
    """The interface of a soft AP that is actually serving, or "" when none is.

    Three questions get conflated here and only one of them is the right one.

    *Does an AP interface exist* is not it — plenty of units keep a dormant ``wlan1``
    around whether or not the hotspot has ever been switched on, so existence answers
    "yes" forever. *What does dumpsys say* is worse: its log buffers mention the hotspot
    long after it last ran, which is the kind of evidence that is never false.

    A soft AP that is **serving** holds its own IPv4 on its own interface — it is the
    DHCP server for whatever joins it, so it is ``192.168.43.1`` or a vendor variant from
    the moment it comes up, client or no client. That is what is looked for, and asking
    the same question a second time is what proves a stop actually took effect rather
    than merely being accepted.

    The interface carrying the address this app reached the unit on is excluded by
    address, never by name. If somebody's server is itself a client of the dashcam's
    hotspot then that hotspot *is* the transfer's link, and stopping it would cut the one
    thing this feature exists to speed up — so the interface that must not be touched is
    identified literally rather than assumed to be called ``wlan0``.
    """
    host = address.partition(":")[0].strip()
    try:
        reply = await adb.shell(address, "ip -o addr show; exit 0", timeout=RADIO_TIMEOUT_S)
    except adb.AdbError:
        return ""
    for line in reply.splitlines():
        match = _IP_LINE.match(line.strip())
        if not match:
            continue
        iface, addr = match.group(1), match.group(2)
        if addr == host or not _AP_NAME.match(iface):
            continue
        return iface
    return ""


#: How a soft AP is asked to stop, in the order the unit is asked.
#:
#: The first is the platform's own API, and on an unrooted unit it is also the one that
#: cannot work. AOSP's ``WifiShellCommand`` gates everything outside a
#: ``NON_PRIVILEGED_COMMANDS`` allowlist on ``uid != Process.ROOT_UID``, and neither
#: ``stop-softap`` nor ``start-softap`` is on that list — so a shell caller gets
#: ``SecurityException: Uid 2000 does not have access to stop-softap wifi command``.
#: That is exactly what this head unit returns, and it is why the hotspot half of this
#: feature did nothing on the first attempt while the Bluetooth half worked: ``svc
#: bluetooth`` has no equivalent gate.
#:
#: The second is the Tethering module's own shell command — a different service with a
#: different permission model, worth asking on a unit whose adbd does run as root or a
#: vendor build that relaxed the check. It is **unverified against this unit** and is
#: tried opportunistically rather than relied upon: an unknown service costs one round
#: trip and says so on stdout, and nothing here believes any of them without
#: :func:`_serving_ap` confirming it afterwards.
_STOP_COMMANDS = ("cmd wifi stop-softap", "cmd tethering stop-all-tethering")


async def _stop_hotspot(address: str) -> tuple[bool, str]:
    """Stop a serving soft AP. Returns (stopped, what the unit said when it would not).

    Success is not a zero exit status; it is :func:`_serving_ap` no longer finding the
    interface. The unit's own words are carried back rather than discarded, because this
    is a failure the operator can do nothing about from the app and everything about from
    the log — and a feature that fails silently on every window is the mistake this
    project has already made once.
    """
    replies: list[str] = []
    for command in _STOP_COMMANDS:
        try:
            reply = await adb.shell(address, command, timeout=RADIO_TIMEOUT_S)
        except adb.AdbError as exc:
            replies.append(f"{command}: {exc}")
            continue
        if not _accepted(reply):
            first = reply.splitlines()[0][:120] if reply.strip() else "refused"
            replies.append(f"{command}: {first}")
            continue
        if not await _serving_ap(address):
            return True, ""
        replies.append(f"{command}: accepted, but the hotspot is still up")
    return False, "; ".join(replies)[:400]


async def _persist_marker(value: str) -> None:
    try:
        await get_settings_service().set(MARKER_KEY, value, internal=True)
    except Exception as exc:
        # The marker is the third line of defence, behind the run's own restore and the
        # on-unit watchdog; failing to write it is worth a line, not a failed transfer.
        log.debug("could not persist the radio-restore marker", error=str(exc))


async def _remove_flag(address: str) -> None:
    with contextlib.suppress(adb.AdbError):
        await adb.shell(address, f"rm -f '{FLAG_PATH}'", timeout=RADIO_TIMEOUT_S)


async def _arm_watchdog(address: str, deadline_s: int) -> asyncio.subprocess.Process | None:
    """Leave a Bluetooth re-enabler running on the unit, gated on the flag file.

    The same shape as the transfer's listener: nothing is backgrounded remotely (this
    unit's ``adb shell`` never returns for a backgrounded command), so the adb child is
    simply never awaited, and the remote command outlives a dropped session — which is
    the one property that matters, because "the session dropped" is exactly the failure
    the watchdog exists for. If the run restores first it removes the flag, and a fired
    watchdog that finds no flag exits without touching anything.
    """
    command = (
        f"sleep {int(deadline_s)}; [ -f '{FLAG_PATH}' ] || exit 0; "
        f"rm -f '{FLAG_PATH}'; cmd bluetooth_manager enable || svc bluetooth enable"
    )
    try:
        return await asyncio.create_subprocess_exec(
            adb.adb_path(),
            "-s",
            address,
            "shell",
            command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except Exception as exc:
        log.warning("could not arm the Bluetooth watchdog on the unit", error=str(exc))
        return None


@dataclass
class RadioQuiet:
    """One run's quieting: what was turned off, and everything needed to undo it."""

    address: str
    online_for: float
    watchdog_deadline_s: int
    #: Decided *before* the disable is issued, so a restore can never miss it. The
    #: asymmetry is deliberate: enabling a radio that is already on is a no-op, while
    #: failing to enable one that was turned off is a silent phone in the morning.
    bluetooth_off: bool = False
    hotspot_restore: tuple[str, str] | None = None
    _task: asyncio.Task | None = None
    _watchdog: asyncio.subprocess.Process | None = None
    #: Whether this run has claimed the radios, so `finish` releases exactly once however
    #: it is reached -- including for a task cancelled before it ever claimed them.
    _owns: bool = False

    @property
    def delay(self) -> float:
        """How much of the ten-second guard is still to run."""
        return max(0.0, QUIET_AFTER_ONLINE_S - self.online_for)

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="ingest-radio-quiet")

    async def _run(self) -> None:
        global _active
        if self.delay > 0:
            await asyncio.sleep(self.delay)
        # Claimed before the lock is taken, not after. The claim is what tells a waiting
        # arrival-restore to stand down, and it has to hold for as long as this run owns
        # the radios -- which ends in `finish`, not when `_quiet` returns.
        _active += 1
        self._owns = True
        try:
            async with _lock:
                await self._quiet()
        except Exception as exc:
            # Best-effort by contract: a radio that cannot be quieted costs throughput,
            # not the transfer.
            log.warning("could not quiet the unit's radios", error=str(exc))

    async def _quiet(self) -> None:
        await self._quiet_bluetooth()
        await self._quiet_hotspot()

    async def _quiet_bluetooth(self) -> None:
        state = await _bluetooth_is_on(self.address)
        if state is None:
            log.warning("could not read the unit's Bluetooth state; leaving it alone")
            return
        if not state:
            return
        self.bluetooth_off = True
        # Marker and flag first, act second: a crash between the disable and the
        # bookkeeping must read as "still off", never the reverse.
        await _persist_marker("bluetooth")
        with contextlib.suppress(adb.AdbError):
            await adb.shell(self.address, f"touch '{FLAG_PATH}'", timeout=RADIO_TIMEOUT_S)
        if await _set_bluetooth(self.address, enable=False):
            log.info("turned the unit's Bluetooth off for the transfer")
            self._watchdog = await _arm_watchdog(self.address, self.watchdog_deadline_s)
        else:
            # Nothing accepted the toggle, so nothing needs restoring.
            self.bluetooth_off = False
            await _persist_marker("")
            log.warning("the unit did not accept a Bluetooth disable; leaving it on")

    async def _quiet_hotspot(self) -> None:
        iface = await _serving_ap(self.address)
        if not iface:
            # Nothing is serving, which is already the state that was asked for. The
            # previous version fired a `stop-softap` here every window regardless, which
            # on a unit that cannot run it produced no effect and no log line either --
            # and those two together are why it looked like it was working.
            return

        # Read the configuration before stopping. Afterwards there is nothing left to
        # read it from.
        config: tuple[str, str] | None = None
        try:
            dump = await adb.shell(self.address, "dumpsys wifi", timeout=15.0)
            config = _parse_softap_config(dump)
        except adb.AdbError as exc:
            log.debug("could not read the hotspot configuration", error=str(exc))

        stopped, why = await _stop_hotspot(self.address)
        if not stopped:
            log.warning(
                "the head unit will not stop its hotspot, so it is still sharing the "
                "radio with the transfer. Android only lets uid 0 stop a soft AP and "
                "this unit's ADB is not root, so there is nothing the app can do from "
                "here -- switch the hotspot off on the unit itself if the throughput "
                "matters",
                iface=iface,
                reply=why,
            )
            return
        if config:
            self.hotspot_restore = config
            log.info("stopped the unit's hotspot for the transfer", iface=iface)
        else:
            log.warning(
                "stopped the unit's hotspot, but its configuration could not be "
                "recovered, so it will not be started again from here; the unit "
                "re-arms its own default at the next engine start",
                iface=iface,
            )

    async def finish(self) -> None:
        """Put back whatever was taken. Every exit path of a run comes through here."""
        task = self._task
        if task is not None and not task.done():
            # The guard may still be sleeping — an idle-adjacent run that ended inside
            # ten seconds — in which case nothing was touched and nothing is restored.
            task.cancel()
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        global _active
        try:
            async with _lock:
                if self.bluetooth_off:
                    await self._restore_bluetooth()
                if self.hotspot_restore:
                    await self._restore_hotspot()
        finally:
            if self._owns:
                _active -= 1
                self._owns = False

    async def _restore_bluetooth(self) -> None:
        restored = False
        if await _set_bluetooth(self.address, enable=True):
            # Confirmed, not assumed. A clean `enable` is not proof the radio came back,
            # and clearing the marker and the flag on an unverified success is the one
            # mistake that strands the driver's hands-free with nothing left holding the
            # promise: watchdog stood down, marker gone, next arrival with nothing to
            # repair. Anything short of a positive "it is on" keeps all three layers in
            # place, and being wrong in that direction costs an `enable` issued at a
            # radio that is already on, which is a no-op.
            restored = await _bluetooth_is_on(self.address) is True
        if restored:
            log.info("turned the unit's Bluetooth back on")
            await _remove_flag(self.address)
            await _persist_marker("")
            self.bluetooth_off = False
        else:
            # The car has usually left. The flag stays so the on-unit watchdog acts,
            # and the marker stays so the next arrival is put right before it is asked
            # for anything.
            log.warning(
                "could not confirm the unit's Bluetooth is back on; the watchdog on the "
                "unit will see to it, or the next arrival"
            )
        watchdog = self._watchdog
        if restored and watchdog is not None and watchdog.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                watchdog.kill()
            with contextlib.suppress(TimeoutError, Exception):
                await asyncio.wait_for(watchdog.wait(), timeout=3.0)

    async def _restore_hotspot(self) -> None:
        ssid, passphrase = self.hotspot_restore or ("", "")
        try:
            await adb.shell(
                self.address,
                f"cmd wifi start-softap '{ssid}' wpa2 '{passphrase}'",
                timeout=RADIO_TIMEOUT_S,
            )
            log.info("started the unit's hotspot again", ssid=ssid)
        except adb.AdbError as exc:
            log.warning("could not start the unit's hotspot again", error=str(exc))


def begin_quiet(address: str, *, online_for: float, watchdog_deadline_s: int) -> RadioQuiet:
    """Start quieting in the background and hand the run the means to undo it."""
    quiet = RadioQuiet(
        address=address,
        online_for=online_for,
        watchdog_deadline_s=watchdog_deadline_s,
    )
    quiet.start()
    return quiet


#: Arrival-time restores in flight. Held for the usual reason: the loop keeps only a weak
#: reference to a running task.
_tasks: set[asyncio.Task] = set()


def restore_if_pending(address: str) -> None:
    """If a previous window left radios off, put them right. Never blocks the caller.

    Called by the poller on the offline-to-online transition — which, for a unit whose
    restore failed, is usually the next morning on the same driveway. Fired rather than
    awaited so it costs the arriving window nothing; it shares the control channel with
    the run that is starting, and both are sub-second calls.
    """
    try:
        pending = str(get_settings_service().get_nowait(MARKER_KEY) or "").strip()
    except Exception:
        pending = ""
    if not pending:
        return
    task = asyncio.create_task(_restore_pending(address, pending), name="ingest-radio-restore")
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _restore_pending(address: str, pending: str) -> None:
    async with _lock:
        if _active:
            # A transfer owns the radios right now, and the marker read here is very
            # likely the one *it* just wrote. Turning Bluetooth back on would undo the
            # window in progress, and clearing the marker afterwards would throw away
            # that window's own promise to restore.
            log.debug("a transfer owns the radios; leaving the pending restore to it")
            return
        # Re-read under the lock rather than trusting the value this task started with:
        # it may have been cleared by the run that wrote it, or rewritten by a newer one,
        # while this task was waiting its turn.
        try:
            pending = str(get_settings_service().get_nowait(MARKER_KEY) or "").strip()
        except Exception:
            pending = ""
        if not pending:
            return
        log.info(
            "a previous window left the unit's radios off; turning them back on",
            radios=pending,
        )
        if "bluetooth" in pending:
            if await _set_bluetooth(address, enable=True):
                await _remove_flag(address)
                await _persist_marker("")
                log.info("the unit's Bluetooth is back on")
            else:
                log.warning(
                    "could not turn the unit's Bluetooth back on; it will be tried "
                    "again the next time the unit appears"
                )


def cancel_pending() -> None:
    """Stop any arrival-time restore still in flight, for shutdown."""
    for task in list(_tasks):
        task.cancel()
