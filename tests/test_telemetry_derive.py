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
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from app.osd.engine import TelemetryExtractor, TelemetryResult, TelemetrySample
from app.osd.parser import OsdReading

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


class TestTheFirstFixIsNotAutomaticallyTrusted:
    """The sequential walk's blind spot, and the bug it produced.

    The anchor starts on the first fix with nothing to check it against, so a misread
    *there* is kept and every good fix after it is rejected for disagreeing with it. On a
    real drive that produced a single stored position of ``-34.8040, -8.6845`` -- a
    plausible Adelaide latitude with the ``13`` gone from its longitude, in the South
    Atlantic -- and every detection in the recording was stamped with it.

    A median cannot be dragged by the outlier it is looking for, which is why the whole
    recording gets a say before the walk starts.
    """

    def test_a_corrupt_first_fix_is_rejected_not_enthroned(self):
        samples = [
            # 138.6845 with the leading digits lost: legal, and 11,000 km away.
            sample(0, -34.8040, -8.6845),
            sample(1, LAT, LON),
            sample(2, LAT + 0.0004, LON + 0.0004),
            sample(3, LAT + 0.0008, LON + 0.0008),
            sample(4, LAT + 0.0012, LON + 0.0012),
        ]
        result = derive(samples)

        assert samples[0].has_fix is False, "the misread first fix was kept as the anchor"
        assert samples[0].lat is None and samples[0].lon is None
        assert [s.has_fix for s in samples[1:]] == [True] * 4, (
            "good fixes were discarded for disagreeing with a corrupt starting point"
        )
        assert result.first_fix is samples[1]

    def test_a_majority_verdict_from_the_walk_is_not_acted_on(self):
        """Too few points for the median to judge, and the walk condemning most of what it
        saw. That is a bad anchor rather than a field of outliers, and throwing away the
        majority on the authority of one unchecked point is the more expensive mistake."""
        samples = [
            sample(0, -34.8040, -8.6845),
            sample(1, LAT, LON),
            sample(2, LAT + 0.0004, LON + 0.0004),
        ]
        result = derive(samples)
        kept = [s for s in samples if s.has_fix]
        assert len(kept) >= 2, "a three-point recording lost most of its positions"
        assert result.distance_m < 1000

    def test_a_genuinely_long_drive_is_left_alone(self):
        """Generic, not regional: the reference is the drive's own centre and its own
        elapsed time, so a real run in a straight line is not corruption anywhere on Earth.

        Ten minutes at about 100 km/h -- 16.7 km end to end, so the far ends sit 8.3 km
        from the centre against a radius the elapsed time puts at 10.8 km.
        """
        # 0.00025 degrees of latitude is ~27.8 m, i.e. 100 km/h at one sample per second.
        samples = [sample(i, LAT + i * 0.00025, LON) for i in range(600)]
        derive(samples)
        assert all(s.has_fix for s in samples), "a legitimate continuous drive was discarded"

    def test_a_lost_southern_minus_is_recovered_from_the_track(self):
        samples = [
            sample(0, LAT, LON),
            sample(1, LAT + 0.0002, LON + 0.0002),
            # OCR read N::34.8084: legal coordinates, but in the wrong hemisphere.
            sample(2, 34.8084, LON + 0.0004),
            sample(3, LAT + 0.0006, LON + 0.0006),
            sample(4, LAT + 0.0008, LON + 0.0008),
        ]

        result = derive(samples)

        assert samples[2].has_fix is True
        assert samples[2].lat == -34.8084
        assert samples[2].gps_status == "recovered"
        assert samples[2].gps_source == "context_repaired"
        assert result.implausible_jumps == 0


class TestNonFiniteCoordinatesNeverSurvive:
    def test_a_nan_is_stripped_before_anything_derives_from_it(self):
        """NaN passes every range check ever written -- `abs(nan) > 90` is False."""
        samples = [
            sample(0, LAT, LON),
            sample(1, float("nan"), LON),
            sample(2, LAT + 0.0004, LON + 0.0004),
        ]
        derive(samples)
        assert samples[1].has_fix is False
        assert samples[1].lat is None and samples[1].lon is None


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


class TestCandidateSelection:
    def test_a_later_failed_frame_cannot_replace_successful_fields(self):
        time = OsdReading(
            captured_at=BASE,
            raw_text="clock",
            confidence=0.91,
            time_status="valid",
        )
        gps = OsdReading(
            lat=LAT,
            lon=LON,
            has_fix=True,
            speed_kmh=61.0,
            raw_text="gps",
            confidence=0.96,
            gps_status="valid",
        )
        failed = OsdReading(
            raw_text="noise",
            confidence=0.97,
            problems=["no recognisable overlay content"],
        )

        combined = TelemetryExtractor._combine_candidates(
            5.0,
            [(5.0, time), (5.4, gps), (5.8, failed)],
            min_confidence=0.6,
        )

        assert combined.captured_at == BASE
        assert combined.has_fix is True
        assert (combined.lat, combined.lon) == (LAT, LON)
        assert combined.speed_kmh == 61.0
        assert combined.ocr_status == "valid"
        assert combined.candidate_count == 3

    def test_all_low_confidence_candidates_are_retained_but_not_trusted(self):
        reading = OsdReading(
            captured_at=BASE,
            lat=LAT,
            lon=LON,
            has_fix=True,
            confidence=0.4,
            raw_text="uncertain",
            time_status="valid",
            gps_status="valid",
        )

        combined = TelemetryExtractor._combine_candidates(0.0, [(0.0, reading)], min_confidence=0.6)

        assert combined.ocr_status == "low_confidence"
        assert combined.has_fix is False
        assert combined.raw_text == "uncertain"


class TestConservativeGapRecovery:
    def test_short_parse_gap_is_interpolated(self):
        samples = [
            sample(0, LAT, LON),
            sample(1, None, None),
            sample(2, LAT + 0.0004, LON + 0.0004),
        ]
        samples[1].gps_status = "parse_failed"

        TelemetryExtractor._recover_short_gaps(samples, max_gap_s=3.0)

        assert samples[1].has_fix is True
        assert samples[1].gps_source == "interpolated"
        assert samples[1].gps_status == "interpolated"

    def test_explicit_camera_no_fix_is_never_interpolated(self):
        samples = [
            sample(0, LAT, LON),
            sample(1, None, None),
            sample(2, LAT + 0.0004, LON + 0.0004),
        ]
        samples[1].gps_status = "no_fix"

        TelemetryExtractor._recover_short_gaps(samples, max_gap_s=3.0)

        assert samples[1].has_fix is False
        assert samples[1].gps_source == "none"

    def test_long_gap_is_not_fabricated(self):
        samples = [sample(0, LAT, LON), sample(5, None, None), sample(10, LAT, LON + 0.01)]
        samples[1].gps_status = "parse_failed"

        TelemetryExtractor._recover_short_gaps(samples, max_gap_s=3.0)

        assert samples[1].has_fix is False

    def test_a_gap_within_the_configured_limit_is_interpolated(self):
        samples = [sample(0, LAT, LON), sample(5, None, None), sample(10, LAT, LON + 0.001)]
        samples[1].gps_status = "parse_failed"

        TelemetryExtractor._recover_short_gaps(samples, max_gap_s=15.0)

        assert samples[1].has_fix is True
        assert samples[1].gps_source == "interpolated"


def test_clock_origin_subtracts_the_video_offset():
    reading = sample(5, LAT, LON)
    assert TelemetryExtractor._clock_origin([reading], Path("20260804174353_camera_0.ts")) == BASE


async def test_extraction_keeps_failed_seconds_and_best_success(monkeypatch):
    import app.osd.engine as engine_module

    extractor = TelemetryExtractor(templates=object())  # type: ignore[arg-type]

    async def strips(*args, **kwargs):
        for offset in (0.0, 0.5, 1.0, 1.5):
            yield offset, np.zeros((50, 1920), dtype=np.uint8)

    texts = iter(
        [
            "2026-08-04 17:43:53 E:138.6769 N:-34.8088 61 km/h",
            "unreadable",
            "unreadable",
            "also unreadable",
        ]
    )

    async def fake_probe(path):
        return SimpleNamespace(width=1920, height=1080)

    monkeypatch.setattr(extractor, "_iter_strips", strips)
    monkeypatch.setattr(engine_module, "probe", fake_probe)
    monkeypatch.setattr(engine_module, "decode_line", lambda mask, templates: (next(texts), 0.95))

    result = await extractor.extract(
        Path("20260804174353_camera_0.ts"),
        region=SimpleNamespace(),
        sample_fps=1.0,
    )

    assert len(result.samples) == 2, "one failed OCR second disappeared from the timeline"
    assert result.samples[0].has_fix is True, "the later failure replaced a valid candidate"
    assert result.samples[1].ocr_status == "failed"
    assert result.samples[1].raw_text, "failed OCR evidence was not retained"
