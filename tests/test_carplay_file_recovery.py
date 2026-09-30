"""Bounded retained-log transfer, corruption/rotation safety and real POSIX shell fixtures."""

import asyncio
import base64
import gzip
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app.ingest import adb, unit_logs
from app.ingest import carplay_timing as timing


def wire(payload, *, compressed=True):
    body = gzip.compress(payload) if compressed else payload
    mode = "gzip" if compressed else "base64"
    return f"CPR1 {mode}\n{base64.b64encode(body).decode()}\nCPR_END"


def line(index):
    return (
        f"2026-09-30T02:33:12Z sample=fixture-{index} session=fixture "
        "schema=8 acc=1 | event=gps_context gps_capture_status=ok\n"
    ).encode()


class Remote:
    def __init__(self, content, *, compressed=True):
        self.content = content
        self.compressed = compressed
        self.commands = []
        self.manifests = 0
        self.chunks = 0
        self.change_final = False
        self.corrupt_chunk = None
        self.cancel_chunk = None
        self.overflow_above = None
        self.append_final = False

    async def shell(self, address, command, **kwargs):
        assert kwargs["timeout"] == timing.RECOVERY_REQUEST_TIMEOUT_S
        self.commands.append(command)
        if "command -v" in command:
            return "CPR_GZIP" if self.compressed else "CPR_BASE64"
        if command == timing._sampler_manifest_command():
            self.manifests += 1
            return "\n".join(
                f"{g} 7:{90 + g + (1 if self.change_final and self.manifests > 1 else 0)}:"
                f"{len(raw) + (20 if self.append_final and self.manifests > 1 else 0)}"
                for g, raw in self.content.items()
            )
        self.chunks += 1
        if self.chunks == self.cancel_chunk:
            raise asyncio.CancelledError
        path = command.split("exec 3<", 1)[1].split(" ||", 1)[0]
        generation = int(path.rsplit(".", 1)[1]) if path != timing.REMOTE_LOG else 0
        skip = int(re.search(r"dd bs=65536 skip=(\d+)", command)[1])
        within = int(re.search(r"tail -c \+(\d+)", command)[1]) - 1
        length = int(re.search(r"head -c (\d+)", command)[1])
        if self.overflow_above and length > self.overflow_above:
            return "CPR_OVERFLOW"
        start = skip * 65536 + within
        result = wire(self.content[generation][start : start + length], compressed=self.compressed)
        if self.chunks == self.corrupt_chunk:
            return result[:-10]
        return result


@pytest.fixture
def stored(monkeypatch):
    batches = []

    async def store(entries):
        batches.append(entries)
        return len(entries), 0

    monkeypatch.setattr(unit_logs, "store", store)
    return batches


async def test_snapshot_transfers_only_frozen_tails_oldest_first_and_stores_once(
    monkeypatch, stored
):
    old = b"discard me\n" * 8000 + b"".join(line(n) for n in range(1000))
    recent = b"".join(line(n) for n in range(1000, 1600))
    remote = Remote({3: old, 0: recent})  # Missing intermediate rotations are ordinary.
    remote.append_final = True
    monkeypatch.setattr(timing, "MAX_RECOVERY_BYTES_PER_FILE", 80_000)
    monkeypatch.setattr(timing.adb, "shell", remote.shell)
    expected = timing.parse_sampler_file((old[-80_000:] + recent[-80_000:]).decode())
    assert await timing.recover_sampler_file("unit") == (len(expected), 0)
    assert len(stored) == 1 and stored[0] == expected
    assert remote.manifests == 2
    assert remote.chunks == 4
    assert all("rm " not in command and " > " not in command for command in remote.commands)


@pytest.mark.parametrize("compressed", [True, False])
async def test_overflow_reduces_chunks_and_base64_fallback_remains_bounded(
    monkeypatch, stored, compressed
):
    payload = b"".join(line(n) for n in range(1000))
    remote = Remote({0: payload}, compressed=compressed)
    remote.overflow_above = 8192
    monkeypatch.setattr(timing.adb, "shell", remote.shell)
    assert await timing.recover_sampler_file("unit") == (1000, 0)
    assert len(stored) == 1
    assert remote.chunks < 25


@pytest.mark.parametrize("failure", ["corrupt", "rotation", "cancel", "disconnect"])
async def test_incomplete_attempt_never_stores_earlier_good_chunks(monkeypatch, stored, failure):
    remote = Remote({0: b"".join(line(n) for n in range(1000))})
    remote.corrupt_chunk = 2 if failure == "corrupt" else None
    remote.cancel_chunk = 2 if failure == "cancel" else None
    remote.change_final = failure == "rotation"

    async def shell(address, command, **kwargs):
        if failure == "disconnect" and remote.chunks == 1:
            raise adb.AdbError("disconnected")
        return await remote.shell(address, command, **kwargs)

    monkeypatch.setattr(timing.adb, "shell", shell)
    with pytest.raises(asyncio.CancelledError if failure == "cancel" else adb.AdbError):
        await timing.recover_sampler_file("unit")
    assert stored == []


async def test_total_deadline_and_unsupported_tools_do_not_store(monkeypatch, stored):
    async def slow(*args, **kwargs):
        await asyncio.sleep(1)

    monkeypatch.setattr(timing, "RECOVERY_TIMEOUT_S", 0.01)
    monkeypatch.setattr(timing.adb, "shell", slow)
    with pytest.raises(adb.AdbError, match="total time limit"):
        await timing.recover_sampler_file("unit")

    async def unsupported(*args, **kwargs):
        return "CPR_UNSUPPORTED"

    monkeypatch.setattr(timing.adb, "shell", unsupported)
    with pytest.raises(adb.AdbError, match="requires"):
        await timing.recover_sampler_file("unit")
    assert stored == []


@pytest.mark.parametrize("payload", [b"", b"short", b"x" * 200_000], ids=["empty", "short", "bomb"])
def test_decompression_rejects_wrong_size_and_bombs(payload):
    with pytest.raises(adb.AdbError, match="integrity"):
        timing._decode_sampler_chunk(wire(payload), 100, compressed=True)


def test_decoder_rejects_crc_truncation_trailing_stream_and_encoded_limit():
    encoded = gzip.compress(b"x" * 100)
    for damaged in [encoded[:-1], encoded[:-8] + b"bad tail", encoded + encoded]:
        raw = "CPR1 gzip\n" + base64.b64encode(damaged).decode() + "\nCPR_END"
        with pytest.raises(adb.AdbError, match="integrity"):
            timing._decode_sampler_chunk(raw, 100, compressed=True)
    with pytest.raises(adb.AdbError, match="exceeds"):
        timing._decode_sampler_chunk(
            "x" * (timing.RECOVERY_BASE64_LIMIT + 65), 100, compressed=True
        )


@pytest.mark.parametrize("manifest", ["0 1:2:3\n0 1:2:3", "9 1:2:3", "0 1:2:-1", "private/path"])
def test_manifest_is_numeric_unique_and_fixed_generation_only(manifest):
    with pytest.raises(adb.AdbError):
        timing._parse_sampler_manifest(manifest)


def posix_shell():
    shell = shutil.which("sh")
    if not shell and os.name == "nt":
        candidate = Path(r"C:\Program Files\Git\usr\bin\sh.exe")
        shell = str(candidate) if candidate.exists() else None
    # Linux CI must have a shell; missing it is a test failure, not a skipped safety check.
    if not shell:
        if os.name == "nt":
            pytest.skip("POSIX test shell unavailable on Windows")
        pytest.fail("POSIX shell required")
    return shell


def run_shell(command):
    result = subprocess.run(
        [posix_shell(), "-c", "PATH=/usr/bin:/bin:$PATH; " + command],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_real_shell_manifest_fd_guards_unaligned_chunks_and_compression(tmp_path, monkeypatch):
    path = tmp_path / "sampler.log"
    payload = os.urandom(180_000)
    path.write_bytes(payload)
    monkeypatch.setattr(timing, "REMOTE_LOG", path.as_posix())
    manifest = timing._parse_sampler_manifest(run_shell(timing._sampler_manifest_command()))
    metadata = manifest[0]
    for compressed, length in [(True, 8192), (False, 8192)]:
        raw = run_shell(
            timing._sampler_chunk_command(0, metadata, 65_537, length, compressed=compressed)
        )
        assert (
            timing._decode_sampler_chunk(raw, length, compressed=compressed)
            == payload[65_537 : 65_537 + length]
        )
    # Incompressible64KiB cannot become a giant ADB reply; host retries a smaller range.
    raw = run_shell(timing._sampler_chunk_command(0, metadata, 0, 65_536, compressed=True))
    assert raw == "CPR_OVERFLOW"
    path.rename(tmp_path / "old.log")
    path.write_bytes(payload)
    assert (
        run_shell(timing._sampler_chunk_command(0, metadata, 0, 100, compressed=True))
        == "CPR_CHANGED"
    )


def test_real_shell_truncation_and_symlinks_are_not_read(tmp_path, monkeypatch):
    path = tmp_path / "sampler.log"
    path.write_bytes(b"x" * 1000)
    monkeypatch.setattr(timing, "REMOTE_LOG", path.as_posix())
    metadata = timing._parse_sampler_manifest(run_shell(timing._sampler_manifest_command()))[0]
    path.write_bytes(b"x" * 10)
    assert (
        run_shell(timing._sampler_chunk_command(0, metadata, 0, 100, compressed=True))
        == "CPR_CHANGED"
    )
    if os.name != "nt":
        path.unlink()
        path.symlink_to(tmp_path / "unrelated")
        result = subprocess.run(
            [posix_shell(), "-c", timing._sampler_manifest_command()], capture_output=True
        )
        assert result.returncode != 0
