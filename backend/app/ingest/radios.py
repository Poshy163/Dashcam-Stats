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
hotspot did not. ``WifiManager.stopSoftAp`` — what ``cmd wifi stop-softap`` calls — gates
on ``NETWORK_STACK``/``MAINLINE_NETWORK_STACK``, which an unrooted shell does not hold, so
the command throws and the hotspot carries on beaconing through every transfer; ``svc
bluetooth`` has no equivalent gate, which is why exactly half the feature appeared to
work. The unrooted way round it is not the WiFi service at all but the tethering binder:
``stopTethering(TETHERING_WIFI)`` gates on ``TETHER_PRIVILEGED``, which ``com.android
.shell`` *does* hold (see :data:`_STOP_VIA_TETHERING`). It works — but its reply is a red
herring, because passed a null result listener it answers with a ``NullPointerException``
raised *after* the teardown has already happened. So a stop is believed only when
:func:`_serving_ap` can no longer find the AP, never because a command returned — and when
it genuinely cannot be done, that is a warning naming the unit's own words, not silence.

**Bluetooth comes down before the hotspot, and the order is load-bearing.** This unit
couples the two: while its Bluetooth is on, it re-arms the soft AP within seconds of any
stop (``cmd bluetooth_manager enable`` is observed to drive the hotspot back up — a vendor
car-kit behaviour). A hotspot stopped while Bluetooth is still on therefore never sticks;
taking Bluetooth down first removes the thing that turns it back on, which is why
:func:`_quiet` disables Bluetooth and only then stops the AP. It also means the restore is
half-automatic: re-enabling Bluetooth brings the operator's hotspot back on its own.

**Never delay the transfer.** Every call here rides the control channel, which is idle
while the bulk socket moves bytes. The quieting itself waits until the unit has been on
the network for :data:`QUIET_AFTER_ONLINE_S`, so a car that is merely turning around on
the driveway keeps its phone connection — one that is still here ten seconds in is
parked.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import re
import time
from dataclasses import dataclass, field

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
WATCHDOG_READY_PATH = f"{FLAG_PATH}.watchdog_ready"

# Hotspot credentials are needed only to undo a process that dies after stopping the
# AP. They never belong in the server database or its backups. A short-lived mode-0600
# capsule stays inside the already-authorised Android shell boundary instead, alongside
# the watchdog flag, and is removed as soon as restoration is verified.
HOTSPOT_CAPSULE_PREFIX = "/data/local/tmp/.dashcam_analyser_hotspot_"
MAX_HOTSPOT_CAPSULE_BYTES = 512
_SAFE_TRANSITION_ID = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
_BOOT_ID = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
_DEVICE_SERIAL = re.compile(r"^[A-Za-z0-9._:-]{1,80}$")

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
#: rather than trusted configuration, and they enter a script delivered to the unit over
#: stdin. Same policy as the card filenames: validated against a conservative shape and
#: refused otherwise, never escaped.
_SAFE_AP_TEXT = re.compile(r"^[A-Za-z0-9 ._@#%+=-]{1,63}$")


async def read_device_boot_id(address: str) -> str | None:
    """Return a privacy-safe stable-device fingerprint plus this boot's UUID.

    The stable half makes a DHCP move safe to follow and rejects IP reuse by another
    unit. The boot half records whether recovery crossed a reboot; it is intentionally
    not the sole identity because a kernel boot UUID changes at every restart.
    """

    try:
        reply = await adb.shell(
            address,
            'serial="$(getprop ro.serialno)"; '
            '[ -n "$serial" ] && [ "$serial" != unknown ] || '
            'serial="$(getprop ro.boot.serialno)"; '
            "printf '%s\\n' \"$serial\"; cat /proc/sys/kernel/random/boot_id",
            timeout=RADIO_TIMEOUT_S,
        )
    except adb.AdbError:
        return None
    lines = [line.strip() for line in reply.splitlines() if line.strip()]
    if len(lines) != 2:
        return None
    serial, boot_id = lines[0], lines[1].lower()
    if not _DEVICE_SERIAL.fullmatch(serial) or not _BOOT_ID.fullmatch(boot_id):
        return None
    fingerprint = hashlib.sha256(serial.encode("utf-8")).hexdigest()[:32]
    return f"{fingerprint}@{boot_id}"


def same_device_identity(stored: str, current: str | None) -> bool:
    """Whether two stored identity tokens name the same physical unit."""

    if current is None:
        return False
    stored_device, separator, _stored_boot = stored.partition("@")
    current_device, current_separator, _current_boot = current.partition("@")
    if separator and current_separator:
        return stored_device == current_device
    # Compatibility for a transition written by the short-lived boot-UUID-only format.
    return stored == current


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


async def _confirm_bluetooth_off(address: str) -> bool:
    """Whether Bluetooth is positively off after a disable request."""
    deadline = time.monotonic() + CONFIRM_TIMEOUT_S
    while True:
        if await _bluetooth_is_on(address) is False:
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(CONFIRM_INTERVAL_S)


#: How long to keep asking whether Bluetooth actually came back, and how often.
#:
#: ``cmd bluetooth_manager enable`` returns "Success" the moment it is accepted, but
#: ``settings global bluetooth_on`` does not read 1 until the stack has finished coming up
#: -- measured by hand at two to three seconds on this unit. Checking once immediately
#: after the enable therefore races the radio and loses, which is what produced 7 "could
#: not confirm" warnings against 23 disables: the restore had worked, the verification had
#: not waited for it. Each of those left the marker set and the driver without Bluetooth
#: until the next arrival repaired it.
CONFIRM_TIMEOUT_S = 8.0
CONFIRM_INTERVAL_S = 0.5

#: Bluetooth enable is vendor-coupled to hotspot enable on the production unit, but the
#: AP interface appears several seconds after Bluetooth itself reports ON.  OFF cannot be
#: certified inside that gap: doing so disarms the watchdog and retires the durable row
#: just before the vendor brings the AP back.  Keep observing slightly beyond the measured
#: ~5-second re-arm, with a short poll so an unexpected AP can be stopped promptly.
HOTSPOT_REARM_SETTLE_S = 6.0
HOTSPOT_REARM_POLL_S = 0.25


async def _confirm_bluetooth_on(address: str) -> bool:
    """Whether Bluetooth is on, giving the radio time to say so.

    Still a positive confirmation rather than an assumption -- a timeout returns False and
    leaves the marker, the flag and the next-arrival repair all in place. It just stops
    counting "not on *yet*" as "not on".
    """
    deadline = time.monotonic() + CONFIRM_TIMEOUT_S
    while True:
        if await _bluetooth_is_on(address) is True:
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(CONFIRM_INTERVAL_S)


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


async def _serving_ap(address: str) -> str | None:
    """Serving AP interface, ``""`` for proven absence, or ``None`` if unreadable.

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
        return None
    parsed_inventory = False
    for line in reply.splitlines():
        match = _IP_LINE.match(line.strip())
        if not match:
            continue
        parsed_inventory = True
        iface, addr = match.group(1), match.group(2)
        if addr == host or not _AP_NAME.match(iface):
            continue
        return iface
    return "" if parsed_inventory else None


async def _ap_interfaces(address: str) -> tuple[str, str] | None:
    """Return ``(separate_ap, transport_ap)`` or ``None`` when state is unreadable.

    A matching interface that owns the ADB target address is conservatively classified
    as transport.  It may be a client interface on an unusually named vendor build, but
    that ambiguity must resolve to "do not stop it", never to severing the transfer.
    """
    host = address.partition(":")[0].strip()
    try:
        reply = await adb.shell(address, "ip -o addr show; exit 0", timeout=RADIO_TIMEOUT_S)
    except adb.AdbError:
        return None
    separate = ""
    transport = ""
    parsed_inventory = False
    for line in reply.splitlines():
        match = _IP_LINE.match(line.strip())
        if not match:
            continue
        parsed_inventory = True
        iface, addr = match.group(1), match.group(2)
        if not _AP_NAME.match(iface):
            continue
        if addr == host:
            transport = iface
        elif not separate:
            separate = iface
    return (separate, transport) if parsed_inventory else None


#: How a soft AP is asked to stop from an unrooted shell — the lever that actually works.
#:
#: ``stopTethering(TETHERING_WIFI)`` on the tethering binder. ``WifiManager.stopSoftAp`` —
#: what ``cmd wifi stop-softap`` calls — gates on ``NETWORK_STACK``/``MAINLINE_NETWORK_STACK``,
#: which the shell does not hold, so on this unit it throws ``SecurityException: Neither
#: user 2000 nor current process has android.permission.MAINLINE_NETWORK_STACK`` and does
#: nothing. The tethering path gates on ``TETHER_PRIVILEGED`` instead, and ``com.android
#: .shell`` (uid 2000) *does* hold it — ``granted=true`` in ``dumpsys package
#: com.android.shell`` on the live unit. There is no ``cmd tethering`` verb
#: (``TetheringService`` overrides only ``dump()``, so ``cmd tethering <anything>`` answers
#: Binder's ``No shell command implementation.``), so the call rides a raw ``service
#: call``: transaction **5** is ``stopTethering(int type, String pkg, String tag,
#: IIntResultListener receiver)`` in the ``android.net.ITetheringConnector`` AIDL, and
#: ``i32 0`` is ``TETHERING_WIFI``. The result listener is passed ``null``, so the
#: *callback* raises ``NullPointerException`` — but only after the teardown has run, which
#: is why success is judged solely by :func:`_stop_took_effect` and ``; exit 0`` keeps that
#: captured exception from surfacing as a failed control call. Verified against the live
#: unit (Unisoc UIS7861, Android 15, uid 2000): with Bluetooth already down, the AP
#: interface dropped inside the settle budget and stayed down.
#:
#: The old warning here feared that a wrong ``service call`` code, with *start* sitting
#: beside *stop*, could switch the hotspot **on**. Two guards make that impossible now.
#: This is reached only when :func:`_serving_ap` has already found an AP *serving*, so any
#: "start" a mis-numbered code triggered would be a no-op on an AP that is already up; and
#: the effect is verified afterwards regardless of what any code did. The hazard was firing
#: at an AP that was *off* — a path that does not exist here, because nothing calls this
#: unless an AP is already serving.
_STOP_VIA_TETHERING = (
    "service call tethering 5 i32 0 s16 com.android.shell s16 com.android.shell null; exit 0"
)

#: The fallback, for a unit whose adbd runs as root (a debuggable build) where
#: ``WifiManager.stopSoftAp`` is reachable directly. Never reached on an unrooted unit: the
#: binder above has by then taken the AP down and :func:`_stop_hotspot` has returned.
_STOP_COMMANDS = ("cmd wifi stop-softap",)


#: How long a stop is given to actually take effect, and how often to look.
#:
#: ``WifiServiceImpl.stopSoftAp`` posts to the WiFi handler thread and returns straight
#: away; ``SoftApManager`` then tears hostapd down and drops the interface's address some
#: unspecified time later. Asking once, immediately, therefore reads a *successful* stop
#: as "accepted, but the hotspot is still up" — and on a rooted unit, where the command
#: finally works, that is every single window. The consequence is not cosmetic: a stop
#: wrongly judged failed means ``hotspot_restore`` is never set, so the hotspot is taken
#: down and then never put back.
STOP_SETTLE_BUDGET_S = 3.0
STOP_SETTLE_POLL_S = 0.4


async def _stop_took_effect(address: str) -> bool:
    """Whether the AP is really gone, allowing for the teardown not being instant."""
    deadline = asyncio.get_running_loop().time() + STOP_SETTLE_BUDGET_S
    while True:
        observed = await _serving_ap(address)
        if observed == "":
            return True
        if asyncio.get_running_loop().time() >= deadline:
            return False
        await asyncio.sleep(STOP_SETTLE_POLL_S)


async def _stop_hotspot(address: str) -> tuple[bool, str]:
    """Stop a serving soft AP. Returns (stopped, what the unit said when it would not).

    Success is not a zero exit status; it is :func:`_serving_ap` no longer finding the
    interface. The unit's own words are carried back rather than discarded, because this
    is a failure the operator can do nothing about from the app and everything about from
    the log — and a feature that fails silently on every window is the mistake this
    project has already made once.
    """
    replies: list[str] = []

    # The tethering binder first: the one lever an unrooted shell has. Fired for effect,
    # never for its reply — the null result listener makes the callback NPE after the
    # teardown, so only _stop_took_effect can tell whether it actually worked.
    with contextlib.suppress(adb.AdbError):
        await adb.shell(address, _STOP_VIA_TETHERING, timeout=RADIO_TIMEOUT_S)
    if await _stop_took_effect(address):
        return True, ""
    replies.append("stopTethering: accepted, but the hotspot is still up")

    # Then the wifi command, for a rooted/debuggable unit where stopSoftAp is reachable.
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
        if await _stop_took_effect(address):
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


#: Where the unit's last hotspot refusal is kept, in the unit's own words.
#:
#: A read-only setting rather than a log line, because this is the half of the feature
#: that fails permanently on an unrooted unit and the log line saying so scrolls away
#: with the window that produced it. The first version of this feature failed silently
#: on every transfer; the second logged it once per run; this makes the standing state —
#: "your unit will not allow it, and here is what it said" — visible on the Settings
#: page for as long as it remains true. Cleared the moment a stop actually works.
REFUSAL_KEY = "ingest.hotspot_refusal"


async def _persist_refusal(value: str) -> None:
    try:
        await get_settings_service().set(REFUSAL_KEY, value, internal=True)
    except Exception as exc:
        log.debug("could not persist the hotspot refusal", error=str(exc))


async def _remove_flag(address: str) -> None:
    with contextlib.suppress(adb.AdbError):
        await adb.shell(
            address,
            f"rm -f '{FLAG_PATH}' '{WATCHDOG_READY_PATH}'",
            timeout=RADIO_TIMEOUT_S,
        )


async def _shell_script(address: str, script: str, *, timeout: float) -> str:
    """Run a shell script over stdin so credentials never appear in local process argv."""

    process = await asyncio.create_subprocess_exec(
        adb.adb_path(),
        "-s",
        address,
        "shell",
        "sh",
        "-s",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _stderr = await asyncio.wait_for(
            process.communicate(script.encode("utf-8")),
            timeout=timeout,
        )
    except TimeoutError:
        process.kill()
        with contextlib.suppress(TimeoutError, Exception):
            await asyncio.wait_for(process.wait(), timeout=3.0)
        raise adb.AdbError(f"adb shell script timed out after {timeout:.0f}s") from None
    except asyncio.CancelledError:
        process.kill()
        raise
    if process.returncode:
        # Do not include stderr: a shell parser can echo the input line containing the
        # passphrase, defeating the purpose of keeping it out of argv.
        raise adb.AdbError(f"adb shell script failed ({process.returncode})")
    return stdout.decode("utf-8", "replace").replace("\r", "").strip()


def _valid_hotspot_capsule_path(path: str) -> bool:
    if not path.startswith(HOTSPOT_CAPSULE_PREFIX) or not path.endswith(".json"):
        return False
    transition_id = path[len(HOTSPOT_CAPSULE_PREFIX) : -len(".json")]
    return bool(_SAFE_TRANSITION_ID.fullmatch(transition_id))


def hotspot_capsule_path(transition_id: str) -> str | None:
    """Return the deterministic recovery path for a validated transition UUID."""

    if not _SAFE_TRANSITION_ID.fullmatch(transition_id):
        return None
    return f"{HOTSPOT_CAPSULE_PREFIX}{transition_id}.json"


async def _arm_watchdog(
    address: str,
    deadline_s: int,
    *,
    restore_bluetooth: bool = True,
    hotspot_baseline: str = "unknown",
    hotspot_capsule_path: str | None = None,
) -> asyncio.subprocess.Process | None:
    """Leave an exact radio-restoration watchdog running on the unit.

    The same shape as the transfer's listener: nothing is backgrounded remotely (this
    unit's ``adb shell`` never returns for a backgrounded command), so the adb child is
    simply never awaited, and the remote command outlives a dropped session — which is
    the one property that matters, because "the session dropped" is exactly the failure
    the watchdog exists for. If the run restores first it removes the flag, and a fired
    watchdog that finds no flag exits without touching anything.
    """
    if hotspot_baseline not in {"on", "off", "transport", "unknown"}:
        return None
    if hotspot_capsule_path is not None and not _valid_hotspot_capsule_path(hotspot_capsule_path):
        return None
    if hotspot_baseline == "unknown" and hotspot_capsule_path is not None:
        # Compatibility for the legacy RadioQuiet caller: a capsule only exists after it
        # positively observed and stopped a serving AP.
        hotspot_baseline = "on"
    if hotspot_baseline == "on" and hotspot_capsule_path is None:
        return None
    restore_commands: list[str] = []
    if restore_bluetooth:
        restore_commands.append("cmd bluetooth_manager enable || svc bluetooth enable")
    if restore_bluetooth and hotspot_baseline in {"on", "off"}:
        # This vendor stack re-arms its AP a few seconds after Bluetooth is enabled. The
        # final AP action therefore follows that settle period, rather than racing the
        # re-arm and leaving the opposite of the captured baseline behind.
        restore_commands.append("sleep 5")
    if hotspot_baseline == "off":
        # OFF is distinct from TRANSPORT. stopTethering is safe only because capture
        # classified an AP carrying the ADB target address as transport, and that state
        # never reaches this branch. Run both supported stop paths: the binder works on
        # the field unit; the cmd fallback covers rooted/debuggable Android builds.
        restore_commands.append(f"({_STOP_VIA_TETHERING})")
        restore_commands.append("cmd wifi stop-softap >/dev/null 2>&1 || true")
    elif hotspot_baseline == "on" and hotspot_capsule_path is not None:
        # The compact JSON capsule was generated from a strict allowlist and read back
        # byte-for-byte before this point. Values are expanded only as quoted argv, so a
        # damaged file cannot become shell syntax. The capsule and recovery flag remain
        # until a later server-side readback positively verifies the exact baseline.
        restore_commands.append(
            f"capsule='{hotspot_capsule_path}'; "
            'ssid="$(sed -n \'s/.*"ssid":"\\([^"]*\\)".*/\\1/p\' "$capsule" | head -n 1)"; '
            'passphrase="$(sed -n \'s/.*"passphrase":"\\([^"]*\\)".*/\\1/p\' "$capsule" | head -n 1)"; '
            'if [ -n "$ssid" ] && [ -n "$passphrase" ]; then '
            'cmd wifi start-softap "$ssid" wpa2 "$passphrase" >/dev/null 2>&1 || true; '
            "fi"
        )
    if not restore_commands:
        return None
    command = (
        f"umask 077; printf armed > '{WATCHDOG_READY_PATH}'; sleep {int(deadline_s)}; "
        f"[ -f '{FLAG_PATH}' ] || {{ rm -f '{WATCHDOG_READY_PATH}'; exit 0; }}; "
        f"rm -f '{WATCHDOG_READY_PATH}'; " + "; ".join(restore_commands)
    )
    try:
        process = await asyncio.create_subprocess_exec(
            adb.adb_path(),
            "-s",
            address,
            "shell",
            command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if process.returncode is not None:
                return None
            reply = await adb.shell(
                address,
                f"[ \"$(cat '{WATCHDOG_READY_PATH}' 2>/dev/null)\" = armed ] && "
                "printf armed; exit 0",
                timeout=RADIO_TIMEOUT_S,
            )
            if reply.strip() == "armed":
                return process
            await asyncio.sleep(0.1)
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        with contextlib.suppress(TimeoutError, Exception):
            await asyncio.wait_for(process.wait(), timeout=1.0)
        return None
    except Exception as exc:
        log.warning("could not arm the Bluetooth watchdog on the unit", error=str(exc))
        return None


@dataclass(frozen=True, slots=True)
class RadioSnapshot:
    """The exact observable baseline captured before any radio side effect."""

    bluetooth: str
    hotspot: str
    hotspot_interface: str | None = None
    transport_interface: str | None = None
    hotspot_config: tuple[str, str] | None = field(default=None, repr=False)


class RadioController:
    """Awaited radio primitives for the durable ingest coordinator.

    Database ownership lives in :mod:`app.ingest.radio_coordinator`; this object owns the
    process-local exclusion and the on-unit Bluetooth watchdog.  Methods deliberately do
    one observable effect at a time so the coordinator can checkpoint intent before the
    command and verification after it.
    """

    def __init__(self, address: str, *, watchdog_deadline_s: int) -> None:
        self.address = address
        self.watchdog_deadline_s = watchdog_deadline_s
        self._watchdog: asyncio.subprocess.Process | None = None
        self._bluetooth_baseline = "unknown"
        self._hotspot_baseline = "unknown"
        self._hotspot_rearm_deadline: float | None = None
        self._transport_interface: str | None = None
        self._hotspot_capsule_path: str | None = None
        self._owns = False

    def claim(self) -> None:
        global _active
        if not self._owns:
            _active += 1
            self._owns = True

    async def capture(self) -> RadioSnapshot:
        """Capture all baseline state under the process radio lock."""
        async with _lock:
            bluetooth_value = await _bluetooth_is_on(self.address)
            bluetooth = (
                "on"
                if bluetooth_value is True
                else "off"
                if bluetooth_value is False
                else "unknown"
            )
            self._bluetooth_baseline = bluetooth
            interfaces = await _ap_interfaces(self.address)
            if interfaces is None:
                self._hotspot_baseline = "unknown"
                self._transport_interface = None
                return RadioSnapshot(bluetooth=bluetooth, hotspot="unknown")
            iface, transport_iface = interfaces
            self._transport_interface = transport_iface or None
            if not iface:
                if transport_iface:
                    self._hotspot_baseline = "transport"
                    return RadioSnapshot(
                        bluetooth=bluetooth,
                        hotspot="transport",
                        hotspot_interface=transport_iface,
                        transport_interface=transport_iface,
                    )
                self._hotspot_baseline = "off"
                return RadioSnapshot(bluetooth=bluetooth, hotspot="off")

            config: tuple[str, str] | None = None
            try:
                dump = await adb.shell(self.address, "dumpsys wifi", timeout=15.0)
                config = _parse_softap_config(dump)
            except adb.AdbError as exc:
                log.debug("could not read the hotspot configuration", error=str(exc))
            self._hotspot_baseline = "on"
            return RadioSnapshot(
                bluetooth=bluetooth,
                hotspot="on",
                hotspot_interface=iface,
                transport_interface=transport_iface or None,
                hotspot_config=config,
            )

    async def persist_hotspot_capsule(
        self,
        transition_id: str,
        config: tuple[str, str],
    ) -> str | None:
        """Persist restore-only credentials inside the device's shell boundary."""
        if not _SAFE_TRANSITION_ID.fullmatch(transition_id):
            return None
        ssid, passphrase = config
        if not _SAFE_AP_TEXT.fullmatch(ssid) or not _SAFE_AP_TEXT.fullmatch(passphrase):
            return None
        body = json.dumps(
            {"schema_version": 1, "ssid": ssid, "passphrase": passphrase},
            ensure_ascii=True,
            separators=(",", ":"),
        )
        body_size = len(body.encode("utf-8"))
        if "'" in body or body_size > MAX_HOTSPOT_CAPSULE_BYTES:
            return None
        target = hotspot_capsule_path(transition_id)
        if target is None:
            return None
        partial = f"{target}.partial"
        command = (
            f"rm -f '{partial}' && umask 077 && printf '%s' '{body}' > '{partial}' && "
            f"chmod 600 '{partial}' && [ -f '{partial}' ] && [ ! -L '{partial}' ] && "
            f"[ \"$(wc -c < '{partial}')\" -eq {body_size} ] && "
            f"[ \"$(cat '{partial}')\" = '{body}' ] && "
            f"(sync '{partial}' 2>/dev/null || sync) && mv -f '{partial}' '{target}' && "
            f"chmod 600 '{target}'"
        )
        try:
            async with _lock:
                await _shell_script(self.address, command, timeout=10.0)
                readback = await adb.shell(
                    self.address,
                    f"[ -f '{target}' ] && [ ! -L '{target}' ] && cat '{target}'",
                    timeout=6.0,
                )
        except adb.AdbError:
            return None
        if readback == body:
            self._hotspot_capsule_path = target
            return target
        return None

    async def read_hotspot_capsule(self, path: str) -> tuple[str, str] | None:
        if not self._valid_capsule_path(path):
            return None
        try:
            async with _lock:
                raw = await adb.shell(
                    self.address,
                    f"[ -f '{path}' ] && [ ! -L '{path}' ] && "
                    f"head -c {MAX_HOTSPOT_CAPSULE_BYTES + 1} '{path}'; exit 0",
                    timeout=6.0,
                )
        except adb.AdbError:
            return None
        if not raw or len(raw.encode("utf-8")) > MAX_HOTSPOT_CAPSULE_BYTES:
            return None
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(value, dict) or set(value) != {"schema_version", "ssid", "passphrase"}:
            return None
        ssid = value.get("ssid")
        passphrase = value.get("passphrase")
        if (
            value.get("schema_version") != 1
            or not isinstance(ssid, str)
            or not isinstance(passphrase, str)
            or not _SAFE_AP_TEXT.fullmatch(ssid)
            or not _SAFE_AP_TEXT.fullmatch(passphrase)
            or len(passphrase) < 8
        ):
            return None
        return ssid, passphrase

    async def remove_hotspot_capsule(self, path: str) -> bool:
        if not self._valid_capsule_path(path):
            return False
        try:
            async with _lock:
                reply = await adb.shell(
                    self.address,
                    f"rm -f '{path}'; [ ! -e '{path}' ] && printf removed; exit 0",
                    timeout=6.0,
                )
        except adb.AdbError:
            return False
        removed = reply.strip() == "removed"
        if removed and self._hotspot_capsule_path == path:
            self._hotspot_capsule_path = None
        return removed

    @staticmethod
    def _valid_capsule_path(path: str) -> bool:
        return _valid_hotspot_capsule_path(path)

    async def _ensure_watchdog_locked(self, *, marker: str) -> bool:
        if self._watchdog is not None and self._watchdog.returncode is None:
            return True
        await _persist_marker(marker)
        try:
            await _remove_flag(self.address)
            await adb.shell(self.address, f"touch '{FLAG_PATH}'", timeout=RADIO_TIMEOUT_S)
        except adb.AdbError:
            log.warning("could not create the radio watchdog flag; leaving radios on")
            return False
        self._watchdog = await _arm_watchdog(
            self.address,
            self.watchdog_deadline_s,
            restore_bluetooth=self._bluetooth_baseline != "off",
            hotspot_baseline=self._hotspot_baseline,
            hotspot_capsule_path=self._hotspot_capsule_path,
        )
        if self._watchdog is None:
            await _remove_flag(self.address)
            await _persist_marker("")
            log.warning("could not prove the radio watchdog was armed; leaving radios on")
            return False
        return True

    async def disable_bluetooth(self) -> bool:
        """Disable and positively verify Bluetooth, with all legacy recovery guards."""
        async with _lock:
            # The remote process must acknowledge that it is alive before Bluetooth is
            # touched. Merely creating a local ``adb`` child is not proof that its shell
            # reached the unit; an immediate transport failure would otherwise leave no
            # independent restoration path after the disable.
            if not await self._ensure_watchdog_locked(marker="bluetooth"):
                return False
            accepted = await _set_bluetooth(self.address, enable=False)
            verified = accepted and await _confirm_bluetooth_off(self.address)
            if verified:
                log.info("turned the unit's Bluetooth off for the transfer")
                return True
            log.warning("could not confirm the unit's Bluetooth is off; aborting radio quiet")
            return False

    async def disable_hotspot(self) -> bool:
        """Stop a separate serving AP and verify its interface disappeared."""
        async with _lock:
            if self._hotspot_capsule_path is None:
                log.warning("hotspot recovery capsule is unavailable; leaving the hotspot on")
                return False
            if not await self._ensure_watchdog_locked(marker="hotspot"):
                return False
            stopped, why = await _stop_hotspot(self.address)
            if stopped:
                await _persist_refusal("")
                log.info("stopped the unit's hotspot for the transfer")
                return True
            await _persist_refusal(why or "the unit refused without saying why")
            log.warning("could not stop the unit's hotspot", reply=why)
            return False

    async def restore_bluetooth(self, baseline: str) -> bool:
        """Restore/verify the baseline Bluetooth state, including an original OFF."""
        async with _lock:
            if baseline == "unknown":
                return False
            if baseline == "on":
                restored = await _set_bluetooth(self.address, enable=True)
                if restored:
                    # Start the settle clock at the accepted enable, not after the
                    # Bluetooth confirmation: that command is what triggers the vendor's
                    # delayed hotspot re-arm. Issuing the idempotent enable even when a
                    # watchdog may already have restored Bluetooth also covers a re-arm
                    # that was in flight just before this process observed the unit.
                    self._hotspot_rearm_deadline = time.monotonic() + HOTSPOT_REARM_SETTLE_S
                restored = restored and await _confirm_bluetooth_on(self.address)
            else:
                self._hotspot_rearm_deadline = None
                current = await _bluetooth_is_on(self.address)
                restored = current is False
                if current is True:
                    restored = await _set_bluetooth(self.address, enable=False)
                    restored = restored and await _confirm_bluetooth_off(self.address)
            if restored:
                # The hotspot is restored afterwards. Standing the shared watchdog down
                # here would strand an originally-on AP if that later operation failed.
                pass
            return restored

    async def _restore_hotspot_off(self) -> bool:
        """Keep the original OFF baseline true through any delayed Bluetooth re-arm."""
        while True:
            serving = await _serving_ap(self.address)
            if serving is None:
                return False
            if serving:
                stopped, why = await _stop_hotspot(self.address)
                if not stopped:
                    log.warning("could not restore the originally-off hotspot", reply=why)
                    return False

            deadline = self._hotspot_rearm_deadline
            if deadline is None:
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._hotspot_rearm_deadline = None
                return True
            await asyncio.sleep(min(HOTSPOT_REARM_POLL_S, remaining))

    async def _wait_for_hotspot_rearm(self) -> str | None:
        """Let an originally-ON AP reappear before explicitly starting a duplicate."""
        while True:
            serving = await _serving_ap(self.address)
            if serving is None or serving:
                if serving:
                    self._hotspot_rearm_deadline = None
                return serving
            deadline = self._hotspot_rearm_deadline
            if deadline is None:
                return ""
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._hotspot_rearm_deadline = None
                return ""
            await asyncio.sleep(min(HOTSPOT_REARM_POLL_S, remaining))

    async def restore_hotspot(
        self,
        baseline: str,
        config: tuple[str, str] | None,
    ) -> bool:
        """Restore the observed AP state and verify the effect.

        This intentionally runs *after* Bluetooth restoration.  Enabling Bluetooth on
        the target vendor unit may re-arm a soft AP; an originally-off hotspot therefore
        needs a final stop to return to the true baseline.
        """
        async with _lock:
            if baseline == "transport":
                return True  # never touched; it is the active data/control path
            if baseline == "unknown":
                return False
            if baseline == "off":
                return await self._restore_hotspot_off()

            serving = await self._wait_for_hotspot_rearm()
            if serving is None:
                # A lost ADB reply and a proven-empty interface inventory are different
                # safety facts.  In particular, Bluetooth restoration may have re-armed
                # this vendor's AP: an unreadable inventory cannot certify the original
                # OFF baseline and must leave durable recovery armed for the next arrival.
                return False
            if not config:
                return False
            ssid, passphrase = config
            if serving:
                try:
                    current = _parse_softap_config(
                        await adb.shell(self.address, "dumpsys wifi", timeout=15.0)
                    )
                except adb.AdbError:
                    current = None
                if current == config:
                    return True
                # A serving AP is not necessarily the captured AP. Replace it only because
                # the exact prior configuration is protected in the recovery capsule.
                stopped, why = await _stop_hotspot(self.address)
                if not stopped:
                    log.warning("could not replace a mismatched restored hotspot", reply=why)
                    return False
            try:
                reply = await _shell_script(
                    self.address,
                    f"cmd wifi start-softap '{ssid}' wpa2 '{passphrase}'",
                    timeout=RADIO_TIMEOUT_S,
                )
            except adb.AdbError:
                return False
            if not _accepted(reply):
                return False
            deadline = time.monotonic() + CONFIRM_TIMEOUT_S
            while True:
                serving = await _serving_ap(self.address)
                if serving is None:
                    return False
                if serving:
                    try:
                        current = _parse_softap_config(
                            await adb.shell(self.address, "dumpsys wifi", timeout=15.0)
                        )
                    except adb.AdbError:
                        current = None
                    if current == config:
                        return True
                if time.monotonic() >= deadline:
                    return False
                await asyncio.sleep(CONFIRM_INTERVAL_S)

    async def _stop_watchdog(self) -> None:
        watchdog = self._watchdog
        if watchdog is not None and watchdog.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                watchdog.kill()
            with contextlib.suppress(TimeoutError, Exception):
                await asyncio.wait_for(watchdog.wait(), timeout=3.0)

    async def stand_down_watchdog(self) -> bool:
        """Disarm only after every changed radio has regained its exact baseline."""
        async with _lock:
            try:
                reply = await adb.shell(
                    self.address,
                    f"rm -f '{FLAG_PATH}' '{WATCHDOG_READY_PATH}'; "
                    f"[ ! -e '{FLAG_PATH}' ] && [ ! -e '{WATCHDOG_READY_PATH}' ] && "
                    "printf cleared; exit 0",
                    timeout=RADIO_TIMEOUT_S,
                )
            except adb.AdbError:
                return False
            if reply.strip() != "cleared":
                return False
            await self._stop_watchdog()
            await _persist_marker("")
            return True

    async def release(self) -> None:
        global _active
        if self._owns:
            _active -= 1
            self._owns = False


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
    hotspot_restore: tuple[str, str] | None = field(default=None, repr=False)
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
        # Bluetooth first, and not for tidiness: while this unit's Bluetooth is on it
        # re-arms the soft AP within seconds of any stop (a vendor car-kit coupling —
        # `cmd bluetooth_manager enable` is observed to drive the hotspot up). Stopping
        # the AP while Bluetooth is still on therefore never sticks; taking Bluetooth down
        # first removes the thing that turns it back on. Confirmed on the live unit.
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
        if iface is None:
            log.warning("could not read the unit's hotspot state; leaving it alone")
            return
        if iface == "":
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
            await _persist_refusal(why or "the unit refused without saying why")
            return
        await _persist_refusal("")
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
            restored = await _confirm_bluetooth_on(self.address)
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
            await _shell_script(
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


#: How long to leave a failed restore alone before trying it again.
#:
#: The poller calls :func:`restore_if_pending` on every tick of the arrival transition, and
#: an arrival *hold* deliberately keeps that branch live for the whole gate -- at the shipped
#: two-second poll and two-minute uptime threshold, about sixty ticks. That was harmless
#: while an accepted ``enable`` cleared the marker on the first one. Now that the marker
#: survives an enable the radio did not honour -- which is the case worth retrying, and the
#: reason the verification exists -- every one of those ticks would spawn a task, take the
#: lock, issue two adb round trips and log twice. A minute between attempts keeps the retry
#: and drops the storm.
RESTORE_RETRY_S = 60.0

#: When the last restore attempt was made, per address.
_last_restore: dict[str, float] = {}


def restore_if_pending(address: str) -> None:
    """If a previous window left radios off, put them right. Never blocks the caller.

    Called by the poller on the offline-to-online transition — which, for a unit whose
    restore failed, is usually the next morning on the same driveway. Fired rather than
    awaited so it costs the arriving window nothing; it shares the control channel with
    the run that is starting, and both are sub-second calls.

    Debounced per address, the way the recorder-health collect is: the caller re-fires this
    on every tick and the marker no longer clears on an unverified success, so without a
    timer a unit whose radio will not come back would be asked once every two seconds for
    the length of the arrival hold.
    """
    try:
        pending = str(get_settings_service().get_nowait(MARKER_KEY) or "").strip()
    except Exception:
        pending = ""
    if not pending:
        return

    now = time.monotonic()
    last = _last_restore.get(address)
    if last is not None and now - last < RESTORE_RETRY_S:
        return
    _last_restore[address] = now

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
            # Confirmed, not assumed -- the same rule `_restore_bluetooth` follows, and for
            # the same reason. A clean `enable` is not proof the radio came back, and this
            # path used to clear the marker and the on-unit watchdog flag on that alone. On
            # a unit where the command is accepted but the radio stays down, that stood the
            # watchdog off, discarded the marker, and left nothing anywhere to try again:
            # the driver's hands-free stayed off indefinitely while the read-only "Radios
            # awaiting restore" setting -- which exists so a silent phone is never a mystery
            # -- read as clean.
            if await _set_bluetooth(address, enable=True) and await _bluetooth_is_on(address):
                await _remove_flag(address)
                await _persist_marker("")
                log.info("the unit's Bluetooth is back on")
            else:
                log.warning(
                    "could not confirm the unit's Bluetooth is back on; the watchdog on "
                    "the unit will see to it, or the next arrival"
                )


async def cancel_pending() -> None:
    """Stop any arrival-time restore still in flight, and wait for it, for shutdown.

    Awaited for the same reason the health tasks are: a restore that is cancelled mid-adb
    is holding a subprocess, and returning before it has unwound leaves that to the
    interpreter to notice at exit.
    """
    tasks = list(_tasks)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _last_restore.clear()
