import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from runpy import run_path

import pytest

proven_peer = run_path(str(Path(__file__).resolve().parents[1] / "tools/enroll_carplay_phone.py"))[
    "proven_peer"
]

NOW = datetime.fromisoformat("2026-11-27T11:30:00+10:30")
START = datetime.fromisoformat("2026-11-27T10:53:12.829+10:30").timestamp()
WIFI = "02:00:00:01:00:01"
CLASSIC = "02:00:00:02:00:02"
OTHER = "02:00:00:03:00:03"
METRICS = """Metrics gathering: enabled
293: {codec, (11-27 11:01:37.858), (com.zjinnova.zlink, 0, 10077), (android.media.mediacodec.encoder=0, android.media.mediacodec.mime=video/avc, android.media.mediacodec.lifetimeMs=505029)}
TransactionLog: gc(3)
"""


def lease(at, mac):
    return f"2026-11-27T{at} - [wlan2.DHCP.Repository] DHCPREQUEST assignedLease null, created/renewed lease clientId: 01, hwAddr: {mac}, netAddr: 192.0.2.3/24"


NETWORK = lease("10:52:34.298448", WIFI) + ",hostname: iPhone\n" + lease("10:52:37.765833", OTHER)


def line(offset, msg):
    return f"{START + offset:.3f}  2537  2537 E zj      : {msg}\n"


def iap(mac):
    return f"[zjBtService]device:{mac}   uuid:00000000-deca-fade-deca-deafdecacafe"


LOG = (
    line(-10, "updateStates:CP")
    + line(-4.339, iap(CLASSIC))
    + line(-1.070, "bt socket closed, read return: -1")
    + line(-0.850, "updateStates:CP")
)


def test_enrolls_distinct_wifi_and_classic_addresses_from_working_projection():
    assert proven_peer(NOW, "2537", WIFI, METRICS, NETWORK, LOG) == CLASSIC
    with pytest.raises(ValueError, match="Multiple Wi-Fi clients"):
        proven_peer(NOW, "2537", OTHER, METRICS, NETWORK, LOG)


@pytest.mark.parametrize(
    ("metrics", "network", "log"),
    [
        (METRICS + "DUMP TIMEOUT", NETWORK, LOG),
        (METRICS, lease("10:52:34", OTHER), LOG),
        (METRICS, lease("10:42:34.123", WIFI), LOG),
        (METRICS, NETWORK, LOG + line(4, iap(OTHER))),
        (METRICS, NETWORK, LOG.replace("2537", "2538")),
        (METRICS, NETWORK, line(-1, "updateStates:CP") + line(-0.5, iap(CLASSIC))),
        (METRICS.replace("11-27", "11-25"), NETWORK, LOG),
    ],
)
def test_enrollment_rejects_unproved_pair(metrics, network, log):
    with pytest.raises(ValueError):
        proven_peer(NOW, "2537", WIFI, metrics, network, log)


def test_parked_enrollment_and_private_lookup(tmp_path):
    script = (
        Path(__file__).resolve().parents[1] / "android/obd-logger/app/src/main/assets/zlink_peer.sh"
    )
    shell = shutil.which("sh") or (
        str(Path(os.environ["PROGRAMFILES"]) / "Git/bin/sh.exe") if os.name == "nt" else None
    )
    if not shell or not Path(shell).exists():
        pytest.skip("POSIX shell unavailable")
    guard = tmp_path / "guard"
    guard.mkdir()
    (guard / "zlink_reconnect_enabled").touch()
    body = script.read_text(encoding="utf-8").replace(
        "/data/local/tmp/dashcam_wifi_startup", "./guard"
    )

    def run(*args, acc=0, wifi=WIFI):
        mocks = f"getprop() {{ echo '{wifi}'; }}\nsettings() {{ echo '{acc}'; }}\npidof() {{ echo 2537; }}\n"
        return subprocess.run(
            [shell, "-s", "--", *args],
            input=mocks + body,
            capture_output=True,
            text=True,
            cwd=tmp_path,
            check=False,
        )

    assert run("enroll", WIFI, CLASSIC, "2537", acc=1).returncode != 0
    assert run("enroll", WIFI, CLASSIC, "2537").stdout.strip() == "enrolled"
    assert run("lookup", WIFI.lower()).stdout.strip() == CLASSIC
    assert run("lookup", OTHER, wifi=OTHER).returncode != 0
    assert run("enroll", WIFI, OTHER, "9999").returncode != 0
    assert run("lookup", WIFI).stdout.strip() == CLASSIC
