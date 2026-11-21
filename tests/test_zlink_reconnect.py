"""Replay startup ordering from the working and failed Q30 trips with synthetic peers."""

import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "android/obd-logger/app/src/main/assets/zlink_reconnect.sh"
)
EDGE = int(datetime(2026, 11, 21, 18, 22, 28, tzinfo=UTC).timestamp())
BT = "02:00:00:00:00:11"
WIFI = "02:00:00:00:00:22"
PACKAGE = "com.zjinnova.zlink"
MAIN = f"{PACKAGE}/com.zjinnova.android.zlink.features.main.MainActivity"
UUID = "00000000-deca-fade-deca-deafdecacafe"


def radio_log(*, early_close=True):
    # The successful start also logged this IOException, after the phone had a lease.
    accept, close = (3.754, 4.403) if early_close else (10.808, 20.593)
    return (
        f"{EDGE + accept:.3f}  2537  2537 E zj      : [zjBtService]device:{BT}   uuid:{UUID}\n"
        f"{EDGE + close:.3f}  2537 15479 E zj      : java.io.IOException: bt socket closed, read return: -1\n"
        f"{EDGE + close:.3f}  2537 15479 E zj      : \tat android.bluetooth.BluetoothSocket.read(BluetoothSocket.java:677)\n"
        f"{EDGE + close:.3f}  2537 15479 E zj      : \tat com.zjinnova.android.zlink.features.wireless."
        "BluetoothService$Wwwwwwwwwwwwwwwwwwwwwwwwwwwwwwww.run(BluetoothService.kt:74)\n"
    )


def lease(offset=12, mac=WIFI):
    stamp = datetime.fromtimestamp(EDGE + offset, UTC).isoformat(timespec="seconds")
    return (
        f"{stamp}.501326 - [wlan2.DHCP.Repository] DHCPREQUEST assignedLease null, "
        f"reqAddr=192.0.2.9, sidSet=false: created/renewed lease clientId: 01, hwAddr: {mac}, "
        "netAddr: 192.0.2.9/24, expTime: 208000692, hostname: iPhone\n"
    )


IDLE_RESOURCES = """ResourceManagerService: 0x1
  Processes:
    Pid: 1234
      Client:
        Name: c2.unisoc.avc.encoder
    Pid: 2537
    Pid: 6789
  Events logs (most recent at top):
"""
IDLE_METRICS = """Dump of the media.metrics process:
Metrics gathering: enabled
Since Boot: Submissions: 400 Accepted: 300
    0: {codec, (11-21 18:18:54.714), (com.zjinnova.zlink, 0, 10077), (android.media.mediacodec.codec=c2.unisoc.avc.decoder, android.media.mediacodec.encoder=0, android.media.mediacodec.latency.n=11022, android.media.mediacodec.mime=video/avc)}
TransactionLog: gc(3)
"""


@pytest.fixture
def check(tmp_path):
    shell = shutil.which("sh")
    if not shell and os.name == "nt":
        shell = str(Path(os.environ["PROGRAMFILES"]) / "Git/bin/sh.exe")
    if not shell or not Path(shell).is_file():
        pytest.skip("POSIX shell unavailable")

    def run(*, armed=True, backup=False, **overrides):
        guard = tmp_path / "guard"
        guard.mkdir(exist_ok=True)
        if armed:
            (guard / "zlink_reconnect_enabled").touch()
        if backup:
            (tmp_path / "backup").touch()
        responses = {
            "acc": "1",
            "bluetooth": "1",
            "status": "0",
            "pid": "2537",
            "wifi_mac": WIFI,
            "activity": f"    topResumedActivity=ActivityRecord{{42 u0 {MAIN} t435 d0}}\n",
            "log": radio_log(),
            "network": lease(),
            "metrics": IDLE_METRICS,
            "resources": IDLE_RESOURCES,
            "now": str(EDGE + 35),
            "helper": "reconnect_requested",
        }
        responses.update(overrides)
        for name, value in responses.items():
            (tmp_path / name).write_text(value + "\n", encoding="utf-8", newline="\n")

        # Run the asset as stdin just as the guard does. All device responses and
        # writable paths are private to this single case.
        script = (
            SCRIPT.read_text(encoding="utf-8")
            .replace("/data/local/tmp/dashcam_wifi_startup", "./guard")
            .replace("/data/local/tmp/.dashcam_analyser_radios", "./backup")
        )
        mocks = r"""
date() { if [ "$1" = '+%s' ]; then cat now; else command date "$@"; fi; }
timeout() { shift; "$@"; }
settings() {
    case "$*" in
        'get global acc_status') cat acc;;
        'get global bluetooth_on') cat bluetooth;;
        'get system persist.sys.carplay.connect.status')
            if [ -f checked_status ] && [ -f later_status ]; then
                cat later_status
            else
                touch checked_status
                cat status
            fi;;
        *) return 1;;
    esac
}
pidof() { [ "$*" = 'com.zjinnova.zlink' ] && cat pid; }
getprop() { [ "$1" = 'persist.zlink.carplay.mac' ] && cat wifi_mac; }
logcat() { cat log && [ ! -f log_failed ]; }
dumpsys() {
    case "$*" in
        *'activity activities') cat activity;;
        *network_stack) cat network;;
        *media.metrics) cat metrics;;
        *media.resource_manager) cat resources;;
        *) return 1;;
    esac
}
app_process() { printf '%s\n' "$*" >> requests; cat helper; }
"""
        result = subprocess.run(
            [shell, "-s", "--", str(EDGE)],
            input=mocks + script,
            cwd=tmp_path,
            env={**os.environ, "TZ": "UTC", "LC_ALL": "C"},
            text=True,
            capture_output=True,
            timeout=8,
            check=True,
        )
        calls = tmp_path / "requests"
        return result.stdout.strip(), calls.read_text().splitlines() if calls.exists() else []

    return run


def test_failed_return_requests_one_retry_for_the_classic_peer(check):
    status, calls = check()
    assert status == "reconnect_requested"
    assert calls == [f"/system/bin com.dashcamstats.obdlogger.CarPlayPhoneBluetooth retry {BT}"]


@pytest.mark.parametrize(
    ("other", "reason"),
    [
        pytest.param(
            {"log": radio_log(early_close=False)}, "lease", id="working-return-lease-before-eof"
        ),
        pytest.param(
            {"log": radio_log() + f"{EDGE + 5}.012 2537 2537 E zj : updateStates:CP\n"},
            "handshake",
            id="successful-handoff-after-eof",
        ),
        pytest.param({"network": lease(-15)}, "lease", id="lease-from-previous-wake"),
        pytest.param(
            {"network": lease() + "DUMP TIMEOUT (3s)\n"}, "lease", id="partial-dhcp-history"
        ),
        pytest.param(
            {"network": lease(2) + lease(12)}, "lease", id="later-renewal-of-earlier-lease"
        ),
        pytest.param({"network": lease(offset=20)}, "lease", id="iphone-still-within-grace-period"),
        pytest.param(
            {"network": lease(mac="02:00:00:00:00:33")}, "lease", id="unrelated-hotspot-client"
        ),
        pytest.param(
            {"log": radio_log().replace(UUID, "00001101-0000-1000-8000-00805f9b34fb")},
            "handshake",
            id="unrelated-service",
        ),
        pytest.param({"log": ""}, "handshake", id="missing-socket-evidence"),
        pytest.param({"log_failed": "1"}, "handshake", id="interrupted-logcat-after-positive-rows"),
        pytest.param(
            {
                "log": f"{EDGE + 2}.012 2537 2537 E zj : [zjBtService]device:02:00:00:00:00:44   uuid:{UUID}\n"
                + radio_log()
            },
            "handshake",
            id="two-classic-peers",
        ),
        pytest.param(
            {"metrics": IDLE_METRICS.replace("18:18:54", "18:22:53")},
            "decoder_history",
            id="decoder-finished-this-wake",
        ),
        pytest.param(
            {"metrics": IDLE_METRICS.split("TransactionLog:")[0]},
            "decoder_history",
            id="incomplete-codec-history",
        ),
        pytest.param(
            {
                "resources": IDLE_RESOURCES.replace(
                    "Pid: 2537", "Pid: 2537\n      Client:\n        Name: c2.unisoc.avc.decoder"
                )
            },
            "decoder",
            id="decoder-active",
        ),
        pytest.param(
            {"resources": IDLE_RESOURCES.replace("Pid: 2537", "Pid: 1111")},
            "decoder",
            id="unknown-codec-owner",
        ),
        pytest.param({"status": "1"}, "projection", id="projection-already-active"),
        pytest.param({"status": "null"}, "projection", id="unknown-projection"),
        pytest.param({"later_status": "1"}, "projection", id="projection-started-during-checks"),
        pytest.param({"acc": "0"}, "ignition", id="ignition-off"),
        pytest.param({"bluetooth": "0"}, "bluetooth", id="bluetooth-off"),
        pytest.param(
            {
                "activity": "topResumedActivity=ActivityRecord{42 u0 com.android.settings/.Settings t1}"
            },
            "foreground",
            id="another-screen",
        ),
        pytest.param({"now": str(EDGE + 56)}, "stale", id="stale-wake"),
        pytest.param({"armed": False}, "disabled", id="opted-out"),
        pytest.param({"backup": True}, "backup", id="backup-active"),
    ],
)
def test_skips_without_touching_phone(check, other, reason):
    status, calls = check(**other)
    assert status == f"skip_{reason}"
    assert calls == []


def test_phone_helper_refusal_is_not_reported_as_a_reconnect(check):
    status, calls = check(helper="skip_unbonded")
    assert status == "skip_phone"
    assert len(calls) == 1
