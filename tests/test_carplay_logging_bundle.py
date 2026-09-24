"""The two on-unit programs travel together and survive retained-log/API parsing."""

import base64
import re
from datetime import UTC, datetime

from app.ingest import carplay_timing


def test_deployment_stages_validates_and_atomically_replaces_both_programs():
    command = carplay_timing.deploy_command()
    uploads = re.findall(r"echo ([A-Za-z0-9+/=]+) \| base64 -d > (\S+)", command)
    assert len(uploads) == 2
    expected = [carplay_timing.script(), carplay_timing.codec_script()]
    destinations = [carplay_timing.REMOTE_SCRIPT, carplay_timing.REMOTE_CODEC_SCRIPT]
    for (payload, path), source, destination in zip(uploads, expected, destinations):
        assert base64.b64decode(payload).decode() == source
        assert path.startswith(destination + ".") and path.endswith(".new")
        assert f"sh -n {path}" in command
        assert f"mv {path} {destination}" in command
    assert command.index("sh -n") < command.index("mv ")


def test_bundle_fingerprint_changes_when_either_program_changes(tmp_path, monkeypatch):
    main = tmp_path / "main.sh"
    codec = tmp_path / "codec.sh"
    main.write_text("BUILD_ID=__DASHCAM_SAMPLER_BUILD__\n", encoding="utf-8")
    codec.write_text("first\n", encoding="utf-8")
    monkeypatch.setattr(carplay_timing, "_SCRIPT_PATH", main)
    monkeypatch.setattr(carplay_timing, "_CODEC_SCRIPT_PATH", codec)
    first = carplay_timing.bundle_id()
    assert first in carplay_timing.script()
    assert "__DASHCAM_SAMPLER_BUILD__" not in carplay_timing.script()
    codec.write_text("second\n", encoding="utf-8")
    second = carplay_timing.bundle_id()
    assert first != second
    main.write_text("# change\nBUILD_ID=__DASHCAM_SAMPLER_BUILD__\n", encoding="utf-8")
    assert carplay_timing.bundle_id() != second


def test_codec_and_transport_evidence_survives_retained_file_parser():
    lines = [
        "sample=s-wire-1 session=s schema=6 acc=1 | event=wireless_link "
        "wire_peer_tcp_sockets=1 wire_peer_tcp6_sockets=1 "
        "wire_peer_root_verified_sockets=1 wire_peer_bytes_received_delta=120000 "
        "zlink_loopback_bytes_received_delta=119980 zlink_loopback_delta_ms=3000",
        "sample=s-codec-1 session=s schema=6 | event=codec_capture "
        "codec_capture_status=ok codec_trace_off=1 codec_matched_n=147 "
        "codec_latency_med_ms=48.671 codec_latency_max_ms=113.165 "
        "codec_capture_start_ms=100000 codec_capture_end_ms=105300",
    ]
    records = carplay_timing.parse_sampler_file(
        "\n".join("2026-09-24T12:00:00Z " + line for line in lines)
    )
    events = [carplay_timing.parse_event(row.occurred_at, row.message) for row in records]
    assert events[0]["wire_peer_tcp6_sockets"] == 1
    assert events[0]["wire_peer_bytes_received_delta"] == 120000
    assert events[0]["zlink_loopback_bytes_received_delta"] == 119980
    assert events[1]["codec_capture_status"] == "ok"
    assert events[1]["codec_latency_med_ms"] == 48.671
    assert events[1]["codec_capture_end_ms"] == 105300
    assert events[0]["codec_matched_n"] is None
    assert events[1]["wire_peer_tcp_sockets"] is None


def test_unavailable_new_fields_stay_null_and_status_is_allowlisted():
    event = carplay_timing.parse_event(
        datetime.now(UTC),
        "schema=5 | event=codec_capture codec_capture_status=PRIVATE_VALUE "
        "codec_matched_n=na codec_latency_med_ms=NaN",
    )
    assert event["codec_capture_status"] is None
    assert event["codec_matched_n"] is None
    assert event["codec_latency_med_ms"] is None
    assert event["wire_peer_tcp6_sockets"] is None


async def test_arm_requires_running_bundle_verification_after_detached_launch(monkeypatch):
    commands = []

    async def fake_shell(address, command, **kwargs):
        commands.append(command)
        return ""

    monkeypatch.setattr(carplay_timing, "_enabled", lambda: True)
    monkeypatch.setattr(carplay_timing.adb, "shell", fake_shell)
    assert not await carplay_timing.arm("test-unit:5555")
    assert len(commands) == 3
    assert "base64 -d" in commands[0]
    assert "setsid sh" in commands[1]
    assert "echo sampler_verified" in commands[2]


async def test_concurrent_arms_do_not_interleave_bundle_installs(monkeypatch):
    import asyncio

    owners = []

    async def fake_shell(address, command, **kwargs):
        owners.append(asyncio.current_task().get_name())
        await asyncio.sleep(0)
        return "sampler_verified\n" if "echo sampler_verified" in command else ""

    monkeypatch.setattr(carplay_timing, "_enabled", lambda: True)
    monkeypatch.setattr(carplay_timing.adb, "shell", fake_shell)
    tasks = [
        asyncio.create_task(carplay_timing.arm("test-unit:5555"), name=str(i)) for i in range(2)
    ]
    assert await asyncio.gather(*tasks) == [True, True]
    assert owners == ["0"] * 3 + ["1"] * 3
