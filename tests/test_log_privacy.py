"""Webhook path credentials are hidden in new and previously persisted logs."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from app.core.logging import REDACTED, redact, redact_text
from app.db.models import LogEntry, UnitLogEntry, utcnow


@pytest.mark.parametrize(
    "url",
    [
        "http://example.test:8123/api/webhook/synthetic-private-token",
        "https://example.test/api/webhooks/123/synthetic-private-token",
        "https://example.test/api/v10/webhooks/123/synthetic-private-token",
        "https://example.test/webhook/synthetic-private-token/route",
        "https://hooks.slack.com/services/team/channel/synthetic-private-token",
        "/api/webhook/synthetic-private-token",
    ],
)
def test_webhook_paths_are_redacted_inside_http_messages_and_nested_context(url):
    message = f'HTTP Request: POST {url} "HTTP/1.1 200 OK"'
    safe = redact_text(message)
    assert "synthetic-private-token" not in safe
    assert REDACTED in safe
    assert '"HTTP/1.1 200 OK"' in safe
    assert redact_text(safe) == safe
    assert "synthetic-private-token" not in str(redact({"request": {"url": url}}))


def test_webhook_url_redaction_preserves_query_redaction_and_non_secret_routes():
    value = "https://example.test/api/webhook/secret-path?token=secret-query&retry=2"
    safe = redact_text(value)
    assert "secret-path" not in safe
    assert "secret-query" not in safe
    assert "retry=2" in safe
    normal = "GET https://example.test/api/journeys?page=2"
    assert redact_text(normal) == normal


def test_url_and_error_objects_are_redacted_before_their_late_string_conversion():
    import httpx

    url = "https://example.test/api/webhook/synthetic-private-token"
    safe = redact({"request": httpx.URL(url), "error": RuntimeError(url)})
    assert "synthetic-private-token" not in json.dumps(safe)
    assert REDACTED in safe["request"]


def test_an_unprintable_context_object_does_not_break_logging():
    class Unprintable:
        def __str__(self):
            raise ValueError("not printable")

    assert redact(Unprintable()) == REDACTED


async def test_log_api_redacts_historical_messages_and_context_without_rewriting_rows(
    client, db_session
):
    url = "https://example.test/api/webhook/synthetic-private-token"
    server = LogEntry(level="INFO", logger="httpx", message=f"POST {url}", context={"target": url})
    unit = UnitLogEntry(
        occurred_at=utcnow(),
        received_at=utcnow(),
        pid=1,
        tid=2,
        level="E",
        tag="Synthetic",
        message=f"POST {url}",
        line_hash="a" * 64,
    )
    db_session.add_all([server, unit])
    await db_session.commit()

    for endpoint in ("/api/logs", "/api/unit-logs"):
        response = await client.get(endpoint)
        assert response.status_code == 200, response.text
        assert "synthetic-private-token" not in response.text
        assert REDACTED in response.text

    await db_session.refresh(server)
    await db_session.refresh(unit)
    assert "synthetic-private-token" in server.message
    assert "synthetic-private-token" in unit.message


async def test_log_pages_have_a_stable_tiebreaker_for_simultaneous_events(client, db_session):
    stamp = utcnow()
    rows = [LogEntry(ts=stamp, level="INFO", logger="test", message=f"event {i}") for i in range(4)]
    db_session.add_all(rows)
    await db_session.commit()
    expected = sorted((row.id for row in rows), reverse=True)
    ids = []
    for page in (1, 2):
        response = await client.get("/api/logs", params={"page": page, "page_size": 2})
        assert response.status_code == 200
        ids.extend(row["id"] for row in response.json()["items"])
    assert ids == expected


@pytest.mark.parametrize("json_logs", [False, True])
@pytest.mark.parametrize("source", ["structlog", "stdlib", "exception_object"])
def test_rendered_tracebacks_redact_credentials_after_exception_formatting(json_logs, source):
    # A subprocess isolates global logging configuration and stdout handlers from
    # other tests while exercising the real final renderer, not only the helper.
    program = f"""
import logging
from app.core.logging import configure_logging, get_logger
configure_logging(json_logs={json_logs!r}, force=True)
try:
    raise RuntimeError("https://example.test/api/webhook/synthetic-private-token")
except RuntimeError as error:
    if {source!r} == "stdlib":
        logging.getLogger("synthetic").exception("request failed")
    elif {source!r} == "exception_object":
        get_logger("synthetic").error("request failed", exc_info=error)
    else:
        get_logger("synthetic").exception("request failed")
"""
    result = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "PYTHONPATH": "backend"},
    )
    assert "synthetic-private-token" not in result.stdout + result.stderr
    assert "RuntimeError" in result.stdout
    assert REDACTED in result.stdout
    if json_logs:
        assert "exception" in json.loads(result.stdout)
