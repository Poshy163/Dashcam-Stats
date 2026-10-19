"""Journey date selection follows the display timezone and preserves list visibility."""

from datetime import UTC, datetime, timedelta

import pytest

from app.core.settings_service import get_settings_service
from app.db.models import Journey, Recording, RecordingState
from app.pipeline.revisions import INVALIDATED_REVISION


async def _journey(session, name, started, **overrides):
    journey = Journey(
        started_at=started,
        ended_at=started + timedelta(minutes=5),
        duration_s=300,
        avg_speed_kmh=40,
        max_speed_kmh=60,
        has_gps=True,
        **overrides,
    )
    session.add(journey)
    await session.flush()
    recording = Recording(
        filename=f"{name}.ts",
        rel_path=f"{name}.ts",
        started_at=started,
        journey_id=journey.id,
        state=RecordingState.COMPLETED,
    )
    session.add(recording)
    await session.flush()
    return journey, recording


@pytest.fixture
async def adelaide(db_session):
    await get_settings_service().set("general.timezone", "Australia/Adelaide")


@pytest.mark.parametrize(
    ("day", "midnight", "next_midnight"),
    [
        (
            "2026-08-08",
            datetime(2026, 8, 7, 14, 30, tzinfo=UTC),
            datetime(2026, 8, 8, 14, 30, tzinfo=UTC),
        ),
        (
            "2026-10-04",
            datetime(2026, 10, 3, 14, 30, tzinfo=UTC),
            datetime(2026, 10, 4, 13, 30, tzinfo=UTC),
        ),
        (
            "2026-04-05",
            datetime(2026, 4, 4, 13, 30, tzinfo=UTC),
            datetime(2026, 4, 5, 14, 30, tzinfo=UTC),
        ),
    ],
)
async def test_selected_day_includes_exact_local_midnight_and_whole_dst_day(
    client, db_session, adelaide, day, midnight, next_midnight
):
    expected = []
    for name, started in (
        ("before", midnight - timedelta(microseconds=1)),
        ("midnight", midnight),
        ("last", next_midnight - timedelta(microseconds=1)),
        ("after", next_midnight),
    ):
        journey, _ = await _journey(db_session, name, started)
        if name in {"midnight", "last"}:
            expected.append(journey.id)
    await db_session.commit()

    response = await client.get(
        "/api/journeys", params={"date_from": day, "date_to": day, "sort": "started_asc"}
    )

    assert response.status_code == 200, response.text
    assert [item["id"] for item in response.json()["items"]] == expected
    assert response.json()["total"] == 2


@pytest.mark.parametrize(
    "instant", ["2026-08-08T00:00:00Z", "2026-08-08T00:00:00", "2026-08-08T09:30:00+09:30"]
)
async def test_explicit_timestamp_is_an_inclusive_instant(client, db_session, adelaide, instant):
    midnight = datetime(2026, 8, 8, tzinfo=UTC)
    boundary, _ = await _journey(db_session, "boundary", midnight)
    await _journey(db_session, "later", midnight + timedelta(seconds=1))
    await db_session.commit()

    response = await client.get("/api/journeys", params={"date_from": instant, "date_to": instant})

    assert response.status_code == 200, response.text
    assert [item["id"] for item in response.json()["items"]] == [boundary.id]


@pytest.mark.parametrize(
    ("date_from", "date_to"),
    [
        ("2026-08-09", "2026-08-08"),
        ("2026-08-08T14:30:00Z", "2026-08-08"),
        ("2026-08-08T01:00:00Z", "2026-08-08T00:00:00Z"),
    ],
)
async def test_reversed_range_is_rejected(client, adelaide, date_from, date_to):
    response = await client.get(
        "/api/journeys", params={"date_from": date_from, "date_to": date_to}
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == "date_from must be on or before date_to"


async def test_timestamp_later_in_the_selected_end_day_is_valid(client, adelaide):
    response = await client.get(
        "/api/journeys",
        params={"date_from": "2026-08-08T13:00:00Z", "date_to": "2026-08-08"},
    )

    assert response.status_code == 200, response.text


async def test_mixed_range_that_overflows_the_display_calendar_is_rejected(client, adelaide):
    response = await client.get(
        "/api/journeys",
        params={"date_from": "9999-12-31T23:00:00Z", "date_to": "9999-12-31"},
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == (
        "date_from is outside the range of dates this application can represent."
    )


@pytest.mark.parametrize(
    "params",
    [
        {"date_from": "not-a-date"},
        {"date_to": "2026-02-30"},
        {"date_from": "0001-01-01"},
        {"date_to": "9999-12-31"},
    ],
)
async def test_unrepresentable_dates_do_not_crash_the_journey_list(client, adelaide, params):
    response = await client.get("/api/journeys", params=params)

    assert response.status_code in {400, 422}, response.text


async def test_date_filter_preserves_manual_history_visibility_and_pagination(
    client, db_session, adelaide
):
    started = datetime(2026, 8, 8, tzinfo=UTC)
    manual, _ = await _journey(db_session, "manual", started, manual=True)
    retained, recording = await _journey(db_session, "retained", started + timedelta(minutes=1))
    recording.state = RecordingState.DELETED
    recording.processed_at = started + timedelta(hours=1)
    _, ignored = await _journey(db_session, "ignored", started + timedelta(minutes=2))
    ignored.ignored = True
    _, unfinished = await _journey(db_session, "unfinished", started + timedelta(minutes=3))
    unfinished.state = RecordingState.PROCESSING
    _, stale = await _journey(db_session, "stale", started + timedelta(minutes=4))
    stale.telemetry_revision = INVALIDATED_REVISION
    parked, _ = await _journey(db_session, "parked", started + timedelta(minutes=5))
    parked.avg_speed_kmh = 0
    parked.max_speed_kmh = 0
    await _journey(db_session, "other-day", started + timedelta(days=1))
    await db_session.commit()
    params = {
        "date_from": "2026-08-08",
        "date_to": "2026-08-08",
        "sort": "started_asc",
        "page_size": 1,
        "page": 2,
    }

    response = await client.get("/api/journeys", params=params)

    assert response.status_code == 200, response.text
    assert response.json()["total"] == 2
    assert response.json()["pages"] == 2
    assert [item["id"] for item in response.json()["items"]] == [retained.id]
    params.update({"include_parked": "true", "page_size": 10, "page": 1})
    response = await client.get("/api/journeys", params=params)
    assert response.status_code == 200, response.text
    assert [item["id"] for item in response.json()["items"]] == [manual.id, retained.id, parked.id]
