"""Parsing and validating the burned-in telemetry overlay.

The rule these tests exist to protect: ``E:00.0000 N:00.0000`` is the camera's *no fix*
placeholder, not a coordinate. Storing it literally would put every parked moment in the
Gulf of Guinea, stretch journey bounds across the planet, and inflate every distance.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.osd.parser import (
    OsdReading,
    enforce_monotonic,
    parse_osd_text,
)
from app.osd.validate import NO_FIX_EPSILON

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


class TestFieldsAreIndependent:
    """Each field stands on its own.

    Every line below is one this parser used to throw away whole. They come from real
    frames where something bright in the scene sat behind the left of the overlay and ate
    the date; the coordinates and speed beside it were never in any doubt. Because the
    three fields were matched as one pattern, a damaged clock discarded a clean fix, and
    long runs of a drive went missing from the map for a cosmetic reason.
    """

    @pytest.mark.parametrize(
        "line",
        [
            "202670 13:49:22 E:138.7075 N:-34.7955 61 km/h",
            "-07-28 13:49:25 E:138.7078 N:-34.7955 57 km/h",
            "2 07-28 13:49:24 E:138.7078 N:-34.7955 57 km/h",
            "8--07-28 13:49:26 E:138.7082 N:-34.7955 50 km/h",
        ],
    )
    def test_a_mangled_date_does_not_cost_the_fix(self, line):
        r = parse_osd_text(line)
        assert r.has_position is True, "coordinates were legible and must survive"
        assert r.lat == pytest.approx(-34.79, abs=0.01)
        assert r.lon == pytest.approx(138.70, abs=0.01)
        assert r.valid is True

    def test_a_damaged_separator_still_yields_everything(self):
        # One date separator decoding as ':' is cosmetic; the digits are all correct.
        r = parse_osd_text("2026-07:28 13:49:03 E:138.7035 N:-34.7956 62 km/h")
        assert r.captured_at == datetime(2026, 7, 28, 13, 49, 3)
        assert r.has_position is True
        assert r.speed_kmh == pytest.approx(62.0)

    def test_speed_survives_unreadable_coordinates(self):
        # "km/h" is a dependable anchor: four glyphs at a fixed place on the line.
        r = parse_osd_text("2026-07-28 13:49:14 /Eh38.7059 N:- 55 66 km/h")
        assert r.speed_kmh == pytest.approx(66.0)
        assert r.has_position is False

    def test_nothing_readable_is_still_not_a_crash(self):
        r = parse_osd_text("k m / h : : E N")
        assert r.valid is False
        assert r.problems and "no recognisable overlay content" in r.problems


class TestCoordinatesAreNeverInvented:
    """Guards against the three ways this parser has silently produced a wrong position.

    A missing fix costs nothing — the overlay repeats every second. A *wrong* fix drags
    journey bounds across the planet, wrecks every distance, and is plainly visible on the
    map. So each of these prefers no answer to a plausible one.
    """

    def test_a_minus_misread_as_a_dot_does_not_flip_hemisphere(self):
        # 'N:.34.7956' is equally consistent with a misread '-' and a misread ':'. Guessing
        # wrong puts an Adelaide drive in Iraq, so the reading is refused instead.
        r = parse_osd_text("2026-07-28 13:49:02 E:138.7035 N:.34.7956 62 km/h")
        assert r.has_position is False
        assert r.lat is None

    def test_an_explicit_sign_is_not_treated_as_ambiguous(self):
        r = parse_osd_text("2026-07-28 13:49:02 E:138.7035 N:-34.7956 62 km/h")
        assert r.lat == pytest.approx(-34.7956)

    @pytest.mark.parametrize(
        "line",
        [
            # The colon in 'E:' read as a minus. `-` and `:` are thin and adjacent in
            # shape and swap for each other in both directions; only one direction was
            # guarded. The separator pattern cannot hold a '-', so the stray sign is
            # absorbed by the coordinate's own optional sign and the reading lands in the
            # Pacific at full confidence with no problem recorded.
            "2026-07-28 13:49:02 E-138.7035 N:-34.7956 62 km/h",
            # The same swap on the latitude, which flips the hemisphere.
            "2026-07-28 13:49:02 E:8.5417 N-47.3769 62 km/h",
        ],
    )
    def test_a_sign_that_replaced_the_labels_colon_is_refused(self, line):
        r = parse_osd_text(line)
        assert r.has_position is False
        assert r.lat is None and r.lon is None

    @pytest.mark.parametrize(
        "line",
        [
            # The real one, off the live library: 42 recordings on 3 September 2026 read
            # this at 0.97 confidence with no problem recorded, and put a stationary car in
            # Shizuoka. Adelaide's longitude is also Japan's, so a lost latitude sign lands
            # somewhere plausible rather than somewhere absurd, and the car was parked, so
            # there was no jump for the outlier check to catch either.
            "2026-09-03 16:04:21 E:138.7044 N::34.7971 0 km/h",
            # The same fault on the longitude.
            "2026-09-03 16:04:21 E::138.7044 N:-34.7971 0 km/h",
        ],
    )
    def test_a_minus_misread_as_a_second_colon_is_refused(self, line):
        """``-`` and ``:`` swap both ways, and this is the direction nothing guarded.

        A colon in the sign position cannot be told from the label's own colon by looking
        at the last character, which is what the dot-shaped test does. Counting them can:
        the overlay prints exactly one colon per label, so a second one is standing where
        the minus should be.
        """
        r = parse_osd_text(line)
        assert r.has_position is False
        assert r.lat is None and r.lon is None

    def test_one_colon_per_label_still_reads_normally(self):
        """The guard counts colons, so it must not fire on the ordinary two-label reading."""
        r = parse_osd_text("2026-09-03 16:04:21 E:138.7044 N:-34.7971 0 km/h")
        assert r.lat == pytest.approx(-34.7971)
        assert r.lon == pytest.approx(138.7044)
        assert r.has_position is True

    def test_a_genuinely_negative_longitude_keeps_its_sign(self):
        """The western hemisphere still parses: the label's colon is beside the minus."""
        r = parse_osd_text("2026-07-28 13:49:02 E:-122.4194 N:37.7749 62 km/h")
        assert r.lon == pytest.approx(-122.4194)
        assert r.lat == pytest.approx(37.7749)

    def test_a_genuine_positive_latitude_is_kept(self):
        # Northern hemisphere: no sign printed at all, and ':' before the digits is the
        # overlay's own separator rather than a damaged minus.
        r = parse_osd_text("2026-07-28 13:49:02 E:2.3522 N:48.8566 62 km/h")
        assert r.lat == pytest.approx(48.8566)
        assert r.has_position is True

    @pytest.mark.parametrize(
        ("line", "expected_lon"),
        [
            # A misread hour let the seconds field swallow the '13' of '138', leaving a
            # longitude of 8.7067 -- off the coast of Nigeria, mid-drive.
            ("2026-07-28 m:49:19 E:138.7067 N:-34.7955 66 km/h", 138.7067),
            ("2026-08-04 15:35: :1138.6754 N:-34.8113 0 km/h", 138.6754),
        ],
    )
    def test_the_clock_cannot_eat_the_leading_digit_of_a_coordinate(self, line, expected_lon):
        r = parse_osd_text(line)
        if r.has_position:
            assert r.lon == pytest.approx(expected_lon)

    def test_seconds_are_not_spliced_onto_a_fraction(self):
        # '22:52:13 .6848' must not become a longitude of 13.6848.
        r = parse_osd_text("2026h07-30 22:52:13 .6848 N:-34.8324 0 km/h")
        assert r.lon != pytest.approx(13.6848)

    def test_a_date_is_never_read_as_a_coordinate(self):
        # With the coordinates gone, a date whose separators decoded as dots has the shape
        # of one. Inventing a position from the clock is worse than reporting none.
        r = parse_osd_text("2026.0731 15:15:03")
        assert r.has_position is False

    def test_a_surviving_date_cannot_become_a_longitude_when_the_clock_dies(self):
        """The same guard, in the one case it used to be missing.

        Coordinates are searched for from the *end* of the timestamp precisely so the date
        cannot be read as one — but that only happened when the whole timestamp matched.
        On this frame, from a car parked in a garage, the time is destroyed and the ``E:``
        field is wreckage, so the search restarted at zero and spliced the day digits onto
        what was left: ``161.0000``, a longitude in the Pacific, at 0.96 confidence.
        """
        r = parse_osd_text("2026-08-16 1 .0000 N:00.0000 0 km/h")
        assert r.has_position is False
        assert r.lon is None

    @pytest.mark.parametrize(
        ("line", "expected_lon"),
        [
            # The clock does not parse in any of these, so the date is stepped over on its
            # own -- and that must not cost the coordinates beside it. A hole in the map is
            # cheap only when it is a hole that had to be there.
            ("202670 13:49:22 E:138.7075 N:-34.7955 61 km/h", 138.7075),
            ("-07-28 13:49:25 E:138.7078 N:-34.7955 57 km/h", 138.7078),
            ("E:138.7078 N:-34.7955 57 km/h", 138.7078),
        ],
    )
    def test_stepping_over_the_date_does_not_cost_a_clean_fix(self, line, expected_lon):
        r = parse_osd_text(line)
        assert r.has_position is True
        assert r.lon == pytest.approx(expected_lon)
        assert r.lat == pytest.approx(-34.7955)


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

    @pytest.mark.parametrize(
        "line",
        [
            "2026-07-31 09:41:54 E:138:7013 N:-34:8056 15 km/h",
            "2026-07-31 09:41:54 E:h138.7013 N:-h34k8056 15 km/h",
        ],
    )
    def test_labelled_coordinates_survive_corrupt_punctuation(self, line):
        r = parse_osd_text(line)
        assert r.has_position is True
        assert r.lon == pytest.approx(138.7013)
        assert r.lat == pytest.approx(-34.8056)
        assert r.problems and any("punctuation recovered" in problem for problem in r.problems)

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
        # The clock is refused, but the fields either side of it are untouched. Fields are
        # parsed independently precisely so a bad digit in the date cannot take a good
        # coordinate down with it.
        assert r.has_fix is True
        assert (r.lat, r.lon) == (-34.8088, 138.6769)

    def test_implausible_year_is_refused(self):
        r = parse_osd_text("1026-08-04 17:44:39 E:138.6769 N:-34.8088 68 km/h")
        assert r.captured_at is None


class TestMonotonic:
    def _reading(self, second: int) -> OsdReading:
        return OsdReading(captured_at=datetime(2026, 8, 4, 17, 44, second))

    def test_backwards_clock_is_rejected_without_discarding_the_reading(self):
        # A bad clock must not take valid GPS or speed from the same line with it.
        readings = [self._reading(10), self._reading(11), self._reading(3), self._reading(12)]
        kept = enforce_monotonic(readings)
        assert len(kept) == 4
        assert [r.captured_at.second if r.captured_at else None for r in kept] == [
            10,
            11,
            None,
            12,
        ]
        assert kept[2].time_status == "rejected"

    def test_repeated_timestamps_survive(self):
        # The overlay ticks at 1 Hz, so sampling at 1 fps legitimately lands twice on the
        # same second. Dropping duplicates would throw away good samples.
        readings = [self._reading(10), self._reading(10), self._reading(11)]
        assert len(enforce_monotonic(readings)) == 3

    def test_readings_without_timestamps_pass_through(self):
        readings = [self._reading(10), OsdReading(), self._reading(11)]
        assert len(enforce_monotonic(readings)) == 3
