from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import subprocess
from dataclasses import replace

import pytest

from app.ingest import adb
from app.ingest.models import RemoteFile

NAME = "20261026192900_camera_0.ts"
SOURCE = "/card/DCIM/Video"
BODY = b"the complete recording, including its last bytes"
DIGEST = hashlib.sha256(BODY).hexdigest()
IDENTITY = (
    f"123|456|{len(BODY)}|1793000000|1793000001|"
    "2026-10-26 01:00:00.123456789 +0000|2026-10-26 01:00:01.987654321 +0000"
)
ITEM = RemoteFile(NAME, len(BODY), 1793000000, directory=SOURCE)


def proof() -> adb.VerifiedRemoteFile:
    return adb.VerifiedRemoteFile(
        "unit:5555", SOURCE, NAME, ITEM.size, ITEM.mtime, IDENTITY, DIGEST
    )


async def test_full_hash_proof_pins_fd_and_is_read_only(monkeypatch):
    commands = []

    async def shell(address, command, **kwargs):
        commands.append((address, command, kwargs))
        return f"dashcam-verified:{IDENTITY}\n{DIGEST}\n"

    monkeypatch.setattr(adb, "shell", shell)
    assert await adb.verify_remote_file("unit", SOURCE, ITEM, DIGEST, timeout=2) == proof()
    address, command, kwargs = commands[0]
    assert address == "unit"
    assert kwargs == {"timeout": 2}
    assert f"exec 7<'{NAME}'" in command
    assert "/proc/$$/fd/7" in command
    assert "sha256sum <&7" in command
    assert "head -c" not in command and "tail -c" not in command and "rm " not in command
    assert "%d|%i|%s|%Y|%Z|%y|%z" in command


@pytest.mark.parametrize(
    "reply",
    [
        "",
        "unsupported",
        f"dashcam-verified:{IDENTITY}\n{'0' * 64}",
        f"dashcam-verified:{IDENTITY.replace('|456|', '|0|')}\n{DIGEST}",
        f"dashcam-verified:{IDENTITY.replace('.123456789', '')}\n{DIGEST}",
        f"dashcam-verified:{IDENTITY.replace(str(ITEM.size), str(ITEM.size + 1), 1)}\n{DIGEST}",
        f"dashcam-verified:{IDENTITY}\n{DIGEST}\nextra",
    ],
)
async def test_malformed_or_mismatched_remote_proof_is_refused(monkeypatch, reply):
    async def shell(*args, **kwargs):
        return reply

    monkeypatch.setattr(adb, "shell", shell)
    assert await adb.verify_remote_file("unit", SOURCE, ITEM, DIGEST) is None


@pytest.mark.parametrize("error", [adb.AdbError("unavailable"), TimeoutError()])
async def test_probe_or_delete_timeout_is_not_success(monkeypatch, error):
    async def shell(*args, **kwargs):
        raise error

    monkeypatch.setattr(adb, "shell", shell)
    assert await adb.verify_remote_file("unit", SOURCE, ITEM, DIGEST) is None
    assert not await adb.delete_verified_remote_file("unit", SOURCE, ITEM, proof())


async def test_cancellation_propagates_without_a_followup_delete(monkeypatch):
    calls = []

    async def shell(*args, **kwargs):
        calls.append(args)
        raise asyncio.CancelledError

    monkeypatch.setattr(adb, "shell", shell)
    with pytest.raises(asyncio.CancelledError):
        await adb.verify_remote_file("unit", SOURCE, ITEM, DIGEST)
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("source", "item", "digest"),
    [
        ("/card/Video'; reboot", ITEM, DIGEST),
        ("/card/../Video", ITEM, DIGEST),
        ("relative/Video", ITEM, DIGEST),
        (SOURCE, replace(ITEM, name="../clip.ts"), DIGEST),
        (SOURCE, replace(ITEM, directory="/other/Video"), DIGEST),
        (SOURCE, replace(ITEM, size=0), DIGEST),
        (SOURCE, ITEM, "bad digest; rm"),
    ],
)
async def test_unsafe_inputs_never_reach_shell(monkeypatch, source, item, digest):
    async def shell(*args, **kwargs):
        raise AssertionError("unsafe input reached the device")

    monkeypatch.setattr(adb, "shell", shell)
    with pytest.raises(adb.AdbError):
        await adb.verify_remote_file("unit", source, item, digest)


@pytest.mark.parametrize(
    "changes",
    [
        {"address": "other:5555"},
        {"source": "/other/Video"},
        {"name": "other.ts"},
        {"size": ITEM.size + 1},
        {"mtime": ITEM.mtime + 1},
        {"sha256": "not-a-digest"},
        {"identity": IDENTITY + '"; rm -rf /'},
    ],
)
async def test_proof_cannot_be_reused_for_another_inventory(monkeypatch, changes):
    async def shell(*args, **kwargs):
        raise AssertionError("unbound proof reached the device")

    monkeypatch.setattr(adb, "shell", shell)
    assert not await adb.delete_verified_remote_file(
        "unit", SOURCE, ITEM, replace(proof(), **changes)
    )


async def test_delete_requires_exact_acknowledgement_and_rechecks_pinned_identity(monkeypatch):
    commands = []

    async def shell(address, command, **kwargs):
        commands.append(command)
        return "dashcam-verified-deleted\nextra"

    monkeypatch.setattr(adb, "shell", shell)
    assert not await adb.delete_verified_remote_file("unit", SOURCE, ITEM, proof())
    assert IDENTITY in commands[0]
    assert commands[0].index(IDENTITY) < commands[0].index("rm -f")
    assert commands[0].count("stat -L -c") == 2
    assert "sha256sum <&7" in commands[0]
    assert commands[0].index("sha256sum") < commands[0].index("rm -f")


@pytest.fixture(params=["sh", "mksh"])
def remote_shell(monkeypatch, request):
    if os.name == "nt":
        pytest.skip("pinned /proc file descriptor fixture requires a POSIX host")
    executable = shutil.which(request.param)
    if not executable:
        pytest.skip(f"{request.param} unavailable")
    prefix = []

    async def shell(address, command, *, timeout):
        result = subprocess.run(
            [executable, "-c", "; ".join([*prefix, command])],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=True,
        )
        return result.stdout.strip()

    monkeypatch.setattr(adb, "shell", shell)
    return prefix


def remote_file(tmp_path):
    path = tmp_path / NAME
    path.write_bytes(BODY)
    stat = path.stat()
    return path, RemoteFile(NAME, stat.st_size, int(stat.st_mtime), directory=str(tmp_path))


async def test_real_shell_full_hash_then_delete(tmp_path, remote_shell):
    path, item = remote_file(tmp_path)
    verified = await adb.verify_remote_file("unit", str(tmp_path), item, DIGEST)
    assert verified is not None
    assert path.read_bytes() == BODY  # Hashing alone never deletes.
    assert await adb.delete_verified_remote_file("unit", str(tmp_path), item, verified)
    assert not path.exists()


async def test_real_shell_same_size_content_mismatch_is_preserved(tmp_path, remote_shell):
    path, item = remote_file(tmp_path)
    assert await adb.verify_remote_file("unit", str(tmp_path), item, "0" * 64) is None
    assert path.read_bytes() == BODY


async def test_real_shell_checks_unsampled_middle_bytes_too(tmp_path, remote_shell):
    body = b"a" * 16_384 + b"b" * 32_768 + b"z" * 16_384
    path = tmp_path / NAME
    changed = bytearray(body)
    changed[20_000] = ord("x")  # Outside the previous first/middle/last 16 KiB samples.
    path.write_bytes(changed)
    item = RemoteFile(NAME, len(body), int(path.stat().st_mtime), directory=str(tmp_path))
    assert (
        await adb.verify_remote_file("unit", str(tmp_path), item, hashlib.sha256(body).hexdigest())
        is None
    )
    assert path.read_bytes() == changed


async def test_real_shell_rejects_symlink(tmp_path, remote_shell):
    path, item = remote_file(tmp_path)
    target = tmp_path / "original.ts"
    path.rename(target)
    path.symlink_to(target)
    assert await adb.verify_remote_file("unit", str(tmp_path), item, DIGEST) is None
    assert path.is_symlink() and target.read_bytes() == BODY


async def test_real_shell_rejects_symlink_replacement_after_verification(tmp_path, remote_shell):
    path, item = remote_file(tmp_path)
    verified = await adb.verify_remote_file("unit", str(tmp_path), item, DIGEST)
    assert verified is not None
    target = tmp_path / "original.ts"
    path.rename(target)
    path.symlink_to(target)
    assert not await adb.delete_verified_remote_file("unit", str(tmp_path), item, verified)
    assert path.is_symlink() and target.read_bytes() == BODY


async def test_real_shell_rejects_replacement_with_same_size_and_mtime(tmp_path, remote_shell):
    path, item = remote_file(tmp_path)
    verified = await adb.verify_remote_file("unit", str(tmp_path), item, DIGEST)
    assert verified is not None
    original_stat = path.stat()
    path.rename(tmp_path / "original.ts")
    path.write_bytes(BODY)
    os.utime(path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    assert not await adb.delete_verified_remote_file("unit", str(tmp_path), item, verified)
    assert path.read_bytes() == BODY


async def test_real_shell_rejects_in_place_rewrite_with_restored_mtime(tmp_path, remote_shell):
    path, item = remote_file(tmp_path)
    verified = await adb.verify_remote_file("unit", str(tmp_path), item, DIGEST)
    assert verified is not None
    original_stat = path.stat()
    path.write_bytes(b"x" * len(BODY))
    os.utime(path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    assert not await adb.delete_verified_remote_file("unit", str(tmp_path), item, verified)
    assert path.read_bytes() == b"x" * len(BODY)


async def test_real_shell_final_hash_rejects_rewrite_even_with_frozen_coarse_metadata(
    tmp_path, remote_shell
):
    path, item = remote_file(tmp_path)
    verified = await adb.verify_remote_file("unit", str(tmp_path), item, DIGEST)
    assert verified is not None
    original_stat = path.stat()
    # Simulate a FAT/FUSE provider whose full stat output cannot distinguish a rewrite.
    # Both pathname and pinned-fd checks still return the original identity verbatim.
    remote_shell.append(f"stat() {{ printf '%s\\n' '{verified.identity}'; }}")
    path.write_bytes(b"x" * len(BODY))
    os.utime(path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    assert path.stat().st_ino == original_stat.st_ino
    assert not await adb.delete_verified_remote_file("unit", str(tmp_path), item, verified)
    assert path.read_bytes() == b"x" * len(BODY)


async def test_real_shell_rejects_path_replacement_during_pinned_hash(tmp_path, remote_shell):
    path, item = remote_file(tmp_path)
    remote_shell.append(
        f"sha256sum() {{ mv '{NAME}' original.ts; cp original.ts '{NAME}'; "
        'command sha256sum "$@"; }'
    )
    assert await adb.verify_remote_file("unit", str(tmp_path), item, DIGEST) is None
    assert path.read_bytes() == BODY and (tmp_path / "original.ts").read_bytes() == BODY


async def test_real_shell_unsupported_stat_or_hash_preserves_file(tmp_path, remote_shell):
    path, item = remote_file(tmp_path)
    remote_shell.append("stat() { return 1; }")
    assert await adb.verify_remote_file("unit", str(tmp_path), item, DIGEST) is None
    remote_shell[:] = ["sha256sum() { return 1; }"]
    assert await adb.verify_remote_file("unit", str(tmp_path), item, DIGEST) is None
    assert path.read_bytes() == BODY
