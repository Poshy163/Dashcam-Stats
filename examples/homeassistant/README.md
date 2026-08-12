# Home Assistant integration

Three ways to see the dashcam backup in Home Assistant. They are complementary, not
alternatives, and they all read the same snapshot from the app so they cannot disagree
with each other.

Start with the REST sensor. It needs no broker, no add-on and no configuration in the app
beyond leaving the API reachable.

| | What it gives you | What it needs |
|---|---|---|
| **REST sensor** | State, speed, backlog, files remaining, unit online | Nothing |
| **Webhook** | A notification on your phone when a backup starts/finishes/fails | A webhook URL in Settings → Backup / Ingest |
| **MQTT** | The same entities, created automatically, updating live | An MQTT broker |

Replace `<app-host>` with wherever the container runs, and the port with the one you
published (the bundled `docker-compose.yml` uses `8098`).

---

## A. REST sensor — start here

Add to `configuration.yaml`:

```yaml
rest:
  - resource: http://<app-host>:8098/api/ingest/status
    scan_interval: 10
    sensor:
      - name: "Dashcam Backup State"
        value_template: "{{ value_json.state }}"
      - name: "Dashcam Backup Throughput"
        unit_of_measurement: "MB/s"
        value_template: "{{ value_json.throughput_mbs | round(1) }}"
      - name: "Dashcam Files Remaining"
        value_template: "{{ value_json.files_total - value_json.files_done }}"
      - name: "Dashcam Backlog"
        unit_of_measurement: "GB"
        device_class: data_size
        value_template: "{{ (value_json.backlog_bytes / 1073741824) | round(2) }}"
    binary_sensor:
      - name: "Dashcam Unit Online"
        device_class: connectivity
        value_template: "{{ value_json.unit_online }}"
```

`state` is one of `disabled`, `idle`, `running`, `ok`, `partial`, `error`, `offline`,
`unauthorized`, `cancelled`.

## B. Webhook — for a notification

A complete automation is in [`automation.yaml`](automation.yaml): separate messages for
started / finished / interrupted / failed, sharing one notification tag so the finish
replaces the start instead of stacking.

Put the webhook URL into **Settings → Backup / Ingest → Home Assistant webhook**. Use
Home Assistant's **internal** address, not a public one:

```
http://<home-assistant-lan-ip>:8123/api/webhook/<your-webhook-id>
```

`local_only: true` only accepts requests whose client IP is on the local network. The app
posts from inside the LAN, so an internal URL satisfies that directly. Going out through
a public hostname and back means Home Assistant sees the reverse proxy's address instead,
and the webhook is silently rejected unless `use_x_forwarded_for` and `trusted_proxies`
are configured for that proxy — an internal URL avoids the question, and the round trip.

Events are `started`, `finished` and `error`. A window where the car turns up with nothing
new to copy sends nothing at all, so this does not fire every time the engine starts.

Payload fields:

| Field | Notes |
| --- | --- |
| `event` | `started` / `finished` / `error` |
| `state` | `ok`, `partial`, `error`, `cancelled`, `unauthorized` |
| `files`, `bytes` | what the run moved — on `started`, what it intends to move |
| `throughput`, `duration_s`, `error` | `finished` / `error` only |
| `backlog_files`, `backlog_bytes` | still on the camera |
| `files_total`, `files_done`, `bytes_total`, `bytes_done`, `current_file`, `unit_online` | live progress |

`started` carries no `throughput`, `duration_s` or `error`, so use
`trigger.json.get('throughput', 0)` rather than `trigger.json.throughput`.

## C. MQTT — live entities, no YAML

Turn on **Publish to MQTT** in the same settings group and fill in the broker. The app
publishes Home Assistant discovery topics (retained), so the entities appear on their own:

- `sensor.dashcam_backup_state`
- `sensor.dashcam_backup_throughput`
- `sensor.dashcam_files_remaining`
- `sensor.dashcam_backlog`
- `binary_sensor.dashcam_unit_online`

State topics live under the configured base topic, `dashcam/backup` by default.

## D. A Lovelace card

```yaml
type: vertical-stack
cards:
  - type: entities
    title: Dashcam backup
    entities:
      - entity: sensor.dashcam_backup_state
        name: Status
      - entity: binary_sensor.dashcam_unit_online
        name: Car on the network
      - entity: sensor.dashcam_files_remaining
        name: Files remaining
      - entity: sensor.dashcam_backlog
        name: Still on the camera
  - type: conditional
    conditions:
      - entity: sensor.dashcam_backup_state
        state: "running"
    card:
      type: gauge
      entity: sensor.dashcam_backup_throughput
      name: Copying
      unit: MB/s
      min: 0
      max: 40
      severity:
        green: 20
        yellow: 8
        red: 0
```

The gauge tops out at 40 MB/s because that is about the ceiling of the transport: the
measured rate off the head unit is ~34 MB/s, against ~10 MB/s for anything routed through
`adbd`. Under about 8 MB/s means something has fallen back to a slower path.
