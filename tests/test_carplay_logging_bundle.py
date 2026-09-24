"""The two on-unit programs travel together and survive retained-log/API parsing."""

import asyncio
import base64
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.ingest import carplay_timing


def test_deployment_stages_validates_and_atomically_replaces_both_programs():
    commands = carplay_timing.deploy_commands()
    uploads = [
        re.fullmatch(r"printf %s ([A-Za-z0-9+/=]+) \| base64 -d >> (\S+)", command)
        for command in commands[1:-1]
    ]
    reconstructed = {}
    for match in uploads:
        assert match
        reconstructed.setdefault(match[2], bytearray()).extend(base64.b64decode(match[1]))
        assert len(match[1]) <= carplay_timing.DEPLOY_CHUNK_BASE64_CHARS
        assert len(match[1]) % 4 == 0
    assert len(reconstructed) == 2
    expected = [carplay_timing.script(), carplay_timing.codec_script()]
    destinations = [carplay_timing.REMOTE_SCRIPT, carplay_timing.REMOTE_CODEC_SCRIPT]
    for (path, payload), source, destination in zip(reconstructed.items(), expected, destinations):
        assert payload.decode() == source
        assert path.startswith(destination + ".") and path.endswith(".new")
        assert f"sh -n {path}" in commands[-1]
        assert f"mv {path} {destination}" in commands[-1]
        assert f"wc -c < {path}" in commands[-1]
    assert commands[-1].rindex("sh -n") < commands[-1].index("mv ")
    assert not any("mv " in command for command in commands[:-1])
    assert max(len(command.encode()) for command in commands) < 24 * 1024
    assert all(len(command) < 32767 and len(command.encode()) < 65536 for command in commands)
    assert carplay_timing.deploy_commands()[0] != commands[0]


def test_actual_shell_uploads_and_publishes_exact_bundle_in_separate_requests(
    tmp_path, monkeypatch
):
    git_bash = Path(r"C:\Program Files\Git\bin\bash.exe")
    shell = str(git_bash) if git_bash.exists() else shutil.which("bash")
    if shell is None:
        pytest.skip("Bash unavailable")
    sampler, codec = tmp_path / "sampler.sh", tmp_path / "codec.sh"
    monkeypatch.setattr(carplay_timing, "REMOTE_SCRIPT", sampler.as_posix())
    monkeypatch.setattr(carplay_timing, "REMOTE_CODEC_SCRIPT", codec.as_posix())
    expected = (carplay_timing.script().encode(), carplay_timing.codec_script().encode())
    sampler.write_bytes(b"old sampler\n")
    codec.write_bytes(b"old codec\n")
    commands = carplay_timing.deploy_commands()
    for command in commands[:-1]:
        subprocess.run([shell, "-c", command], capture_output=True, text=True, check=True)
        assert sampler.read_bytes() == b"old sampler\n"
        assert codec.read_bytes() == b"old codec\n"
    subprocess.run([shell, "-c", commands[-1]], capture_output=True, text=True, check=True)
    assert (sampler.read_bytes(), codec.read_bytes()) == expected
    assert not list(tmp_path.glob("*.new"))


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
    assert len(commands) == len(carplay_timing.deploy_commands()) + 2
    assert any("base64 -d" in command for command in commands)
    assert "setsid sh" in commands[-2]
    assert "echo sampler_verified" in commands[-1]


async def test_concurrent_arms_do_not_interleave_bundle_installs(monkeypatch):
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
    count = len(carplay_timing.deploy_commands()) + 2
    assert owners == ["0"] * count + ["1"] * count


@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_failed_chunk_never_publishes_or_launches_and_cleans_owned_stages(
    monkeypatch, cleanup_fails
):
    commands = []
    chunks = 0

    async def fake_shell(address, command, **kwargs):
        nonlocal chunks
        commands.append(command)
        if "base64 -d" in command:
            chunks += 1
            if chunks == 2:
                raise carplay_timing.adb.AdbError("chunk transport failed")
        if command.startswith("rm -f ") and cleanup_fails:
            raise carplay_timing.adb.AdbError("cleanup transport failed")
        return ""

    monkeypatch.setattr(carplay_timing, "_enabled", lambda: True)
    monkeypatch.setattr(carplay_timing.adb, "shell", fake_shell)
    assert not await carplay_timing.arm("chunk-failure-unit:5555")
    assert chunks == 2
    assert not any("mv " in command or "setsid" in command for command in commands)
    stages = re.findall(r"> (\S+)", commands[0])
    assert commands[-1] == "rm -f " + " ".join(stages)
    assert all(stage.endswith(".new") for stage in stages)
    assert "*" not in commands[-1]


async def test_cancelled_upload_cleans_staging_without_launch(monkeypatch):
    commands = []

    async def fake_shell(address, command, **kwargs):
        commands.append(command)
        if "base64 -d" in command:
            raise asyncio.CancelledError
        return ""

    monkeypatch.setattr(carplay_timing, "_enabled", lambda: True)
    monkeypatch.setattr(carplay_timing.adb, "shell", fake_shell)
    with pytest.raises(asyncio.CancelledError):
        await carplay_timing.arm("cancelled-unit:5555")
    assert commands[-1].startswith("rm -f ")
    assert not any("mv " in command or "setsid" in command for command in commands)


@pytest.mark.parametrize("outcome,delay", [(False, 30), (True, 300), (RuntimeError, 30)])
async def test_presence_retry_after_failure_is_short_but_not_every_tick(
    monkeypatch, outcome, delay
):
    clock = [1000.0]
    calls = []
    release = asyncio.Event()

    async def fake_arm(address):
        calls.append(address)
        await release.wait()
        if outcome is RuntimeError:
            raise RuntimeError("unexpected arm failure")
        return outcome

    monkeypatch.setattr(carplay_timing, "_enabled", lambda: True)
    monkeypatch.setattr(carplay_timing, "recover_on_unit_present", lambda address: None)
    monkeypatch.setattr(carplay_timing.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(carplay_timing, "arm", fake_arm)
    carplay_timing.reset_for_tests()
    try:
        carplay_timing.on_unit_present("retry-unit")
        await asyncio.sleep(0)
        carplay_timing.on_unit_present("retry-unit")
        assert calls == ["retry-unit"]
        release.set()
        first = await asyncio.gather(*list(carplay_timing._tasks), return_exceptions=True)
        if outcome is RuntimeError:
            assert isinstance(first[0], RuntimeError)
        clock[0] += delay - 1
        carplay_timing.on_unit_present("retry-unit")
        await asyncio.sleep(0)
        assert len(calls) == 1
        clock[0] += 1
        carplay_timing.on_unit_present("retry-unit")
        await asyncio.gather(*list(carplay_timing._tasks), return_exceptions=True)
        assert len(calls) == 2
    finally:
        await carplay_timing.shutdown()
        carplay_timing.reset_for_tests()
