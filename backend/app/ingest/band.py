"""Holding the transfer for a 5 GHz link, and saying so when it is not one.

The unit's WiFi is a single-stream chip, and the difference between its two bands is not
a percentage, it is the whole feature: on 5 GHz the link negotiates 433 Mbps and the
transfer runs at ~32 MB/s (docs/dashcam-backup-investigation.md §15); on 2.4 GHz the same
chip negotiates 72 Mbps and the same transfer crawls at ~5 MB/s. A one-to-two-minute
driveway window moves 4 GB on one band and barely 500 MB on the other. And the unit
*locks on*: once associated, Android does not roam while the signal is adequate, so a
unit that arrived on 2.4 GHz stays there for the whole window, every window.

So this module reads the band and, if asked, refuses to spend the window on the slow one.
``cmd wifi status`` is on AOSP's ``NON_PRIVILEGED_COMMANDS`` allowlist, so unlike
``stop-softap`` an unrooted shell may ask. A held run comes back as IDLE rather than an
error on purpose: IDLE is the one state the poller re-checks every thirty seconds while
the car is still on the driveway, it is not persisted as a failed run, and it does not
page anybody. The reason is carried in the status, not in ``last_error`` — a hold is not
a fault, and putting it there would leave the Backup page reporting "last attempt had a
problem" for the rest of the day after the car left.

**Why there is no "force it onto 5 GHz" here, though there nearly was.**

The only lever an unrooted shell has to make Android re-pick a band is to bounce the STA
radio — ``cmd wifi set-wifi-enabled disabled`` then ``enabled``. It was written, and then
it was taken out again, because it cannot be made safe on *this* device.

``set-wifi-enabled disabled`` writes ``Settings.Global.WIFI_ON = 0`` synchronously, and
that setting survives a reboot. So between the disable and the re-enable there is a window
in which the unit's WiFi is persistently off. The unit has **no battery**: the engine can
stop at any instant, and if it stops inside that window the unit powers down with WiFi
disabled and boots deaf — at that engine start and every one after it. Nothing in this app
can reach it again, because reaching it is what WiFi was for. Recovery means physically
getting into the car and re-enabling WiFi through the unit's own UI.

The obvious mitigation does not work either. Chaining the re-enable into the same remote
command looks like it should cover it, but the disable *cuts the link its own shell is
riding on* — and a remote command here does not outlive its adb session. This repo relies
on exactly that fact elsewhere: :func:`app.ingest.adb.stop_listener` kills the local adb
child precisely in order to end the remote ``tar | nc``, and its docstring says so — "the
adb session *is* the listener's lifetime". So the re-enable would be killed along with the
link the disable had just destroyed, which is the *permanent* version of the failure rather
than a narrow race. (The Bluetooth watchdog in :mod:`app.ingest.radios` is not a
counterexample: turning Bluetooth off does not disturb the WiFi link its own session runs
over, so that session survives to fire the restore.)

Detaching the re-enabler with ``setsid`` before issuing the disable would close that hole
but not the first one, and the first one is the fatal one: a two-second window, entered
deliberately, several times a day, on a device whose power can be cut at any moment by
somebody turning a key. Weighed against saving a few minutes of transfer, that is not a
trade worth making — and it is precisely the "never leave the car worse than it was found"
rule this subsystem is built around.

The two safe ways to actually get 5 GHz, neither of which belongs in this module:

* **Give the router a 5 GHz-only SSID and point the unit at it.** No 2.4 GHz band to lock
  onto, nothing for this code to do, no risk. This is the real fix and the one to reach for.
* ``cmd wifi connect-network <ssid> wpa2 <passphrase> -b <bssid>`` pins an exact BSSID and
  therefore a band, without ever setting ``WIFI_ON`` to 0. It is genuinely safe, and it is
  not here only because it needs the network's passphrase stored in this app — a real
  decision for the operator to make rather than one to take on their behalf.
"""

from __future__ import annotations

import asyncio
import re

from app.core.logging import get_logger
from app.core.settings_service import get_settings_service
from app.ingest import adb

log = get_logger(__name__)

#: Ceiling on the control calls here, same reasoning as the radio quieting's.
BAND_TIMEOUT_S = 6.0

#: How long ``list-scan-results`` is given to fill after a ``start-scan``. A single-radio
#: chip doing a full dual-band sweep takes a few seconds; longer than this and the window
#: is being spent on the question instead of the answer.
SCAN_SETTLE_S = 4.0

#: The weakest 5 GHz signal Android would even consider associating with.
#:
#: Android's own number rather than a guess: ``WifiNetworkSelector`` drops any candidate
#: below ``getEntryRssi(freq)`` before selection looks at it, and the 5 GHz entry threshold
#: (``ScoringParams`` rssi5 ENTRY) is -77 dBm. Used only to phrase the diagnostic honestly
#: — "your network is not reachable on 5 GHz from where the car parks" is a different
#: problem from "it is reachable and the unit chose 2.4 anyway", and they have different
#: fixes.
MIN_5G_RSSI_DBM = -75

#: 2.4 GHz channels live at 2412-2484 MHz; anything at 4900 MHz or above is the 5 GHz
#: band or better (5955+ is 6 GHz, which this chip cannot do, but a unit that could
#: would deserve a pass, not a hold).
_FAST_FLOOR_MHZ = 4900

#: ``Frequency: 5220MHz`` as WifiInfo renders itself inside ``cmd wifi status``. The
#: optional space absorbs the one rendering difference seen across releases.
_FREQUENCY = re.compile(r"Frequency:\s*(\d{4,5})\s*MHz", re.IGNORECASE)

#: The connected SSID, from the ``Wifi is connected to "…"`` line ``status`` prints,
#: falling back to the WifiInfo rendering for a build that words it differently.
_STATUS_SSID = re.compile(r'connected to\s+"([^"\r\n]+)"')
_INFO_SSID = re.compile(r'SSID:\s*"?([^",\r\n]+)"?\s*,')

#: One scan row: BSSID, frequency, RSSI lead the line in every release's formatting.
_SCAN_ROW = re.compile(r"^\s*([0-9a-fA-F:]{17})\s+(\d{4,5})\s+(-?\d+)")


def _policy() -> str:
    try:
        return str(get_settings_service().get_nowait("ingest.wifi_band") or "any")
    except Exception:
        return "any"


def parse_link(status_reply: str) -> tuple[int | None, str]:
    """(frequency MHz, ssid) as read from ``cmd wifi status`` output.

    Both degrade to "unknown" separately: the frequency is what the gate needs, the SSID
    only what the scan diagnostic matches against, and a build that words one line oddly
    must not take the other down with it.
    """
    freq_match = _FREQUENCY.search(status_reply)
    frequency = int(freq_match.group(1)) if freq_match else None
    ssid_match = _STATUS_SSID.search(status_reply) or _INFO_SSID.search(status_reply)
    ssid = ssid_match.group(1).strip() if ssid_match else ""
    return frequency, ssid


def is_fast(frequency_mhz: int) -> bool:
    return frequency_mhz >= _FAST_FLOOR_MHZ


async def read_link(address: str) -> tuple[int | None, str]:
    """The live link's (frequency MHz, ssid), or (None, "") when the unit will not say."""
    try:
        reply = await adb.shell(address, "cmd wifi status", timeout=BAND_TIMEOUT_S)
    except adb.AdbError as exc:
        log.debug("could not read the unit's WiFi status", error=str(exc))
        return None, ""
    return parse_link(reply)


def parse_scan_for_5g(scan_reply: str, ssid: str) -> bool | None:
    """Whether *ssid* is visible on 5 GHz at a usable signal. None when nothing parsed.

    The SSID is matched by substring rather than by column, because the scan table puts
    the SSID between whitespace-padded columns and an SSID may itself contain spaces —
    slicing columns is exactly the kind of parsing that breaks on the first network name
    with two words in it. A substring can false-positive against a *different* network
    whose name contains this one; this only ever chooses which sentence to log, so the
    cost of being wrong is a slightly misleading hint.
    """
    parsed_any = False
    for line in scan_reply.splitlines():
        row = _SCAN_ROW.match(line)
        if not row:
            continue
        parsed_any = True
        if not ssid or ssid not in line:
            continue
        frequency, rssi = int(row.group(2)), int(row.group(3))
        if is_fast(frequency) and rssi >= MIN_5G_RSSI_DBM:
            return True
    return False if parsed_any else None


async def _5g_visible(address: str, ssid: str) -> bool | None:
    """Ask the unit to scan and look for its own network on 5 GHz. None means unknown."""
    try:
        await adb.shell(address, "cmd wifi start-scan", timeout=BAND_TIMEOUT_S)
        await asyncio.sleep(SCAN_SETTLE_S)
        reply = await adb.shell(address, "cmd wifi list-scan-results", timeout=BAND_TIMEOUT_S)
    except adb.AdbError as exc:
        log.debug("could not scan for the 5GHz band", error=str(exc))
        return None
    return parse_scan_for_5g(reply, ssid)


async def gate(address: str) -> bool:
    """Whether the transfer may start. Called once per run, before a byte moves.

    Every path that cannot positively say "the band is wrong" lets the transfer proceed —
    a backup that quietly stopped happening is a worse failure than one that ran slowly.
    """
    policy = _policy()
    if policy not in ("prefer_5ghz", "require_5ghz"):
        # Clear rather than skip. A hold published by an earlier run under a stricter
        # policy would otherwise stand for as long as the unit is online, and the Backup
        # page suppresses its real error banner while a hold is showing -- so a stale one
        # hides genuine failures behind a reassuring explanation.
        _publish(None, held=False, reason=None)
        return True

    frequency, ssid = await read_link(address)
    if frequency is None:
        _publish(None, held=False, reason=None)
        log.warning(
            "could not read the unit's WiFi band; transferring anyway rather than "
            "holding a backup on an unreadable answer"
        )
        return True
    if is_fast(frequency):
        _publish(frequency, held=False, reason=None)
        return True

    if policy == "prefer_5ghz":
        _publish(frequency, held=False, reason=None)
        log.warning(
            "transferring on 2.4GHz — expect roughly a fifth of the usual speed",
            frequency_mhz=frequency,
        )
        return True

    # Holding. Worth one scan to tell the two very different causes apart, because they
    # have different fixes and the operator can act on neither without being told which
    # one they have.
    visible = await _5g_visible(address, ssid)
    if visible is True:
        detail = (
            "your network is on 5GHz and in range, but the unit associated on 2.4GHz and "
            "Android will not move a link that still works. A 5GHz-only SSID for the car "
            "fixes this for good"
        )
    elif visible is False:
        detail = (
            "your network is not reachable on 5GHz from where the car parks, so the unit "
            "has nothing better to join. Moving the access point, or adding one nearer "
            "the driveway, is the only thing that helps"
        )
    else:
        detail = "the unit would not say whether 5GHz is in range"

    reason = (
        f"waiting for a 5GHz link — the unit is on 2.4GHz ({frequency} MHz), which moves "
        f"about 5 MB/s against 32. {detail}. Re-checked every half minute while the car "
        "is here"
    )
    _publish(frequency, held=True, reason=reason)
    log.info(
        "holding the transfer for the 5GHz band",
        frequency_mhz=frequency,
        five_ghz_in_range=visible,
    )
    return False


def _publish(frequency: int | None, *, held: bool, reason: str | None) -> None:
    """Put the band where the Backup page can see it. Import-local to avoid a cycle."""
    from app.ingest.status import get_status

    get_status().set_wifi(frequency, held=held, reason=reason)
