"""Generic notification delivery remains independent of bundle storage."""

from unittest.mock import AsyncMock, MagicMock

import httpx
import paho.mqtt.client as mqtt

from app.ingest import reporter


def test_mqtt_publishes_standalone_state_without_discovery(monkeypatch):
    settings = {
        "mqtt_base_topic": "garage/backup/",
        "mqtt_host": "broker.local",
        "mqtt_port": 1883,
        "mqtt_user": "",
    }
    monkeypatch.setattr(reporter, "_get", lambda key, default=None: settings.get(key, default))
    connection = MagicMock()
    monkeypatch.setattr(mqtt, "Client", lambda: connection)

    reporter._publish_mqtt_blocking(
        {
            "state": "ok",
            "files_total": 5,
            "files_done": 3,
            "backlog_bytes": 1073741824,
            "throughput_mbs": 8.5,
            "unit_online": True,
        }
    )

    connection.connect.assert_called_once_with("broker.local", 1883, keepalive=30)
    assert [call.args for call in connection.publish.call_args_list] == [
        ("garage/backup/state", "ok"),
        ("garage/backup/throughput", "8.5"),
        ("garage/backup/files_remaining", "2"),
        ("garage/backup/backlog_gb", "1.0"),
        ("garage/backup/unit_online", "ON"),
    ]
    connection.disconnect.assert_called_once()


async def test_unreachable_webhook_does_not_block_mqtt_or_raise(monkeypatch):
    settings = {"webhook_url": "http://notification.local/events", "mqtt_enabled": True}
    monkeypatch.setattr(reporter, "_get", lambda key, default=None: settings.get(key, default))
    connection = AsyncMock()
    connection.__aenter__.return_value = connection
    connection.post.side_effect = httpx.ConnectError("offline")
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: connection)
    publish_mqtt = AsyncMock()
    monkeypatch.setattr(reporter, "_publish_mqtt", publish_mqtt)

    await reporter.publish("completed", extra={"incident_code": "preview"})

    connection.post.assert_called_once()
    assert connection.post.call_args.args == (settings["webhook_url"],)
    body = connection.post.call_args.kwargs["json"]
    assert body["event"] == "completed"
    assert body["incident_code"] == "preview"
    publish_mqtt.assert_awaited_once_with(body)
