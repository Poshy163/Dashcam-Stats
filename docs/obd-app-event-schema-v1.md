# OBD app event stream v1

The Android logger owns a durable, transition-only event ring. This stream explains boot,
network, sleep, OBD connection, drive and backup handoff behaviour that is not tied to a completed
drive bundle. It is observability only: a missing or invalid stream never blocks footage, an OBD
bundle, a verification receipt or radio recovery.

## Storage and transport

The app retains at most 2,048 events or seven days and atomically projects the newest 512 to:

```text
/storage/Tfcard/Android/data/com.dashcamstats.obdlogger/files/obd/events.json
```

The writer flushes `events.json.partial` before a same-directory rename. The server accepts only a
regular, non-symlink file no larger than 512 KiB. It reads the file in parallel with the normal
arrival inventories, including visits with no new footage or completed OBD bundle.

The server retains accepted rows for 90 days up to 50,000 rows. It hashes the app-scoped source and
session UUIDs, deduplicates on `(source_id_hash, sequence)`, and never exposes the raw UUIDs or
their hashes. `source_id` is a random UUID persisted for one app installation, not an Android ID,
MAC, VIN or adapter identity. `sequence` is positive, monotonic and persisted across process/boot
restarts.

## Exact JSON shape

All named objects use exactly these fields. Nullable event fields remain present.

```json
{
  "schema_version": 1,
  "source_id": "11111111-1111-4111-8111-111111111111",
  "generated_at_utc": "2026-09-01T10:00:00Z",
  "first_sequence": 41,
  "last_sequence": 42,
  "producer": {
    "app_version_name": "0.2.5",
    "app_version_code": 8,
    "build_git_sha": "0123456789ab"
  },
  "events": [
    {
      "sequence": 41,
      "occurred_at_utc": "2026-09-01T09:59:58Z",
      "session_id": "22222222-2222-4222-8222-222222222222",
      "kind": "obd.ble_connection",
      "level": "info",
      "outcome": "connected",
      "reason_code": "gatt_connected",
      "drive_id": null,
      "metrics": {"connect_ms": 812, "attempt": 1}
    }
  ]
}
```

Events are strictly ascending by sequence. For a non-empty snapshot the first/last fields match
the array; an empty snapshot uses zero for both. Timestamps are timezone-aware ISO-8601. Producer
version text is limited to 64 identifier characters, the version code is a positive signed
32-bit integer, and the build is 12 lowercase hexadecimal characters or `unknown`.

No event has a free-form message or string-valued details map. Raw adapter replies, exception
text, Bluetooth addresses, SSIDs/BSSIDs, IPs, paths, credentials and hardware identifiers are not
allowed. Invalid input is reported to the WebUI only as one of `device_unreachable`,
`invalid_snapshot`, `storage_error` or `internal_error`.

## Codes

Kinds:

```text
app.boot                 app.service              network.wifi
power.sleep_window       obd.ble_connection       obd.elm_session
obd.ecu_session          obd.poll_health          drive.lifecycle
ingest.handoff           radio.observation        bundle.export
receipt.verification
```

Levels are `info`, `warning` and `error`.

Outcomes:

```text
started       succeeded      failed          retrying        connected
disconnected  available      lost            requested       acknowledged
resumed       verified       changed         skipped         completed
interrupted   recovered      observed        pruned
```

Reason codes (or null):

```text
boot_completed             package_replaced          service_started
service_stopped            start_command              uncaught_restart
wifi_available             wifi_lost                  default_network_changed
backup_active              wifi_connected             wifi_disconnected
server_owned               ingestion_state_unknown    property_refused
readback_unavailable       readback_mismatch          scheduled_connect
adapter_discovered         adapter_not_found          gatt_connected
gatt_disconnected          gatt_error                 gatt_timeout
services_ready             notifications_ready        elm_ready
elm_timeout                protocol_search_failed     adapter_voltage_valid
ecu_proof_valid            ecu_offline                engine_running
engine_stopped             voltage_below_start        voltage_below_stop
connection_lost            connection_failed          backoff_scheduled
retry_woken                ble_callback               sleep_wake
screen_on                  user_present               power_connected
acc_on                     acc_off
engine_detected            device_restart             ingestion_requested
request_observed           quiesce_entered            quiesce_acknowledged
resume_observed            resume_completed           request_expired
bluetooth_on               bluetooth_off              hotspot_on
hotspot_off                state_unknown              export_started
export_completed           export_failed              receipt_verified
receipt_invalid            retention_pruned           first_sample_persisted
drive_summary              cadence_gap                poll_timeout
manual_request             configuration_disabled     storage_unavailable
permission_denied          unknown
```

Metrics are finite non-negative numeric scalars. Supported keys:

```text
elapsed_ms                    attempt                       retry_delay_ms
scan_ms                       connect_ms                    discovery_ms
subscribe_ms                  elm_init_ms                   ecu_probe_ms
first_sample_ms               poll_cycle_ms                 polling_duty_cycle_percent
sleep_target_s                sleep_observed_s              wifi_frequency_mhz
sample_count                  pending_bundle_count          bundle_bytes
receipt_count                 gap_count                     timeout_count
command_count                 consecutive_failures          queue_depth
```

Durations have bounded millisecond/second ranges, percentage is 0–100, sleep values are at most
3,600 seconds, bundle size is at most 512 MiB, and counters are bounded. Adding a new code or metric
requires a coordinated app/server contract update; silently accepting arbitrary fields would
defeat the privacy boundary.

## API and UI

```text
GET /api/obd/events
GET /api/obd/events?drive_id={drive_id}
GET /api/obd/events?kind={kind}&level={level}&since={ISO8601}
```

The endpoint is paginated newest-first. It returns sequence, event/received timestamps, canonical
codes, optional drive ID, numeric metrics and producer build identity. The global timeline appears
under Backup. A compact filtered timeline appears on an OBD drive page only when matching events
exist, beside the immutable bundle's drive diagnostics.
