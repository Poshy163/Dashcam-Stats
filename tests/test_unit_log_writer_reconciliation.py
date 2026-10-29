"""Execute the writer reconciliation against isolated properties, never a device."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from app.ingest import unit_logs


class Settings:
    def __init__(self, allowed="", denied="", errors=False):
        self.values = {
            unit_logs.ALLOW_KEY: allowed,
            unit_logs.DENY_KEY: denied,
            unit_logs.WINDOW_MANAGER_ERRORS_KEY: errors,
        }

    def get_nowait(self, key, default=None):
        return self.values.get(key, default)


def receipt(body: str) -> str:
    data = body.encode("ascii")
    return (
        f"present 1:2:2000:600:1:{len(data)} {hashlib.sha256(data).hexdigest()}\n"
        f"{body}{unit_logs._LEVEL_READ_COMPLETE}\n"
    )


def test_old_persisted_defaults_cannot_reenable_window_manager_errors(monkeypatch):
    monkeypatch.setattr(
        unit_logs, "get_settings_service", lambda: Settings(",".join(unit_logs.LEGACY_ALLOW_TAGS))
    )
    levels = unit_logs.writer_levels()
    assert levels["WindowManager"] == "F"
    for tag in (
        "AndroidRuntime",
        "DEBUG",
        "ActivityManager",
        "ThermalManagerService",
        "lowmemorykiller",
        "UnisocWatchdog",
        "ZQC-CamSubStream0",
        "ZQC-CamSubStream1",
    ):
        assert levels[tag] == "E"
    assert "WindowManager:F" in unit_logs.capture_command()


@pytest.mark.parametrize("opt_in", [False, "true", "false", 1, None])
def test_window_manager_errors_require_an_explicit_boolean(monkeypatch, opt_in):
    monkeypatch.setattr(unit_logs, "get_settings_service", lambda: Settings(errors=opt_in))
    assert unit_logs.writer_levels()["WindowManager"] == "F"


def test_diagnostic_opt_in_and_deny_precedence_match_the_reader(monkeypatch):
    monkeypatch.setattr(unit_logs, "get_settings_service", lambda: Settings(errors=True))
    assert unit_logs.writer_levels()["WindowManager"] == "E"
    assert "WindowManager:E" in unit_logs.capture_command()
    monkeypatch.setattr(
        unit_logs, "get_settings_service", lambda: Settings("Custom", "Custom,WindowManager", True)
    )
    assert unit_logs.writer_levels() == {"Custom": "S", "WindowManager": "S"}
    assert "WindowManager:S" in unit_logs.capture_command()
    assert "WindowManager:E" not in unit_logs.capture_command()


@pytest.mark.parametrize(
    "tag",
    [
        "Foo:S",
        "$(evil)",
        "x;echo bad",
        "x\nmalicious",
        "x y",
        "x/../y",
        "Foo+Bar",
        ".Leading",
        "Trailing.",
        "Consecutive..Dots",
        "x" * 57,
    ],
)
def test_unsafe_configured_tags_never_enter_writer_commands(monkeypatch, tag):
    monkeypatch.setattr(
        unit_logs, "get_settings_service", lambda: Settings(f"Good,{tag}", "Denied")
    )
    assert unit_logs.writer_levels() == {"Good": "E", "WindowManager": "F", "Denied": "S"}


def test_valid_property_and_filter_tags_keep_vendor_punctuation_and_length_bound(monkeypatch):
    vendor = "vendor.sprd.modules.thm@2.0-impl"
    boundary = "x" * 56
    monkeypatch.setattr(
        unit_logs,
        "get_settings_service",
        lambda: Settings(f"{vendor},{boundary},Under_score", "Denied"),
    )
    levels = unit_logs.writer_levels()
    assert levels[vendor] == levels[boundary] == levels["Under_score"] == "E"
    assert len("log.tag." + boundary) == 64


@pytest.mark.parametrize(
    "records",
    [
        "Bad;tag - - E\n",
        "Tag - - E\nTag - - E\n",
        "Tag - - E extra\n",
        "Tag - - $(bad)\n",
        "Tag - - E\x00\n",
        "Tag:S - - E\n",
    ],
)
def test_receipt_validation_rejects_every_bad_record_before_mutation(records):
    with pytest.raises(ValueError):
        unit_logs._parse_level_receipt(receipt(unit_logs._LEVEL_HEADER + "\n" + records))


def test_receipt_limits_hash_and_framing_are_checked():
    body = unit_logs._LEVEL_HEADER + "\n" + "".join(f"Tag{i} - E E\n" for i in range(65))
    with pytest.raises(ValueError, match="too many"):
        unit_logs._parse_level_receipt(receipt(body))
    good = receipt(unit_logs._LEVEL_HEADER + "\nTag - E E\n")
    for damaged in (
        good.replace("Tag - E E", "Tag - F F"),
        good[:-2],
        good.replace(":600:", ":666:"),
    ):
        with pytest.raises(ValueError):
            unit_logs._parse_level_receipt(damaged)


def test_excessive_policy_is_refused_instead_of_partially_applied(monkeypatch):
    monkeypatch.setattr(
        unit_logs, "get_settings_service", lambda: Settings(",".join(f"Tag{i}" for i in range(65)))
    )
    with pytest.raises(ValueError, match="too many"):
        unit_logs.writer_levels()


async def test_bad_receipt_or_unverified_apply_never_reports_logging_ready(monkeypatch):
    calls = []
    monkeypatch.setattr(unit_logs, "get_settings_service", lambda: Settings())

    async def corrupt(address, command, **kwargs):
        calls.append(command)
        return "present malformed\n"

    monkeypatch.setattr(unit_logs.adb, "shell", corrupt)
    assert not await unit_logs.ensure_logging("unit")
    assert len(calls) == 1

    async def unverified(address, command, **kwargs):
        calls.append(command)
        if unit_logs._LEVEL_READ_COMPLETE in command:
            return f"absent\n{unit_logs._LEVEL_READ_COMPLETE}\n"
        return "running"

    calls.clear()
    monkeypatch.setattr(unit_logs.adb, "shell", unverified)
    assert not await unit_logs.ensure_logging("unit")
    assert len(calls) == 2


class PropertyHarness:
    def __init__(self, directory, shell):
        self.directory = directory
        self.shell = shell
        self.properties = directory / "properties"
        self.properties.mkdir()
        self.bin = directory / "bin"
        self.bin.mkdir()
        scripts = {
            "getprop": '#!/bin/sh\nif [ -f "$PROP_ROOT/$1" ]; then cat "$PROP_ROOT/$1"; fi\n',
            "setprop": """#!/bin/sh
if [ "$1" = "$FAIL_PROPERTY" ]; then
  [ "$SILENT_FAILURE" = 1 ] && exit 0
  exit 1
fi
printf '%s' "$2" > "$PROP_ROOT/$1" || exit 1
if [ "$1" = ctl.start ] && [ "$2" = logd ]; then
  printf running > "$PROP_ROOT/init.svc.logd"
fi
""",
        }
        for name, source in scripts.items():
            file = self.bin / name
            file.write_text(source)
            file.chmod(0o700)
        self.environment = dict(
            os.environ,
            PROP_ROOT=str(self.properties),
            PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
        )

    def put(self, tag, level):
        (self.properties / f"log.tag.{tag}").write_text(level)

    def get(self, tag):
        file = self.properties / f"log.tag.{tag}"
        return file.read_text() if file.exists() else ""

    def run(self, command):
        return subprocess.run(
            [self.shell, "-c", command],
            text=True,
            capture_output=True,
            env=self.environment,
            timeout=18,
        )

    def snapshot(self):
        result = self.run(unit_logs._level_receipt_read_command())
        assert result.returncode == 0, result.stderr
        return unit_logs._parse_level_receipt(result.stdout)

    def apply(self, levels):
        token, previous = self.snapshot()
        return self.run(unit_logs._level_setup_command(levels, token, previous))


@pytest.fixture(params=["sh", "mksh"])
def properties(tmp_path, monkeypatch, request):
    if os.name == "nt":
        pytest.skip("production shell descriptor/proc contracts require Linux")
    shell = shutil.which(request.param)
    if not shell:
        pytest.skip(f"{request.param} unavailable")
    monkeypatch.setattr(unit_logs, "REMOTE_LEVELS", str(tmp_path / "levels"))
    monkeypatch.setattr(unit_logs, "REMOTE_LEVEL_GUARD", str(tmp_path / "guard"))
    return PropertyHarness(tmp_path, shell)


def test_writer_policy_migrates_legacy_defaults_and_preserves_unmanaged_tags(properties):
    properties.put("WindowManager", "E")
    properties.put("ActivityManager", "E")
    properties.put("UnrelatedApp", "D")
    result = properties.apply({"WindowManager": "F", "AndroidRuntime": "E", "Noise": "S"})
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == unit_logs._LEVEL_APPLIED
    assert properties.get("WindowManager") == "F"
    assert properties.get("ActivityManager") == ""
    assert properties.get("UnrelatedApp") == "D"
    assert properties.get("Noise") == "S"
    assert properties.get("AndroidRuntime") == "E"
    assert (properties.properties / "init.svc.logd").read_text() == "running"
    _, saved = properties.snapshot()
    assert saved["WindowManager"].original == "-"


def test_restart_then_remove_custom_tag_restores_its_original_override(properties):
    properties.put("Custom", "W")
    assert properties.apply({"Custom": "E", "WindowManager": "F"}).returncode == 0
    # No module/process ownership cache participates: a new snapshot reads the device receipt.
    assert properties.apply({"WindowManager": "F"}).returncode == 0
    assert properties.get("Custom") == "W"
    assert "Custom" not in properties.snapshot()[1]


def test_removal_preserves_a_subsequent_external_property_change(properties):
    assert properties.apply({"Custom": "E"}).returncode == 0
    properties.put("Custom", "D")
    assert properties.apply({"WindowManager": "F"}).returncode == 0
    assert properties.get("Custom") == "D"
    assert "Custom" not in properties.snapshot()[1]


@pytest.mark.parametrize("silent", [False, True])
def test_partial_setprop_failure_retains_recovery_receipt_and_never_claims_success(
    properties, silent
):
    properties.environment.update(
        FAIL_PROPERTY="log.tag.ZFailed", SILENT_FAILURE="1" if silent else "0"
    )
    result = properties.apply({"AFirst": "E", "ZFailed": "E"})
    assert result.returncode != 0
    assert unit_logs._LEVEL_APPLIED not in result.stdout
    assert properties.get("AFirst") == "E"
    assert "AFirst" in properties.snapshot()[1]
    properties.environment.pop("FAIL_PROPERTY")
    assert properties.apply({"WindowManager": "F"}).returncode == 0
    assert properties.get("AFirst") == ""
    assert properties.get("ZFailed") == ""


def test_receipt_change_between_snapshot_and_apply_fails_before_property_mutation(properties):
    token, prior = properties.snapshot()
    Path(unit_logs.REMOTE_LEVELS).write_text(unit_logs._LEVEL_HEADER + "\nInjected - E E\n")
    Path(unit_logs.REMOTE_LEVELS).chmod(0o600)
    result = properties.run(unit_logs._level_setup_command({"AndroidRuntime": "E"}, token, prior))
    assert result.returncode != 0
    assert properties.get("AndroidRuntime") == ""
    assert not (properties.properties / "persist.log.tag").exists()


@pytest.mark.parametrize("unsafe", ["symlink", "fifo", "hardlink", "mode"])
def test_unsafe_receipt_cannot_trigger_any_property_change(properties, unsafe):
    receipt_path = Path(unit_logs.REMOTE_LEVELS)
    other = properties.directory / "other"
    other.write_text(unit_logs._LEVEL_HEADER + "\nCustom - E E\n")
    other.chmod(0o600)
    if unsafe == "symlink":
        receipt_path.symlink_to(other)
    elif unsafe == "fifo":
        os.mkfifo(receipt_path)
    elif unsafe == "hardlink":
        os.link(other, receipt_path)
    else:
        receipt_path.write_text(other.read_text())
        receipt_path.chmod(0o666)
    result = properties.run(unit_logs._level_receipt_read_command())
    assert result.returncode != 0
    assert properties.get("Custom") == ""
    assert not (properties.properties / "persist.log.tag").exists()


def test_stable_guard_keeps_its_inode(properties):
    assert properties.apply({"Custom": "E"}).returncode == 0
    guard = Path(unit_logs.REMOTE_LEVEL_GUARD)
    before = guard.stat().st_ino
    assert properties.apply({"WindowManager": "F"}).returncode == 0
    assert guard.stat().st_ino == before


def test_busy_guard_prevents_concurrent_property_mutation(properties):
    import fcntl

    assert properties.apply({"Custom": "E"}).returncode == 0
    guard = Path(unit_logs.REMOTE_LEVEL_GUARD)
    with guard.open("r") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = properties.apply({"Custom": "S"})
    assert result.returncode != 0
    assert properties.get("Custom") == "E"
