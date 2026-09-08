from __future__ import annotations

import pytest

from app.ingest import adb
from app.ingest.models import RemoteFile


async def test_adb_timeout_survives_process_exit_before_kill(monkeypatch):
    """A normal timeout must not turn a process-exit race into a startup crash."""

    class ExitedProcess:
        returncode = 0

        async def communicate(self):
            raise TimeoutError

        def kill(self):
            raise ProcessLookupError

        async def wait(self):
            return 0

    async def spawn(*_args, **_kwargs):
        return ExitedProcess()

    monkeypatch.setattr(adb, "adb_path", lambda: "adb")
    monkeypatch.setattr(adb.asyncio, "create_subprocess_exec", spawn)

    with pytest.raises(adb.AdbError, match=r"adb -s unit:5555 timed out after 1s"):
        await adb._adb("-s", "unit:5555", "shell", "true", timeout=1)


async def test_reclaim_only_counts_the_exact_remote_identity(monkeypatch):
    commands: list[str] = []

    async def fake_shell(address, command, **kwargs):
        commands.append(command)
        # Only one of two remote paths still has the inventoried size and mtime.
        return "deleted\n"

    monkeypatch.setattr(adb, "shell", fake_shell)
    removed = await adb.delete_if_unchanged(
        "unit:5555",
        "/card/Video",
        [RemoteFile("old.ts", 100, 123), RemoteFile("replaced.ts", 100, 124)],
    )

    assert removed == 1
    assert "stat -c '%s|%Y' 'old.ts'" in commands[0]
    assert "= '100|123'" in commands[0]
    assert "stat -c '%s|%Y' 'replaced.ts'" in commands[0]
    assert commands[0].startswith("cd '/card/Video' || exit 0; ")


async def test_reclaim_rejects_an_unsafe_source_before_shell(monkeypatch):
    async def fail(*args, **kwargs):
        raise AssertionError("unsafe source reached the unit shell")

    monkeypatch.setattr(adb, "shell", fail)
    with pytest.raises(adb.AdbError, match="source path"):
        await adb.delete_if_unchanged(
            "unit:5555", "/card/Video'; reboot", [RemoteFile("clip.ts", 1, 2)]
        )
