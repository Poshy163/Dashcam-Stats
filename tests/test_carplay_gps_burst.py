"""Run the production GPS cadence state machine with elapsed-clock observations."""

import shutil
import subprocess
from pathlib import Path

import pytest

SOURCE = (Path(__file__).resolve().parents[1] / "backend/app/ingest/carplay_timing.sh").read_text(
    encoding="utf-8"
)
PROGRAM = SOURCE.split("# BEGIN_GPS_BURST_AWK\n", 1)[1].split("# END_GPS_BURST_AWK", 1)[0]


class Probe:
    def __init__(self):
        self.saved = ""
        self.previous_start = None

    def read(
        self,
        now,
        *,
        fix=None,
        acc=1,
        started=1,
        enabled=1,
        status="ok",
        cost=300,
        gap=None,
    ):
        awk = shutil.which("awk") or r"C:\Program Files\Git\usr\bin\awk.exe"
        if not Path(awk).is_file():
            pytest.skip("awk unavailable")
        start = now - cost
        if gap is None:
            gap = "na" if self.previous_start is None else start - self.previous_start
        self.previous_start = start
        fields = (
            f"gps_capture_status={status} gps_started={started} loc_gps_enabled={enabled} "
            f"loc_gps_fix_elapsed_ms={fix if fix is not None else 'na'}"
        )
        args = [awk]
        for name, value in {
            "saved": self.saved,
            "now": now,
            "poll_start": start,
            "gap": gap,
            "cost": cost,
            "acc": acc,
        }.items():
            args.extend(["-v", f"{name}={value}"])
        result = subprocess.run(
            [*args, PROGRAM], input=fields, text=True, capture_output=True, timeout=5, check=True
        )
        assert not result.stderr
        self.saved, output = result.stdout.strip().split("|", 1)
        row = dict(token.split("=", 1) for token in output.split())
        assert row["gps_next_interval_ms"] in {"5000", "15000"}
        return row


def test_burst_requires_known_ignition_and_successful_fast_capture():
    probe = Probe()
    for now, acc, status, cost in (
        (100_000, 0, "ok", 300),
        (115_000, "na", "ok", 300),
        (130_000, 1, "dump_error", 300),
        (145_000, 1, "ok", 1000),
    ):
        row = probe.read(now, acc=acc, status=status, cost=cost)
        assert row["gps_next_interval_ms"] == "15000"
        assert row["gps_burst_start_ms"] == "na"
    row = probe.read(160_000)
    assert row["gps_next_interval_ms"] == "5000"
    assert row["gps_burst_reason"] == "startup_on"
    assert row["gps_burst_start_ms"] == "159700"


def test_two_advancing_fresh_fixes_end_burst_but_first_fix_is_only_a_baseline():
    probe = Probe()
    assert probe.read(100_000)["gps_fix_advanced"] == "na"
    first = probe.read(105_000, fix=104_000)
    assert first["gps_fix_advanced"] == "na"
    assert first["gps_next_interval_ms"] == "5000"
    assert probe.read(110_000, fix=109_000)["gps_next_interval_ms"] == "5000"
    end = probe.read(115_000, fix=114_000)
    assert end["gps_next_interval_ms"] == "15000"
    assert end["gps_burst_end"] == "fresh_progress"
    assert end["gps_burst_reason"] == "startup_on"
    assert end["gps_burst_elapsed_ms"] == "15300"
    baseline = probe.read(130_000, fix=129_000)
    assert baseline["gps_burst_reason"] == "none"
    assert baseline["gps_burst_start_ms"] == "na"


def test_equal_missing_or_regressing_fix_resets_consecutive_progress():
    probe = Probe()
    probe.read(100_000, fix=99_000)
    assert probe.read(102_000, fix=101_000)["gps_fix_advanced"] == "1"
    repeat = probe.read(104_000, fix=101_000)
    assert repeat["gps_fix_advanced"] == "0"
    assert repeat["gps_next_interval_ms"] == "5000"
    probe.read(106_000, fix=105_000)
    missing = probe.read(108_000)
    assert missing["gps_fix_advanced"] == "na"
    assert missing["gps_last_known_fix_age_ms"] == "3000"
    probe.read(110_000, fix=109_000)
    regressed = probe.read(112_000, fix=108_000)
    assert regressed["gps_fix_advanced"] == "na"
    assert regressed["gps_last_known_fix_age_ms"] == "3000"
    assert probe.read(114_000, fix=113_000)["gps_next_interval_ms"] == "5000"
    assert probe.read(116_000, fix=115_000)["gps_burst_end"] == "fresh_progress"


@pytest.mark.parametrize(
    "values,ended",
    [
        ({"status": "dump_error"}, "read_failed"),
        ({"status": "unsupported"}, "read_failed"),
        ({"cost": 1000}, "slow_read"),
        ({"cost": 10_000}, "slow_read"),
        ({"acc": 0}, "ignition_off"),
        ({"acc": "na"}, "ignition_unknown"),
    ],
)
def test_unhealthy_read_or_ignition_ends_episode_without_restarting_next_pass(values, ended):
    probe = Probe()
    probe.read(100_000, fix=99_000)
    row = probe.read(115_000, **values)
    assert row["gps_next_interval_ms"] == "15000"
    assert row["gps_burst_end"] == ended
    assert row["gps_last_known_fix_age_ms"] == "16000"
    assert probe.read(130_000)["gps_next_interval_ms"] == "15000"


def test_persistent_missing_fix_is_bounded_and_does_not_restart_after_cooldown():
    probe = Probe()
    fast = []
    row = probe.read(100_000)
    fast.append(row)
    for now in range(105_000, 190_000, 5000):
        row = probe.read(now)
        if row["gps_next_interval_ms"] == "5000":
            fast.append(row)
    assert len(fast) == 17  # No next five-second read is scheduled at/after 90 seconds.
    assert row["gps_burst_end"] == "budget"
    assert int(row["gps_burst_elapsed_ms"]) < 90_000
    for now in range(200_000, 650_001, 15_000):
        row = probe.read(now)
        assert row["gps_next_interval_ms"] == "15000"
        assert row["gps_burst_reason"] == "none"


def test_fresh_to_stale_edge_waits_for_cooldown_and_is_not_retried_indefinitely():
    probe = Probe()
    probe.read(100_000, fix=99_000)
    probe.read(105_000, fix=104_000)
    probe.read(110_000, fix=109_000)
    # First stale edge is inside cooldown and is consumed, not queued.
    assert probe.read(125_000, fix=109_000)["gps_next_interval_ms"] == "15000"
    assert probe.read(410_000, fix=109_000, gap=15_000)["gps_next_interval_ms"] == "15000"
    probe.read(425_000, fix=424_000)
    # The fresh-to-stale edge survives the 5-to-10-second gray zone.
    assert probe.read(431_000, fix=424_000)["gps_next_interval_ms"] == "15000"
    row = probe.read(434_000, fix=424_000)
    assert row["gps_burst_reason"] == "fix_stale"
    assert row["gps_next_interval_ms"] == "5000"


@pytest.mark.parametrize("enabled,started", [(0, 1), (1, 0), ("na", 1), (1, "na")])
def test_provider_must_be_enabled_and_started_for_staleness_or_recovery(enabled, started):
    probe = Probe()
    probe.read(100_000, fix=99_000)
    probe.read(105_000, fix=104_000)
    assert probe.read(110_000, fix=109_000)["gps_burst_end"] == "fresh_progress"
    row = probe.read(410_000, fix=109_000, enabled=enabled, started=started, gap=15_000)
    assert row["gps_next_interval_ms"] == "15000"


@pytest.mark.parametrize(
    "edge,expected", [("ignition", "ignition_on"), ("receiver", "receiver_start")]
)
def test_observed_edges_can_start_later_episode_after_cooldown(edge, expected):
    probe = Probe()
    probe.read(100_000, fix=99_000)
    probe.read(105_000, fix=104_000)
    probe.read(110_000, fix=109_000)
    probe.read(400_000, acc=0 if edge == "ignition" else 1, started=0, gap=15_000)
    row = probe.read(415_000)
    assert row["gps_burst_reason"] == expected
    assert row["gps_next_interval_ms"] == "5000"


def test_gap_discards_progress_and_retains_only_explicit_old_fix_age():
    probe = Probe()
    probe.read(100_000, fix=99_000)
    probe.read(105_000, fix=104_000)
    row = probe.read(170_000, fix=169_000)
    assert row["gps_fix_advanced"] == "na"
    assert row["gps_next_interval_ms"] == "15000"  # Original cooldown survives sleep.
    row = probe.read(410_000)
    assert row["gps_burst_reason"] == "poll_gap"
    assert row["gps_last_known_fix_age_ms"] == "241000"
    assert row["gps_fix_advanced"] == "na"


def test_clock_reversal_forgets_old_fix_and_cooldown():
    probe = Probe()
    probe.read(100_000, fix=99_000)
    row = probe.read(10_000)
    assert row["gps_last_known_fix_age_ms"] == "na"
    assert row["gps_fix_advanced"] == "na"
    assert row["gps_burst_reason"] == "startup_on"
    assert row["gps_burst_start_ms"] == "9700"


def test_future_fix_is_unavailable_and_cannot_establish_progress():
    probe = Probe()
    row = probe.read(100_000, fix=101_000)
    assert row["gps_last_known_fix_age_ms"] == "na"
    assert row["gps_fix_advanced"] == "na"
    row = probe.read(105_000, fix=106_000)
    assert row["gps_burst_end"] == "none"


def test_freshness_and_poll_gap_boundaries_are_explicit():
    probe = Probe()
    probe.read(100_000, fix=99_000)
    assert probe.read(105_000, fix=100_000)["gps_burst_end"] == "none"  # Exactly 5 s.
    assert probe.read(110_000, fix=104_999)["gps_burst_end"] == "none"  # Too old.
    assert probe.read(115_000, fix=110_000)["gps_burst_end"] == "none"
    assert probe.read(120_000, fix=115_000)["gps_burst_end"] == "fresh_progress"
    # At 45 s, comparison is retained; beyond 45 s it is discarded even if fresh.
    assert probe.read(165_000, fix=164_000)["gps_fix_advanced"] == "1"
    assert probe.read(210_001, fix=209_000)["gps_fix_advanced"] == "na"


@pytest.mark.parametrize("enabled,started", [(0, 1), (1, 0)])
def test_fresh_looking_cached_fix_cannot_end_burst_without_active_provider(enabled, started):
    probe = Probe()
    probe.read(100_000, fix=99_000)
    for now in (105_000, 110_000):
        row = probe.read(now, fix=now - 1000, enabled=enabled, started=started)
        assert row["gps_fix_advanced"] == "1"
        assert row["gps_burst_end"] == "none"
        assert row["gps_next_interval_ms"] == "5000"
