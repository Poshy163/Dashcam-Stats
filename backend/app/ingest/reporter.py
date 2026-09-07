"""Publish ingest events through optional generic webhook and MQTT channels."""

from __future__ import annotations

from app.core.logging import get_logger
from app.ingest.models import DeltaPlan, RunResult
from app.ingest.models import ingest_setting as _get
from app.ingest.status import get_status

log = get_logger(__name__)


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
    event: str,
    *,
    plan: DeltaPlan | None = None,
    result: RunResult | None = None,
    extra: dict[str, object] | None = None,
) -> None:
    """Fan out one event. Never raises: reporting must not be able to fail a transfer.

    ``extra`` is merged into the webhook body for events that carry something the snapshot
    does not — the health watcher's incidents ride here. Additive only, so nothing an
    automation already matches on can change shape.
    """
    body = _payload(event, plan, result)
    if extra:
        body.update(extra)
    url = str(_get("webhook_url", "") or "").strip()
    if url:
        try:
            import httpx

            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(url, json=body)
            if response.status_code >= 400:
                log.warning(
                    "webhook endpoint rejected the event",
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
                "could not reach the webhook endpoint",
                url=url,
                error=f"{type(exc).__name__}: {exc}",
            )

    if bool(_get("mqtt_enabled", False)):
        try:
            await _publish_mqtt(body)
        except Exception as exc:
            log.debug("mqtt publish failed", error=f"{type(exc).__name__}: {exc}")


async def _publish_mqtt(body: dict[str, object]) -> None:
    import asyncio

    await asyncio.to_thread(_publish_mqtt_blocking, body)


def _publish_mqtt_blocking(body: dict[str, object]) -> None:
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        log.debug("paho-mqtt is not installed; skipping MQTT reporting")
        return

    base = str(_get("mqtt_base_topic", "dashcam/backup") or "dashcam/backup").rstrip("/")
    client = mqtt.Client()
    user = str(_get("mqtt_user", "") or "")
    if user:
        client.username_pw_set(user, str(_get("mqtt_pass", "") or ""))
    client.connect(str(_get("mqtt_host", "")), int(_get("mqtt_port", 1883)), keepalive=30)
    try:
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
