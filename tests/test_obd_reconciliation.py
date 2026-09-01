from app.ingest.obd_reconciliation import POLL_PLAN_VERSION, specs_for_poll_plan


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
