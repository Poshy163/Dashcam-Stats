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

**What is actually possible, measured rather than assumed (2026-09-03).**

An earlier version of this note offered ``cmd wifi connect-network … -b <bssid>`` as the
safe way to pin a band, needing only the passphrase. That was wrong for this hardware:
``connect-network`` **is not in this build's verb list at all**. Nor is ``disconnect``, nor
any roam verb; ``add-suggestion`` exists but its own help says shell suggestions need an
approval that requires root, and this is a ``user`` build with ``ro.debuggable=0``, so
``adb root`` can never succeed. The ROM has no ``wifi_frequency_band`` setting either, and
the unit's own Wi-Fi picker groups by SSID, so there is not even a band to tap in the UI.

And the deeper reason none of that would have helped: ``dumpsys wifi`` logs **"No partial
scan because firmware roaming is supported"** every twenty seconds. The *firmware* owns
BSSID selection on this chip. Both radios are one saved network, so Android's network
selection has nothing to switch to — the band is a BSSID roam, and the firmware only roams
when the current link degrades. Observed sitting on 2.4 GHz at -57 dBm while the same access
point's 5 GHz radio was twenty decibels stronger at -37 dBm. The selection nudge below is
real and does run every cycle; it simply cannot overrule that.

So the levers that remain are both outside the unit:

* **The access point.** Disassociating the client makes it re-associate from scratch and
  pick the strongest radio. That is now implemented here — see
  :func:`_maybe_kick_to_fast_band` and :mod:`app.ingest.unifi` — bounded to one bounce per
  visit behind a cooldown, and never able to hold up a copy.
* **Band steering, or a 5 GHz-only SSID, on the router.** Steering on the existing SSID
  needs no new network and fixes it permanently at association time; a dedicated 5 GHz SSID
  is the most certain of all. Either removes the need for the bounce entirely.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time

from app.core.logging import get_logger
from app.core.settings_service import get_settings_service
from app.ingest import adb, unifi

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

#: The one non-privileged lever that actually moves an associated unit toward 5 GHz, short
#: of storing the network passphrase.
#:
#: ``set-network-selection-config <screen-off> <screen-on> -a <override>`` is on AOSP's
#: ``NON_PRIVILEGED_COMMANDS`` allowlist (probed live on the unit, Android 15, uid 2000,
#: rc=0). The two flags turn OFF the "sufficiency check" -- the "the network I'm on is good
#: enough, stop looking for a better one" test that is *exactly* why the unit locks to
#: 2.4 GHz and never re-evaluates -- for both the screen-off and screen-on cases; ``-a 1``
#: (``WifiNetworkSelectionConfig.ASSOCIATED_NETWORK_SELECTION_OVERRIDE_ENABLED``) forces
#: network selection to keep running while associated. Together they let the
#: ThroughputScorer, which favours 5 GHz through its throughput term, move the link -- and
#: keep it there -- without ever touching ``WIFI_ON`` (see the long note above on why the
#: STA-bounce is forbidden on a battery-less unit).
#:
#: It is runtime state, not persisted: the unit reboots on every ignition and comes back
#: with the default sufficiency check on, so this is re-applied every visit rather than set
#: once. Applying it is idempotent and, verified live, does not disturb an already-good
#: link.
_SELECTION_NUDGE = "cmd wifi set-network-selection-config disabled disabled -a 1"


#: ``MAC: 40:45:da:9b:3b:fe`` as ``cmd wifi status`` prints it. This is the address the
#: access point knows the unit by, and it is read live rather than configured because
#: Android may hand out a per-network randomised MAC -- a stored one would go stale the
#: first time that rotated, and the bounce would then target nothing.
_MAC = re.compile(r"MAC:\s*([0-9a-fA-F:]{17})")

#: How long the unit is given to come back on the fast radio after the access point drops
#: it. A re-association is a scan, an association and a DHCP lease; on this unit that has
#: been comfortably under fifteen seconds, and waiting longer would eat the driveway window
#: this is supposed to protect.
KICK_SETTLE_S = 25.0

#: Gap between checks while waiting for it to come back.
KICK_POLL_S = 2.5

#: The least time between two bounces. Under ``require_5ghz`` the poller re-runs this gate
#: every thirty seconds for as long as the car is on the driveway, and a unit that keeps
#: choosing 2.4 GHz -- because that is genuinely the better radio where it is parked -- must
#: not be disconnected over and over for the whole visit.
KICK_COOLDOWN_S = 300.0

#: When the last bounce was asked for, so the cooldown survives across gate calls.
_last_kick_at: float | None = None


def reset_kick_cooldown_for_tests() -> None:
    global _last_kick_at
    _last_kick_at = None


def _policy() -> str:
    try:
        return str(get_settings_service().get_nowait("ingest.wifi_band") or "any")
    except Exception:
        return "any"


def _kick_enabled() -> bool:
    try:
        return bool(get_settings_service().get_nowait("ingest.unifi_enabled"))
    except Exception:
        return False


def parse_mac(status_reply: str) -> str:
    """The unit's own MAC, as the access point sees it, or "" when it will not say."""
    found = _MAC.search(status_reply)
    return found.group(1).lower() if found else ""


async def read_client_mac(address: str) -> str:
    """Ask the unit for its Wi-Fi MAC. "" rather than an exception when it cannot answer."""
    try:
        reply = await adb.shell(address, "cmd wifi status", timeout=BAND_TIMEOUT_S)
    except adb.AdbError as exc:
        log.debug("could not read the unit's WiFi MAC", error=str(exc))
        return ""
    return parse_mac(reply)


async def _maybe_kick_to_fast_band(address: str, frequency: int) -> int:
    """Ask the access point to bounce the unit, and report the band it came back on.

    Returns the frequency to act on -- unchanged when the bounce is switched off, not
    configured, still inside its cooldown, refused, or simply did not move it. The unit
    cannot be made to change band from its own shell (see :mod:`app.ingest.unifi` for the
    measurements), so this is the only lever there is; it is still only ever a courtesy,
    and every failure path here falls through to copying on the slow band.
    """
    global _last_kick_at

    if not _kick_enabled():
        return frequency
    now = time.monotonic()
    if _last_kick_at is not None and now - _last_kick_at < KICK_COOLDOWN_S:
        log.debug("not bouncing the unit again yet", since_s=round(now - _last_kick_at, 1))
        return frequency

    mac = await read_client_mac(address)
    if not mac:
        return frequency

    # Stamped before the call, not after: a bounce that times out still disconnected the
    # unit, and retrying that every thirty seconds is the failure this cooldown prevents.
    _last_kick_at = now
    asked, detail = await unifi.kick_client(mac)
    if not asked:
        log.info("could not ask the access point to move the unit to 5GHz", reason=detail)
        return frequency
    log.info("asked the access point to reconnect the unit so it re-picks a radio", mac=mac)

    deadline = time.monotonic() + KICK_SETTLE_S
    current = frequency
    while time.monotonic() < deadline:
        await asyncio.sleep(KICK_POLL_S)
        seen, _ssid = await read_link(address)
        if seen is None:
            # Mid-reassociation the unit has no link to report; that is the expected middle
            # of this operation, not a failure.
            continue
        current = seen
        if is_fast(seen):
            return seen
    return current


def _nudge_enabled() -> bool:
    try:
        return bool(get_settings_service().get_nowait("ingest.wifi_selection_nudge"))
    except Exception:
        return False


async def apply_selection_nudge(address: str) -> bool:
    """Ask the unit to stop treating a working 2.4 GHz link as reason not to look for 5 GHz.

    Returns whether the command was issued cleanly. Never raises: this is a best-effort
    nudge alongside the transfer, and an unreachable unit or a build that words the verb
    differently must not take a backup down with it -- the band gate still does its own job
    either way.
    """
    try:
        await adb.shell(address, _SELECTION_NUDGE, timeout=BAND_TIMEOUT_S)
        return True
    except adb.AdbError as exc:
        log.debug("could not apply the 5GHz network-selection nudge", error=str(exc))
        return False


async def _maybe_nudge_selection(address: str) -> None:
    """Apply the selection nudge if it is switched on. Called only when policy wants 5 GHz."""
    if _nudge_enabled():
        await apply_selection_nudge(address)


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

    # Nudge before reading the band, and on every run, not only when the unit is on 2.4.
    # Applied on 2.4 it starts a move to 5 GHz; applied on 5 GHz it keeps the unit
    # re-evaluating so it does not slide back. The move itself takes a scan cycle or two,
    # so this run may still read 2.4 -- under `require` the thirty-second re-check catches
    # the result, and under `prefer` the following window does.
    await _maybe_nudge_selection(address)

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

    # On the slow radio, and the unit itself cannot be made to leave it. If the operator has
    # given this app their access point, ask it to drop the unit so it re-associates and
    # picks the strongest radio -- which at every measured parking spot is the 5 GHz one.
    frequency = await _maybe_kick_to_fast_band(address, frequency)
    if is_fast(frequency):
        _publish(frequency, held=False, reason=None)
        log.info(
            "the access point moved the unit onto 5GHz; transferring at full speed",
            frequency_mhz=frequency,
        )
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


#: How often to refresh the live link frequency while the unit remains online.
LINK_REFRESH_INTERVAL_S = 30.0

_last_link_refresh_at: float = 0.0
_link_refresh_task: asyncio.Task[None] | None = None


async def refresh_link_if_due(address: str) -> int | None:
    """Refresh the unit's live link frequency if due or unknown."""
    global _last_link_refresh_at
    now = time.monotonic()
    from app.ingest.status import get_status

    status = get_status()
    if (
        status.wifi_frequency_mhz is not None
        and now - _last_link_refresh_at < LINK_REFRESH_INTERVAL_S
    ):
        return status.wifi_frequency_mhz

    _last_link_refresh_at = now
    freq, _ssid = await read_link(address)
    if freq is not None:
        _publish(freq, held=status.wifi_band_hold, reason=status.wifi_band_hold_reason)
    return freq


def on_unit_present(address: str) -> None:
    """Schedule a non-blocking refresh of the Wi-Fi link frequency."""
    global _link_refresh_task
    if _link_refresh_task is not None and not _link_refresh_task.done():
        return
    _link_refresh_task = asyncio.create_task(
        refresh_link_if_due(address),
        name="ingest-band-refresh",
    )


async def shutdown() -> None:
    """Cancel any pending link-refresh task."""
    global _link_refresh_task
    if _link_refresh_task is not None and not _link_refresh_task.done():
        _link_refresh_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _link_refresh_task
    _link_refresh_task = None
