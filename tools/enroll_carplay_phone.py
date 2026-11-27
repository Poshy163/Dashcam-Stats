"""Bind one previously working CarPlay phone for the parked Q30's startup watcher."""

import argparse
import re
import subprocess
from datetime import datetime, timedelta

PACKAGE = "com.zjinnova.zlink"
GUARD = "/data/local/tmp/dashcam_wifi_startup/guard.apk"
IAP_UUID = "00000000-deca-fade-deca-deafdecacafe"
MAC = r"(?:[A-Fa-f0-9]{2}:){5}[A-Fa-f0-9]{2}"


def address(value: str) -> str:
    if not re.fullmatch(MAC, value) or value.upper() in {"00:00:00:00:00:00", "FF:FF:FF:FF:FF:FF"}:
        raise ValueError("Missing or invalid peer address")
    return value.upper()


def proven_peer(now: datetime, pid: str, saved: str, metrics: str, network: str, log: str) -> str:
    """Correlate the latest completed video session with its classic and Wi-Fi peers."""
    saved = address(saved)
    if not pid.isascii() or not pid.isdecimal() or int(pid) < 1:
        raise ValueError("ZLink process is unavailable")
    if (
        "DUMP TIMEOUT" in metrics + network
        or "TransactionLog:" not in metrics
        or "Metrics gathering: enabled" not in metrics
    ):
        raise ValueError("Android returned incomplete decoder or network history")
    decoders = []
    for line in metrics.splitlines():
        event = re.search(
            r"\{codec, \((\d\d-\d\d \d\d:\d\d:\d\d\.\d{3})\), \(com\.zjinnova\.zlink,", line
        )
        if (
            not event
            or "android.media.mediacodec.encoder=0," not in line
            or "android.media.mediacodec.mime=video/" not in line
        ):
            continue
        duration = re.search(r"android\.media\.mediacodec\.lifetimeMs=(\d+)[,)]", line)
        if not duration:
            raise ValueError("Incomplete ZLink decoder lifetime")
        end = datetime.strptime(f"{now.year}-{event[1]}", "%Y-%m-%d %H:%M:%S.%f").replace(
            tzinfo=now.tzinfo
        )
        if end > now:
            end = end.replace(year=now.year - 1)
        decoders.append((end, end - timedelta(milliseconds=int(duration[1]))))
    if not decoders:
        raise ValueError("No completed ZLink video session")
    end, start = max(decoders)
    if now - end > timedelta(days=1) or not timedelta(seconds=2) <= end - start <= timedelta(
        days=1
    ):
        raise ValueError("No recent sustained ZLink video session")
    # The Q30's iAP socket hands off to the decoder within seconds. A different
    # handset appearing since that handoff invalidates enrollment for this snapshot.
    t0 = start.timestamp()
    classic = None
    closed = False
    progressed = False
    for line in log.splitlines():
        row = re.match(rf"\s*(\d+\.\d+)\s+{pid}\s+\d+\s+E\s+zj\s*:\s*(.*)", line)
        if not row or float(row[1]) < t0 - 12:
            continue
        at, msg = float(row[1]), row[2]
        if "[zjBtService]device:" in msg:
            announcement = re.search(
                rf"\[zjBtService\]device:\s*({MAC})\s+uuid:{IAP_UUID}(?:\s|$)", msg, re.I
            )
            if not announcement or (classic and address(announcement[1]) != classic):
                raise ValueError("Ambiguous ZLink phone identity")
            if at <= t0 + 1:
                classic, closed, progressed = address(announcement[1]), False, False
        elif classic and at <= t0 + 3:
            if "bt socket closed, read return: -1" in msg:
                closed = True
            elif closed and "updateStates:CP" in msg:
                progressed = True
    if not classic or not progressed:
        raise ValueError("No matching successful Bluetooth handoff for the latest decoder")
    peers = set()
    iphones = set()
    for line in network.splitlines():
        if "[wlan2.DHCP.Repository] DHCPREQUEST assignedLease " not in line:
            continue
        stamp = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d+)", line.strip())
        lease = re.search(rf"created/renewed lease .*?hwAddr:\s*({MAC}),", line, re.I)
        if stamp and lease:
            leased = datetime.fromisoformat(stamp[1]).replace(tzinfo=now.tzinfo)
            if start - timedelta(seconds=90) <= leased <= start + timedelta(seconds=1):
                peer = address(lease[1])
                peers.add(peer)
                if re.search(r"hostname:\s*[^,]*iphone", line, re.I):
                    iphones.add(peer)
    if saved not in peers:
        raise ValueError("Saved Wi-Fi peer has no lease at the successful Bluetooth handoff")
    if len(peers) > 1 and iphones != {saved}:
        raise ValueError("Multiple Wi-Fi clients and no unique corroborating iPhone lease")
    return classic


def enroll(serial: str, check_only: bool) -> None:
    def shell(command: str) -> str:
        result = subprocess.run(
            ["adb", "-s", serial, "shell", command],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=18,
            check=False,
        )
        if result.returncode:
            raise ValueError("Dashcam command unavailable or refused")
        return result.stdout.strip()

    if shell("settings get global acc_status") != "0":
        raise ValueError("Park the dashcam first")
    pid = shell(f"pidof {PACKAGE}")
    if not re.fullmatch(r"[1-9]\d*", pid, re.ASCII):
        raise ValueError("ZLink process is unavailable")
    saved = address(shell("getprop persist.zlink.carplay.mac"))
    now = datetime.strptime(shell("date '+%Y-%m-%dT%H:%M:%S%z'"), "%Y-%m-%dT%H:%M:%S%z")
    classic = proven_peer(
        now,
        pid,
        saved,
        shell("dumpsys -t 4 media.metrics"),
        shell("dumpsys -t 4 network_stack"),
        shell(f"logcat -d -b main -v epoch --pid={pid} -s zj:E"),
    )
    if check_only:
        print("Confirmed the saved CarPlay phone from the latest completed session.")
        return
    # Only validated fixed-format MAC addresses and a decimal PID enter this command.
    asset = f"unzip -p {GUARD} assets/zlink_peer.sh | /system/bin/sh -s --"
    if (
        shell(f"{asset} enroll {saved} {classic} {pid}") != "enrolled"
        or shell(f"{asset} lookup {saved}") != classic
    ):
        raise ValueError("Dashcam did not confirm enrollment")
    print("Enrolled and verified the saved CarPlay phone.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", required=True, help="The dashcam's connected ADB serial")
    parser.add_argument(
        "--check-only", action="store_true", help="Verify the existing evidence without enrolling"
    )
    options = parser.parse_args()
    try:
        enroll(options.serial, options.check_only)
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        parser.exit(
            1, f"{exc}\n" if isinstance(exc, ValueError) else "ADB is unavailable or timed out.\n"
        )
