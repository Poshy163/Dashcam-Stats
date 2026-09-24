"""Run the production transport AWK against realistic Android socket layouts."""

import ipaddress
import shutil
import subprocess
from pathlib import Path

import pytest

from app.ingest import carplay_timing

HEADER = "State Recv-Q Send-Q Local Address:Port Peer Address:Port\n"
PROC_HEADER = (
    "  sl  local_address rem_address st tx_queue rx_queue tr tm->when retrnsmt uid timeout inode\n"
)


def proc_endpoint(host, port):
    raw = ipaddress.ip_address(host).packed
    return (
        b"".join(raw[i : i + 4][::-1] for i in range(0, len(raw), 4)).hex().upper() + f":{port:04X}"
    )


def proc_row(local, remote, uid, inode):
    return f"0: {proc_endpoint(*local)} {proc_endpoint(*remote)} 01 00000000:00000000 00:00000000 00000000 {uid} 0 {inode}\n"


def fixture(wire_bytes=1000, loop_bytes=900, cookie="abcd", uid_marker="", proc=True):
    return {
        "p4": PROC_HEADER,
        "p6": PROC_HEADER
        + (proc_row(("fe80::1234", 5000), ("fe80::5678", 6000), 0, 8123) if proc else "")
        + proc_row(("::ffff:127.0.0.1", 7000), ("::ffff:127.0.0.1", 8000), 10123, 8124),
        "n4": "",
        "n6": "fe80::5678 lladdr aa:bb:cc:dd:ee:ff REACHABLE\n",
        "s4": HEADER,
        "s6": HEADER
        + f"ESTAB 256 32 [fe80::1234]%wlan2:5000 [fe80::5678]%wlan2:6000 {uid_marker} ino:8123 sk:{cookie}\n"
        + f" ts cubic rtt:4.5/1.0 bytes_received:{wire_bytes} lastrcv:2\n"
        + "ESTAB 128 16 [::ffff:127.0.0.1]:7000 [::ffff:127.0.0.1]:8000 uid:10123 ino:8124 sk:abce\n"
        + f" ts cubic bytes_received:{loop_bytes} rtt:0.5/0.1 lastrcv:1\n",
    }


def run(parts, previous="", stamp=1000, uid="10123", statuses=None):
    awk = shutil.which("awk") or r"C:\Program Files\Git\usr\bin\awk.exe"
    if not Path(awk).exists():
        pytest.skip("AWK unavailable")
    script = (
        Path(carplay_timing.__file__).with_name("carplay_timing.sh").read_text(encoding="utf-8")
    )
    program = script.split("transport_v6_summary() {", 1)[1].split("station_summary() {", 1)[0]
    program = program.split('-v stamp="$link_start" \'', 1)[1].rsplit("'", 1)[0]
    source = "".join(
        f"@{name} {(statuses or {}).get(name, 0)}\n{parts[name]}\n"
        for name in ("p4", "p6", "n4", "n6", "s4", "s6")
    )
    result = subprocess.run(
        [awk, "-v", f"uid={uid}", "-v", f"previous={previous}", "-v", f"stamp={stamp}", program],
        input=source,
        text=True,
        capture_output=True,
        check=True,
    ).stdout
    public, state = result.split("|", 1)
    return dict(token.split("=", 1) for token in public.split()), state, public


def test_ipv6_root_receiver_and_mapped_loopback_are_observed_without_ss_root_uid():
    values, _, public = run(fixture())
    assert values["wire_peer_tcp_sockets"] == "1"
    assert values["wire_peer_tcp6_sockets"] == "1"
    assert values["wire_peer_tcp4_sockets"] == "0"
    assert values["wire_peer_root_verified_sockets"] == "1"
    assert values["wire_peer_proc_verified_sockets"] == "1"
    assert values["wire_peer_rx_queue_bytes"] == "256"
    assert values["wire_peer_bytes_received_total"] == "1000"
    assert values["zlink_loopback_tcp_sockets"] == "1"
    assert values["zlink_loopback_bytes_received_total"] == "900"
    assert values["zlink_loopback_rx_queue_bytes"] == "128"
    assert values["wire_peer_bytes_received_delta"] == "na"
    for secret in ("fe80", "127.0.0.1", "aa:bb", "10123", "8123", "abcd", "wlan2"):
        assert secret not in public


def test_consecutive_membership_gives_deltas_but_turnover_failure_and_reset_do_not():
    _, first, _ = run(fixture())
    second, state, _ = run(fixture(wire_bytes=2000, loop_bytes=1800), first, stamp=4000)
    assert second["wire_peer_bytes_received_delta"] == "1000"
    assert second["zlink_loopback_bytes_received_delta"] == "900"
    assert second["wire_peer_delta_ms"] == "3000"
    assert second["wire_peer_topology_changed"] == "0"
    changed, _, _ = run(fixture(wire_bytes=3000, cookie="dcba"), state, stamp=7000)
    assert changed["wire_peer_topology_changed"] == "1"
    assert changed["wire_peer_bytes_received_delta"] == "na"
    reset, _, _ = run(fixture(wire_bytes=1), state, stamp=7000)
    assert reset["wire_peer_bytes_received_delta"] == "na"
    missing, interrupted, _ = run(fixture(), state, stamp=7000, statuses={"s6": 124})
    assert missing["wire_peer_tcp_sockets"] == "na"
    recovered, _, _ = run(fixture(wire_bytes=3000), interrupted, stamp=10000)
    assert recovered["wire_peer_bytes_received_delta"] == "na"


@pytest.mark.parametrize("change", ["proc_missing", "proc_tuple", "proc_inode", "proc_denied"])
def test_root_ownership_requires_matching_readable_proc_tuple_and_inode(change):
    parts = fixture(proc=change != "proc_missing")
    statuses = {}
    if change == "proc_tuple":
        parts["p6"] = parts["p6"].replace(":1770", ":1771")
    if change == "proc_inode":
        parts["p6"] = parts["p6"].replace("0 0 8123", "0 0 9999")
    if change == "proc_denied":
        statuses["p6"] = 1
    values, _, _ = run(parts, statuses=statuses)
    assert values["wire_peer_tcp_sockets"] == "1"
    assert values["wire_peer_root_verified_sockets"] == "0"
    assert values["wire_peer_uid_unknown_sockets"] == "1"


def test_empty_is_zero_but_failed_or_malformed_family_is_unavailable():
    parts = {
        k: PROC_HEADER if k.startswith("p") else HEADER if k.startswith("s") else ""
        for k in fixture()
    }
    values, _, _ = run(parts)
    assert values["wire_peer_tcp_sockets"] == "0"
    assert values["wire_peer_bytes_received_total"] == "0"
    values, _, _ = run(parts, statuses={"n6": 1})
    assert values["wire_peer_tcp_sockets"] == "na"
    assert values["zlink_loopback_tcp_sockets"] == "0"
    parts["s6"] += "ESTAB 0 0 INVALID BROKEN ino:123\n"
    values, _, _ = run(parts)
    assert values["wire_peer_tcp_sockets"] == "na"


def test_zone_inside_brackets_expanded_ipv6_and_multiline_details_match():
    parts = fixture()
    parts["n6"] = "fe80:0:0:0:0:0:0:5678 lladdr aa:bb:cc:dd:ee:ff STALE\n"
    parts["s6"] = parts["s6"].replace("[fe80::1234]%wlan2", "[fe80::1234%wlan2]")
    parts["s6"] = parts["s6"].replace(
        "rtt:4.5/1.0 bytes_received:", "rtt:4.5/1.0\n bytes_received:"
    )
    values, _, _ = run(parts)
    assert values["wire_peer_root_verified_sockets"] == "1"
    assert values["wire_peer_bytes_received_total"] == "1000"


def test_missing_bytes_is_unavailable_and_missing_app_uid_does_not_hide_wire_peer():
    parts = fixture()
    parts["s6"] = parts["s6"].replace("bytes_received:1000", "")
    values, _, _ = run(parts, uid="na")
    assert values["wire_peer_tcp_sockets"] == "1"
    assert values["wire_peer_bytes_received_total"] == "na"
    assert values["zlink_loopback_tcp_sockets"] == "na"


def test_ipv4_and_native_ipv6_loopback_plus_netid_layout():
    parts = fixture()
    parts["n4"] = "192.0.2.2 lladdr aa:bb:cc:dd:ee:ff REACHABLE\n"
    parts["p4"] += proc_row(("192.0.2.1", 111), ("192.0.2.2", 222), 1000, 91)
    parts["s4"] = (
        "Netid State Recv-Q Send-Q Local Address:Port Peer Address:Port\n"
        "tcp ESTAB 1 2 192.0.2.1:111 192.0.2.2:222 uid:1000 ino:91 sk:a1\n"
        " bytes_received:50\n"
        "tcp ESTAB 3 4 127.0.0.1:333 127.0.0.1:444 uid:10123 ino:92 sk:a2\n"
        " bytes_received:40\n"
    )
    parts["s6"] += "ESTAB 5 6 [::1]:555 [::1]:666 uid:10123 ino:93 sk:a3\n bytes_received:30\n"
    values, _, _ = run(parts)
    assert values["wire_peer_tcp_sockets"] == "2"
    assert values["wire_peer_tcp4_sockets"] == "1"
    assert values["wire_peer_proc_verified_sockets"] == "2"
    assert values["wire_peer_root_verified_sockets"] == "1"
    assert values["wire_peer_bytes_received_total"] == "1050"
    assert values["zlink_loopback_tcp_sockets"] == "3"
    assert values["zlink_loopback_bytes_received_total"] == "970"


def test_socket_reordering_preserves_continuity_but_missing_identity_does_not():
    parts = fixture()
    _, state, _ = run(parts)
    blocks = parts["s6"].removeprefix(HEADER).splitlines(keepends=True)
    parts["s6"] = HEADER + "".join(blocks[2:] + blocks[:2])
    values, _, _ = run(parts, state, stamp=4000)
    assert values["wire_peer_topology_changed"] == "0"
    assert values["wire_peer_bytes_received_delta"] == "0"
    parts["s6"] = parts["s6"].replace("ino:8123 sk:abcd", "")
    values, state, _ = run(parts, state, stamp=7000)
    assert values["wire_peer_bytes_received_delta"] == "na"
    values, _, _ = run(parts, state, stamp=10000)
    assert values["wire_peer_bytes_received_delta"] == "na"


def test_same_link_local_neighbor_on_explicit_other_interface_is_excluded():
    parts = fixture()
    parts["s6"] = parts["s6"].replace("%wlan2", "%wlan0")
    values, _, _ = run(parts)
    assert values["wire_peer_tcp_sockets"] == "0"


def test_explicit_ss_root_uid_is_still_unknown_without_proc_validation():
    values, _, _ = run(fixture(uid_marker="uid:0", proc=False))
    assert values["wire_peer_root_verified_sockets"] == "0"
    assert values["wire_peer_uid_unknown_sockets"] == "1"


def test_loopback_partial_counter_coverage_is_explicit_and_availability_resets_delta():
    parts = fixture()
    for n in range(5):
        parts["s6"] += (
            f"ESTAB 0 0 [::1]:{9000 + n} [::1]:{9100 + n} uid:10123 ino:{10000 + n} sk:{20000 + n:x}\n"
            " ts cubic rtt:0.1/0.1\n"
        )
    values, previous, _ = run(parts)
    assert values["zlink_loopback_tcp_sockets"] == "6"
    assert values["zlink_loopback_counter_sockets"] == "1"
    assert values["zlink_loopback_bytes_received_total"] == "900"
    parts["s6"] = parts["s6"].replace("bytes_received:900", "bytes_received:1800")
    values, previous, _ = run(parts, previous, stamp=4000)
    assert values["zlink_loopback_bytes_received_delta"] == "900"
    parts["s6"] = parts["s6"].replace(
        " ts cubic rtt:0.1/0.1", " ts cubic rtt:0.1/0.1 bytes_received:3", 1
    )
    values, previous, _ = run(parts, previous, stamp=7000)
    assert values["zlink_loopback_counter_sockets"] == "2"
    assert values["zlink_loopback_bytes_received_total"] == "1803"
    assert values["zlink_loopback_topology_changed"] == "1"
    assert values["zlink_loopback_bytes_received_delta"] == "na"
    parts["s6"] = parts["s6"].replace(" bytes_received:3", "", 1)
    values, previous, _ = run(parts, previous, stamp=10000)
    assert values["zlink_loopback_bytes_received_delta"] == "na"
    parts["s6"] = parts["s6"].replace("bytes_received:1800", "")
    values, _, _ = run(parts, previous, stamp=13000)
    assert values["zlink_loopback_counter_sockets"] == "0"
    assert values["zlink_loopback_bytes_received_total"] == "na"


def test_compact_wireless_message_retains_last_numeric_field():
    _, _, public = run(fixture(wire_bytes=999999999999999, loop_bytes=999999999999999))
    legacy = "peer_tcp_sockets=0 peer_rx_queue_bytes=0 peer_tx_queue_bytes=0 peer_tcp_rtt_max_ms=na peer_recent_rtt_max_ms=na peer_receive_age_min_ms=na peer_bytes_received_total=na"
    station = "ap_station_count=3 ap_signal_min_dbm=-65 ap_rx_bitrate_min_mbps=866.7 ap_tx_retries_total=999999999999 ap_tx_failed_total=999999999999"
    message = (
        "sample=1790000000-99999999999-99999-link-999999 session=1790000000-99999999999-99999 schema=6 acc=1 | "
        "event=wireless_link link_poll_gap_ms=999999999 link_probe_ms=999999999 link_context_age_ms=999999999 "
        + legacy
        + " "
        + station
        + " "
        + public
    )
    assert len(message) < 2048
    [record] = carplay_timing.parse_sampler_file("2026-09-24T12:00:00Z " + message)
    assert record.message.endswith("zlink_loopback_topology_changed=na")
    # Even giving every metric the full width of an unsigned 64-bit counter
    # leaves room for the stable sample identity and compact context prefix.
    metrics = " ".join(
        token.split("=", 1)[0] + "=" + "9" * 20
        for token in (legacy + " " + station + " " + public).split()
    )
    worst = message[: message.index("peer_tcp_sockets=")] + metrics
    assert len(worst) < 2048
    [record] = carplay_timing.parse_sampler_file("2026-09-24T12:00:00Z " + worst)
    assert record.message.endswith("zlink_loopback_topology_changed=" + "9" * 20)
    script = (
        Path(carplay_timing.__file__).with_name("carplay_timing.sh").read_text(encoding="utf-8")
    )
    assert 'link_head="session=$SESSION schema=6 ${link_acc:-acc=na}"' in script
    # Android mksh treats a bare pipe as pattern alternation in these patterns.
    assert r"transport_stats=${transport_result%%\|*}" in script
    assert r"transport_state=${transport_result#*\|}" in script
