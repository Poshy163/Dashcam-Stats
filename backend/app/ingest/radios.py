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
run's own restore, plus a watchdog left running on the unit that re-enables Bluetooth when
its renewable lease expires whether or not this app still exists, plus a marker persisted
on this side so that anything still off is turned back on the moment the unit is next seen,
before a single byte is asked for. The watchdog gates on a flag file the restore removes,
so a stale watchdog whose run already restored does nothing.

**Only turn off what can be turned back on.** Bluetooth's state is readable
(``settings get global bluetooth_on``), so it is toggled freely and only when it was
actually on. A generic hotspot is restarted only when its exact SSID and passphrase were
recovered from ``dumpsys wifi``. The production head unit has one narrower opt-in path:
its approved system controller exposes a package-scoped action that asks Android to start
Wi-Fi tethering from the already-saved configuration. The exact Zlink and controller
builds are attested before either radio changes, and the recovery capsule contains only a
mode name. Either path has to keep the captured AP interface visible through a final
stability window before recovery is called complete.

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
couples the two: while its Bluetooth is on, it can re-arm the soft AP within seconds of any
stop. A hotspot stopped while Bluetooth is still on therefore may not stick; taking
Bluetooth down first removes that race. Restoration never relies on that coupling,
however: after Bluetooth returns, the attested controller explicitly starts the saved
tethering profile and the captured AP interface is verified.

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
import math
import re
import secrets
import time
from collections.abc import Awaitable, Callable
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
WATCHDOG_LEASE_PATH = f"{FLAG_PATH}.watchdog_lease"
WATCHDOG_LEASE_PARTIAL_PATH = f"{WATCHDOG_LEASE_PATH}.partial"
WATCHDOG_EXPIRY_CLAIM_PATH = f"{WATCHDOG_READY_PATH}.expired"
WATCHDOG_PID_PATH = f"{FLAG_PATH}.watchdog_pid"
WATCHDOG_PID_PARTIAL_PATH = f"{WATCHDOG_PID_PATH}.partial"
WATCHDOG_SCRIPT_PATH = f"{FLAG_PATH}.watchdog.sh"
WATCHDOG_SCRIPT_PARTIAL_PATH = f"{WATCHDOG_SCRIPT_PATH}.partial"
WATCHDOG_ACTIVE_PATH = f"{FLAG_PATH}.watchdog_active"
WATCHDOG_ACTIVE_PARTIAL_PATH = f"{WATCHDOG_ACTIVE_PATH}.partial"
WATCHDOG_SCRIPT_PREFIX = f"{FLAG_PATH}.watchdog_"
_WATCHDOG_TOKEN = re.compile(r"^[0-9a-f]{32}$")

# The lease is an Android monotonic-uptime second, not wall-clock time. The watchdog polls
# locally on the unit, while the server renews with enough headroom to survive several
# transient ADB failures without either restoring mid-transfer or weakening crash recovery.
WATCHDOG_LEASE_POLL_S = 2
WATCHDOG_MAX_RENEW_INTERVAL_S = 30.0
WATCHDOG_RENEW_RETRY_S = 1.0

# Hotspot credentials are needed only to undo a process that dies after stopping the
# AP. They never belong in the server database or its backups. A short-lived mode-0600
# capsule stays inside the already-authorised Android shell boundary instead, alongside
# the watchdog flag, and is removed as soon as restoration is verified.
HOTSPOT_CAPSULE_PREFIX = "/data/local/tmp/.dashcam_analyser_hotspot_"
MAX_HOTSPOT_CAPSULE_BYTES = 512
_SAFE_TRANSITION_ID = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
_BOOT_ID = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
_DEVICE_SERIAL = re.compile(r"^[A-Za-z0-9._:-]{1,80}$")

# ``bluetooth_rearm`` is retained as the schema-v2 capsule name for compatibility with
# transitions already on disk. The original passive Bluetooth edge was not reliable. On
# the approved production image, restoration now uses the exact FunctionCore system
# package's credential-free tethering action and then verifies the captured AP interface.
# Generic units retain the exact-credentials rule.
HOTSPOT_RESTORE_EXACT = "exact_config"
HOTSPOT_RESTORE_BLUETOOTH_REARM = "bluetooth_rearm"
_HOTSPOT_RESTORE_MODES = {
    HOTSPOT_RESTORE_EXACT,
    HOTSPOT_RESTORE_BLUETOOTH_REARM,
}
ZLINK_PACKAGE = "com.zjinnova.zlink"
ZLINK_SUPPORTED_VERSION = "6.1.02"
ZLINK_SUPPORTED_VERSION_CODE = "600102"
ZLINK_SYSTEM_APK_PATH = "package:/system/app/CarZhiJian/CarZhiJian.apk"
ZLINK_SYSTEM_APK_SHA256 = "c8f43e1a2dbd957220194f59ded0eb64581a571fde59d55386e6c5b4d49967d3"
HOTSPOT_CONTROLLER_PACKAGE = "com.zqc.functioncore"
HOTSPOT_CONTROLLER_VERSION = "1.0.5"
HOTSPOT_CONTROLLER_VERSION_CODE = "5"
HOTSPOT_CONTROLLER_APK_PATH = "package:/system/app/FunctionCore/FunctionCore.apk"
HOTSPOT_CONTROLLER_APK_SHA256 = "5335519733cd7c361715f8a8c96e0062b58fb8a50292a30a78b33db63ff1917a"
HOTSPOT_START_ACTION = "action.start.tethering"
_ZLINK_ATTESTATION_ATTEMPTS = 3
_ZLINK_ATTESTATION_RETRY_S = 0.25
_BLUETOOTH_REARM_CAPSULE = {
    "schema_version": 2,
    "restore_mode": HOTSPOT_RESTORE_BLUETOOTH_REARM,
}

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


@dataclass(frozen=True, slots=True)
class HotspotRecoveryPlan:
    """Strict restore material recovered from the on-unit transition capsule."""

    mode: str
    config: tuple[str, str] | None = field(default=None, repr=False)


async def supports_zlink_bluetooth_rearm(address: str) -> bool:
    """Whether the exact approved Zlink and hotspot-controller builds are installed.

    Presence alone is insufficient: a vendor update may change the unprotected action's
    meaning. The historical function name is retained for callers and persisted v2
    recovery capsules; restoration no longer depends on a Bluetooth edge.
    """

    command = (
        f'path="$(pm path {ZLINK_PACKAGE} 2>/dev/null | head -n 1)"; '
        f'version="$(dumpsys package {ZLINK_PACKAGE} 2>/dev/null | '
        "sed -n 's/^[[:space:]]*versionName=//p' | head -n 1)" + '"; '
        f'version_code="$(dumpsys package {ZLINK_PACKAGE} 2>/dev/null | '
        "sed -n 's/^[[:space:]]*versionCode=\\([0-9]*\\).*$/\\1/p' | "
        "head -n 1)" + '"; '
        'digest=""; '
        f"if [ \"$path\" = '{ZLINK_SYSTEM_APK_PATH}' ]; then "
        "digest=\"$(sha256sum '/system/app/CarZhiJian/CarZhiJian.apk' "
        "2>/dev/null | cut -d' ' -f1)\"; fi; "
        f'controller_path="$(pm path {HOTSPOT_CONTROLLER_PACKAGE} 2>/dev/null | '
        'head -n 1)"; '
        f'controller_version="$(dumpsys package {HOTSPOT_CONTROLLER_PACKAGE} 2>/dev/null | '
        "sed -n 's/^[[:space:]]*versionName=//p' | head -n 1)" + '"; '
        f'controller_version_code="$(dumpsys package {HOTSPOT_CONTROLLER_PACKAGE} '
        "2>/dev/null | sed -n 's/^[[:space:]]*versionCode=\\([0-9]*\\).*$/\\1/p' | "
        "head -n 1)" + '"; '
        'controller_digest=""; '
        f"if [ \"$controller_path\" = '{HOTSPOT_CONTROLLER_APK_PATH}' ]; then "
        "controller_digest=\"$(sha256sum '/system/app/FunctionCore/FunctionCore.apk' "
        "2>/dev/null | cut -d' ' -f1)\"; fi; "
        "printf '%s\\n%s\\n%s\\n%s\\n%s\\n%s\\n%s\\n%s' "
        '"$path" "$version" "$version_code" "$digest" "$controller_path" '
        '"$controller_version" "$controller_version_code" "$controller_digest"; exit 0'
    )
    for attempt in range(1, _ZLINK_ATTESTATION_ATTEMPTS + 1):
        try:
            reply = await adb.shell(address, command, timeout=15.0)
        except adb.AdbError:
            # The AP connection can briefly stall while Android is associating. A failed
            # read is not evidence that the exact build changed, but neither is it safe
            # enough to quiet radios on its own. Confirm twice more before declining.
            if attempt < _ZLINK_ATTESTATION_ATTEMPTS:
                await asyncio.sleep(_ZLINK_ATTESTATION_RETRY_S)
                continue
            return False
        lines = [line.strip() for line in reply.splitlines()]
        if len(lines) != 8:
            if attempt < _ZLINK_ATTESTATION_ATTEMPTS:
                await asyncio.sleep(_ZLINK_ATTESTATION_RETRY_S)
                continue
            return False
        return (
            lines[0] == ZLINK_SYSTEM_APK_PATH
            and lines[1] == ZLINK_SUPPORTED_VERSION
            and lines[2] == ZLINK_SUPPORTED_VERSION_CODE
            and lines[3] == ZLINK_SYSTEM_APK_SHA256
            and lines[4] == HOTSPOT_CONTROLLER_APK_PATH
            and lines[5] == HOTSPOT_CONTROLLER_VERSION
            and lines[6] == HOTSPOT_CONTROLLER_VERSION_CODE
            and lines[7] == HOTSPOT_CONTROLLER_APK_SHA256
        )
    return False


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
    A redacted or oddly-shaped value returns None. The durable coordinator then leaves a
    generic AP alone unless the operator explicitly enabled the package-gated Zlink
    Bluetooth re-arm strategy.
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
HOTSPOT_REARM_STABILITY_S = 1.0


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

#: The exact production controller's registered receiver calls Android's privileged
#: ``startTethering(TETHERING_WIFI, ...)`` API without accepting or emitting credentials.
#: Android therefore reuses the saved system hotspot profile. The package and action are
#: fixed literals and the package build is attested before either radio changes. The
#: broadcast result is not success evidence; only the captured AP interface becoming
#: stable is.
_START_VIA_HOTSPOT_CONTROLLER = (
    f"am broadcast --user 0 -p {HOTSPOT_CONTROLLER_PACKAGE} "
    f"-a {HOTSPOT_START_ACTION} >/dev/null 2>&1 || true"
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


async def _request_saved_hotspot_start(address: str) -> bool:
    """Ask the attested controller to start Android's saved Wi-Fi tethering profile."""

    try:
        await adb.shell(address, _START_VIA_HOTSPOT_CONTROLLER, timeout=RADIO_TIMEOUT_S)
    except adb.AdbError:
        return False
    return True


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


@dataclass(frozen=True, slots=True)
class WatchdogHandle:
    """Identity of one detached on-unit recovery process and its private artifacts."""

    token: str
    pid: int

    def __post_init__(self) -> None:
        if not _WATCHDOG_TOKEN.fullmatch(self.token) or self.pid < 0:
            raise ValueError("invalid detached watchdog identity")

    @property
    def script_path(self) -> str:
        return f"{WATCHDOG_SCRIPT_PREFIX}{self.token}.sh"

    @property
    def script_partial_path(self) -> str:
        return f"{self.script_path}.partial"

    @property
    def ready_path(self) -> str:
        return f"{self.script_path}.ready"

    @property
    def lease_path(self) -> str:
        return f"{self.script_path}.lease"

    @property
    def lease_partial_path(self) -> str:
        return f"{self.lease_path}.partial"

    @property
    def expiry_claim_path(self) -> str:
        return f"{self.ready_path}.expired"


def _new_watchdog_handle(*, pid: int = 0) -> WatchdogHandle:
    return WatchdogHandle(token=secrets.token_hex(16), pid=pid)


def _watchdog_owner_cleanup_command(
    handle: WatchdogHandle,
    *,
    allow_unpublished: bool = False,
    prove: bool = False,
) -> str:
    """Clean only the exact generation named by ``handle``.

    A late generation must never disarm the active successor. The active record is
    therefore re-read immediately before shared state is removed; a mismatch is a
    fail-closed result with no FLAG or active-record mutation.
    """

    expected = f"{handle.token} {handle.pid}" if handle.pid > 0 else ""
    candidate_artifacts = " ".join(
        f"'{path}'"
        for path in (
            handle.script_path,
            handle.script_partial_path,
            handle.ready_path,
            handle.lease_path,
            handle.lease_partial_path,
            handle.expiry_claim_path,
        )
    )
    command = (
        f"state=\"$(cat '{WATCHDOG_ACTIVE_PATH}' 2>/dev/null)\"; "
        'token="${state%% *}"; pid="${state#* }"; owned=0; '
    )
    if expected:
        command += f'[ "$state" = "{expected}" ] && owned=1; '
    else:
        command += (
            f'[ "$token" = "{handle.token}" ] && case "$pid" in ""|*[!0-9]*) ;; *) owned=1;; esac; '
        )
    command += (
        'if [ "$owned" -eq 1 ]; then '
        f'script="{handle.script_path}"; '
        "tries=0; while :; do cmdline=\"$(tr '\\000' ' ' < \"/proc/$pid/cmdline\" "
        '2>/dev/null)"; case "$cmdline" in *"$script"*) ;; *) break;; esac; '
        '[ "$tries" -ge 3 ] && { printf busy; exit 0; }; '
        'kill "$pid" 2>/dev/null || true; sleep 1; tries="$((tries + 1))"; done; '
        f'[ "$(cat \'{WATCHDOG_ACTIVE_PATH}\' 2>/dev/null)" = "$state" ] || '
        "{ printf mismatch; exit 0; }; "
        f"rm -f '{FLAG_PATH}' '{WATCHDOG_ACTIVE_PATH}' '{WATCHDOG_ACTIVE_PARTIAL_PATH}' "
        f"{candidate_artifacts}; "
        "printf cleared; exit 0; fi; "
    )
    if allow_unpublished:
        cleanup_state = f"cleanup:{handle.token}"
        command += (
            f'if [ -z "$state" ]; then cleanup_state="{cleanup_state}"; '
            f"if (set -C; printf '%s\\n' \"$cleanup_state\" > "
            f"'{WATCHDOG_ACTIVE_PATH}') 2>/dev/null; then "
            f'trap \'current="$(cat "{WATCHDOG_ACTIVE_PATH}" 2>/dev/null)"; '
            f'[ "$current" = "$cleanup_state" ] && rm -f "{WATCHDOG_ACTIVE_PATH}"\' '
            "EXIT; trap 'exit 1' HUP INT TERM; "
            f'[ "$(cat \'{WATCHDOG_ACTIVE_PATH}\' 2>/dev/null)" = "$cleanup_state" ] '
            "|| exit 1; "
            f"rm -f '{FLAG_PATH}' {candidate_artifacts}; "
            "printf cleared; exit 0; fi; fi; "
        )
    command += f"rm -f {candidate_artifacts}; "
    command += "printf mismatch; exit 0" if prove else "exit 0"
    return command


def _watchdog_cleanup_command(*, prove: bool = False) -> str:
    """Return an exact, PID-checked cleanup for this module's detached watchdog."""

    legacy_artifacts = " ".join(
        f"'{path}'"
        for path in (
            WATCHDOG_READY_PATH,
            WATCHDOG_LEASE_PATH,
            WATCHDOG_LEASE_PARTIAL_PATH,
            WATCHDOG_EXPIRY_CLAIM_PATH,
            WATCHDOG_PID_PATH,
            WATCHDOG_PID_PARTIAL_PATH,
            WATCHDOG_SCRIPT_PATH,
            WATCHDOG_SCRIPT_PARTIAL_PATH,
        )
    )
    command = (
        f"state=\"$(cat '{WATCHDOG_ACTIVE_PATH}' 2>/dev/null)\"; "
        'token="${state%% *}"; pid="${state#* }"; '
        f'script="{WATCHDOG_SCRIPT_PREFIX}${{token}}.sh"; '
        'valid=1; [ "${#token}" -eq 32 ] 2>/dev/null || valid=0; '
        'case "$token" in ""|*[!0-9a-f]*) valid=0;; esac; '
        'case "$pid" in ""|*[!0-9]*) valid=0;; esac; '
        'if [ "$valid" -eq 1 ]; then '
        "cmdline=\"$(tr '\\000' ' ' < \"/proc/$pid/cmdline\" 2>/dev/null)\"; "
        'case "$cmdline" in *"$script"*) kill "$pid" 2>/dev/null || true;; esac; '
        "fi; "
        f"rm -f '{FLAG_PATH}' '{WATCHDOG_ACTIVE_PATH}' '{WATCHDOG_ACTIVE_PARTIAL_PATH}'; "
        'if [ "$valid" -eq 1 ]; then '
        'rm -f "$script" "$script.partial" "$script.ready" "$script.lease" '
        '"$script.lease.partial" "$script.ready.expired"; fi; '
        f"rm -f {legacy_artifacts}"
    )
    if prove:
        command += f"; if [ ! -e '{FLAG_PATH}' ] && [ ! -e '{WATCHDOG_READY_PATH}' ]"
        command += f" && [ ! -e '{WATCHDOG_ACTIVE_PATH}' ]"
        command += f" && [ ! -e '{WATCHDOG_LEASE_PATH}' ] && [ ! -e '{WATCHDOG_PID_PATH}' ]"
        command += f" && [ ! -e '{WATCHDOG_SCRIPT_PATH}' ]"
        command += '; then if [ "$valid" -eq 0 ] || [ ! -e "$script" ]; then printf cleared; fi; fi; exit 0'
    return command


async def _remove_flag(address: str) -> None:
    with contextlib.suppress(adb.AdbError):
        await adb.shell(address, _watchdog_cleanup_command(), timeout=RADIO_TIMEOUT_S)


async def _discard_watchdog_candidate(address: str, handle: WatchdogHandle) -> None:
    """Remove a launch that failed before it could publish a complete active record."""

    with contextlib.suppress(adb.AdbError):
        await adb.shell(
            address,
            _watchdog_owner_cleanup_command(handle, allow_unpublished=True),
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
    hotspot_restore_mode: str | None = None,
) -> WatchdogHandle | None:
    """Leave an exact radio-restoration watchdog running on the unit.

    The recovery loop is written to a mode-0700 script and launched with the head unit's
    proven Toybox ``setsid -d`` + ``nohup`` path, with every stdio descriptor detached.
    Its numeric PID, command line, readiness gate and monotonic lease are all read back
    through a second ADB session before any radio can be touched. The loop therefore
    survives loss of the launching ADB client and of this server process. If the healthy
    run restores first it removes the flag and kills only the PID whose command line still
    names this exact script; a fired watchdog that finds no flag exits without touching
    either radio.
    """
    if hotspot_baseline not in {"on", "off", "transport", "unknown"}:
        return None
    if hotspot_capsule_path is not None and not _valid_hotspot_capsule_path(hotspot_capsule_path):
        return None
    if hotspot_baseline == "unknown" and hotspot_capsule_path is not None:
        # Compatibility for the legacy RadioQuiet caller: a capsule only exists after it
        # positively observed and stopped a serving AP.
        hotspot_baseline = "on"
    if hotspot_restore_mode is None and hotspot_capsule_path is not None:
        hotspot_restore_mode = HOTSPOT_RESTORE_EXACT
    if hotspot_restore_mode is not None and hotspot_restore_mode not in _HOTSPOT_RESTORE_MODES:
        return None
    if hotspot_baseline == "on":
        if hotspot_capsule_path is None or hotspot_restore_mode is None:
            return None
        if hotspot_restore_mode == HOTSPOT_RESTORE_BLUETOOTH_REARM and not restore_bluetooth:
            return None
    restore_commands: list[str] = []
    if restore_bluetooth:
        # Bluetooth's baseline is captured independently in the durable server row.  A
        # later-damaged hotspot capsule must not suppress this last-resort recovery.
        restore_commands.append("cmd bluetooth_manager enable || svc bluetooth enable")
    if restore_bluetooth and hotspot_baseline in {"on", "off"}:
        # This vendor stack re-arms its AP a few seconds after Bluetooth is enabled. The
        # final AP action therefore follows that settle period, rather than racing the
        # re-arm and leaving the opposite of the captured baseline behind.
        restore_commands.append(f"sleep {math.ceil(HOTSPOT_REARM_SETTLE_S)}")
    if hotspot_baseline == "off":
        # OFF is distinct from TRANSPORT. stopTethering is safe only because capture
        # classified an AP carrying the ADB target address as transport, and that state
        # never reaches this branch. Run both supported stop paths: the binder works on
        # the field unit; the cmd fallback covers rooted/debuggable Android builds.
        restore_commands.append(f"({_STOP_VIA_TETHERING})")
        restore_commands.append("cmd wifi stop-softap >/dev/null 2>&1 || true")
    elif (
        hotspot_baseline == "on"
        and hotspot_capsule_path is not None
        and hotspot_restore_mode == HOTSPOT_RESTORE_EXACT
    ):
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
    elif (
        hotspot_baseline == "on"
        and hotspot_capsule_path is not None
        and hotspot_restore_mode == HOTSPOT_RESTORE_BLUETOOTH_REARM
    ):
        # The mode name is capsule compatibility. The attested controller action is the
        # deterministic recovery path; it starts the saved profile without credentials.
        restore_commands.append(_START_VIA_HOTSPOT_CONTROLLER)
    if not restore_commands:
        return None
    lease_ttl_s = max(1, int(deadline_s))
    candidate = _new_watchdog_handle()
    script_path = candidate.script_path
    script_partial_path = candidate.script_partial_path
    ready_path = candidate.ready_path
    lease_path = candidate.lease_path
    lease_partial_path = candidate.lease_partial_path
    expiry_claim_path = candidate.expiry_claim_path
    owned_state = f"{candidate.token} $$"
    cleanup_owned = (
        "cleanup_owned() { "
        f"current=\"$(cat '{WATCHDOG_ACTIVE_PATH}' 2>/dev/null)\"; "
        f'if [ "$current" = "{owned_state}" ]; then '
        f"rm -f '{WATCHDOG_ACTIVE_PATH}' '{WATCHDOG_ACTIVE_PARTIAL_PATH}' "
        f"'{ready_path}' '{lease_path}' '{lease_partial_path}' "
        f"'{expiry_claim_path}' '{script_path}' '{script_partial_path}'; fi; }}; "
    )
    watchdog_script = (
        f"umask 077; token='{candidate.token}'; {cleanup_owned}"
        f"rm -f '{lease_partial_path}' '{expiry_claim_path}'; "
        f"(set -C; printf '%s %s\\n' \"$token\" \"$$\" > '{WATCHDOG_ACTIVE_PATH}') "
        "2>/dev/null || exit 1; "
        f"chmod 600 '{WATCHDOG_ACTIVE_PATH}' || {{ cleanup_owned; exit 1; }}; "
        'now="$(cut -d. -f1 /proc/uptime 2>/dev/null)"; '
        'case "$now" in ""|*[!0-9]*) cleanup_owned; exit 1;; esac; '
        f'expiry="$((now + {lease_ttl_s}))"; '
        f"printf '%s\\n' \"$expiry\" > '{lease_partial_path}' && "
        f"chmod 600 '{lease_partial_path}' && "
        f"mv -f '{lease_partial_path}' '{lease_path}' && "
        f"printf armed > '{ready_path}' || {{ cleanup_owned; exit 1; }}; "
        "while :; do "
        f'[ "$(cat \'{WATCHDOG_ACTIVE_PATH}\' 2>/dev/null)" = "{owned_state}" ] '
        "|| exit 0; "
        f"[ -f '{FLAG_PATH}' ] && [ \"$(cat '{ready_path}' 2>/dev/null)\" = armed ] "
        "|| { cleanup_owned; exit 0; }; "
        'now="$(cut -d. -f1 /proc/uptime 2>/dev/null)"; '
        f"expiry=\"$(cat '{lease_path}' 2>/dev/null)\"; "
        'case "$now:$expiry" in "":*|*:""|*[!0-9:]*) remaining=0;; '
        '*) remaining="$((expiry - now))";; esac; '
        'if [ "$remaining" -gt 0 ]; then '
        f"delay={WATCHDOG_LEASE_POLL_S}; "
        '[ "$remaining" -le "$delay" ] && delay="$remaining"; '
        'sleep "$delay"; continue; fi; '
        f"mv -f '{ready_path}' '{expiry_claim_path}' 2>/dev/null || exit 0; "
        f'[ "$(cat \'{WATCHDOG_ACTIVE_PATH}\' 2>/dev/null)" = "{owned_state}" ] '
        "|| exit 0; "
        f"[ -f '{FLAG_PATH}' ] || {{ cleanup_owned; exit 0; }}; "
        'now="$(cut -d. -f1 /proc/uptime 2>/dev/null)"; '
        f"expiry=\"$(cat '{lease_path}' 2>/dev/null)\"; "
        'case "$now:$expiry" in "":*|*:""|*[!0-9:]*) remaining=0;; '
        '*) remaining="$((expiry - now))";; esac; '
        'if [ "$remaining" -gt 0 ]; then '
        f"mv -f '{expiry_claim_path}' '{ready_path}' "
        "2>/dev/null || exit 0; continue; fi; "
        f"rm -f '{ready_path}' '{lease_path}' '{lease_partial_path}' "
        f"'{expiry_claim_path}'; break; done; "
        f"if [ -f '{FLAG_PATH}' ] && "
        f'[ "$(cat \'{WATCHDOG_ACTIVE_PATH}\' 2>/dev/null)" = "{owned_state}" ]; then '
        + "; ".join(restore_commands)
        + "; fi; cleanup_owned"
    )
    launcher = (
        f"umask 077; rm -f '{script_partial_path}'; "
        f"cat > '{script_partial_path}' <<'DASHCAM_RADIO_WATCHDOG'\n"
        f"{watchdog_script}\n"
        "DASHCAM_RADIO_WATCHDOG\n"
        f"chmod 700 '{script_partial_path}' && "
        f"mv -f '{script_partial_path}' '{script_path}' || exit 1; "
        f"setsid -d nohup sh '{script_path}' </dev/null >/dev/null 2>&1 &"
    )
    try:
        await _shell_script(address, launcher, timeout=10.0)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            reply = await adb.shell(
                address,
                _watchdog_probe_command(candidate),
                timeout=RADIO_TIMEOUT_S,
            )
            if reply.strip().isdigit():
                return WatchdogHandle(candidate.token, int(reply.strip()))
            await asyncio.sleep(0.1)
        await _discard_watchdog_candidate(address, candidate)
        return None
    except asyncio.CancelledError:
        with contextlib.suppress(Exception):
            await asyncio.shield(_discard_watchdog_candidate(address, candidate))
        raise
    except Exception as exc:
        log.warning("could not arm the Bluetooth watchdog on the unit", error=str(exc))
        await _discard_watchdog_candidate(address, candidate)
        return None


def _watchdog_probe_command(handle: WatchdogHandle) -> str:
    """Prove that the detached PID still owns the armed, unexpired lease."""

    expected_pid = f'[ "$pid" = "{handle.pid}" ] || exit 0; ' if handle.pid > 0 else ""
    return (
        f"state=\"$(cat '{WATCHDOG_ACTIVE_PATH}' 2>/dev/null)\"; "
        'token="${state%% *}"; pid="${state#* }"; '
        f'[ "$token" = "{handle.token}" ] || exit 0; '
        'case "$pid" in ""|*[!0-9]*) exit 0;; esac; ' + expected_pid + f"[ -f '{FLAG_PATH}' ] && "
        f"[ \"$(cat '{handle.ready_path}' 2>/dev/null)\" = armed ] && "
        f"[ -s '{handle.lease_path}' ] && kill -0 \"$pid\" 2>/dev/null || exit 0; "
        "cmdline=\"$(tr '\\000' ' ' < \"/proc/$pid/cmdline\" 2>/dev/null)\"; "
        f'case "$cmdline" in *"{handle.script_path}"*) ;; *) exit 0;; esac; '
        'now="$(cut -d. -f1 /proc/uptime 2>/dev/null)"; '
        f"expiry=\"$(cat '{handle.lease_path}' 2>/dev/null)\"; "
        'case "$now:$expiry" in "":*|*:""|*[!0-9:]*) exit 0;; esac; '
        '[ "$expiry" -gt "$now" ] || exit 0; printf "%s" "$pid"; exit 0'
    )


async def _watchdog_is_armed(address: str, handle: WatchdogHandle) -> bool:
    try:
        reply = await adb.shell(
            address,
            _watchdog_probe_command(handle),
            timeout=RADIO_TIMEOUT_S,
        )
    except adb.AdbError:
        return False
    return reply.strip() == str(handle.pid)


async def _renew_watchdog_lease(
    address: str,
    deadline_s: int,
    handle: WatchdogHandle,
) -> bool:
    """Atomically extend an armed watchdog using only the unit's monotonic uptime."""

    lease_ttl_s = max(1, int(deadline_s))
    command = (
        f"state=\"$(cat '{WATCHDOG_ACTIVE_PATH}' 2>/dev/null)\"; "
        f'[ "$state" = "{handle.token} {handle.pid}" ] || exit 0; '
        f'pid="{handle.pid}"; '
        f"[ -f '{FLAG_PATH}' ] && kill -0 \"$pid\" 2>/dev/null && "
        f"[ \"$(cat '{handle.ready_path}' 2>/dev/null)\" = armed ] || exit 0; "
        "cmdline=\"$(tr '\\000' ' ' < \"/proc/$pid/cmdline\" 2>/dev/null)\"; "
        f'case "$cmdline" in *"{handle.script_path}"*) ;; *) exit 0;; esac; '
        'now="$(cut -d. -f1 /proc/uptime 2>/dev/null)"; '
        'case "$now" in ""|*[!0-9]*) exit 1;; esac; '
        f'expiry="$((now + {lease_ttl_s}))"; umask 077; '
        f"rm -f '{handle.lease_partial_path}'; "
        f"printf '%s\\n' \"$expiry\" > '{handle.lease_partial_path}' && "
        f"chmod 600 '{handle.lease_partial_path}' && "
        f"mv -f '{handle.lease_partial_path}' '{handle.lease_path}' || "
        f"{{ rm -f '{handle.lease_partial_path}'; exit 1; }}; "
        f"[ -f '{FLAG_PATH}' ] && kill -0 \"$pid\" 2>/dev/null && "
        f"[ \"$(cat '{WATCHDOG_ACTIVE_PATH}' 2>/dev/null)\" = "
        f'"{handle.token} {handle.pid}" ] && '
        f"[ \"$(cat '{handle.ready_path}' 2>/dev/null)\" = armed ] || "
        f"{{ rm -f '{handle.lease_path}' '{handle.lease_partial_path}'; exit 0; }}; "
        "printf renewed; exit 0"
    )
    try:
        reply = await adb.shell(address, command, timeout=RADIO_TIMEOUT_S)
    except adb.AdbError:
        return False
    return reply.strip() == "renewed"


def _watchdog_renew_interval(deadline_s: int) -> float:
    """Renew with two-thirds of the last proven lease still available."""

    return max(1.0, min(WATCHDOG_MAX_RENEW_INTERVAL_S, max(1, int(deadline_s)) / 3))


async def _watchdog_renewal_loop(
    address: str,
    deadline_s: int,
    watchdog: WatchdogHandle,
    lost: asyncio.Event,
) -> None:
    """Keep the remote lease alive only while this owner and its watchdog are alive."""

    interval = _watchdog_renew_interval(deadline_s)
    while True:
        await asyncio.sleep(interval)
        try:
            renewed = await _renew_watchdog_lease(address, deadline_s, watchdog)
        except Exception:
            renewed = False
        if renewed:
            continue
        # One immediate retry distinguishes a brief control-channel stumble from a lost
        # detached process without spending a meaningful part of the finite recovery lease.
        await asyncio.sleep(WATCHDOG_RENEW_RETRY_S)
        try:
            renewed = await _renew_watchdog_lease(address, deadline_s, watchdog)
        except Exception:
            renewed = False
        if renewed:
            continue
        lost.set()
        log.error(
            "the detached on-unit radio watchdog could not be proven; "
            "the transfer must stop while the last lease can still restore the radios",
            watchdog_pid=watchdog.pid,
        )
        return


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
        self._watchdog: WatchdogHandle | None = None
        self._watchdog_renewal_task: asyncio.Task[None] | None = None
        self._watchdog_lost = asyncio.Event()
        self._bluetooth_baseline = "unknown"
        self._hotspot_baseline = "unknown"
        self._hotspot_rearm_deadline: float | None = None
        self._transport_interface: str | None = None
        self._hotspot_capsule_path: str | None = None
        self._hotspot_restore_mode: str | None = None
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
        ssid, passphrase = config
        if not _SAFE_AP_TEXT.fullmatch(ssid) or not _SAFE_AP_TEXT.fullmatch(passphrase):
            return None
        body = json.dumps(
            {"schema_version": 1, "ssid": ssid, "passphrase": passphrase},
            ensure_ascii=True,
            separators=(",", ":"),
        )
        target = await self._persist_hotspot_capsule_body(transition_id, body)
        if target is not None:
            self._hotspot_restore_mode = HOTSPOT_RESTORE_EXACT
        return target

    async def persist_bluetooth_rearm_capsule(self, transition_id: str) -> str | None:
        """Freeze the opt-in vendor recovery method without storing AP credentials."""

        body = json.dumps(_BLUETOOTH_REARM_CAPSULE, separators=(",", ":"))
        target = await self._persist_hotspot_capsule_body(transition_id, body)
        if target is not None:
            self._hotspot_restore_mode = HOTSPOT_RESTORE_BLUETOOTH_REARM
        return target

    async def _persist_hotspot_capsule_body(
        self,
        transition_id: str,
        body: str,
    ) -> str | None:
        if not _SAFE_TRANSITION_ID.fullmatch(transition_id):
            return None
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

    async def read_hotspot_recovery_plan(self, path: str) -> HotspotRecoveryPlan | None:
        """Read one strict, versioned recovery capsule without exposing its contents."""

        raw = await self._read_hotspot_capsule(path)
        if raw is None:
            return None
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(value, dict):
            return None
        if set(value) == {"schema_version", "restore_mode"}:
            if value == _BLUETOOTH_REARM_CAPSULE:
                return HotspotRecoveryPlan(HOTSPOT_RESTORE_BLUETOOTH_REARM)
            return None
        if set(value) != {"schema_version", "ssid", "passphrase"}:
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
        return HotspotRecoveryPlan(HOTSPOT_RESTORE_EXACT, (ssid, passphrase))

    async def read_hotspot_capsule(self, path: str) -> tuple[str, str] | None:
        """Compatibility wrapper for callers that understand credential capsules."""

        plan = await self.read_hotspot_recovery_plan(path)
        return plan.config if plan is not None and plan.mode == HOTSPOT_RESTORE_EXACT else None

    async def _read_hotspot_capsule(self, path: str) -> str | None:
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
        return raw

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
            self._hotspot_restore_mode = None
        return removed

    @staticmethod
    def _valid_capsule_path(path: str) -> bool:
        return _valid_hotspot_capsule_path(path)

    async def _ensure_watchdog_locked(self, *, marker: str) -> bool:
        if self._watchdog is not None:
            if self._watchdog_lost.is_set() or not await _watchdog_is_armed(
                self.address, self._watchdog
            ):
                log.error("the detached radio watchdog is no longer alive")
                self._watchdog_lost.set()
                return False
            renewal = self._watchdog_renewal_task
            if renewal is None or renewal.done():
                if not await _renew_watchdog_lease(
                    self.address, self.watchdog_deadline_s, self._watchdog
                ):
                    log.warning(
                        "could not re-establish radio watchdog lease renewal; "
                        "leaving the remaining radios on"
                    )
                    return False
                self._start_watchdog_renewal()
            return True
        await self._stop_watchdog_renewal()
        self._watchdog_lost.clear()
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
            hotspot_restore_mode=self._hotspot_restore_mode,
        )
        if self._watchdog is None:
            # Candidate cleanup is generation-scoped. A newer server process may have
            # published its watchdog while this launch was in flight, so neither the
            # shared flag nor the conservative recovery marker may be cleared here.
            log.warning(
                "could not prove the radio watchdog was armed; "
                "leaving radios on and recovery state intact"
            )
            return False
        self._start_watchdog_renewal()
        return True

    def _start_watchdog_renewal(self) -> None:
        watchdog = self._watchdog
        renewal = self._watchdog_renewal_task
        if watchdog is None or self._watchdog_lost.is_set():
            return
        if renewal is not None and not renewal.done():
            return
        self._watchdog_renewal_task = asyncio.create_task(
            _watchdog_renewal_loop(
                self.address,
                self.watchdog_deadline_s,
                watchdog,
                self._watchdog_lost,
            ),
            name="ingest-radio-watchdog-renewal",
        )

    async def _stop_watchdog_renewal(self) -> None:
        renewal = self._watchdog_renewal_task
        self._watchdog_renewal_task = None
        if renewal is not None and not renewal.done():
            renewal.cancel()
        if renewal is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await renewal

    async def watchdog_healthy(self) -> bool:
        """Positively verify the detached recovery process while ingest is active."""

        async with _lock:
            watchdog = self._watchdog
            if watchdog is None:
                return True
            if self._watchdog_lost.is_set():
                return False
            if await _watchdog_is_armed(self.address, watchdog):
                return True
            # A second bounded read prevents one lost ADB reply from cancelling a healthy
            # bulk transfer, while still detecting a dead PID far inside its finite lease.
            await asyncio.sleep(WATCHDOG_RENEW_RETRY_S)
            healthy = await _watchdog_is_armed(self.address, watchdog)
            if not healthy:
                self._watchdog_lost.set()
            return healthy

    async def disable_bluetooth(
        self,
        *,
        before_change: Callable[[], Awaitable[None]] | None = None,
    ) -> bool:
        """Disable and positively verify Bluetooth, with all legacy recovery guards."""
        async with _lock:
            # The remote process must acknowledge that it is alive before Bluetooth is
            # touched. Merely creating a local ``adb`` child is not proof that its shell
            # reached the unit; an immediate transport failure would otherwise leave no
            # independent restoration path after the disable.
            if not await self._ensure_watchdog_locked(marker="bluetooth"):
                return False
            # This hook is intentionally inside the radio lock and after the remote
            # watchdog handshake. The OBD coordinator uses it to re-read the exact live
            # quiesce lease after every potentially-blocking database checkpoint, leaving
            # no unbounded work between that proof and the first radio side effect.
            if before_change is not None:
                await before_change()
            accepted = await _set_bluetooth(self.address, enable=False)
            verified = accepted and await _confirm_bluetooth_off(self.address)
            if verified:
                log.info("turned the unit's Bluetooth off for the transfer")
                return True
            log.warning("could not confirm the unit's Bluetooth is off; aborting radio quiet")
            return False

    async def disable_hotspot(
        self,
        *,
        before_change: Callable[[], Awaitable[None]] | None = None,
    ) -> bool:
        """Stop a separate serving AP and verify its interface disappeared."""
        async with _lock:
            if self._hotspot_capsule_path is None:
                log.warning("hotspot recovery capsule is unavailable; leaving the hotspot on")
                return False
            if not await self._ensure_watchdog_locked(marker="hotspot"):
                return False
            if before_change is not None:
                await before_change()
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

    async def _wait_for_hotspot_rearm(
        self,
        *,
        expected_interface: str | None = None,
    ) -> str | None:
        """Observe the AP through settle, plus a stable tail for heuristic recovery."""

        stable_since: float | None = None
        while True:
            serving = await _serving_ap(self.address)
            if serving is None:
                return None
            observed_at = time.monotonic()
            if expected_interface is not None:
                if serving == expected_interface:
                    if stable_since is None:
                        stable_since = observed_at
                else:
                    stable_since = None
            deadline = self._hotspot_rearm_deadline
            if deadline is None:
                if expected_interface is None or stable_since is None:
                    return serving
                deadline = observed_at
            settle_complete = observed_at >= deadline
            stability_complete = expected_interface is None or (
                stable_since is not None and observed_at - stable_since >= HOTSPOT_REARM_STABILITY_S
            )
            if settle_complete and stability_complete:
                self._hotspot_rearm_deadline = None
                return serving
            if settle_complete and expected_interface is not None and stable_since is None:
                self._hotspot_rearm_deadline = None
                return serving
            verify_until = deadline
            if stable_since is not None:
                verify_until = max(verify_until, stable_since + HOTSPOT_REARM_STABILITY_S)
            await asyncio.sleep(min(HOTSPOT_REARM_POLL_S, max(0.0, verify_until - observed_at)))

    async def restore_hotspot(
        self,
        baseline: str,
        config: tuple[str, str] | None,
        restore_mode: str | None = None,
        *,
        expected_interface: str | None = None,
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

            if restore_mode == HOTSPOT_RESTORE_BLUETOOTH_REARM:
                # Schema-v2 named this after the passive Bluetooth edge that first exposed
                # the vendor coupling. Field evidence showed that edge is not reliable
                # after a real transfer. The exact package-gated controller now starts the
                # saved tethering profile explicitly; no SSID or passphrase crosses ADB.
                if not expected_interface:
                    return False
                serving = await _serving_ap(self.address)
                if serving is None:
                    return False
                if serving != expected_interface:
                    if not await supports_zlink_bluetooth_rearm(self.address):
                        return False
                    if not await _request_saved_hotspot_start(self.address):
                        return False
                    self._hotspot_rearm_deadline = time.monotonic() + HOTSPOT_REARM_SETTLE_S
                serving = await self._wait_for_hotspot_rearm(expected_interface=expected_interface)
                return serving == expected_interface

            serving = await self._wait_for_hotspot_rearm(expected_interface=None)
            if serving is None:
                # A lost ADB reply and a proven-empty interface inventory are different
                # safety facts.  In particular, Bluetooth restoration may have re-armed
                # this vendor's AP: an unreadable inventory cannot certify the original
                # OFF baseline and must leave durable recovery armed for the next arrival.
                return False
            if restore_mode not in {None, HOTSPOT_RESTORE_EXACT}:
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

    async def stand_down_watchdog(self) -> bool:
        """Disarm only after every changed radio has regained its exact baseline."""
        async with _lock:
            # Cancel first so a renewal already in flight cannot recreate a lease after
            # the flag and readiness gate are removed below.
            await self._stop_watchdog_renewal()
            watchdog = self._watchdog
            cleanup = (
                _watchdog_owner_cleanup_command(watchdog, prove=True)
                if watchdog is not None
                else _watchdog_cleanup_command(prove=True)
            )
            try:
                reply = await adb.shell(self.address, cleanup, timeout=RADIO_TIMEOUT_S)
            except adb.AdbError:
                return False
            if reply.strip() != "cleared":
                return False
            self._watchdog = None
            self._watchdog_lost.clear()
            await _persist_marker("")
            return True

    async def release(self) -> None:
        global _active
        # If baseline restoration did not stand the watchdog down, ceasing renewal is the
        # hand-off to its last proven on-device expiry. Do not kill the remote process.
        await self._stop_watchdog_renewal()
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
    _watchdog: WatchdogHandle | None = None
    _watchdog_renewal_task: asyncio.Task[None] | None = None
    _watchdog_lost: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
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
            self._start_watchdog_renewal()
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
            await self._stop_watchdog_renewal()
            if self._owns:
                _active -= 1
                self._owns = False

    def _start_watchdog_renewal(self) -> None:
        watchdog = self._watchdog
        if watchdog is None or self._watchdog_lost.is_set():
            return
        renewal = self._watchdog_renewal_task
        if renewal is not None and not renewal.done():
            return
        self._watchdog_renewal_task = asyncio.create_task(
            _watchdog_renewal_loop(
                self.address,
                self.watchdog_deadline_s,
                watchdog,
                self._watchdog_lost,
            ),
            name="ingest-radio-watchdog-renewal",
        )

    async def _stop_watchdog_renewal(self) -> None:
        renewal = self._watchdog_renewal_task
        self._watchdog_renewal_task = None
        if renewal is not None and not renewal.done():
            renewal.cancel()
        if renewal is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await renewal

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
            watchdog = self._watchdog
            cleanup_ok = True
            if watchdog is not None:
                try:
                    reply = await adb.shell(
                        self.address,
                        _watchdog_owner_cleanup_command(watchdog, prove=True),
                        timeout=RADIO_TIMEOUT_S,
                    )
                    cleanup_ok = reply.strip() == "cleared"
                except adb.AdbError:
                    cleanup_ok = False
            else:
                await _remove_flag(self.address)
            if not cleanup_ok:
                log.warning("a newer radio watchdog replaced this restore generation")
                return
            log.info("turned the unit's Bluetooth back on")
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
        if restored:
            self._watchdog = None
            self._watchdog_lost.clear()

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
