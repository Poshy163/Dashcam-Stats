# OBD telemetry — system overview

This is the connective map of the vehicle-telemetry subsystem: what runs where, how a
drive's data travels from the ECU to a chart, and which invariants each hop is allowed to
assume. It exists so that someone (or some agent) arriving cold can find the load-bearing
pieces without re-deriving them. The deep documents stay authoritative for their own
layers:

| Layer | Deep document |
| --- | --- |
| Android logger on the head unit | [`obd-dashcam-logger.md`](obd-dashcam-logger.md) |
| Bundle wire format | [`obd-bundle-schema-v1.md`](obd-bundle-schema-v1.md) |
| Server ingestion, queue and HA delivery | [`obd-server-import.md`](obd-server-import.md) |
| Head unit hardware/radio behaviour | [`head-unit-reference.md`](head-unit-reference.md) |
| Footage pipeline the OBD path rides beside | [`../ARCHITECTURE.md`](../ARCHITECTURE.md) |

## 1. The three codebases

1. **Android logger** — `android/obd-logger/` in this repo. A single-activity app with a
   `connectedDevice` foreground service that owns the car's BLE ELM327 adapter. Built and
   unit-tested by CI (`android-obd-logger` job); production signing is a deliberate local
   step with a keystore that never enters this repo. The same signing certificate must be
   reused for every release or the head unit will refuse the upgrade.
2. **This server** (backend + frontend). Ships as one container image, published by
   `release.yml` to GHCR as `:main` + `:sha-<short>` on every push to main after the full
   CI matrix passes. The deployment tracks `:main` and updates by re-pulling.
3. **Home Assistant integration** (`obd2_ble`, separate repository). Custom component with
   two mutually exclusive source modes; deployed by copying files into HA's
   `custom_components/obd2_ble` and restarting. Its repo has no git remote — local commits
   are the record — so never assume a GitHub URL for it.

## 2. End-to-end data path

```
ECU ──BLE/ELM327──► Android logger ──TF card──► server ingest ──SQLite──► drives API/UI
                        (samples)     (bundle)    (validate)        │
                                                                    └──► HA import (hourly
                                                                         statistics + last
                                                                         known values)
```

1. **Engine start.** The logger sits in `parked`, connecting briefly on a timer to read
   adapter voltage (`ATRV`) and nothing else. Voltage ≥ 13.2 V (alternator charging) moves
   it to `probing`; a checksum-valid Mode 01 `0100` reply is the only thing that may
   declare `ecu_online` and open a drive (UUIDv7 `drive_id`).
2. **Sampling.** Fast PIDs (RPM, speed, load, throttle…) are read every cycle; slower
   tiers (coolant, intake, fuel trims, O2 sensors) every Nth cycle — see `ObdPollPlan`.
   One sparse diagnostic command (DTCs, readiness, Mode 09, freeze frame) is interleaved
   after each committed sample so a slow ECU reply can never stall the fast loop. Samples
   land in on-device SQLite (WAL) with stable `<drive_id>-<sequence>` IDs.
3. **Engine stop.** Voltage < 13.0 V sustained through a 30 s grace — vetoed while RPM
   > 300 — closes the drive. The exporter writes
   `<drive_id>.obd2.zip.partial` → fsync → rename into `…/files/obd/ready/` on the TF
   card. The bundle is immutable from that moment.
4. **Transfer.** The server's ingest poller notices the head unit on the LAN (one cheap
   socket probe per tick), and every footage pull also inventories `ready/`. Bundles are
   copied, validated, and retained under `/data/obd/verified` before anything else
   happens. Deletion of the on-device copy is *receipt-gated* (§4).
5. **Import.** A durable queue row per bundle identity drives the HA hand-off: latest
   values plus hourly UTC statistics are POSTed to the integration's `/api/obd2_ble/import`
   route with capped retry. The full-resolution samples never leave the server.
6. **Presentation.** HA shows current state and hourly history forever; the server's
   **OBD drives** pages chart every retained sample and join drives to footage journeys.

## 3. Android logger — the rules that matter

- **Radio ownership is explicit.** The app only claims the adapter after the operator
  ticks the ownership checkbox; HA's direct-BLE mode must be off first. Never run both.
- **Taint is transport-only** (since 0.1.1). `command()` returns only after the next ELM
  prompt, so the command stream stays synchronized even when a reply is garbage. A
  malformed but prompt-complete reply to an optional live PID is recorded once as a
  `parser_failure` diagnostic and that PID is suppressed for the rest of the connection.
  Missing prompt, overflow, failed write or disconnect still taint → close GATT →
  bounded exponential backoff → fresh `ATZ`.
- **A fatal mid-cycle fault persists the partial sample first**, marked
  `failed_after_partial`/`partial`, so observed values survive the reconnect. Each
  reconnect opens a *new* drive; sample IDs cannot collide.
- **Command allowlist.** Read-only Modes 01/02/03/07/09/0A plus adapter-local AT. Mode
  04/08, monitors, resets and persistent ELM writes are refused outright.
- **Public status file** at
  `…/Android/data/com.dashcamstats.obdlogger/files/obd/status.json`: schema-v4 state,
  ownership/controlled-voltage-mode flags, independent adapter/BLE/ECU/engine state, bounded
  ATRV value/source/time/freshness/quality, command-category and timing metrics, last drive,
  pending bundle count, last error, app version name/code, poll-plan version and the validated
  12-character build Git revision (or `unknown` when built without VCS metadata). The same
  identity is present in fallback/error status and
  is exposed under `logger` by `/api/obd/status`. It is best-effort telemetry for the
  server — never a control channel.
- `pending_bundle_count` stays non-zero until the logger's next drain pass (engine start
  or service restart) observes the server's receipt and prunes its raw rows. That is by
  design, not a stuck queue.
- Boot and upgrade persistence come from `BootReceiver` handling both `BOOT_COMPLETED`
  and `MY_PACKAGE_REPLACED`.

## 4. Server ingestion — invariants

The ingest poller (`backend/app/ingest/poller.py`) owns presence detection with a
sub-second tick. Its behaviours, each earned by a production failure:

- **Arrival gate**: the first pull of a visit waits until unit uptime clears a threshold,
  so backups happen on arrival rather than as doomed seconds on departure.
- **Idle recheck** every 30 s while the car stays; **re-drain cooldown** after a pull that
  moved files.
- **Bounded error retry** (`ERROR_RETRY_DELAYS_S` = 15/30/60 s): a failed run while the
  unit stays online is retried on that schedule, then stops for the visit. Before this
  existed, one receive timeout stranded a ready bundle until the next drive.
- **OBD radio ownership**: every pull reads `status.json` first. While it reports
  `ownership_enabled: true` the pull leaves Bluetooth *and* the hotspot alone in **every**
  logger state — `parked` needs the radio to notice the next engine start. A transient
  failed read never downgrades a previously observed positive ownership signal.

The OBD transfer itself (`obd_transfer.py`) is failure-fenced from footage: telemetry can
never turn a successful footage backup into a failed one. Its deletion protocol:

1. Copy to staging, stream-validate (four fixed members, sizes, SHA-256s, gzip bounds),
   atomically retain under `/data/obd/verified`, register all raw history in one
   transaction.
2. Atomically publish `receipts/<drive_id>.verified.json` on the unit (partial → verify →
   rename), then **re-read the final receipt in a separate shell round trip** and compare
   exact bytes — an ADB drop around the rename must not authorise deleting the only copy.
3. Rename the device bundle to a unique tombstone, hash that isolated inode, delete only
   on exact hash match. A replacement arriving around the rename is retained.

Queue rows (`obd_bundles`) carry the durable state machine
(`waiting_for_backup → copying → validating → ready_to_import → importing → imported`,
with `retry_wait`/`failed`/`quarantined`), identity `(drive_id, bundle_sha256,
schema_version)`, and survive restarts. HTTP 429/5xx/network → capped backoff; auth/schema
→ wait for operator Retry; re-import of an imported identity reports "already imported".

## 5. Home Assistant integration

- **Two source modes**, mutually exclusive per config entry: `direct_ble` (the integration
  owns the adapter) and `dashcam_import` (this pipeline owns it; the integration only
  accepts authenticated imports). The ownership cutover procedure lives in
  `obd-dashcam-logger.md`.
- Imported values keep their **original observation timestamps** and are attributed
  `ecu_data_status: last_known`. `binary_sensor …_ecu_connected` stays off in import mode
  deliberately.
- **Adapter voltage is retained across HA restarts** (integration ≥ 0.3.2): the import
  store persists the value plus its observation timestamp (bounded 0–40 V), and the
  sensor's direct-mode freshness window does not apply in import mode. Before 0.3.2 a
  restart wiped it to unavailable.
- Long-term history goes in as **external statistics** under
  `obd2_ble:<vehicle>_*` ids (distance, runtime, fuel, idle, averages/maxima,
  missing-data %). Hourly rows carry separate speed/RPM observation counts so overlapping
  hours average correctly; additive intervals are split at UTC hour boundaries.

**Home Assistant is structurally the wrong store for high-resolution history** — recorder
states purge after ~10 days, short-term statistics likewise, and neither can be backdated
by an integration; hourly-forever is the ceiling, and direct-DB backfill tools (e.g.
ha-backfill) buy only purged 5-minute rows at the cost of schema-coupled writes against a
stopped HA. That boundary is the reason the drives UI below exists. Do not try to move
raw samples into HA.

## 6. Server data model and API

Tables (`backend/app/db/models.py`): `obd_bundles` (queue + identity), `obd_drives`
(per-drive rollups, units, manifest/summary JSON), `obd_samples` (every sample, typed
columns per metric, `(drive_db_id, sequence)` unique), `obd_diagnostics` (sparse events).

Authenticated API (session cookie or `X-API-Key` header once a key is configured in
Settings → security; do not put standing credentials in API URLs):

```text
GET  /api/obd/status                      logger/transfer/queue snapshot
GET  /api/obd/bundles                     queue rows (filter by state)
GET  /api/obd/drives                      drive list with rollups + import_state
GET  /api/obd/drives/summary              library-wide aggregate totals
GET  /api/obd/drives/{drive_id}/series    every sample + diagnostics + journey link
GET  /api/obd/drives/{drive_id}/bundle    hash-checked immutable archive download
GET  /api/obd/drives/for-journey/{id}     best time-overlap drive for a footage journey
POST /api/obd/drives/{drive_id}/reprocess rebuild lifecycle/gap projection idempotently
POST /api/obd/bundles/{id}/validate       manual revalidation / quarantine promotion
POST /api/obd/bundles/{id}/retry          re-queue a failed HA import
POST /api/obd/queue/rebuild               re-register orphans in /data/obd/verified
```

Journey ↔ drive matching is a server-side UTC span-overlap join (both clocks are already
normalised); best overlap wins. It is exposed in both directions: the series response
carries `journey {id, title, overlap_s}`, and `for-journey` returns the drive.

## 7. Frontend pages

- **`/obd`** (`ObdDrives.tsx`): library totals row (drives, km, time, fuel + overall
  L/100 km, idle share, top speed, samples stored) above a paginated drive table with
  HA import-state badges.
- **`/obd/:driveId`** (`ObdDriveDetail.tsx`): two rollup tile rows (including client-side
  derived stats: stopped share, moving-only average, hardest accel/braking, coolant
  warm-up), ten SVG time charts, time-in-band bars for speed and RPM, and the diagnostic
  event table. A journey button appears when a footage journey overlaps.
- **Journey detail** embeds an "Engine telemetry" tile row for its matched drive.

Chart rendering rules (all in `ObdDriveDetail.tsx`, no chart library):

- Series bridge only up to **1.5× the explicit poll-plan cadence** (5/15/60 s by tier).
  Sparse or interrupted data therefore cannot teach the renderer that a long outage was
  normal. Isolated observations render as measured dots, never silently discarded; the
  legend names the last observation time and marks it stale beyond the same threshold.
- Tooltips follow mouse **and touch** (`pointermove`, `touch-action: pan-y` so the head
  unit's screen still scrolls), snap to the nearest sample, and show elapsed time plus
  wall-clock date/time; a null at the exact sample remains null rather than borrowing a
  nearby value. Each series carries its own unit and measured/derived provenance.
- Axis tick decimals follow the axis *range*, not value magnitude, and an all-non-negative
  series never gets a padded negative floor.
- Derived time-in-band numbers charge each interval to the sample ending it, capped at
  15 s, so dropouts are not billed to whichever band the car died in.

## 8. Operational shape

- **Server deploy**: push to main → full CI (backend, frontend, Android, Docker smoke) →
  GHCR publish → re-pull `:main` on the Docker host (Dockge stack "dashcam"). `/health`
  plus the served `index-*.js` fingerprint confirm which build is live. Rollback = pin the
  previous `sha-*` tag.
- **HA deploy**: back up the deployed `custom_components/obd2_ble` first, hash-verify the
  deployed files against the last-deployed commit before overwriting, copy, clear
  `__pycache__`, restart HA, verify the entry loads and imported values survived.
- **Android deploy**: build + sign (same certificate, bump `versionCode`), `adb install
  -r` over the LAN, confirm the installed `base.apk` SHA-256 matches the artifact and the
  foreground service is up. The head unit's address is DHCP — do not hard-code it, and
  keep the server's `unit_adb_address` setting current.
- **The head unit has no battery**: it is only reachable while the car is running, freshly
  parked, or externally powered. Design every automated interaction around short windows.
- The HA bearer token lives only in a mode-0600 file mounted into the container
  (`HA_TOKEN_FILE`); it is absent from compose, git, logs and API responses by
  construction. Rotate by replacing the file and restarting the stack.

## 9. What has been proven on real hardware

As of 2026-08-30, verified end-to-end on the physical car (Nissan Tiida, UIS7861 head
unit, Android 14): parked→probing→ecu_online on a real engine start; drive close on
voltage decay + grace; bundle export; **unattended** server collection 52 s after the
bundle appeared; receipt-gated remote deletion; HA import with original timestamps;
voltage retention across an HA restart; Bluetooth remaining enabled through footage pulls
while the logger held ownership; and hourly statistics accumulating per drive. The
sparse-tier chart rendering was validated against the first real drives' data.
