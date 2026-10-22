"""Temperature is retained diagnostic evidence, independent of CarPlay surface health."""

from datetime import UTC, datetime, timedelta
from time import perf_counter
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.api.routes.system import unit_temperature_history
from app.db.models import UnitLogEntry
from app.ingest import temperature_history as temperature

START = datetime(2026, 10, 22, 1, tzinfo=UTC)


def row(
    seconds,
    value="78.5",
    *,
    session="drive-one",
    tail="event=diagnostic_context",
    extra="",
    **kwargs,
):
    return SimpleNamespace(
        occurred_at=START + timedelta(seconds=seconds),
        message=f"sample={seconds} session={session} schema=8 acc=0 soc={value} {extra} | {tail}",
        **kwargs,
    )


def history(rows, *, hours=1, end=None):
    return temperature.history(
        rows, hours=hours, range_start=START, range_end=end or START + timedelta(hours=hours)
    )


def test_parked_heartbeat_and_hot_readings_do_not_require_a_surface_or_phone():
    result = history([row(0, "107.123", tail="no phone on hotspot"), row(60, "117.4")])
    assert result["unique_observation_count"] == 2
    assert result["maximum_c"] == 117.4
    assert result["minimum_c"] == 107.123
    assert result["latest"]["soc_c"] == 117.4
    assert result["source"] == "retained_carplay_timing"
    assert result["sensor"] == "soc-thmzone"


def test_frame_copies_and_multiple_surfaces_collapse_to_the_original_context_head():
    rows = [
        row(10),
        row(12, extra="context_age_ms=4000", tail="layer=#1 fps=30"),
        row(12.1, extra="context_age_ms=4000", tail="layer=#2 fps=25"),
        row(15, extra="context_age_ms=7000", tail="layer=#1 fps=30"),
        row(25),
    ]
    result = history(rows)
    assert result["source_row_count"] == 5
    assert result["unique_observation_count"] == 2
    assert result["latest"]["occurred_at"] == START + timedelta(seconds=25)


def test_frame_only_history_uses_context_capture_time_instead_of_receipt_or_frame_time():
    rows = [
        row(
            100,
            extra="context_age_ms=60000",
            tail="layer=#1 fps=30",
            received_at=START + timedelta(days=1),
        ),
        row(103, extra="context_age_ms=63000", tail="layer=#2 fps=25"),
    ]
    result = history(rows)
    assert result["unique_observation_count"] == 1
    assert result["latest"]["occurred_at"] == START + timedelta(seconds=40)


def test_slow_surface_probe_and_no_surface_copies_do_not_become_new_readings():
    result = history(
        [
            row(10),
            row(30, extra="context_age_ms=15000", tail="layer=#1 fps=30"),
            row(33, extra="context_age_ms=18000", tail="no video surface"),
        ]
    )
    assert result["unique_observation_count"] == 1
    assert result["latest"]["occurred_at"] == START + timedelta(seconds=10)


def test_full_input_limit_of_repeated_context_uses_bounded_lookup_work():
    rows = []
    for index in range(temperature.MAX_INPUT_ROWS // 2):
        rows.append(row(index * 15))
        rows.append(row(index * 15 + 10, extra="context_age_ms=5000", tail="layer=#1 fps=30"))
    started = perf_counter()
    result = history(rows, hours=168)
    # A repeated-head drive is realistic and used to make each copy scan every
    # direct observation. Allow generous CI overhead while catching that growth.
    assert perf_counter() - started < 10
    assert result["source_row_count"] == temperature.MAX_INPUT_ROWS
    assert result["unique_observation_count"] == temperature.MAX_INPUT_ROWS // 2
    assert len(result["points"]) <= temperature.MAX_OUTPUT_POINTS


@pytest.mark.parametrize(
    "value", ["", "nan", "NaN", "inf", "-inf", "garbage", "200.001", "-40.001"]
)
def test_malformed_or_implausible_values_are_null_boundaries_and_can_be_latest(value):
    result = history([row(0, "80"), row(30, value)])
    assert result["latest"]["quality"] == "invalid"
    assert result["latest"]["soc_c"] is None
    assert result["minimum_c"] == result["maximum_c"] == 80
    assert result["unique_observation_count"] == 2
    assert result["points"][0]["segment_id"] != result["points"][-1]["segment_id"]


def test_missing_value_is_unavailable_and_unrelated_events_do_not_invent_readings():
    result = history(
        [
            row(0, "80"),
            row(30, "na"),
            SimpleNamespace(
                occurred_at=START + timedelta(seconds=60),
                message="session=drive-one | event=gps_context gps_capture_status=ok",
            ),
        ]
    )
    assert result["latest"]["quality"] == "unavailable"
    assert result["latest"]["soc_c"] is None
    assert result["latest"]["occurred_at"] == START + timedelta(seconds=30)
    assert result["unique_observation_count"] == 2


@pytest.mark.parametrize(
    "extra", ["soc=20", "context_age_ms=-1", "context_age_ms=nan", "context_age_ms=120001"]
)
def test_ambiguous_value_or_invalid_context_age_does_not_produce_a_valid_temperature(extra):
    result = history([row(30, "107", extra=extra, tail="layer=#1 fps=30")])
    assert result["latest"]["quality"] == "invalid"
    assert result["maximum_c"] is None


@pytest.mark.parametrize(
    "extra,tail",
    [
        ("soc=80", "event=diagnostic_context"),
        ("context_age_ms=garbage", "layer=#1 fps=30"),
    ],
)
def test_invalid_context_cannot_be_deduplicated_into_a_previous_valid_reading(extra, tail):
    result = history([row(0, "80"), row(1, "80", extra=extra, tail=tail)])
    assert result["unique_observation_count"] == 2
    assert result["latest"]["quality"] == "invalid"
    assert result["latest"]["soc_c"] is None
    assert result["points"][0]["segment_id"] != result["points"][-1]["segment_id"]


def test_other_sensors_are_ignored_but_different_capture_sessions_are_independent():
    result = history(
        [
            row(0, "80"),
            row(0, "80", session="drive-two"),
            row(10, "38", extra="sensor=battery"),
            row(15, "90", extra="soc_sensor=soc-thmzone"),
        ]
    )
    assert result["unique_observation_count"] == 3
    assert result["minimum_c"] == 80
    assert result["maximum_c"] == 90
    assert len({point["segment_id"] for point in result["points"]}) >= 2


def test_nulls_session_changes_missing_sessions_and_large_time_gaps_break_lines():
    result = history(
        [
            row(0, "80"),
            row(15, "81"),
            row(30, "na"),
            row(45, "82"),
            row(60, "83", session="two"),
            row(200, "84", session="two"),
            row(215, "85", session=""),
            row(230, "86", session=""),
        ]
    )
    segments = [point["segment_id"] for point in result["points"]]
    assert segments[0] == segments[1]
    assert len(set(segments[1:])) == len(segments[1:])
    assert result["gap_threshold_s"] == 90


def test_buckets_keep_actual_minimum_maximum_and_last_in_time_order():
    result = history([row(0, "90"), row(1, "80"), row(2, "117"), row(3, "85")])
    assert [(point["soc_c"], point["kind"]) for point in result["points"]] == [
        (80, "min"),
        (117, "max"),
        (85, "last"),
    ]
    assert all(point["samples"] == 4 for point in result["points"])
    assert result["unique_observation_count"] == 4


def test_range_filters_use_corrected_source_time_and_never_include_future_rows():
    result = history(
        [
            row(0, extra="context_age_ms=1000", tail="layer=#1 fps=30"),
            row(10, "80"),
            row(3601, "120"),
        ]
    )
    assert result["unique_observation_count"] == 1
    assert result["maximum_c"] == 80


def test_output_cap_reports_loss_and_retains_global_extremes_and_latest_unknown(monkeypatch):
    monkeypatch.setattr(temperature, "MAX_OUTPUT_POINTS", 5)
    rows = [
        row(index * 100, str(value), session=f"session-{index}")
        for index, value in enumerate([30, 150, 90, 91, 92, 93, 94, 95])
    ]
    rows.append(row(900, "na", session="last"))
    result = history(rows)
    assert result["output_truncated"] is result["truncated"] is True
    assert len(result["points"]) <= 5
    assert {point["soc_c"] for point in result["points"]} >= {30, 150, None}
    assert result["latest"]["soc_c"] is None
    assert result["unique_observation_count"] == 9


def test_duplicate_global_minimum_cannot_displace_global_maximum_when_output_is_capped(monkeypatch):
    monkeypatch.setattr(temperature, "MAX_OUTPUT_POINTS", 5)
    rows = [row(0, "30", extra="load=1"), row(0, "30", extra="load=2")]
    rows += [
        row(index * 100, str(value)) for index, value in enumerate([150, 90, 91, 92, 93, 94, 95], 1)
    ]
    result = history(rows)
    assert result["output_truncated"]
    assert {point["soc_c"] for point in result["points"]} >= {30, 150, 95}
    assert len(result["points"]) <= 5


async def add_log(session, seconds, value="80", *, tag="CarPlayTiming", received=None):
    source = row(seconds, value)
    entry = UnitLogEntry(
        occurred_at=source.occurred_at,
        received_at=received or source.occurred_at,
        pid=1,
        tid=1,
        level="E",
        tag=tag,
        message=source.message,
        line_hash=f"{seconds}-{value}-{tag}",
    )
    session.add(entry)
    await session.flush()


async def test_api_uses_read_only_bounded_source_query_and_reports_input_truncation(
    db_session, monkeypatch
):
    import app.api.routes.system as system

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return (START + timedelta(hours=1)).astimezone(tz or UTC)

    monkeypatch.setattr(system, "datetime", Clock)
    monkeypatch.setattr(temperature, "MAX_INPUT_ROWS", 2)
    for seconds in (0, 15, 30):
        await add_log(db_session, seconds, str(80 + seconds), received=START + timedelta(days=10))
    await add_log(db_session, 40, "150", tag="OtherTag")
    await add_log(db_session, 3601, "160")
    await db_session.commit()
    before = await db_session.scalar(select(func.count()).select_from(UnitLogEntry))
    result = await unit_temperature_history(session=db_session, hours=1)
    assert result["source_row_count"] == 2
    assert result["input_truncated"] is result["truncated"] is True
    assert not result["output_truncated"]
    assert result["minimum_c"] == 95
    assert result["maximum_c"] == 110
    assert result["latest"]["occurred_at"] == START + timedelta(seconds=30)
    assert await db_session.scalar(select(func.count()).select_from(UnitLogEntry)) == before


async def test_api_attribution_margin_suppresses_a_copy_of_pre_range_context(
    db_session, monkeypatch
):
    import app.api.routes.system as system

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return (START + timedelta(hours=1)).astimezone(tz or UTC)

    monkeypatch.setattr(system, "datetime", Clock)
    await add_log(db_session, -1)
    copied = row(10, "80", extra="context_age_ms=7000", tail="layer=#1 fps=30")
    db_session.add(
        UnitLogEntry(
            occurred_at=copied.occurred_at,
            pid=1,
            tid=1,
            level="E",
            tag="CarPlayTiming",
            message=copied.message,
            line_hash="copy-of-pre-range",
        )
    )
    await db_session.commit()
    result = await unit_temperature_history(session=db_session, hours=1)
    assert result["source_row_count"] == 2
    assert result["unique_observation_count"] == 0
    assert result["latest"] is None and result["points"] == []


async def test_unrelated_diagnostics_do_not_consume_temperature_input_budget(
    db_session, monkeypatch
):
    import app.api.routes.system as system

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return (START + timedelta(hours=1)).astimezone(tz or UTC)

    monkeypatch.setattr(system, "datetime", Clock)
    monkeypatch.setattr(temperature, "MAX_INPUT_ROWS", 2)
    await add_log(db_session, 0, "107")
    for index in range(10):
        db_session.add(
            UnitLogEntry(
                occurred_at=START + timedelta(seconds=index + 1),
                pid=1,
                tid=1,
                level="E",
                tag="CarPlayTiming",
                message="session=s | event=gps_context",
                line_hash=f"unrelated-{index}",
            )
        )
    await db_session.commit()
    result = await unit_temperature_history(session=db_session, hours=1)
    assert result["source_row_count"] == result["unique_observation_count"] == 1
    assert not result["input_truncated"]
    assert result["latest"]["soc_c"] == 107


@pytest.mark.parametrize("hours,bucket_s", [(1, 15), (6, 60), (24, 300), (168, 1800)])
async def test_api_allowed_ranges_and_empty_history_are_explicit(client, hours, bucket_s):
    response = await client.get(f"/api/unit-logs/temperature?hours={hours}")
    assert response.status_code == 200
    body = response.json()
    assert body["bucket_s"] == bucket_s
    assert body["points"] == [] and body["latest"] is None
    assert body["minimum_c"] is body["maximum_c"] is None
    assert body["unique_observation_count"] == 0 and not body["truncated"]


@pytest.mark.parametrize("hours", [0, 2, 12, 169, -1, "nan", "1.5"])
async def test_api_rejects_unapproved_or_malformed_range(client, hours):
    response = await client.get(f"/api/unit-logs/temperature?hours={hours}")
    assert response.status_code == 422
