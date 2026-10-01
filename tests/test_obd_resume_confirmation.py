"""An ambiguous rm/sync reply needs positive, accessible-directory resume evidence."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.ingest import adb, obd_control

STATUS_PATH = "/storage/card/app/obd/status.json"


async def test_already_absent_handshake_never_removes_or_syncs_again(monkeypatch):
    shell = AsyncMock(return_value="resumed")
    monkeypatch.setattr(adb, "shell", shell)
    assert await obd_control.resume_logger("unit", STATUS_PATH)
    shell.assert_awaited_once()
    assert "rm -f" not in shell.await_args.args[1]
    assert "sync" not in shell.await_args.args[1]


async def test_lost_removal_reply_can_succeed_only_with_fresh_positive_absence(monkeypatch):
    shell = AsyncMock(side_effect=["", adb.AdbError("sync exceeded 6s"), "resumed"])
    monkeypatch.setattr(adb, "shell", shell)
    assert await obd_control.resume_logger("unit", STATUS_PATH)
    commands = [call.args[1] for call in shell.await_args_list]
    assert len(commands) == 3
    assert "rm -f" in commands[1] and "sync" in commands[1]
    assert "rm -f" not in commands[2] and "sync" not in commands[2]
    assert all(call.kwargs == {"timeout": 6.0} for call in shell.await_args_list)


@pytest.mark.parametrize("readback", ["", "denied", "resumed\nerror", adb.AdbError("offline")])
async def test_ambiguous_delete_is_not_success_without_positive_readback(monkeypatch, readback):
    shell = AsyncMock(side_effect=["", adb.AdbError("timeout"), readback])
    monkeypatch.setattr(adb, "shell", shell)
    assert not await obd_control.resume_logger("unit", STATUS_PATH)
    assert shell.await_count == 3


async def test_successful_remove_still_needs_absence_readback(monkeypatch):
    shell = AsyncMock(side_effect=["", "", ""])
    monkeypatch.setattr(adb, "shell", shell)
    assert not await obd_control.resume_logger("unit", STATUS_PATH)


async def test_failed_initial_probe_can_recover_with_final_evidence(monkeypatch):
    shell = AsyncMock(side_effect=[adb.AdbError("read timeout"), "", "resumed"])
    monkeypatch.setattr(adb, "shell", shell)
    assert await obd_control.resume_logger("unit", STATUS_PATH)


async def test_cancellation_is_not_converted_to_resume_success(monkeypatch):
    shell = AsyncMock(side_effect=["", asyncio.CancelledError()])
    monkeypatch.setattr(adb, "shell", shell)
    with pytest.raises(asyncio.CancelledError):
        await obd_control.resume_logger("unit", STATUS_PATH)
    assert shell.await_count == 2


async def test_unsafe_status_path_cannot_issue_cleanup(monkeypatch):
    shell = AsyncMock()
    monkeypatch.setattr(adb, "shell", shell)
    assert not await obd_control.resume_logger("unit", "/storage/card/../other/status.json")
    shell.assert_not_called()


@pytest.mark.parametrize("removal", ["", adb.AdbError("uncertain cleanup")])
async def test_quiesce_never_publishes_request_after_unproven_cleanup(monkeypatch, removal):
    shell = AsyncMock(side_effect=["", removal, ""])
    monkeypatch.setattr(adb, "shell", shell)
    with pytest.raises(obd_control.LoggerControlError, match="prior logger handshake"):
        await obd_control.request_quiesce("unit", STATUS_PATH, request_id="new-request")
    assert shell.await_count == 3
    assert all("printf '%s'" not in call.args[1] for call in shell.await_args_list)


@pytest.fixture
def shell_path():
    if os.name == "nt":
        candidate = Path("C:/Program Files/Git/usr/bin/sh.exe")
        if candidate.is_file():
            return str(candidate)
        pytest.skip("POSIX shell unavailable")
    candidate = shutil.which("sh")
    if not candidate:
        pytest.skip("POSIX shell unavailable")
    return candidate


@pytest.mark.parametrize(
    "state,expected",
    [
        ("empty", "resumed"),
        ("control_absent", "resumed"),
        ("parent_absent", ""),
        ("parent_unlistable", ""),
        ("control_unlistable", ""),
        ("request_present", ""),
        ("ack_present", ""),
        ("control_file", ""),
        ("control_symlink", ""),
        ("request_dangling_symlink", ""),
        ("ack_dangling_symlink", ""),
    ],
)
def test_actual_absence_shell_requires_accessible_storage(tmp_path, shell_path, state, expected):
    parent = tmp_path / "obd"
    directory = parent / "control"
    request = directory / "ingestion-request.json"
    ack = directory / "ingestion-ack.json"
    prefix = ""
    if state != "parent_absent":
        parent.mkdir()
    if state not in {"parent_absent", "control_absent", "control_file", "control_symlink"}:
        directory.mkdir()
    if state == "request_present":
        request.write_text("request", encoding="utf-8")
    elif state == "ack_present":
        ack.write_text("ack", encoding="utf-8")
    elif state == "control_file":
        directory.write_text("unexpected file", encoding="utf-8")
    elif state == "control_symlink":
        target = parent / "elsewhere"
        target.mkdir()
        try:
            directory.symlink_to(target, target_is_directory=True)
        except OSError:
            pytest.skip("test account cannot create symlinks")
    elif state.endswith("dangling_symlink"):
        target = request if state.startswith("request") else ack
        try:
            target.symlink_to(directory / "missing")
        except OSError:
            pytest.skip("test account cannot create symlinks")
    elif state == "parent_unlistable":
        # Models a filesystem/SELinux read failure even on a root-owned CI runner.
        prefix = "ls() { return 1; }; "
    elif state == "control_unlistable":
        prefix = 'ls() { case "$2" in */control) return 1;; *) command ls "$@";; esac; }; '

    paths = obd_control.ControlPaths(directory.as_posix(), request.as_posix(), ack.as_posix())
    command = prefix + obd_control._control_absence_command(paths)
    if os.name == "nt":
        command = "PATH=/usr/bin:/bin:$PATH; " + command
    completed = subprocess.run(
        [shell_path, "-c", command], capture_output=True, text=True, timeout=5, check=False
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == expected
