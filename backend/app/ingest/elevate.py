"""Asking the unit's adbd to restart as root, on a unit that will allow it.

Root buys exactly one thing here, and it buys it decisively: the hotspot. Android gates
``stop-softap`` on ``uid == Process.ROOT_UID`` — see :mod:`app.ingest.radios` for the
whole story — so on an ordinary unit the hotspot beacons through every transfer and
there is nothing the app can do about it. On a unit whose adbd *can* run as root, the
same command simply works, and the feature that is currently half a feature becomes a
whole one.

**Whether it is possible is a property of the build, not of anything this app does.**
``adbd``'s ``should_drop_privileges`` grants root only when ``__android_log_is_debuggable()``
is true — ``ro.debuggable=1``, i.e. a ``userdebug`` or ``eng`` build. A production build
answers ``adbd cannot run as root in production builds`` and there is no way round it
short of reflashing. Plenty of aftermarket head units ship debuggable; plenty do not.
The only way to find out is to ask, so this asks once and remembers.

Four rules shape this, and three of them are about not making things worse.

**Never under a running transfer.** ``adb root`` restarts the daemon, and the listener
serving the bulk socket lives *inside* an adb session — the session is its lifetime, by
the same design that lets the Bluetooth watchdog outlive a dropped connection. Restarting
adbd while bytes are moving would cut the very thing this exists to speed up. So the
elevation happens once per run, after the delta has proved there is footage worth the
trouble and before a single listener is launched, and nowhere else. The presence poll
must never call it: that reconnects every couple of seconds, and a daemon restart on each
tick would leave the unit permanently unreachable.

**Ask once, not once per window.** A refusal is a fact about the build, so it is cached
and never asked again in this process. That cache is deliberately *in memory* rather than
persisted: a unit that is reflashed or replaced re-answers the question at the next app
start, which is the self-healing direction. A *success* is never cached, because it is
not a property of the build — the unit reboots with the car and comes back as uid 2000
every single window, so every run has to ask again. When it is already root the ask is
one fast round trip that restarts nothing, which is what makes asking every run cheap.

**A failed elevation must not cost the window.** Everything here is best-effort: the
worst honest outcome is a transfer that runs with the hotspot still up, which is exactly
what happens today. The one genuinely dangerous case is a daemon that restarts and does
not come back on TCP, which would lose the unit for the rest of the window — so the
reconnect is retried against a budget rather than a single attempt, and a unit that never
returns is reported loudly. It self-heals at the next engine start, because the unit
reboots and adbd comes back exactly as it was configured.

**Root is not put back.** There is no ``adb unroot`` at the end of a run, for two reasons
that point the same way: the unit reboots when the engine stops and reverts on its own,
and un-rooting would mean a *second* daemon restart — another chance to lose the link,
paid to undo something the ignition is about to undo for free. That does mean adbd stays
root on the LAN until the car is switched off, which is a real change to the unit's
posture and is why the setting is flagged as one to think about.
"""

from __future__ import annotations

import asyncio
import time

from app.core.logging import get_logger
from app.core.settings_service import get_settings_service
from app.ingest import adb

log = get_logger(__name__)

#: Where the learned answer is shown, so a feature that silently cannot work says why.
STATE_KEY = "ingest.adb_root_state"

#: How long the daemon is given to come back after a restart, and how often to look.
#:
#: A restart is a second or two in practice. The budget is what stops a unit that is
#: never coming back from eating the window: past this, the run carries on without root,
#: which is the behaviour it would have had anyway.
RECONNECT_BUDGET_S = 8.0
RECONNECT_POLL_S = 0.5

#: Ceiling on each individual call inside the retry loop.
#:
#: Load-bearing, not tidiness. Both ``reconnect`` and ``id -u`` default to the twenty-second
#: control timeout, and the budget above is only consulted between iterations — so a single
#: attempt against a daemon that is down could block for forty seconds and the loop would
#: get exactly one try, having spent half the driveway window to do it. Each call has to
#: fail faster than the budget for the budget to mean anything.
ATTEMPT_TIMEOUT_S = 2.0

#: Addresses whose build has already said no. See the module docstring for why this is
#: memory rather than a setting.
_impossible: set[str] = set()

#: Set when a restart was issued and the daemon did not come back inside the budget.
#:
#: The distinction the caller needs. "This unit will not give root" is the ordinary answer
#: and costs nothing — the run carries on and the hotspot stays up. "The daemon went away
#: and has not returned" means every subsequent call would be issued at a unit that cannot
#: hear it, and a run that carries on regardless fires a start webhook for a transfer that
#: can never move a byte and then sits in a listener timeout until the window is gone.
#: Both are a False from :func:`ensure_root`, so the difference is recorded here rather
#: than lost in the return value.
_channel_lost = False


def channel_lost() -> bool:
    """Whether the last elevation left the control channel unusable."""
    return _channel_lost


#: Addresses not to elevate again until this monotonic time, and for how long.
#:
#: The loop-breaker, and it is what makes returning the window to the poller safe. A run
#: that loses the channel comes back as IDLE so the poller re-drains as soon as the unit
#: answers again — but if the *reason* was simply that this unit's adbd takes longer to
#: re-bind than the budget allows, an unguarded retry would restart it again, wait, give
#: up again, and spend the whole window restarting a daemon instead of copying footage.
#: So a lost channel buys a quiet period: the next run skips the elevation entirely and
#: transfers exactly as it would with the feature switched off, which is the degradation
#: this module promises. Longer than a driveway window on purpose — the ignition resets
#: everything anyway, so "not again this visit" is the intent.
_cooldown: dict[str, float] = {}
COOLDOWN_S = 600.0

#: What adbd says, and what each answer means.
_ALREADY = "already running as root"

#: The build will not allow it, ever. Deliberately narrow: only the two phrasings that
#: actually mean "this is a production build". Broader words like "permission denied"
#: were tried here and removed — they can come from a vendor policy or an SELinux denial
#: on a unit that *is* debuggable, and matching them would cache a permanent "no" and
#: tell the operator their firmware is the problem when it is not.
_REFUSALS = ("cannot run as root", "production build")

#: The unit is simply not there. ``adb root`` does not raise for these: ``_adb`` ignores
#: the exit status, so a departed car comes back as the *string* ``error: device ... not
#: found``. Without this it matches neither a refusal nor "already root", falls into the
#: "a restart must be in progress" branch, and spends the entire reconnect budget waiting
#: for a daemon that was never restarted — several seconds of a window, to discover the
#: car left.
#:
#: Matched on fragments rather than whole sentences because the client interpolates the
#: address into the middle of the interesting one: the real text is ``error: device
#: '192.168.1.122:5555' not found``, so a literal ``device not found`` matches nothing.
_ABSENT = (
    "not found",
    "device offline",
    "no devices",
    "device still connecting",
    "cannot connect",
    "connection refused",
    "error: closed",
)


def _wanted() -> bool:
    """Whether the operator asked for this *and* for the thing it exists to enable.

    Both are checked rather than trusting the UI's own gating: ``requires`` disables the
    input, it does not retract a value already stored, so a unit could otherwise go on
    restarting its daemon every run to enable a hotspot stop nobody is asking for.
    """
    try:
        settings = get_settings_service()
        return bool(settings.get_nowait("ingest.adb_root")) and bool(
            settings.get_nowait("ingest.quiet_radios")
        )
    except Exception:
        return False


#: The last state written, so an unchanged answer is not re-written every run.
#:
#: Worth a variable because runs are not rare: the poller starts another one as soon as
#: each finishes for as long as the car is on the driveway, and on a rooted unit every one
#: of them reaches the same "available" conclusion. Writing that afresh each time is a
#: database round trip per run to store a string that already says exactly that.
_written: str | None = None


async def _remember(state: str) -> None:
    global _written
    if state == _written:
        return
    try:
        await get_settings_service().set(STATE_KEY, state, internal=True)
        _written = state
    except Exception as exc:
        log.debug("could not record the adb root state", error=str(exc))


async def _probe_uid(address: str, *, timeout: float = ATTEMPT_TIMEOUT_S) -> str | None:
    """The uid the channel reports, or None when it did not answer at all.

    The distinction between ``"2000"`` and ``None`` is what tells "the daemon is back but
    did not elevate" from "the daemon is not there", and only the second is a reason to
    stop the run. It cannot be taken from ``adb.reconnect``: ``_adb`` raises only on a
    *timeout*, so a plain ``cannot connect`` comes back as an ordinary result and a
    reconnect against a dead unit looks exactly like a successful one.
    """
    try:
        reply = (await adb.shell(address, "id -u", timeout=timeout)).strip()
    except adb.AdbError:
        return None
    return reply.splitlines()[-1].strip() if reply else ""


async def _is_root(address: str, *, timeout: float = ATTEMPT_TIMEOUT_S) -> bool:
    """Whether the control channel is currently uid 0. False when it cannot be asked."""
    return await _probe_uid(address, timeout=timeout) == "0"


async def _wait_for_daemon(address: str) -> bool:
    """Get the control channel back after a restart. True once it answers as root.

    Polled rather than slept at, because the difference between a fixed wait and this is
    footage: the daemon is usually back inside a second, and the window is ninety.
    """
    global _channel_lost
    answered = False
    deadline = time.monotonic() + RECONNECT_BUDGET_S
    while time.monotonic() < deadline:
        await asyncio.sleep(RECONNECT_POLL_S)
        try:
            # Disconnect and connect, not connect alone. The server on this side is still
            # holding a socket to a daemon that no longer exists, and it will happily
            # report that stale entry as `device`.
            await adb.reconnect(address, timeout=ATTEMPT_TIMEOUT_S)
        except adb.AdbError:
            continue
        uid = await _probe_uid(address)
        if uid is not None:
            # It spoke. Whatever it said, the channel is usable, so the run must carry on
            # rather than be abandoned -- a unit that came back as 2000 is exactly the
            # ordinary "no root here" outcome.
            answered = True
            if uid == "0":
                return True
    _channel_lost = not answered
    return False


async def ensure_root(address: str) -> bool:
    """Try to get the control channel to uid 0. True only when it is positively root.

    Never raises. Every failure path returns False and leaves the run to proceed exactly
    as it would have without this feature at all.
    """
    global _channel_lost
    _channel_lost = False
    if not _wanted() or address in _impossible:
        return False
    if time.monotonic() < _cooldown.get(address, 0.0):
        # A previous run in this visit restarted the daemon and lost it. Let the transfer
        # happen unelevated rather than spend the window restarting adbd again.
        log.debug("skipping the root elevation; the last one lost the control channel")
        return False

    try:
        reply = await adb.root(address)
    except adb.AdbError as exc:
        # A timeout here is the interesting one: the daemon may be mid-restart. Fall
        # through to the wait rather than concluding anything, because concluding
        # "no root" while it is actually coming back would leave the run talking to a
        # channel it thinks is dead.
        log.debug("the root request did not answer cleanly", error=str(exc))
        reply = ""

    lowered = reply.lower()
    if any(gone in lowered for gone in _ABSENT):
        # The car left between the card listing and here. Nothing was restarted, so there
        # is nothing to wait for and nothing to conclude about the build.
        log.debug("the unit was gone when its ADB was asked to elevate", reply=reply[:120])
        return False

    if any(refusal in lowered for refusal in _REFUSALS):
        # A fact about the build. Asked once, never again in this process.
        _impossible.add(address)
        await _remember(
            "not available: this unit runs a production build, so its ADB cannot become "
            "root and its hotspot cannot be stopped from here"
        )
        log.info(
            "the head unit will not give ADB root, so the hotspot cannot be stopped; "
            "not asking again",
            reply=reply[:200] or "no answer",
        )
        return False

    if _ALREADY in lowered:
        # The ordinary answer on the second and later runs of a window. Nothing
        # restarted, nothing to wait for.
        await _remember("available: ADB is running as root on this unit")
        return True

    # Anything else is a restart in progress -- including the empty reply from a call
    # that timed out while the daemon was going down.
    if await _wait_for_daemon(address):
        await _remember("available: ADB is running as root on this unit")
        log.info("the head unit's ADB is now root, so its hotspot can be stopped")
        return True

    # The daemon did not elevate inside the budget. `_wait_for_daemon` has already
    # recorded whether that is because it never answered at all (the channel is gone and
    # the caller must not transfer into it) or because it answered as uid 2000 (ordinary,
    # carry on without root). Never cached either way: the unit reboots at the next engine
    # start and comes back exactly as configured.
    if not _channel_lost:
        log.info("the head unit's ADB came back but not as root; carrying on without it")
        return False
    _cooldown[address] = time.monotonic() + COOLDOWN_S
    log.warning(
        "the head unit's ADB did not come back after being asked to restart as root, so "
        "this window has lost its control channel. It returns by itself at the next "
        "engine start; switch 'Let the app restart the dashcam's ADB as root' off if it "
        "keeps happening",
        address=address,
    )
    await _remember(
        "unknown: the last attempt restarted the unit's ADB and it did not come back "
        "before the run gave up"
    )
    return False


def reset_for_tests() -> None:
    global _written, _channel_lost
    _impossible.clear()
    _cooldown.clear()
    _written = None
    _channel_lost = False
