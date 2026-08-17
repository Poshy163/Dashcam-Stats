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
actually on. The hotspot can always be *stopped* — ``cmd wifi stop-softap`` is a no-op
on a hotspot that is not running — but starting it back needs its SSID and passphrase,
which a shell can only recover by parsing ``dumpsys wifi``. So it is restarted only when
it was positively seen running *and* its configuration was recovered, and the log says
which. A hotspot that cannot be restored is still stopped, because "make sure it is off"
is the entire point and the unit — which has no battery and reboots with every engine
start — re-arms its own boot-time defaults next drive; but that choice is recorded
loudly rather than silently.

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

#: Ceiling on every radio control call. These are sub-second against a healthy unit, and
#: against a unit that has just driven away the only wrong answer is a long one.
RADIO_TIMEOUT_S = 6.0

#: Interface names vendors give a running soft AP. Existence is the detector: Android
#: creates the AP interface when the hotspot starts and removes it when it stops, which
#: makes this a far more literal answer than grepping state machines out of ``dumpsys``
#: — whose log buffers mention the hotspot long after it last ran.
_AP_IFACES = ("ap0", "softap0", "swlan0", "wlan1", "wlan2")

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
        lowered = reply.lower()
        if "error" in lowered or "unknown" in lowered or "not found" in lowered:
            continue
        return True
    return False


async def _hotspot_iface(address: str) -> str:
    """The running soft AP's interface name, or "" when none exists."""
    probes = " ".join(_AP_IFACES)
    command = f'for i in {probes}; do [ -e "/sys/class/net/$i" ] && echo "$i"; done; exit 0'
    try:
        reply = await adb.shell(address, command, timeout=RADIO_TIMEOUT_S)
    except adb.AdbError:
        return ""
    for line in reply.splitlines():
        if line.strip():
            return line.strip()
    return ""


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

    @property
    def delay(self) -> float:
        """How much of the ten-second guard is still to run."""
        return max(0.0, QUIET_AFTER_ONLINE_S - self.online_for)

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="ingest-radio-quiet")

    async def _run(self) -> None:
        if self.delay > 0:
            await asyncio.sleep(self.delay)
        try:
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
        iface = await _hotspot_iface(self.address)
        config: tuple[str, str] | None = None
        if iface:
            # Read the configuration *before* stopping — and regardless of the outcome,
            # stop it: the whole point is the radio going quiet, and the unit re-arms its
            # own defaults at the next engine start.
            try:
                dump = await adb.shell(self.address, "dumpsys wifi", timeout=15.0)
                config = _parse_softap_config(dump)
            except adb.AdbError as exc:
                log.debug("could not read the hotspot configuration", error=str(exc))
        try:
            await adb.shell(self.address, "cmd wifi stop-softap", timeout=RADIO_TIMEOUT_S)
        except adb.AdbError as exc:
            if iface:
                log.warning("could not stop the unit's hotspot", error=str(exc))
            return
        if iface and config:
            self.hotspot_restore = config
            log.info("stopped the unit's hotspot for the transfer", iface=iface)
        elif iface:
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
        if self.bluetooth_off:
            await self._restore_bluetooth()
        if self.hotspot_restore:
            await self._restore_hotspot()

    async def _restore_bluetooth(self) -> None:
        if await _set_bluetooth(self.address, enable=True):
            log.info("turned the unit's Bluetooth back on")
            await _remove_flag(self.address)
            await _persist_marker("")
            self.bluetooth_off = False
        else:
            # The car has usually left. The flag stays so the on-unit watchdog acts,
            # and the marker stays so the next arrival is put right before it is asked
            # for anything.
            log.warning(
                "could not turn the unit's Bluetooth back on; the watchdog on the "
                "unit will, or the next arrival"
            )
        watchdog = self._watchdog
        if not self.bluetooth_off and watchdog is not None and watchdog.returncode is None:
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
                "could not turn the unit's Bluetooth back on; it will be tried again "
                "the next time the unit appears"
            )


def cancel_pending() -> None:
    """Stop any arrival-time restore still in flight, for shutdown."""
    for task in list(_tasks):
        task.cancel()
