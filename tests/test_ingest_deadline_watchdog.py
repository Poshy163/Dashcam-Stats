"""A detached restore deadline must never grow with a later property rewrite."""

import subprocess

import pytest
from test_ingest_radio_unit_report import _report_config
from test_ingest_radio_unit_report import posix_shell as _posix_shell

from app.ingest import radios

posix_shell = _posix_shell


@pytest.mark.parametrize("reported", [True, False])
def test_fixed_device_ceiling_survives_property_growth_missing_reads_and_acc_change(
    posix_shell, reported
):
    report = _report_config(sleep_deadline_uptime_s=1400) if reported else None
    guard = radios._watchdog_sleep_guard_functions(
        report,
        sleep_deadline_uptime_s=None if reported else 1400,
    ).replace("/system/bin/", "")
    # Function-only shell stubs exercise the generated logic on sh and Android mksh.
    script = (
        'getprop() { printf "%s\\n" "$window_value"; }; '
        'settings() { printf "%s\\n" "$acc_value"; }; '
        'reason=lease_expired; acc_off_at=""; acc_elapsed=0; '
        + guard
        + 'window_value=1200; acc_value=0; now=1000; remaining=900; sleep_fold; echo "$remaining"; '
        'window_value=3600; now=1100; remaining=900; sleep_fold; echo "$remaining"; '
        'window_value=""; now=1200; remaining=900; sleep_fold; echo "$remaining"; '
        'window_value=3600; acc_value=1; now=1340; remaining=900; sleep_fold; echo "$remaining $reason"'
    )
    result = subprocess.run([posix_shell, "-c", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["340", "240", "140", "0 pre_sleep"]


def test_shorter_property_or_lease_still_wins(posix_shell):
    guard = radios._watchdog_sleep_guard_functions(
        _report_config(sleep_deadline_uptime_s=1400)
    ).replace("/system/bin/", "")
    script = (
        "getprop() { echo 120; }; settings() { echo 0; }; "
        'reason=lease_expired; acc_off_at=""; acc_elapsed=0; '
        + guard
        + 'now=1000; remaining=900; sleep_fold; echo "$remaining"; '
        'now=1010; remaining=10; sleep_fold; echo "$remaining"'
    )
    result = subprocess.run([posix_shell, "-c", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["60", "10"]


@pytest.mark.parametrize("value", [True, -1, "$(id)", float("nan"), 2**53])
def test_deadline_cannot_carry_shell_syntax_or_unsafe_number(value):
    with pytest.raises(ValueError):
        _report_config(sleep_deadline_uptime_s=value)
