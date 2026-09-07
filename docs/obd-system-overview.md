# OBD telemetry system overview

This subsystem runs entirely within Dashcam Analyser and its Android companion. These
documents describe each layer:

| Layer | Document |
| --- | --- |
| Android logger | [`obd-dashcam-logger.md`](obd-dashcam-logger.md) |
| Bundle format | [`obd-bundle-schema-v1.md`](obd-bundle-schema-v1.md) |
| Server storage and recovery | [`obd-server-import.md`](obd-server-import.md) |
| Head-unit radio behavior | [`head-unit-reference.md`](head-unit-reference.md) |
| Footage pipeline | [`../ARCHITECTURE.md`](../ARCHITECTURE.md) |

## Components and data path

1. `android/obd-logger/` runs a foreground service on the head unit and owns the BLE
   ELM327 adapter after the operator explicitly transfers ownership from every other BLE
   client.
2. The server and web UI ship together in the Dashcam Analyser container.

```text
ECU -- BLE/ELM327 --> Android logger -- atomic bundle --> server ingest
                                                        |-- SQLite raw history
                                                        `-- OBD drives UI/API
```

The logger moves from `parked` to `probing` when adapter voltage suggests the engine is
running. Only a checksum-valid Mode 01 response may declare `ecu_online` and open a drive.
Fast PIDs are sampled every cycle, slower tiers at their configured cadence, and sparse
diagnostic commands are interleaved after committed samples.

At engine stop, the logger closes the drive and atomically publishes an immutable bundle
to the TF card. The server copies and validates it, stores the archive under
`/data/obd/verified`, and commits its full-resolution contents to SQLite. A receipt that is
written and read back successfully gates deletion of the on-device copy.

## Safety and ownership

- Exactly one BLE client may own the adapter. Stop phone scanners and any other competing
  client before enabling the logger.
- Malformed prompt-complete optional PID responses are recorded and suppressed for the
  rest of that connection. Missing prompts, failed writes, overflows, and disconnects
  close GATT and enter bounded retry.
- A fatal mid-cycle fault stores the partial sample before reconnecting. Each reconnect
  opens a new drive, so sample identities cannot collide.
- The command allowlist permits read-only OBD modes and adapter-local commands. Reset,
  monitor, and persistent-write commands are refused.
- When the public status file reports logger ownership, footage ingest leaves Bluetooth
  and the hotspot alone, including while the logger is parked or backing off.

## Server storage and API

The server stores bundle identity and state in `obd_bundles`, per-drive rollups in
`obd_drives`, every typed observation in `obd_samples`, and sparse events in
`obd_diagnostics`. Bundle states are `waiting_for_backup`, `copying`, `validating`,
`stored`, `failed`, and `quarantined`.

The authenticated API provides logger and backup health, bundle inspection and validation,
drive summaries, every retained sample, archive download, journey matching, and idempotent
reprocessing. See [obd-server-import.md](obd-server-import.md) for the endpoint list and
recovery procedure.

The OBD drives list shows library totals and stored drive summaries. Drive detail charts
keep sparse observations visible, bridge only cadence-sized gaps, expose original wall-clock
timestamps, and preserve measured/derived provenance. Journey details embed telemetry from
the best overlapping drive.

## Deployment boundaries

Server releases pass backend, frontend, Android, and container checks before publication.
Android production builds must reuse the existing signing certificate and increment
`versionCode`; the head unit rejects an upgrade signed by another key. Verify the installed
APK hash and foreground service after deployment.

The head unit has no battery and is reachable only while running, freshly parked, or on
external power. Treat build results, server storage checks, physical BLE behavior, and a
completed real transfer as separate evidence.

As of 2026-08-30, the physical setup had demonstrated engine detection, drive close,
bundle export, unattended collection, receipt-gated deletion, and retained raw-history
charts. This dated result is historical evidence, not proof of the current deployment.
