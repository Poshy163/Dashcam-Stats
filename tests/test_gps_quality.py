"""GPS plausibility: which positions are believed, and where a route is broken.

Every case here comes from the live 995-recording library, where the heat map had grown a
spray of long straight lines through suburbs, reserves and an airport. The cause was not one
bug but a gap between two checks that each looked reasonable alone:

* consecutive steps were allowed up to 400 km/h, which at the overlay's 1 Hz sampling
  licenses a 111 m jump every second;
* the journey-level check had a 5 km floor on its rejection radius.

Nothing at all examined a coordinate between roughly 100 m and 5 km out of place — which is
precisely the range one misread digit produces. ``E:138.6510`` losing three digits to a
pixel gap reads as ``E:138.6``: a legal longitude, 4.7 km west, at 0.94 confidence.

The controls matter as much as the failures. Recording 250 in that library is sustained
110 km/h motorway driving whose 1 Hz steps read as high as 169 km/h purely from the
overlay's 11 m quantisation, and a threshold tight enough to catch the corruption above
must still leave every one of those fixes alone.
"""

from __future__ import annotations

import math

import pytest

from app.osd.parser import parse_osd_text
from app.osd.reasons import GpsQuality, GpsReason
from app.osd.track_quality import (
    MAX_GAP_M,
    Fix,
    classify,
    reachable_radius_m,
    segments,
)

# Adelaide, where the corpus was recorded.
LAT, LON = -34.8088, 138.6769

#: Degrees of latitude per metre, near enough at this latitude for building fixtures.
_DEG_PER_M = 1.0 / 111_320.0


def straight_run(count: int, *, speed_kmh: float = 60.0, start_t: float = 0.0) -> list[Fix]:
    """A vehicle driving due north at a constant speed, sampled once per second."""
    step = speed_kmh / 3.6 * _DEG_PER_M
    return [Fix(t_s=start_t + i, lat=LAT + i * step, lon=LON) for i in range(count)]


def qualities(fixes: list[Fix]) -> list[GpsQuality]:
    return [v.quality for v in classify(fixes)]


class TestNormalDriving:
    """Case 1: a continuous route must come through completely untouched."""

    def test_a_clean_urban_route_keeps_every_fix(self):
        fixes = straight_run(60, speed_kmh=50.0)
        assert qualities(fixes) == [GpsQuality.VALID] * 60

    def test_a_clean_route_is_one_unbroken_segment(self):
        verdicts = classify(straight_run(60, speed_kmh=50.0))
        assert len(segments(verdicts)) == 1

    def test_a_stationary_vehicle_is_not_mistaken_for_damage(self):
        """The overlay samples once a second whether or not the car is moving."""
        fixes = [Fix(t_s=float(i), lat=LAT, lon=LON) for i in range(40)]
        assert qualities(fixes) == [GpsQuality.VALID] * 40


class TestLegitimateFastDriving:
    """Case 7: motorway speeds must survive, quantisation noise and all."""

    def test_110_kmh_is_kept(self):
        fixes = straight_run(60, speed_kmh=110.0)
        assert qualities(fixes) == [GpsQuality.VALID] * 60

    def test_110_kmh_with_overlay_quantisation_is_kept(self):
        """The real recording-250 case.

        Coordinates are printed to four decimal places, so consecutive fixes snap to an
        11 m grid and a steady 30 m/s reads as alternating hops of roughly 47 m and 20 m —
        an apparent 169 km/h. Rejecting that would delete every motorway drive in the
        library, which is the failure mode this threshold is most at risk of.
        """
        step = 110.0 / 3.6 * _DEG_PER_M
        fixes = [
            Fix(
                t_s=float(i),
                # Round each coordinate onto the overlay's own 4-decimal grid.
                lat=round(LAT + i * step, 4),
                lon=round(LON, 4),
            )
            for i in range(60)
        ]
        assert qualities(fixes) == [GpsQuality.VALID] * 60

    def test_the_ceiling_sits_above_any_legal_road_speed(self):
        # 130 km/h is the highest limit anywhere in Australia; one second of it must fit
        # inside the radius with room to spare.
        assert reachable_radius_m(1.0) > 130.0 / 3.6


class TestSharpTurns:
    """Case 8: a real corner moves the whole neighbourhood, so it must not look like noise."""

    def test_a_right_angle_turn_is_kept(self):
        step = 50.0 / 3.6 * _DEG_PER_M
        north = [Fix(t_s=float(i), lat=LAT + i * step, lon=LON) for i in range(15)]
        corner_lat = LAT + 14 * step
        east = [
            Fix(t_s=float(15 + i), lat=corner_lat, lon=LON + (i + 1) * step * 1.22)
            for i in range(15)
        ]
        assert qualities(north + east) == [GpsQuality.VALID] * 30

    def test_a_u_turn_is_kept(self):
        """Doubling back retraces real positions; only the *direction* reverses."""
        step = 50.0 / 3.6 * _DEG_PER_M
        out = [Fix(t_s=float(i), lat=LAT + i * step, lon=LON) for i in range(12)]
        back = [Fix(t_s=float(12 + i), lat=LAT + (11 - i) * step, lon=LON) for i in range(12)]
        assert qualities(out + back) == [GpsQuality.VALID] * 24


class TestSingleBadPoint:
    """Case 2: one wildly wrong fix between good ones."""

    def test_an_isolated_outlier_is_rejected(self):
        fixes = straight_run(30)
        # 138.6769 read as 138.6 -- three digits lost to a pixel gap.
        fixes[15] = Fix(t_s=15.0, lat=fixes[15].lat, lon=138.6)
        verdicts = classify(fixes)
        assert verdicts[15].quality is GpsQuality.REJECTED
        assert sum(v.quality is GpsQuality.REJECTED for v in verdicts) == 1

    def test_the_reason_names_the_check_that_caught_it(self):
        fixes = straight_run(30)
        fixes[15] = Fix(t_s=15.0, lat=fixes[15].lat, lon=138.6)
        assert GpsReason.ISOLATED_POSITION_OUTLIER in classify(fixes)[15].reasons

    def test_its_neighbours_are_untouched(self):
        fixes = straight_run(30)
        fixes[15] = Fix(t_s=15.0, lat=fixes[15].lat, lon=138.6)
        verdicts = classify(fixes)
        assert verdicts[14].quality is GpsQuality.VALID
        assert verdicts[16].quality is GpsQuality.VALID

    def test_a_sub_kilometre_outlier_is_caught(self):
        """The blind band. Small enough that the old 5 km journey radius never saw it."""
        fixes = straight_run(30)
        fixes[15] = Fix(t_s=15.0, lat=fixes[15].lat + 600 * _DEG_PER_M, lon=LON)
        assert classify(fixes)[15].quality is GpsQuality.REJECTED


class TestImpossibleJumpAndReturn:
    """Case 6: out and straight back — the shape one corrupt digit makes."""

    def test_an_excursion_that_returns_is_rejected(self):
        """Recording 505: 2.6 km out at t=53 s, 3.1 km back at t=64 s.

        A backwards-looking check passes this twice, because the bad point is reachable
        from its predecessor and its successor is reachable from the bad point.
        """
        fixes = straight_run(30)
        fixes[15] = Fix(t_s=15.0, lat=-34.7670, lon=138.6986)
        assert classify(fixes)[15].quality is GpsQuality.REJECTED

    def test_the_route_is_not_drawn_through_the_excursion(self):
        fixes = straight_run(30)
        fixes[15] = Fix(t_s=15.0, lat=-34.7670, lon=138.6986)
        drawn = {i for run in segments(classify(fixes)) for i in run}
        assert 15 not in drawn


class TestConsecutiveCorruption:
    """Case 3: several bad points in a row."""

    def test_three_consecutive_outliers_are_all_rejected(self):
        fixes = straight_run(40)
        for i in (18, 19, 20):
            fixes[i] = Fix(t_s=float(i), lat=fixes[i].lat, lon=138.6)
        verdicts = classify(fixes)
        assert all(verdicts[i].quality is GpsQuality.REJECTED for i in (18, 19, 20))

    def test_a_wholly_corrupt_recording_cannot_be_judged_from_inside_itself(self):
        """The limit of what a neighbourhood can know, asserted rather than assumed.

        When almost every reading in a clip is wrong the same way — the rear camera losing
        its minus sign for a whole recording is the real example — the median describes the
        corruption, and the handful of good fixes become the ones that disagree with it.

        This module cannot do better, because nothing inside the recording contradicts the
        majority. That is exactly why :mod:`app.osd.outliers` asks the question again at
        journey level, where the *other* recordings of the same drive supply the reference
        this one lacks. Documented here so the division of labour is not mistaken for a bug.
        """
        fixes = [Fix(t_s=float(i), lat=LAT, lon=138.6) for i in range(20)]
        fixes[0] = Fix(t_s=0.0, lat=LAT, lon=LON)
        fixes[1] = Fix(t_s=1.0, lat=LAT, lon=LON)
        verdicts = classify(fixes)
        # The majority is believed; the two genuinely-good fixes are the ones condemned.
        assert [v.quality for v in verdicts[:2]] == [GpsQuality.REJECTED] * 2
        assert all(v.quality is GpsQuality.VALID for v in verdicts[2:])

    def test_a_scattered_majority_of_outliers_withholds_the_verdict(self):
        """When no coherent track exists at all, nothing is condemned.

        Distinct from the case above: there the corruption agrees with itself and wins the
        vote, here it does not, and more than half the samples look wrong. Discarding most
        of a recording on that authority is the more expensive mistake, so the pass stops.
        """
        fixes = [
            Fix(t_s=float(i), lat=LAT + (i % 7) * 0.05, lon=LON + (i % 5) * 0.05) for i in range(20)
        ]
        verdicts = classify(fixes)
        assert not any(GpsReason.ISOLATED_POSITION_OUTLIER in v.reasons for v in verdicts)


class TestDropouts:
    """Cases 4 and 5: short holes may be bridged, long ones must not be."""

    def test_a_short_dropout_stays_one_segment(self):
        step = 50.0 / 3.6 * _DEG_PER_M
        before = [Fix(t_s=float(i), lat=LAT + i * step, lon=LON) for i in range(10)]
        # Three seconds missing, then the drive continues from where it would have been.
        after = [Fix(t_s=float(13 + i), lat=LAT + (13 + i) * step, lon=LON) for i in range(10)]
        assert len(segments(classify(before + after))) == 1

    def test_a_long_dropout_breaks_the_route(self):
        """Case 5, and the one the screenshots were really about.

        Two valid positions several kilometres apart with nothing trustworthy between them
        must not become a straight line. The gap is kept as a gap.
        """
        before = [Fix(t_s=float(i), lat=LAT, lon=LON) for i in range(10)]
        after = [Fix(t_s=float(300 + i), lat=LAT + 0.05, lon=LON + 0.05) for i in range(10)]
        verdicts = classify(before + after)
        assert verdicts[10].breaks_segment
        assert GpsReason.GPS_GAP in verdicts[10].reasons

    def test_a_long_dropout_yields_two_segments_not_one_line(self):
        before = [Fix(t_s=float(i), lat=LAT, lon=LON) for i in range(10)]
        after = [Fix(t_s=float(300 + i), lat=LAT + 0.05, lon=LON + 0.05) for i in range(10)]
        assert len(segments(classify(before + after))) == 2

    def test_both_ends_of_a_long_gap_are_still_valid(self):
        """A gap is an observation about coverage, not a fault in either fix."""
        before = [Fix(t_s=float(i), lat=LAT, lon=LON) for i in range(10)]
        after = [Fix(t_s=float(300 + i), lat=LAT + 0.05, lon=LON + 0.05) for i in range(10)]
        verdicts = classify(before + after)
        assert verdicts[9].quality is GpsQuality.VALID
        assert verdicts[10].quality is GpsQuality.VALID

    def test_a_distant_pair_breaks_even_when_the_clock_says_otherwise(self):
        """The clock is read by the same OCR as everything else, so distance gets a say."""
        fixes = [
            Fix(t_s=0.0, lat=LAT, lon=LON),
            Fix(t_s=1.0, lat=LAT, lon=LON),
            Fix(t_s=2.0, lat=LAT, lon=LON),
            Fix(t_s=3.0, lat=LAT + (MAX_GAP_M + 200) * _DEG_PER_M, lon=LON),
        ]
        assert classify(fixes)[3].breaks_segment or (
            classify(fixes)[3].quality is GpsQuality.REJECTED
        )


class TestSegmentBoundaries:
    """Case 9: a recording boundary is not, by itself, a reason to break or to join."""

    def test_a_continuous_drive_across_a_boundary_stays_one_segment(self):
        step = 50.0 / 3.6 * _DEG_PER_M
        first = [Fix(t_s=float(i), lat=LAT + i * step, lon=LON) for i in range(10)]
        second = [Fix(t_s=float(10 + i), lat=LAT + (10 + i) * step, lon=LON) for i in range(10)]
        assert len(segments(classify(first + second))) == 1

    def test_a_boundary_with_a_real_gap_does_break(self):
        first = [Fix(t_s=float(i), lat=LAT, lon=LON) for i in range(10)]
        second = [Fix(t_s=float(600 + i), lat=LAT + 0.1, lon=LON) for i in range(10)]
        assert len(segments(classify(first + second))) == 2


class TestTimestampFaults:
    """Cases 10 and 11: duplicate and out-of-order samples must not become teleportation."""

    def test_duplicate_timestamps_are_not_treated_as_teleportation(self):
        """Two samples stamped the same second are a 1 Hz clock, not movement."""
        fixes = [
            Fix(t_s=0.0, lat=LAT, lon=LON),
            Fix(t_s=1.0, lat=LAT, lon=LON),
            Fix(t_s=1.0, lat=LAT + 5 * _DEG_PER_M, lon=LON),
            Fix(t_s=2.0, lat=LAT + 10 * _DEG_PER_M, lon=LON),
            Fix(t_s=3.0, lat=LAT + 15 * _DEG_PER_M, lon=LON),
        ]
        assert all(v.quality is GpsQuality.VALID for v in classify(fixes))

    def test_identical_duplicate_samples_are_kept(self):
        fixes = [Fix(t_s=float(i // 2), lat=LAT, lon=LON) for i in range(20)]
        assert all(v.quality is GpsQuality.VALID for v in classify(fixes))

    def test_the_radius_never_collapses_on_a_zero_time_delta(self):
        """Two places at the same instant is a contradiction, not an infinite speed."""
        assert reachable_radius_m(0.0) == reachable_radius_m(1.0)
        assert math.isfinite(reachable_radius_m(0.0))


class TestCoordinateParsing:
    """Cases 12 and 13: the source-level fix — a partial read is never a position."""

    @pytest.mark.parametrize(
        ("raw", "why"),
        [
            ("2026-08-04 11:11:51 E:138.6 :k:-34.7981 5 km/h", "three digits lost"),
            ("2026-08-04 11:11:52 E:138.6 4:-34.7981 5 km/h", "two digits lost"),
            ("2026-08-04 11:12:00 E:138.6 2N:-34.7981 0 km/h", "two digits lost"),
            ("2026-08-04 11:11:50 E:138.651:k:-34.7981 12 km/h", "one digit lost"),
            ("2026-08-04 11:12:13 E:138.65124:-34.7981 0 km/h", "a separator read as a digit"),
            ("2026-08-03 12:46:51 E:138.6178 N:-34.8 :", "latitude truncated"),
            ("2026-08-01 15:09:33 E:138.63967N:-34.7629 16E/h", "longitude one digit long"),
        ],
    )
    def test_a_partial_coordinate_is_refused(self, raw, why):
        reading = parse_osd_text(raw, confidence=0.94)
        assert not reading.has_fix, why
        assert reading.lat is None and reading.lon is None

    @pytest.mark.parametrize(
        "raw",
        [
            "2026-08-04 11:11:55 E:138.6514:-34.7981 0 km/h",
            "2026-08-04 17:44:38   E:138.6769 N:-34.8088  68 km/h",
            # A spurious space *inside* the fraction is rejoined by compaction, because
            # whitespace is inferred from pixel gaps and is the least trustworthy thing
            # in the string.
            "2026-07-31 15:36:02 E:138.6670 N:-34.871 2 km/h",
        ],
    )
    def test_a_clean_coordinate_is_still_read(self, raw):
        assert parse_osd_text(raw, confidence=0.94).has_fix

    def test_a_camera_printing_another_precision_is_supported(self):
        """The tolerant path is per-camera, not per-frame."""
        raw = "2026-08-04 12:00:00 E:138.676912 N:-34.808811 5 km/h"
        assert parse_osd_text(raw, confidence=0.9, coord_decimals=6).has_fix

    def test_that_camera_still_refuses_a_truncated_read(self):
        raw = "2026-08-04 12:00:00 E:138.6769 N:-34.808811 5 km/h"
        assert not parse_osd_text(raw, confidence=0.9, coord_decimals=6).has_fix

    def test_an_ambiguous_sign_is_refused_rather_than_guessed(self):
        """Case 13. A dot in the sign cell is equally a misread minus or a misread colon."""
        reading = parse_osd_text(
            "2026-08-04 12:00:00 E:138.7014 N:.34.8056 60 km/h", confidence=0.9
        )
        assert not reading.has_fix
        assert reading.gps_reason is GpsReason.COORDINATE_PARSE_FAILURE

    def test_a_wrong_hemisphere_is_caught_by_its_neighbours(self):
        """Belt and braces: when the sign survives parsing but is still wrong."""
        fixes = straight_run(30)
        fixes[15] = Fix(t_s=15.0, lat=-fixes[15].lat, lon=LON)
        assert classify(fixes)[15].quality is GpsQuality.REJECTED

    def test_the_no_fix_placeholder_is_not_a_place(self):
        reading = parse_osd_text("2026-08-04 12:00:00 E:00.0000 N:00.0000 0 km/h")
        assert not reading.has_fix
        assert reading.gps_reason is GpsReason.NO_FIX


class TestQualityStates:
    """The four states downstream features distinguish."""

    def test_a_camera_no_fix_is_not_a_rejection(self):
        fixes = [Fix(t_s=0.0, lat=None, lon=None, no_fix=True)]
        verdict = classify(fixes)[0]
        assert verdict.quality is GpsQuality.NO_FIX
        assert verdict.reasons == [GpsReason.NO_FIX]

    def test_an_unreadable_coordinate_is_a_parse_failure_not_a_bad_place(self):
        verdict = classify([Fix(t_s=0.0, lat=None, lon=None)])[0]
        assert verdict.quality is GpsQuality.REJECTED
        assert verdict.reasons == [GpsReason.COORDINATE_PARSE_FAILURE]

    def test_an_out_of_range_coordinate_is_invalid(self):
        verdict = classify([Fix(t_s=0.0, lat=91.0, lon=LON)])[0]
        assert verdict.quality is GpsQuality.REJECTED
        assert verdict.reasons == [GpsReason.INVALID_LAT_LON]

    def test_a_synthetic_position_is_marked_interpolated(self):
        fixes = straight_run(10)
        fixes[5] = Fix(t_s=5.0, lat=fixes[5].lat, lon=fixes[5].lon, synthetic=True)
        assert classify(fixes)[5].quality is GpsQuality.INTERPOLATED

    def test_interpolated_positions_are_still_drawable(self):
        fixes = straight_run(10)
        fixes[5] = Fix(t_s=5.0, lat=fixes[5].lat, lon=fixes[5].lon, synthetic=True)
        assert 5 in {i for run in segments(classify(fixes)) for i in run}

    def test_every_verdict_carries_a_machine_readable_account(self):
        fixes = straight_run(30)
        fixes[15] = Fix(t_s=15.0, lat=fixes[15].lat, lon=138.6)
        logged = classify(fixes)[15].as_log()
        assert logged["gps_quality"] == "rejected"
        assert logged["gps_reasons"] == ["isolated_position_outlier"]
        assert logged["gps_detail"]


class TestTooLittleToJudge:
    """The refusals that keep this from inventing verdicts."""

    def test_two_points_have_no_neighbourhood_but_still_have_physics(self):
        """A pair is too small to vote, yet 11,000 km in one second is still impossible.

        The neighbourhood pass declines — three samples are the fewest that can arbitrate.
        The forward walk does not need to arbitrate: it keeps the anchor, which is the last
        position still believed, and refuses the one that cannot be reached from it. That
        is the same convention the extractor has always used, and it is why a corrupt first
        fix cannot condemn the good ones behind it.
        """
        fixes = [Fix(t_s=0.0, lat=LAT, lon=LON), Fix(t_s=1.0, lat=LAT, lon=13.8769)]
        verdicts = classify(fixes)
        assert verdicts[0].quality is GpsQuality.VALID
        assert verdicts[1].quality is GpsQuality.REJECTED
        assert verdicts[1].reasons == [GpsReason.IMPLIED_SPEED_OUTLIER]

    def test_an_empty_track_is_not_an_error(self):
        assert classify([]) == []

    def test_a_single_fix_is_kept(self):
        assert classify([Fix(t_s=0.0, lat=LAT, lon=LON)])[0].quality is GpsQuality.VALID


class TestTheForwardWalkKnowsWhenItIsTheOnlyJudge:
    """Pass 3 keeps a bad anchor's victims only where pass 2 never got to speak.

    The forward walk cannot tell which end of a disagreeing pair is lying. On a short track
    that is the only opinion available, and taking it literally means one corrupt *first*
    fix condemns every good fix behind it — a clip that yielded three positions storing only
    the wrong one. On a long track the neighbourhood pass has already arbitrated, and its
    silence is evidence: fixes that are unreachable from their anchors after that are
    corruption, and keeping them because there are many of them would publish every
    impossible position in the recording as valid.
    """

    def test_a_short_track_does_not_let_one_bad_first_fix_condemn_the_rest(self):
        # Three fixes: the first is nonsense, the last two are a normal 11 m step apart.
        # Below MIN_NEIGHBOURS, so pass 2 declines and pass 3 is the only judge there is.
        fixes = [
            Fix(t_s=0.0, lat=LAT, lon=13.8769),
            Fix(t_s=1.0, lat=LAT, lon=LON),
            Fix(t_s=2.0, lat=LAT, lon=LON + 0.0001),
        ]
        verdicts = classify(fixes)
        kept = [v.quality for v in verdicts]
        assert kept.count(GpsQuality.VALID) >= 2, (
            "the two good fixes were thrown away with the bad anchor"
        )

    def test_a_long_track_still_rejects_its_impossible_positions(self):
        """The majority guard used to swallow these: many disagreements read as one bad anchor."""
        fixes = [Fix(t_s=float(i), lat=LAT, lon=LON + i * 0.0001) for i in range(12)]
        # Half the track is thrown to the other side of the world, one fix at a time, so
        # each is unreachable from the fix before it rather than forming a plausible run.
        for i in range(1, 12, 2):
            fixes[i] = Fix(t_s=fixes[i].t_s, lat=fixes[i].lat, lon=13.8769)

        verdicts = classify(fixes)
        rejected = [i for i, v in enumerate(verdicts) if v.quality is GpsQuality.REJECTED]
        assert rejected, "a recording full of impossible positions published all of them"
        assert all(v.lon != 13.8769 for i, v in enumerate(fixes) if verdicts[i].drawable)
