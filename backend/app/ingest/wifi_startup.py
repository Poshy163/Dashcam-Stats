"""Re-arm an explicitly installed, on-unit ignition Wi-Fi guard after a reboot.

Opt-in lives on the unit as a private marker. Discovering another unit never enables
this feature. The companion APK provides the shell entry point; its normal app UID
cannot change autojoin. An existing warm-sleep watcher is left running untouched.
"""

from __future__ import annotations

import asyncio
import time

from app.core.logging import get_logger
from app.ingest import adb
from app.ingest.status import get_status

log = get_logger(__name__)
ROOT = "/data/local/tmp/dashcam_wifi_startup"
CLASS = "com.dashcamstats.obdlogger.WifiStartupGuard"
ARM_INTERVAL_S = 60.0
_last: dict[str, float] = {}
_tasks: set[asyncio.Task[bool]] = set()


def arm_command() -> str:
    # Paths and package are fixed. No SSIDs, keys, network IDs or remote values become
    # shell syntax. A valid PID is read-only evidence, never a target for kill.
    return f"""root={ROOT}
[ -f "$root/enabled" ] || {{ echo not_enabled; exit 0; }}
[ "$(settings get global acc_status)" = 0 ] || {{ echo ignition_hold; exit 0; }}
[ ! -f "$root/lease" ] || {{ echo recovery_pending; exit 0; }}
pid=$(cat "$root/watch.pid" 2>/dev/null)
case "$pid" in
  ''|*[!0-9]*) ;;
  *) if [ -r "/proc/$pid/cmdline" ] && tr '\\000' ' ' < "/proc/$pid/cmdline" | grep -Fq '{CLASS}'; then
       echo already_running; exit 0
     fi ;;
esac
apk=$(pm path com.dashcamstats.obdlogger | sed -n 's/^package://p' | head -1)
case "$apk" in
  /data/app/*/base.apk) ;;
  *) echo companion_unavailable; exit 0 ;;
esac
cp "$apk" "$root/guard.apk.new" && chmod 600 "$root/guard.apk.new" && mv "$root/guard.apk.new" "$root/guard.apk" || exit 1
setsid env CLASSPATH="$root/guard.apk" app_process /system/bin {CLASS} watch </dev/null >/dev/null 2>&1 &
echo launched
"""


async def arm(address: str) -> bool:
    try:
        result = await adb.shell(address, arm_command(), timeout=10.0)
    except adb.AdbError:
        log.warning("could not check the on-unit startup Wi-Fi guard")
        return False
    # Launch is only a request. The guard's bounded events log is runtime evidence.
    return result.strip() in {"launched", "already_running"}


def on_unit_present(address: str) -> None:
    if get_status().ignition_state != "off":
        return
    now = time.monotonic()
    if now - _last.get(address, -ARM_INTERVAL_S) < ARM_INTERVAL_S:
        return
    _last[address] = now
    task = asyncio.create_task(arm(address), name="ingest-wifi-startup")
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def shutdown() -> None:
    for task in list(_tasks):
        task.cancel()
    await asyncio.gather(*_tasks, return_exceptions=True)
    _tasks.clear()
    _last.clear()
