"""Nonsense input must be refused, not crash the request.

Registering a route proves it responds; it does not prove it survives what a browser, a
crawler or a fat-fingered URL will send it. On the live deployment
``/api/recordings/99999999999999999999999999`` returned HTTP 500 with a full traceback --
written, for the log endpoints, into the very table being queried.

The cause is arithmetic, not validation. SQLite stores integers as signed 64-bit, and
anything wider raises ``OverflowError`` when the driver binds it. ``OverflowError`` is an
``ArithmeticError``, not a ``ValueError``, so the handler that would have turned it into a
clean 400 never saw it. The same gap swallowed ``date_from=0001-01-01``, which underflows
``datetime.min`` once stamped with a +9:30 zone.

Every assertion here returned 500 before the bounds were added.
"""

from __future__ import annotations

import pytest

#: One past what SQLite can bind. 2**63 - 1 itself must still behave normally.
TOO_BIG = 2**63
STILL_VALID = 2**63 - 1

ID_PATHS = [
    "/api/recordings/{id}",
    "/api/recordings/{id}/telemetry",
    "/api/recordings/{id}/detections",
    "/api/recordings/{id}/plates",
    "/api/recordings/{id}/osd-debug",
    "/api/recordings/{id}/osd-debug.png",
    "/api/journeys/{id}",
    "/api/plates/{id}",
    "/api/plates/{id}/observations",
    "/api/vehicles/{id}",
    "/stream/{id}",
]

FILTER_PATHS = [
    "/api/recordings?journey_id={id}",
    "/api/recordings?camera_id={id}",
    "/api/logs?recording_id={id}",
    "/api/logs?job_id={id}",
    "/api/map/heatmap?journey_id={id}",
    "/api/map/routes?journey_id={id}",
]


class TestIdsWiderThanTheDatabase:
    @pytest.mark.parametrize("template", ID_PATHS + FILTER_PATHS)
    async def test_an_unstorable_id_is_refused_not_crashed(self, client, template):
        response = await client.get(template.format(id=TOO_BIG))
        assert response.status_code != 500, (
            f"{template} returns 500 for an id SQLite cannot store; the traceback is "
            f"written to the log table. Body: {response.text[:200]}"
        )
        assert response.status_code in (404, 422)

    @pytest.mark.parametrize("template", ID_PATHS)
    async def test_the_largest_storable_id_still_behaves_normally(self, client, template):
        """The bound is the storage limit, not a policy: 2**63-1 is a real id shape."""
        response = await client.get(template.format(id=STILL_VALID))
        assert response.status_code in (200, 404), (
            f"{template} rejected {STILL_VALID}, which SQLite can store"
        )

    @pytest.mark.parametrize("template", ID_PATHS)
    async def test_a_negative_id_is_refused(self, client, template):
        """No autoincrement key is ever negative, and several routes returned 200."""
        response = await client.get(template.format(id=-1))
        assert response.status_code == 422, (
            f"{template} accepted a negative id and answered {response.status_code}"
        )


class TestPagination:
    async def test_a_page_number_that_overflows_the_offset_is_refused(self, client):
        """offset = (page - 1) * page_size binds as a SQLite integer too."""
        response = await client.get("/api/recordings", params={"page": 10**20})
        assert response.status_code == 422, response.text[:200]

    async def test_an_ordinary_page_still_works(self, client):
        assert (await client.get("/api/recordings", params={"page": 3})).status_code == 200


class TestDatesAtTheEndsOfTheCalendar:
    @pytest.mark.parametrize("value", ["0001-01-01", "9999-12-31"])
    async def test_an_extreme_date_is_refused_not_crashed(self, client, value):
        """A date picker held on the up arrow reaches year one; +9:30 underflows it."""
        for param in ("date_from", "date_to"):
            response = await client.get("/api/recordings", params={param: value})
            assert response.status_code != 500, (
                f"{param}={value} returned 500: {response.text[:200]}"
            )
            assert response.status_code in (200, 400, 422)

    async def test_an_ordinary_date_still_filters(self, client):
        response = await client.get("/api/recordings", params={"date_from": "2026-08-01"})
        assert response.status_code == 200
