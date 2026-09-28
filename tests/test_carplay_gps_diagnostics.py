"""Location metadata survives the real file/database/API path without inventing fixes."""

import json
from datetime import UTC, datetime

import pytest

from app.ingest import carplay_timing, unit_logs


def event(fields: str):
    return carplay_timing.parse_event(
        datetime.now(UTC), "schema=7 acc=na | event=gps_context " + fields
    )


def test_gps_metadata_retained_without_coordinates_and_unknown_ignition():
    line = (
        "sample=s-gps-1 session=s schema=7 acc=na | event=gps_context "
        "gps_capture_status=ok gps_capture_start_ms=100000 gps_capture_ms=340 "
        "gps_uptime_ms=100320 gps_poll_gap_ms=15020 gps_dump_rc=0 "
        "location_mode=3 location_enabled=1 gps_started=1 gnss_reports=500 "
        "gnss_ttff_reports=2 gnss_ttff_mean_s=45.5 gnss_ttff_sd_s=20 "
        "loc_gps_present=1 loc_gps_enabled=1 loc_gps_fix_elapsed_ms=100000 "
        "loc_gps_age_ms=320 loc_gps_hacc_m=1.5 loc_gps_satellites=32 "
        "loc_gps_zlink_listener=0 gps_zlink_process_present=1 gps_native_process_present=1 "
        "latitude=PRIVATE longitude=SECRET nmea=PRIVATE_PHONE_LOCATION"
    )
    [record] = carplay_timing.parse_sampler_file("2026-09-28T04:00:00Z " + line)
    parsed = carplay_timing.parse_event(record.occurred_at, record.message)
    assert parsed["kind"] == "gps_context"
    assert parsed["acc_on"] is None
    assert parsed["gps_capture_status"] == "ok"
    assert parsed["loc_gps_age_ms"] == 320
    assert parsed["loc_gps_satellites"] == 32
    assert parsed["loc_gps_zlink_listener"] == 0
    assert parsed["gps_native_process_present"] == 1
    assert parsed["loc_network_age_ms"] is None
    assert parsed["gnss_ttff_mean_s"] == 45.5
    serialized = json.dumps(parsed, default=str)
    assert not any(word in serialized for word in ("PRIVATE", "SECRET", "latitude", "longitude"))


@pytest.mark.parametrize(
    "status",
    ["dump_error", "unsupported", "limit", "parser_error", "tool_unavailable", "PRIVATE", "na"],
)
def test_failed_or_unknown_capture_never_exposes_partial_fix(status):
    parsed = event(
        f"gps_capture_status={status} gps_capture_ms=4000 gps_dump_rc=124 "
        "loc_gps_present=1 loc_gps_enabled=1 loc_gps_age_ms=0 loc_gps_hacc_m=1 "
        "gps_started=1 location_enabled=1 gnss_reports=40 gnss_ttff_reports=1 "
        "gnss_ttff_mean_s=0 location_mode=3 gps_zlink_process_present=1"
    )
    for key in (
        "loc_gps_present",
        "loc_gps_enabled",
        "loc_gps_age_ms",
        "loc_gps_hacc_m",
        "gps_started",
        "location_enabled",
        "gnss_reports",
        "gnss_ttff_mean_s",
    ):
        assert parsed[key] is None
    assert parsed["gps_capture_ms"] == 4000
    assert parsed["gps_dump_rc"] == 124
    assert parsed["location_mode"] == 3
    assert parsed["gps_zlink_process_present"] == 1
    if status in ("PRIVATE", "na"):
        assert parsed["gps_capture_status"] is None


def test_absent_provider_missing_fix_and_zero_age_are_distinct():
    parsed = event(
        "gps_capture_status=ok loc_gps_present=1 loc_gps_enabled=0 loc_gps_age_ms=na "
        "loc_network_present=0 loc_fused_present=1 loc_fused_enabled=1 "
        "loc_fused_age_ms=0 loc_fused_fix_elapsed_ms=1000"
    )
    assert parsed["loc_gps_present"] == 1
    assert parsed["loc_gps_enabled"] == 0
    assert parsed["loc_gps_age_ms"] is None
    assert parsed["loc_network_present"] == 0
    assert parsed["loc_network_age_ms"] is None
    assert parsed["loc_fused_age_ms"] == 0


@pytest.mark.parametrize("bad", ["-1", "NaN", "Infinity", "PRIVATE"])
def test_invalid_numeric_location_values_remain_unknown(bad):
    parsed = event(
        f"gps_capture_status=ok loc_gps_age_ms={bad} loc_gps_hacc_m={bad} "
        f"gps_capture_ms={bad} gnss_ttff_reports={bad} loc_gps_enabled={bad}"
    )
    for key in (
        "loc_gps_age_ms",
        "loc_gps_hacc_m",
        "gps_capture_ms",
        "gnss_ttff_reports",
        "loc_gps_enabled",
    ):
        assert parsed[key] is None


def test_zero_report_kpi_is_not_instant_acquisition_and_legacy_is_unknown():
    parsed = event("gps_capture_status=ok gnss_ttff_reports=0 gnss_ttff_mean_s=0 gnss_ttff_sd_s=0")
    assert parsed["gnss_ttff_reports"] == 0
    assert parsed["gnss_ttff_mean_s"] is None
    assert parsed["gnss_ttff_sd_s"] is None
    legacy = event("")
    assert legacy["gps_capture_status"] is None
    assert legacy["loc_gps_present"] is None


def test_complete_gps_record_fits_existing_retention_budget():
    fields = {"gps_capture_status": "ok"}
    # Exercise every new field with realistic large counters, without truncating tail providers.
    for name in carplay_timing._gps_diagnostics(fields):
        if name != "gps_capture_status":
            fields[name] = "1234567890123" if name.endswith("_ms") else "123.456789"
    fields.update(
        gps_next_interval_ms="15000",
        gps_burst_reason="receiver_start",
        gps_burst_end="ignition_unknown",
        gps_fix_advanced="1",
        loc_passive_satellites="32",
    )
    message = (
        "sample=1790567540-85610084-16652-gps-999999 session=1790567540-85610084-16652 "
        "schema=8 acc=1 | event=gps_context "
        + " ".join(f"{key}={value}" for key, value in fields.items())
    )
    assert len(message) < unit_logs.MAX_MESSAGE_CHARS
    [retained] = carplay_timing.parse_sampler_file("2026-09-28T04:00:00Z " + message)
    assert retained.message == message
    parsed = carplay_timing.parse_event(retained.occurred_at, retained.message)
    assert parsed["gps_burst_reason"] == "receiver_start"
    assert parsed["gps_burst_end"] == "ignition_unknown"
    assert parsed["gps_last_known_fix_age_ms"] == 1234567890123
    assert parsed["loc_passive_satellites"] == 32


async def test_gps_observation_is_recovered_deduplicated_and_exposed_by_timing_api(db_session):
    from app.api.routes.system import carplay_timing_samples

    stamp = datetime.now(UTC).replace(microsecond=0)
    message = (
        "sample=gps-api-session-gps-1 session=gps-api-session schema=7 acc=1 "
        "| event=gps_context gps_capture_status=ok gps_uptime_ms=10000 "
        "loc_gps_present=1 loc_gps_enabled=1 loc_gps_fix_elapsed_ms=9750 "
        "loc_gps_age_ms=250 loc_gps_hacc_m=2 gnss_ttff_reports=1 gnss_ttff_mean_s=57"
    )
    entries = carplay_timing.parse_sampler_file(f"{stamp:%Y-%m-%dT%H:%M:%SZ} {message}")
    assert await unit_logs.store(entries) == (1, 0)
    assert await unit_logs.store(entries) == (0, 1)
    response = await carplay_timing_samples(session=db_session, hours=1)
    assert response["sampler_schema"] == 8
    assert response["samples"] == []  # GPS observations never fabricate frame measurements.
    [observation] = response["events"]
    assert observation["kind"] == "gps_context"
    assert observation["loc_gps_age_ms"] == 250
    assert observation["gnss_ttff_mean_s"] == 57
    assert observation["acc_on"] is True
    assert response["sessions"][0]["event_count"] == 1
    assert response["sessions"][0]["sample_count"] == 0


def test_burst_metadata_survives_failed_capture_without_inventing_a_current_fix():
    parsed = event(
        "gps_capture_status=dump_error gps_next_interval_ms=15000 "
        "gps_burst_reason=ignition_on gps_burst_start_ms=100000 "
        "gps_burst_elapsed_ms=6000 gps_burst_end=read_failed "
        "gps_fix_advanced=1 gps_last_known_fix_age_ms=9000 loc_gps_age_ms=9000"
    )
    assert parsed["gps_next_interval_ms"] == 15000
    assert parsed["gps_burst_reason"] == "ignition_on"
    assert parsed["gps_burst_start_ms"] == 100000
    assert parsed["gps_burst_elapsed_ms"] == 6000
    assert parsed["gps_burst_end"] == "read_failed"
    assert parsed["gps_last_known_fix_age_ms"] == 9000
    assert parsed["gps_fix_advanced"] is None
    assert parsed["loc_gps_age_ms"] is None


@pytest.mark.parametrize("value", ["na", "-1", "PRIVATE", "Infinity", "1.5"])
def test_invalid_burst_metadata_is_not_exposed(value):
    parsed = event(
        f"gps_capture_status=ok gps_next_interval_ms={value} gps_burst_reason={value} "
        f"gps_burst_end={value} gps_burst_start_ms={value} gps_burst_elapsed_ms={value} "
        f"gps_fix_advanced={value} gps_last_known_fix_age_ms={value}"
    )
    for name in (
        "gps_next_interval_ms",
        "gps_burst_reason",
        "gps_burst_end",
        "gps_burst_start_ms",
        "gps_burst_elapsed_ms",
        "gps_fix_advanced",
        "gps_last_known_fix_age_ms",
    ):
        assert parsed[name] is None


def test_schema_seven_without_burst_fields_remains_unknown():
    parsed = event("gps_capture_status=ok loc_gps_age_ms=0")
    assert parsed["diagnostic_schema"] == 7
    assert parsed["loc_gps_age_ms"] == 0
    assert parsed["gps_next_interval_ms"] is None
    assert parsed["gps_burst_reason"] is None
    assert parsed["gps_fix_advanced"] is None
    assert parsed["gps_last_known_fix_age_ms"] is None
