from __future__ import annotations

import pytest

from app.ingest import adb


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
