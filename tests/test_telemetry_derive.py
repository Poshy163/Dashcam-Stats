"""Deriving distance, heading and rollups from OCR'd fixes.

Both guards here exist because of real damage seen on a live library.

*Upper bound.* A misread coordinate that still lands inside the valid range passes every
per-point check -- ``138.6769`` read as ``13.8769`` is a legal longitude roughly 13,000 km
away. Only comparing consecutive fixes catches it, and until that existed a two-minute
clip reported a 31,768 km journey.

*Lower bound.* The overlay prints four decimal places, so a stationary car jitters by
about 11 m per sample. Integrating that noise over a parked segment invents hundreds of
metres of travel.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.osd.engine import TelemetryExtractor, TelemetryResult, TelemetrySample

BASE = datetime(2026, 8, 4, 17, 43, 53)
# Adelaide, where the corpus was recorded.
LAT, LON = -34.8088, 138.6769


def sample(index: int, lat: float | None, lon: float | None, speed: float = 60.0):
    return TelemetrySample(
        t_offset_s=float(index),
        captured_at=BASE + timedelta(seconds=index),
        lat=lat,
        lon=lon,
        has_fix=lat is not None,
        speed_kmh=speed,
        heading_deg=None,
        ocr_confidence=0.95,
        raw_text="",
    )


def derive(samples, *, min_move_m: float = 12.0) -> TelemetryResult:
    result = TelemetryResult(samples=samples)
    TelemetryExtractor._derive(result, min_move_m=min_move_m)
    return result


class TestImplausibleJumps:
    def test_a_misread_digit_does_not_become_a_journey(self):
        """The 31,768 km bug: one bad longitude in an otherwise clean track."""
        samples = [
            sample(0, LAT, LON),
            sample(1, LAT + 0.0004, LON + 0.0004),
            # 138.6769 -> 13.8769: still a legal longitude, ~11,000 km away.
            sample(2, LAT + 0.0008, 13.8769),
            sample(3, LAT + 0.0012, LON + 0.0012),
        ]
        result = derive(samples)

        assert result.implausible_jumps == 1
        assert samples[2].has_fix is False, "the bad fix must not stay plottable"
        assert samples[2].lat is None and samples[2].lon is None
        # A few hundred metres of real movement, not thousands of kilometres.
        assert result.distance_m < 1000, f"distance still inflated: {result.distance_m}"
        assert any("implausible" in w or "misread" in w for w in result.warnings)

    def test_the_anchor_survives_a_rejected_sample(self):
        """Rejecting the bad point must not strand the track on it."""
        samples = [
            sample(0, LAT, LON),
            sample(1, LAT + 0.0008, 13.8769),  # rejected
            sample(2, LAT + 0.0016, LON),  # ~180 m from sample 0
        ]
        result = derive(samples)
        assert result.implausible_jumps == 1
        assert samples[2].has_fix is True, "a good fix after a bad one was discarded"
        assert result.distance_m > 0

    def test_first_and_last_fix_exclude_rejected_points(self):
        samples = [
            sample(0, LAT, LON),
            sample(1, LAT + 0.0004, LON + 0.0004),
            sample(2, 0.0 + 5.0, 13.8769),  # rejected
        ]
        result = derive(samples)
        assert result.last_fix is not None
        assert result.last_fix.has_fix is True

    def test_fast_but_legal_travel_is_kept(self):
        """110 km/h must not be mistaken for corruption."""
        # ~30 m per second along a meridian.
        samples = [sample(i, LAT + i * 0.00027, LON) for i in range(6)]
        result = derive(samples)
        assert result.implausible_jumps == 0
        assert result.distance_m > 100


class TestJitter:
    def test_a_parked_car_travels_nowhere(self):
        """Every sample within the overlay's quantisation of the last."""
        samples = [sample(i, LAT + (i % 2) * 0.00005, LON, speed=0.0) for i in range(60)]
        result = derive(samples)
        assert result.distance_m == 0.0, f"jitter accumulated {result.distance_m} m"

    def test_headings_are_only_set_on_real_movement(self):
        samples = [sample(i, LAT + (i % 2) * 0.00005, LON, speed=0.0) for i in range(10)]
        derive(samples)
        assert all(s.heading_deg is None for s in samples)


class TestRollups:
    def test_average_speed_ignores_idle_samples(self):
        """A journey with traffic lights must not average down to walking pace."""
        moving = [sample(i, LAT + i * 0.0003, LON, speed=60.0) for i in range(10)]
        stopped = [sample(10 + i, LAT + 0.003, LON, speed=0.0) for i in range(30)]
        result = derive(moving + stopped)
        assert result.avg_speed_kmh == 60.0
        assert result.max_speed_kmh == 60.0

    def test_no_fixes_yields_no_distance(self):
        samples = [sample(i, None, None, speed=0.0) for i in range(5)]
        result = derive(samples)
        assert result.distance_m == 0.0
        assert result.first_fix is None
