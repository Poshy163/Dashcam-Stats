"""Which journeys count as drives, on the list and on the dashboard.

Clustering groups recordings by time and place, which says nothing about whether the car
moved, so a parked afternoon becomes a journey exactly like a drive does. On the live
library twenty-two of them had accumulated and the dashboard's "latest run telemetry"
panel was showing one: nineteen minutes, forty-seven metres, zero average.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.settings_service import get_settings_service
from app.db.models import Journey, Recording, RecordingState, StageState
from app.pipeline.revisions import CURRENT_REVISIONS


async def _journey_with_a_clip(session, *, avg, top, name, minutes_ago: int) -> Journey:
    started = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    journey = Journey(
        started_at=started,
        ended_at=started + timedelta(minutes=10),
        duration_s=600.0,
        avg_speed_kmh=avg,
        max_speed_kmh=top,
        has_gps=True,
    )
    session.add(journey)
    await session.flush()
    session.add(
        Recording(
            rel_path=name,
            filename=name,
            size_bytes=2048,
            started_at=started,
            state=RecordingState.COMPLETED,
            processed_at=datetime.now(UTC),
            journey_id=journey.id,
            metadata_state=StageState.DONE,
            telemetry_state=StageState.DONE,
            detection_state=StageState.DONE,
            plate_state=StageState.DONE,
            metadata_revision=CURRENT_REVISIONS["metadata"],
            telemetry_revision=CURRENT_REVISIONS["telemetry"],
            detection_revision=CURRENT_REVISIONS["detection"],
            plate_revision=CURRENT_REVISIONS["plates"],
        )
    )
    await session.flush()
    return journey


@pytest.fixture
async def library(db_session):
    """A real drive, a parked session recorded after it, and one with no speed at all."""
    settings = get_settings_service()
    await settings.set("journeys.min_avg_speed_kmh", 5.0)
    await settings.set("journeys.min_top_speed_kmh", 10.0)
    drive = await _journey_with_a_clip(
        db_session, avg=42.0, top=80.0, name="drive.ts", minutes_ago=120
    )
    parked = await _journey_with_a_clip(
        db_session, avg=0.0, top=1.0, name="parked.ts", minutes_ago=30
    )
    unknown = await _journey_with_a_clip(
        db_session, avg=None, top=None, name="unknown.ts", minutes_ago=10
    )
    await db_session.commit()
    return {"drive": drive.id, "parked": parked.id, "unknown": unknown.id}


class TestTheJourneysList:
    async def test_only_drives_are_listed(self, client, library):
        response = await client.get("/api/journeys")

        assert response.status_code == 200
        assert [item["id"] for item in response.json()["items"]] == [library["drive"]]

    async def test_the_parked_ones_can_still_be_asked_for(self, client, library):
        """They have to stay reachable: this is the footage the retention rule removes."""
        response = await client.get("/api/journeys?include_parked=true")

        listed = {item["id"] for item in response.json()["items"]}
        assert listed == set(library.values())

    async def test_a_hidden_journey_still_opens_by_id(self, client, library):
        """Hidden from the index, not withdrawn -- the link in a log still has to work."""
        response = await client.get(f"/api/journeys/{library['parked']}")

        assert response.status_code == 200
        assert response.json()["id"] == library["parked"]

    async def test_thresholds_at_zero_show_everything(self, client, library):
        settings = get_settings_service()
        await settings.set("journeys.min_avg_speed_kmh", 0.0)
        await settings.set("journeys.min_top_speed_kmh", 0.0)

        response = await client.get("/api/journeys")

        listed = {item["id"] for item in response.json()["items"]}
        assert listed == set(library.values()), "zero means no test, not a test nothing passes"


class TestTheDashboard:
    async def test_the_latest_run_panel_shows_the_last_drive(self, client, library):
        """It used to show whatever was recorded last, which for a parked car is a park."""
        response = await client.get("/api/status")

        assert response.json()["latest_journey"]["id"] == library["drive"]

    async def test_the_journey_count_matches_the_list(self, client, library):
        response = await client.get("/api/status")

        assert response.json()["totals"]["journeys"] == 1
