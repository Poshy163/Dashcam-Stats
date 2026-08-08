"""Parsing and validating the burned-in telemetry overlay.

The rule these tests exist to protect: ``E:00.0000 N:00.0000`` is the camera's *no fix*
placeholder, not a coordinate. Storing it literally would put every parked moment in the
Gulf of Guinea, stretch journey bounds across the planet, and inflate every distance.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.osd.parser import (
    NO_FIX_EPSILON,
    OsdReading,
    enforce_monotonic,
    parse_osd_text,
)

# Exactly as decoded from real frames, including the wide inter-field spacing.
REAL_LINE = "2026-08-04 17:44:39   E:138.6769 N:-34.8088  68 km/h"
REAL_NOFIX = "2026-08-05 15:04:09   E:00.0000 N:00.0000  0 km/h"


class TestHappyPath:
    def test_parses_a_real_overlay_line(self):
        r = parse_osd_text(REAL_LINE, confidence=0.97)
        assert r.valid
        assert r.captured_at == datetime(2026, 8, 4, 17, 44, 39)
        # "E:" is longitude and "N:" is latitude -- printed in that order, which is the
        # opposite of the usual lat/lon convention and easy to transpose.
        assert r.lon == pytest.approx(138.6769)
        assert r.lat == pytest.approx(-34.8088)
        assert r.has_fix is True
        assert r.has_position is True
        assert r.speed_kmh == pytest.approx(68.0)
        assert r.confidence == pytest.approx(0.97)

    def test_southern_hemisphere_stays_negative(self):
        r = parse_osd_text(REAL_LINE)
        assert r.lat is not None and r.lat < 0, "Adelaide must not end up in the north"

    def test_raw_text_is_preserved(self):
        r = parse_osd_text(REAL_LINE)
        assert r.raw_text == REAL_LINE


class TestNoFix:
    def test_zero_coordinates_mean_no_fix_not_null_island(self):
        r = parse_osd_text(REAL_NOFIX)
        assert r.valid
        assert r.has_fix is False
        assert r.has_position is False
        # The critical assertion: nothing may be storable as a coordinate.
        assert r.lat is None
        assert r.lon is None

    def test_timestamp_still_recovered_without_a_fix(self):
        # A parked segment has no GPS but its clock is still the best source of true time.
        r = parse_osd_text(REAL_NOFIX)
        assert r.captured_at == datetime(2026, 8, 5, 15, 4, 9)
        assert r.speed_kmh == pytest.approx(0.0)

    def test_near_zero_within_epsilon_is_also_no_fix(self):
        r = parse_osd_text("2026-08-05 15:04:09 E:0.0000 N:0.0000 0 km/h")
        assert r.has_fix is False
        assert NO_FIX_EPSILON > 0


class TestOcrTolerance:
    def test_space_inside_a_coordinate_is_tolerated(self):
        # Glyph segmentation infers spaces from pixel gaps; a slightly wide gap inside a
        # number must not cost the whole reading.
        r = parse_osd_text("2026-08-04 17:44:39 E:138. 6769 N:-34.8088 68 km/h")
        assert r.has_fix is True
        assert r.lon == pytest.approx(138.6769)

    def test_missing_unit_suffix_still_parses(self):
        r = parse_osd_text("2026-08-04 17:44:39 E:138.6769 N:-34.8088 68")
        assert r.speed_kmh == pytest.approx(68.0)

    def test_field_order_carries_meaning_when_literals_are_misread(self):
        # 'E' and 'N' are rare glyphs; the parser keys on order so one misread letter does
        # not discard an otherwise perfect line.
        r = parse_osd_text("2026-08-04 17:44:39 ?:138.6769 ?:-34.8088 68 km/h")
        assert r.has_fix is True
        assert r.lon == pytest.approx(138.6769)
        assert r.lat == pytest.approx(-34.8088)

    def test_timestamp_only_fallback(self):
        r = parse_osd_text("2026-08-04 17:44:39 garbage garbage")
        assert r.valid
        assert r.captured_at == datetime(2026, 8, 4, 17, 44, 39)
        assert r.has_fix is False

    def test_unreadable_line_is_invalid_but_does_not_raise(self):
        r = parse_osd_text("############")
        assert r.valid is False
        assert r.problems


class TestValidation:
    def test_out_of_range_coordinates_are_rejected(self):
        r = parse_osd_text("2026-08-04 17:44:39 E:998.6769 N:-94.8088 68 km/h")
        assert r.has_fix is False
        assert r.lat is None and r.lon is None

    def test_implausible_speed_is_dropped_but_position_kept(self):
        r = parse_osd_text(
            "2026-08-04 17:44:39 E:138.6769 N:-34.8088 999 km/h", max_speed_kmh=300.0
        )
        assert r.speed_kmh is None
        assert r.has_fix is True

    @pytest.mark.parametrize(
        "bad",
        [
            "2026-13-04 17:44:39 E:138.6769 N:-34.8088 68 km/h",  # month 13
            "2026-08-32 17:44:39 E:138.6769 N:-34.8088 68 km/h",  # day 32
            "2026-08-04 25:44:39 E:138.6769 N:-34.8088 68 km/h",  # hour 25
        ],
    )
    def test_impossible_datetimes_are_refused(self, bad):
        r = parse_osd_text(bad)
        assert r.captured_at is None
        assert r.valid is False

    def test_implausible_year_is_refused(self):
        r = parse_osd_text("1026-08-04 17:44:39 E:138.6769 N:-34.8088 68 km/h")
        assert r.captured_at is None


class TestMonotonic:
    def _reading(self, second: int) -> OsdReading:
        return OsdReading(captured_at=datetime(2026, 8, 4, 17, 44, second))

    def test_backwards_timestamps_are_discarded(self):
        # Time only moves forward inside one segment; a backwards jump is a misread digit
        # and is cheaper to drop than to let it reorder a journey.
        readings = [self._reading(10), self._reading(11), self._reading(3), self._reading(12)]
        kept = enforce_monotonic(readings)
        assert [r.captured_at.second for r in kept] == [10, 11, 12]

    def test_repeated_timestamps_survive(self):
        # The overlay ticks at 1 Hz, so sampling at 1 fps legitimately lands twice on the
        # same second. Dropping duplicates would throw away good samples.
        readings = [self._reading(10), self._reading(10), self._reading(11)]
        assert len(enforce_monotonic(readings)) == 3

    def test_readings_without_timestamps_pass_through(self):
        readings = [self._reading(10), OsdReading(), self._reading(11)]
        assert len(enforce_monotonic(readings)) == 3
