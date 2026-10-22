"""Transient probes cannot outlive a verified lease or collide through UiAutomation."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from test_ingest_maintenance import BOOT, COUNTDOWN, NORMAL, arm_for_test, power

from app.ingest import adb, maintenance


@pytest.fixture
def held(monkeypatch, tmp_path, app_config):
    controller = maintenance.MaintenanceController(tmp_path / "cancelled.json")
    monkeypatch.setattr(maintenance, "_controller", controller)
    arm_for_test(controller, maintenance.time.monotonic())
    controller._address = "unit"
    monkeypatch.setattr(maintenance, "HEARTBEAT_INTERVAL_S", 0)
    monkeypatch.setattr(maintenance, "HEARTBEAT_RETRY_DELAY_S", 0)
    return controller


@pytest.mark.parametrize("stage", ["screen", "power", "publish"])
async def test_transient_probe_retries_whole_fresh_proof_without_extending_deadlines(
    monkeypatch, held, stage
):
    calls = {"screen": 0, "power": 0, "publish": 0}
    original_end, original_device_end = held._end, held._device_end

    def maybe_fail(name):
        calls[name] += 1
        if stage == name and calls[name] == 1:
            raise maintenance.MaintenanceTransient(name, f"{name}_transport", "unavailable")

    async def screen(_address):
        maybe_fail("screen")
        return NORMAL

    async def power_proof(_address):
        maybe_fail("power")
        return maintenance.PowerProof(BOOT, 1000)

    async def publish(_proof):
        maybe_fail("publish")
        assert held._end == original_end
        assert held._device_end == original_device_end
        held._end = maintenance.time.monotonic() + maintenance.RESTORE_RESERVE_S

    monkeypatch.setattr(maintenance, "_screen", screen)
    monkeypatch.setattr(maintenance, "_power", power_proof)
    monkeypatch.setattr(held, "_publish", publish)
    finish = AsyncMock()
    monkeypatch.setattr(held, "_end_hold", finish)
    await held._heartbeat()
    assert calls[stage] == 2
    assert calls["screen"] == 2
    assert calls["publish"] == (2 if stage == "publish" else 1)
    assert maintenance.EVIDENCE_TTL_S == 30
    assert maintenance.DEVICE_HEARTBEAT_TTL_S == 45
    finish.assert_awaited_once_with(
        "expired", "Maintenance ended; radios are restored before backup can resume."
    )


@pytest.mark.parametrize("screen", [COUNTDOWN, "<not-valid-xml"])
async def test_returned_countdown_or_invalid_screen_never_retries_or_renews(
    monkeypatch, held, screen
):
    probe = AsyncMock(return_value=screen)
    power_probe, publish, finish = AsyncMock(), AsyncMock(), AsyncMock()
    monkeypatch.setattr(maintenance, "_screen", probe)
    monkeypatch.setattr(maintenance, "_power", power_probe)
    monkeypatch.setattr(held, "_publish", publish)
    monkeypatch.setattr(held, "_end_hold", finish)
    await held._heartbeat()
    probe.assert_awaited_once()
    power_probe.assert_not_awaited()
    publish.assert_not_awaited()
    assert finish.await_args.args[0] == "failed"


async def test_unsafe_power_is_never_retried_or_published(monkeypatch, held):
    probe = AsyncMock(side_effect=maintenance.MaintenanceRefused("ignition changed"))
    monkeypatch.setattr(maintenance, "_screen", AsyncMock(return_value=NORMAL))
    monkeypatch.setattr(maintenance, "_power", probe)
    publish, finish = AsyncMock(), AsyncMock()
    monkeypatch.setattr(held, "_publish", publish)
    monkeypatch.setattr(held, "_end_hold", finish)
    await held._heartbeat()
    probe.assert_awaited_once()
    publish.assert_not_awaited()
    assert finish.await_args.args[0] == "failed"


async def test_transient_exhaustion_retains_old_evidence_and_logs_only_bounded_codes(
    monkeypatch, held
):
    messages = []

    class Logger:
        def warning(self, event, **fields):
            messages.append((event, fields))

    previous = held._verified_at
    screen = AsyncMock(side_effect=adb.AdbError("secret-payload-and-private-device-output"))
    monkeypatch.setattr(maintenance, "_screen", screen)
    monkeypatch.setattr(maintenance, "log", Logger())
    finish = AsyncMock()
    monkeypatch.setattr(held, "_end_hold", finish)
    await held._heartbeat()
    assert screen.await_count == maintenance.HEARTBEAT_MAX_ATTEMPTS
    assert held._verified_at == previous
    assert "secret-payload" not in str(messages) + str(finish.await_args)
    assert messages[-1][1]["reason_code"] == "screen_transport"
    assert finish.await_args.args[0] == "failed"


async def test_original_evidence_horizon_bounds_a_stuck_refresh(monkeypatch, held):
    held._verified_at = maintenance.time.monotonic() - maintenance.EVIDENCE_TTL_S + 0.03
    previous = held._verified_at

    async def stuck(_address):
        await asyncio.Event().wait()

    monkeypatch.setattr(maintenance, "_screen", stuck)
    publish, finish = AsyncMock(), AsyncMock()
    monkeypatch.setattr(held, "_publish", publish)
    monkeypatch.setattr(held, "_end_hold", finish)
    started = maintenance.time.monotonic()
    await held._heartbeat()
    assert maintenance.time.monotonic() - started < 0.5
    assert held._verified_at == previous
    publish.assert_not_awaited()
    assert "screen_timeout" in finish.await_args.args[1]


async def test_slow_refresh_does_not_append_another_full_interval(monkeypatch, held):
    now, delays, starts = [100.0], [], []
    held._verified_at, held._end = now[0], 1000
    monkeypatch.setattr(maintenance, "HEARTBEAT_INTERVAL_S", 10)
    monkeypatch.setattr(maintenance.time, "monotonic", lambda: now[0])

    async def pause(seconds):
        delays.append(seconds)
        now[0] += seconds

    async def screen(_address):
        starts.append(now[0])
        now[0] += 6
        return NORMAL

    async def power_proof(_address):
        now[0] += 1
        return maintenance.PowerProof(BOOT, 1000)

    async def publish(_proof):
        now[0] += 1
        if len(starts) == 2:
            held._end = now[0] + maintenance.RESTORE_RESERVE_S

    monkeypatch.setattr(maintenance.asyncio, "sleep", pause)
    monkeypatch.setattr(maintenance, "_screen", screen)
    monkeypatch.setattr(maintenance, "_power", power_proof)
    monkeypatch.setattr(held, "_publish", publish)
    monkeypatch.setattr(held, "_end_hold", AsyncMock())
    await held._heartbeat()
    assert starts == [110, 120]
    assert delays == [10, 2]


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"acc2": "1"}, "ignition_not_off"),
        ({"policy": "false"}, "sleep_policy"),
        ({"sleeping": "1"}, "device_sleeping"),
        ({"before": "nan"}, "power_uptime"),
        ({"after": "1004 0"}, "power_sample_window"),
        ({"boot2": "bad"}, "power_boot"),
    ],
)
async def test_power_refusals_are_classified_without_disclosing_raw_state(
    monkeypatch, changes, code
):
    monkeypatch.setattr(adb, "shell", AsyncMock(return_value=power(**changes)))
    with pytest.raises(maintenance.MaintenanceRefused, match=code):
        await maintenance._power("unit")


@pytest.mark.parametrize("operation", ["screen", "power", "publish"])
async def test_transport_exceptions_have_safe_stage_reasons(monkeypatch, held, operation):
    monkeypatch.setattr(adb, "shell", AsyncMock(side_effect=adb.AdbError("raw-secret-output")))
    monkeypatch.setattr(maintenance, "_ensure_ui_helper", AsyncMock())
    held._token, held._device_end = BOOT, 1800
    with pytest.raises(maintenance.MaintenanceTransient) as result:
        if operation == "screen":
            await maintenance._screen("unit")
        elif operation == "power":
            await maintenance._power("unit")
        else:
            await held._publish(maintenance.PowerProof(BOOT, 1000))
    assert result.value.stage == operation
    assert "raw-secret" not in str(result.value)


@pytest.mark.parametrize("exit_code,reason", [(75, "screen_busy"), (124, "screen_timeout")])
async def test_real_screen_wrapper_reports_busy_or_timeout_without_reusing_xml(
    monkeypatch, tmp_path, exit_code, reason
):
    shell = shutil.which("sh")
    if shell is None and os.name == "nt":
        candidate = Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "Git/bin/sh.exe"
        shell = str(candidate) if candidate.is_file() else None
    if shell is None:
        pytest.skip("POSIX shell required for the actual Android snapshot wrapper")
    asset, output, helper = tmp_path / "fresh.dex", tmp_path / "dump.xml", tmp_path / "helper.sh"
    asset.write_bytes(maintenance.UI_HELPER_ASSET.read_bytes())
    output.write_text(NORMAL)
    helper.write_text(f"printf '%s' '{NORMAL}' > \"$1\"\nexit {exit_code}\n")

    async def execute(_address, command, *, timeout):
        remote = re.search(
            r"/data/local/tmp/\.dashcam_stats_maintenance_ui\.[a-f0-9]+\.xml", command
        )
        assert remote is not None
        command = (
            command.replace(remote[0], output.as_posix())
            .replace(maintenance.UI_HELPER_PATH, asset.as_posix())
            .replace(
                f"app_process /data/local/tmp {maintenance.UI_HELPER_CLASS}",
                f"sh '{helper.as_posix()}'",
            )
        )
        result = subprocess.run(
            [shell, "-c", command], check=True, capture_output=True, text=True, timeout=3
        )
        return result.stdout

    monkeypatch.setattr(adb, "shell", execute)
    monkeypatch.setattr(maintenance, "_ensure_ui_helper", AsyncMock())
    with pytest.raises(maintenance.MaintenanceTransient) as failure:
        await maintenance._screen("unit")
    assert failure.value.code == reason
    assert not output.exists()


def test_real_java_snapshot_lock_serializes_processes_and_releases_on_kill(tmp_path):
    java_root = Path(os.environ.get("JAVA_HOME", "."))
    sdk = Path(os.environ.get("ANDROID_SDK_ROOT") or os.environ.get("ANDROID_HOME") or ".")
    suffix = ".exe" if os.name == "nt" else ""
    javac = java_root / "bin" / f"javac{suffix}"
    java = java_root / "bin" / f"java{suffix}"
    library = sdk / "platforms/android-35/android.jar"
    if not javac.is_file() or not java.is_file() or not library.is_file():
        pytest.skip("Installed JDK17 and SDK35 required for the production Java lock regression")
    source = Path(__file__).resolve().parents[1] / "tools/maintenance-ui/FreshUiSnapshot.java"
    harness = tmp_path / "SnapshotLockProbe.java"
    harness.write_text(
        """
import java.io.*;
import java.lang.reflect.*;
import java.nio.channels.*;
public class SnapshotLockProbe {
  public static void main(String[] args) throws Exception {
    Method method = Class.forName("com.dashcamstats.maintenance.FreshUiSnapshot")
      .getDeclaredMethod("acquireSnapshotLock", FileChannel.class);
    method.setAccessible(true);
    try (RandomAccessFile file = new RandomAccessFile(args[0], "rw");
         FileChannel channel = file.getChannel()) {
      try (FileLock lock = (FileLock) method.invoke(null, channel)) {
        System.out.println("locked"); System.out.flush();
        if (args.length > 1) {
          new File(args[1]).createNewFile();
          Thread.sleep(30000);
        }
      } catch (InvocationTargetException busy) {
        if (!"ui_snapshot_busy".equals(busy.getCause().getMessage())) throw busy;
        System.out.println("busy");
      }
    }
  }
}
""",
        encoding="utf-8",
    )
    subprocess.run(
        [
            str(javac),
            "--release",
            "8",
            "-cp",
            str(library),
            "-d",
            str(tmp_path),
            str(source),
            str(harness),
        ],
        check=True,
        capture_output=True,
        timeout=15,
    )
    command = [
        str(java),
        "-cp",
        os.pathsep.join((str(tmp_path), str(library))),
        "SnapshotLockProbe",
        str(tmp_path / "owner.lock"),
    ]
    ready = tmp_path / "ready"
    owner = subprocess.Popen([*command, str(ready)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and owner.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists(), owner.communicate(timeout=2)
        contender = subprocess.run(command, check=True, capture_output=True, text=True, timeout=5)
        assert contender.stdout.strip() == "busy"
    finally:
        if owner.poll() is None:
            owner.kill()
        owner.communicate(timeout=3)
    for _ in range(2):
        successor = subprocess.run(command, check=True, capture_output=True, text=True, timeout=5)
        assert successor.stdout.strip() == "locked"
