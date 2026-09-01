from app.ingest.obd_reconciliation import (
    POLL_PLAN_VERSION,
    specs_for_poll_plan,
    vehicle_data_present,
)


def test_v2_and_v3_keep_their_own_cadence_contracts() -> None:
    """New logger builds must not rewrite the historical graph's expected spacing."""
    version2, v2 = specs_for_poll_plan(2)
    version3, v3 = specs_for_poll_plan(POLL_PLAN_VERSION)

    assert version2 == 2
    assert version3 == 3
    v2_timing = next(spec for spec in v2 if spec.name == "timing_advance")
    v3_timing = next(spec for spec in v3 if spec.name == "timing_advance")
    assert (v2_timing.tier, v2_timing.cadence_s) == ("fast", 5.0)
    assert (v3_timing.tier, v3_timing.cadence_s) == ("medium", 15.0)


def test_unknown_legacy_plan_is_not_mistaken_for_v3() -> None:
    version, specs = specs_for_poll_plan(None)

    assert version == 1
    assert next(spec for spec in specs if spec.name == "vehicle_speed").cadence_s == 5.0


class TestDrivesThatCapturedNoVehicleData:
    """A silent bus under a healthy adapter must never read as a clean drive.

    Regression cover for drive 01a05d40: the ECU proof passed, the bus then answered
    nothing for six and a half minutes, and because ``ATRV`` is adapter-local every one of
    its 80 samples still committed with ``transport: ok``. The drive was filed
    ``complete`` / ``clean_end`` / ``error_count 0`` and rendered as a page with no
    statistics, with only ``data_completeness_percentage`` (12.3%) hinting otherwise.
    """

    def test_a_drive_with_no_motion_evidence_is_not_present(self):
        summary = {
            "distance_km": None,
            "average_speed_kmh": None,
            "maximum_speed_kmh": None,
            "average_rpm": 0.0,
            "maximum_rpm": 0.0,
        }
        assert vehicle_data_present(summary) is False

    def test_a_normal_drive_is_present(self):
        summary = {
            "distance_km": 4.64,
            "average_speed_kmh": 42.9,
            "maximum_speed_kmh": 74.0,
            "average_rpm": 1620.1,
            "maximum_rpm": 2575.0,
        }
        assert vehicle_data_present(summary) is True

    def test_a_stationary_drive_still_counts_as_captured(self):
        """Idling in a driveway reports zero speed, which is data. Only the *absence* of
        every motion signal means the bus never answered -- otherwise this rule would
        rewrite every legitimate stationary drive as a fault."""
        summary = {
            "distance_km": 0.0,
            "average_speed_kmh": 0.0,
            "maximum_speed_kmh": 0.0,
            "average_rpm": 780.0,
            "maximum_rpm": 900.0,
        }
        assert vehicle_data_present(summary) is True

    def test_rpm_alone_is_enough(self):
        """A vehicle whose speed PID is unsupported still proves the bus is alive."""
        summary = {
            "distance_km": None,
            "average_speed_kmh": None,
            "maximum_speed_kmh": None,
            "maximum_rpm": 2100.0,
        }
        assert vehicle_data_present(summary) is True

    def test_zero_rpm_is_not_evidence(self):
        """`average_rpm: 0.0` was what drive 01a05d40 reported. A bus that answers gives a
        running engine a non-zero crank speed, so zero here means nothing came back."""
        summary = {"distance_km": None, "maximum_rpm": 0.0}
        assert vehicle_data_present(summary) is False
