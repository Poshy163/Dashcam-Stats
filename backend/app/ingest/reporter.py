"""Getting the ingest's state to Home Assistant.

Three channels, because they answer different questions and only one of them works with no
extra infrastructure:

* **REST** -- ``/api/ingest/status`` is the sensor source. Nothing to configure here beyond
  leaving the endpoint on; Home Assistant polls it. This always works.
* **Webhook** -- a POST on start/finish/error, which is what actually reaches a phone.
* **MQTT** -- discovery config published once (retained) plus state during a run, for a
  live dashboard. Optional, and only if a broker exists.

All three read the same snapshot from :mod:`app.ingest.status`, so a notification can never
disagree with the page.
"""

from __future__ import annotations

import json

from app.core.logging import get_logger
from app.core.settings_service import get_settings_service
from app.ingest.models import DeltaPlan, RunResult
from app.ingest.status import get_status

log = get_logger(__name__)

#: Entities published by MQTT discovery: (object_id, name, topic, unit, device_class).
_MQTT_SENSORS = (
    ("dashcam_backup_state", "Dashcam Backup State", "state", None, None),
    ("dashcam_backup_throughput", "Dashcam Backup Throughput", "throughput", "MB/s", None),
    ("dashcam_backup_remaining", "Dashcam Files Remaining", "files_remaining", None, None),
    ("dashcam_backup_backlog", "Dashcam Backlog", "backlog_gb", "GB", "data_size"),
)


def _get(key: str, default=None):
    try:
        return get_settings_service().get_nowait(f"ingest.{key}")
    except Exception:
        return default


def _payload(event: str, plan: DeltaPlan | None, result: RunResult | None) -> dict[str, object]:
    snapshot = get_status().snapshot()
    body: dict[str, object] = {"event": event, **snapshot}
    if plan is not None:
        body["files"] = len(plan.files)
        body["bytes"] = plan.bytes
    if result is not None:
        body["files"] = result.files
        body["bytes"] = result.bytes
        body["throughput"] = result.throughput_mbs
        body["duration_s"] = round(result.seconds, 1)
        body["error"] = result.error
    body.setdefault("throughput", snapshot.get("throughput_mbs"))
    return body


async def publish(
    event: str, *, plan: DeltaPlan | None = None, result: RunResult | None = None
) -> None:
    """Fan out one event. Never raises: reporting must not be able to fail a transfer."""
    body = _payload(event, plan, result)
    url = str(_get("ha_webhook_url", "") or "").strip()
    if url:
        try:
            import httpx

            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(url, json=body)
            if response.status_code >= 400:
                log.warning(
                    "home assistant rejected the webhook",
                    url=url,
                    status=response.status_code,
                )
        except Exception as exc:
            # Warning, not debug. A mistyped URL is the most likely thing to be wrong here
            # and it fails silently by nature -- there is no reply to notice the absence
            # of. One live deployment pointed at https:// on a host serving plain HTTP on
            # 8123, and simply never saw a notification again with nothing in the log to
            # say why. Two of these per transfer at most, so saying so cannot be noisy.
            log.warning(
                "could not reach the Home Assistant webhook",
                url=url,
                error=f"{type(exc).__name__}: {exc}",
            )

    if bool(_get("ha_mqtt_enabled", False)):
        try:
            await _publish_mqtt(body)
        except Exception as exc:
            log.debug("home assistant mqtt publish failed", error=f"{type(exc).__name__}: {exc}")


async def _publish_mqtt(body: dict[str, object]) -> None:
    import asyncio

    await asyncio.to_thread(_publish_mqtt_blocking, body)


def _publish_mqtt_blocking(body: dict[str, object]) -> None:
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        log.debug("paho-mqtt is not installed; skipping MQTT reporting")
        return

    base = str(_get("ha_mqtt_base_topic", "dashcam/backup") or "dashcam/backup").rstrip("/")
    client = mqtt.Client()
    user = str(_get("ha_mqtt_user", "") or "")
    if user:
        client.username_pw_set(user, str(_get("ha_mqtt_pass", "") or ""))
    client.connect(str(_get("ha_mqtt_host", "")), int(_get("ha_mqtt_port", 1883)), keepalive=30)
    try:
        # Discovery first, retained, so the entities exist before any state arrives and
        # survive a Home Assistant restart without this app republishing them.
        for object_id, name, topic, unit, device_class in _MQTT_SENSORS:
            config = {
                "name": name,
                "state_topic": f"{base}/{topic}",
                "unique_id": object_id,
                "device": {"identifiers": ["dashcam_analyser"], "name": "Dashcam Analyser"},
            }
            if unit:
                config["unit_of_measurement"] = unit
            if device_class:
                config["device_class"] = device_class
            client.publish(
                f"homeassistant/sensor/{object_id}/config", json.dumps(config), retain=True
            )
        client.publish(
            "homeassistant/binary_sensor/dashcam_unit_online/config",
            json.dumps(
                {
                    "name": "Dashcam Unit Online",
                    "state_topic": f"{base}/unit_online",
                    "unique_id": "dashcam_unit_online",
                    "device_class": "connectivity",
                    "payload_on": "ON",
                    "payload_off": "OFF",
                    "device": {"identifiers": ["dashcam_analyser"], "name": "Dashcam Analyser"},
                }
            ),
            retain=True,
        )

        remaining = int(body.get("files_total") or 0) - int(body.get("files_done") or 0)
        backlog_gb = round(int(body.get("backlog_bytes") or 0) / 1_073_741_824, 2)
        client.publish(f"{base}/state", str(body.get("state", "idle")))
        client.publish(f"{base}/throughput", str(body.get("throughput_mbs") or 0))
        client.publish(f"{base}/files_remaining", str(max(0, remaining)))
        client.publish(f"{base}/backlog_gb", str(backlog_gb))
        client.publish(f"{base}/unit_online", "ON" if body.get("unit_online") else "OFF")
        client.loop_write()
    finally:
        client.disconnect()
