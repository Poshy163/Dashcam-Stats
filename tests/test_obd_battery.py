"""What the adapter voltage is allowed to claim about the battery.

The trap this module exists to avoid is treating one number as one measurement. While the
engine turns, the alternator owns the bus and the reading says nothing at all about state of
charge; only once it stops does the voltage belong to the battery. Most of these tests are
about refusing to answer, or about answering with the uncertainty attached.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.obd import battery

START = datetime(2026, 9, 3, 13, 0, 0, tzinfo=UTC)


def _series(readings: list[tuple[float, float | None]], step_s: float = 5.0):
    """Build samples from ``(volts, rpm)`` pairs spaced *step_s* apart."""
    return [
        battery.VoltageSample(at=START + timedelta(seconds=i * step_s), volts=v, rpm=rpm)
        for i, (v, rpm) in enumerate(readings)
    ]


# ---------------------------------------------------------------------------------------
# The curve
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("volts", "expected"),
    [
        (12.73, 100.0),
        (12.62, 90.0),
        (12.50, 80.0),
        (12.10, 50.0),
        (11.66, 20.0),
        (11.36, 0.0),
    ],
)
def test_the_published_curve_points_are_reproduced(volts, expected):
    assert battery.state_of_charge_pct(volts) == pytest.approx(expected, abs=0.01)


def test_between_points_it_interpolates_rather_than_stepping():
    midpoint = battery.state_of_charge_pct((12.50 + 12.62) / 2)
    assert 84.0 < midpoint < 86.0


@pytest.mark.parametrize(("volts", "expected"), [(15.0, 100.0), (10.0, 0.0), (0.0, 0.0)])
def test_readings_off_the_end_of_the_curve_clamp(volts, expected):
    assert battery.state_of_charge_pct(volts) == expected


def test_it_is_monotonic_across_the_whole_range():
    steps = [battery.state_of_charge_pct(11.0 + i * 0.01) for i in range(200)]
    assert steps == sorted(steps)


# ---------------------------------------------------------------------------------------
# Refusing to answer
# ---------------------------------------------------------------------------------------


def test_no_voltage_at_all_produces_no_estimate():
    assert battery.estimate([]) is None


def test_an_engine_that_never_stopped_yields_no_state_of_charge():
    """The common case for an interrupted drive, and a real "cannot know", not a failure."""
    result = battery.estimate(_series([(13.6, 900.0), (14.0, 2200.0), (13.8, 1500.0)]))

    assert result is not None
    assert result["state_of_charge"] is None
    assert "engine off" in result["state_of_charge_unavailable_reason"]
    # The charging system is still perfectly reportable from the same readings.
    assert result["charging"]["state"] == "healthy"


def test_a_running_engine_never_contributes_to_the_charge_estimate():
    """14 V is the regulator, and a flat battery reads 14 V while being charged too."""
    charging_only = battery.estimate(_series([(14.2, 2000.0)] * 6))
    assert charging_only is not None
    assert charging_only["state_of_charge"] is None


# ---------------------------------------------------------------------------------------
# Answering, with the caveats attached
# ---------------------------------------------------------------------------------------


def test_a_rest_after_shutdown_gives_a_provisional_charge_level():
    result = battery.estimate(
        _series([(13.6, 900.0), (13.4, 800.0), (12.8, 0.0), (12.8, None), (12.7, None)])
    )

    soc = result["state_of_charge"]
    assert soc["percent"] == 97
    # The last reading, not an average of the run: surface charge decays downwards, so
    # averaging would reintroduce exactly the bias the caveat warns about.
    assert soc["resting_v"] == 12.7
    assert soc["confidence"] == "provisional"
    assert soc["engine_off_confirmed"] is True
    assert "surface charge" in soc["summary"]


def test_a_long_rest_is_reported_as_settled_without_the_caveat():
    readings = [(13.6, 900.0)] + [(12.4, 0.0)] * 400  # 400 * 5s = well past the threshold
    soc = battery.estimate(_series(readings))["state_of_charge"]

    assert soc["confidence"] == "settled"
    assert "surface charge" not in soc["summary"]
    assert soc["rest_duration_s"] >= 1800


def test_a_single_reading_says_so_rather_than_implying_a_trend():
    soc = battery.estimate(_series([(13.6, 900.0), (12.5, 0.0)]))["state_of_charge"]

    assert soc["confidence"] == "single_reading"
    assert soc["sample_count"] == 1
    assert "one reading" in soc["summary"]


def test_the_uncertainty_reflects_what_one_adapter_step_is_worth():
    """The adapter quantises to 0.1 V, which mid-curve is about seven points of charge."""
    soc = battery.estimate(_series([(13.6, 900.0), (12.10, 0.0), (12.10, 0.0)]))["state_of_charge"]

    assert 5.0 <= soc["uncertainty_pct"] <= 9.0


def test_rest_duration_covers_only_the_trailing_rest_not_the_whole_drive():
    """The bug this guards: a pre-crank reading plus a post-shutdown one is not 20 minutes
    of rest, however far apart the two timestamps are."""
    readings = [(12.5, 0.0)] + [(13.8, 1500.0)] * 200 + [(12.6, 0.0), (12.6, None)]
    soc = battery.estimate(_series(readings))["state_of_charge"]

    # Two trailing samples five seconds apart -- not the ~17 minutes the drive spanned.
    assert soc["rest_duration_s"] == pytest.approx(5.0)
    assert soc["confidence"] == "provisional"
    assert soc["resting_v"] == 12.6


def test_the_starter_motor_is_not_mistaken_for_a_flat_battery():
    """Cranking sags the bus by volts. Fed to the curve it would read as nearly dead."""
    readings = [(12.6, 0.0), (12.6, 0.0), (9.8, 0.0)] + [(13.9, 1200.0)] * 5
    result = battery.estimate(_series(readings))

    assert result["cranking_dip_v"] == 9.8
    # 9.8 V would be 0 % on the curve; the resting readings either side are what count.
    assert result["state_of_charge"]["percent"] > 80


# ---------------------------------------------------------------------------------------
# The charging system, which is the other half of the same number
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("volts", "state"),
    [(13.0, "low"), (13.1, "low"), (13.6, "healthy"), (14.4, "healthy"), (15.2, "high")],
)
def test_the_charging_verdict_tracks_the_regulation_band(volts, state):
    # Above the charging floor these are all "engine running" readings.
    readings = [(max(volts, battery.CHARGING_FLOOR_V), 1500.0)] * 5
    result = battery.estimate(_series(readings))

    if volts < battery.CHARGING_FLOOR_V:
        pytest.skip("below the floor this is not a charging reading at all")
    assert result["charging"]["state"] == state


def test_a_low_alternator_is_named_as_something_to_check():
    result = battery.estimate(_series([(13.05, 1500.0)] * 5))

    assert result["charging"]["state"] == "low"
    assert "alternator" in result["charging"]["summary"]


def test_rows_without_a_voltage_are_skipped_rather_than_defaulted():
    class Row:
        def __init__(self, v, at, rpm=None):
            self.adapter_voltage_v = v
            self.captured_at = at
            self.engine_rpm = rpm

    rows = [Row(None, START), Row(12.6, START + timedelta(seconds=5)), Row(12.7, None)]
    out = battery.samples_from_rows(rows)

    assert len(out) == 1
    assert out[0].volts == 12.6
