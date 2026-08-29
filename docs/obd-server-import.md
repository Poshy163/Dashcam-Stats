# OBD bundle backup and Home Assistant import

The server treats OBD backup and Home Assistant delivery as two independent durable
operations. A failed, offline or unauthorised Home Assistant instance never changes the
result of a footage backup. The server retains the original verified archive plus the full
five-second sample history; Home Assistant receives a bounded final-sample identity,
the newest non-null value and original UTC timestamp for each telemetry field, and at most
744 UTC-hour statistics rows.

The on-device companion and the one-owner BLE cutover are documented in
[obd-dashcam-logger.md](obd-dashcam-logger.md). Do that ownership cutover before enabling
the companion. Two clients must not share the ELM327 adapter.
The strict archive/member/field contract is
[obd-bundle-schema-v1.md](obd-bundle-schema-v1.md).

## Deployment configuration

Create a Home Assistant long-lived access token from the intended service account's profile
and store only the token in a host file excluded from source control. Do not put it in the
compose environment, the Dashcam Analyser settings API, a command line or this repository.

```yaml
services:
  dashcam:
    image: ghcr.io/poshy163/dashcam-analyser:latest
    environment:
      - HA_URL=http://homeassistant:8123
      - HA_TOKEN_FILE=/run/secrets/home_assistant_token
      - HA_OBD_IMPORT_PATH=/api/obd2_ble/import
      # Override only if the removable-volume alias differs on the physical unit:
      # - DASHCAM_OBD_REMOTE_DIR=/storage/Tfcard/Android/data/com.dashcamstats.obdlogger/files/obd/ready
      # - DASHCAM_OBD_STATUS_PATH=/storage/Tfcard/Android/data/com.dashcamstats.obdlogger/files/obd/status.json
      # - DASHCAM_OBD_REMOTE_RECEIPTS_DIR=/storage/Tfcard/Android/data/com.dashcamstats.obdlogger/files/obd/receipts
    secrets:
      - home_assistant_token

secrets:
  home_assistant_token:
    file: ./secrets/home_assistant_token
```

`HA_URL` may use plain HTTP only for a loopback, private/LAN, `.local`, or container host.
Use HTTPS for a public DNS name. `HA_OBD_IMPORT_PATH` must remain an absolute `/api/...`
path. The token file must be a regular non-symlink file, at most 16 KiB, and not writable by
group or other users. Docker's read-only secret mount is accepted. Status, logs, database
errors and API responses redact bearer values and never return the token.

The default device paths are:

```text
/storage/Tfcard/Android/data/com.dashcamstats.obdlogger/files/obd/ready
/storage/Tfcard/Android/data/com.dashcamstats.obdlogger/files/obd/status.json
/storage/Tfcard/Android/data/com.dashcamstats.obdlogger/files/obd/receipts
```

Confirm all three through ADB on the physical unit before relying on an arrival window. Missing
or malformed `status.json` is best-effort status only and never fails bundle or footage
backup.

`status.json` is also read at the start of every footage pull, whether or not a bundle is
waiting. While it reports `ownership_enabled: true` the pull leaves the unit's Bluetooth and
hotspot alone instead of quieting them for transfer throughput — the logger owns that radio in
every state, including `parked` and `backoff`, because voltage probing is how it notices the
next engine start. A transient failed read never downgrades a previously observed positive
ownership signal. Separately, a footage run that fails while the unit stays online is retried
on a bounded backoff (15 s, 30 s, 60 s) within the same visit rather than waiting for the next
offline/online edge, so one receive timeout cannot strand a ready bundle for a whole day.

## Durable flow

1. Inventory accepts only `<safe-drive-id>.obd2.zip`, ignores sibling `.partial` files and
   copies oldest first into a unique `/data/obd/staging/.transfer-*.partial` directory.
2. The server refuses links, unexpected/path-like/duplicate/encrypted/compressed ZIP
   members, excessive sizes or compression ratios, invalid hashes, unsafe IDs and malformed
   JSON. It independently streams and bounds `samples.ndjson.gz`.
3. A valid archive is flushed and atomically renamed into `/data/obd/verified`. In one
   database transaction the server adds its immutable identity, summary, diagnostics and
   every high-resolution sample. Unique drive, sample ID and drive-sequence constraints make
   replays idempotent. The manifest's non-negative drive `error_count` is retained alongside
   the summary rather than inferred from sparse diagnostic events. A transaction failure
   leaves no partial history.
4. Only after the verified copy and transaction succeed does the server atomically publish
   `receipts/<drive_id>.verified.json` on the dashcam. The strict receipt contains only
   `schema_version`, `drive_id`, and the lowercase bundle SHA-256, is capped at 512 bytes,
   and is read back before deletion. A receipt write/type/content/sync failure retains the
   source archive. A same-name/size duplicate is also SHA-256 hashed on the device before
   this fast path; a mismatch or unavailable remote hash command falls back to the bounded
   copy/validation path. Immediately before deletion the server atomically renames the
   current device pathname to a unique same-directory tombstone, hashes that isolated inode,
   and deletes only that exact hash. A replacement that arrives before or after the rename is
   retained. The server then may delete the proven source archive. Invalid bytes move to
   `/data/obd/quarantine`; the device copy remains.
   Even when the manifest cannot be trusted, a durable rejection row records the safe
   filename, observed SHA-256, size and redacted error. It therefore remains in counts and
   manual recovery after restart rather than becoming an untracked `.bad` file.
5. The independent HA worker claims the oldest eligible drive and revalidates its retained
   bytes. Its gzip JSON request uses `(drive_id, bundle_sha256, schema_version)` as the
   idempotency key. HTTP 404 during HA route startup, 408, 425, 429 (including HA's busy
   response), 5xx and transport failures use capped exponential retry and `Retry-After`;
   other 4xx responses stop for operator action.

The HA body keeps the literal final sample for drive/session identity and adds a strict
`latest_values` map derived while streaming validation. Each present telemetry field carries
its newest non-null value plus its original UTC timestamp, so tiered polling cannot hide a
fresh coolant/trim/O2 value just because the final fast cycle omitted it. Diagnostics use the
same rule: DTC, MIL, readiness, calibration, protocol, CVN and freeze-frame continuity values
carry their original event timestamps, and older drives never regress newer retained state.

Each hourly row carries total `sample_count` plus `speed_sample_count` and
`rpm_sample_count`. The latter two count only non-null readings, so Home Assistant can merge
overlapping drives without diluting an average when a transport sample lacked that PID.
Additive duration/distance/fuel/idle intervals are split at UTC-hour boundaries. Expected
and missing-sample accounting assigns expected observation instants to their actual hour;
missing data and cross-hour gaps are not charged wholesale to the previous hour.

On restart, an `importing` claim becomes immediately retryable before another claim is
made. Orphan discovery then runs in the background, so an archive history cannot block
`/health`. Known, unchanged archive sizes are not rehashed on every boot; each one is still
revalidated immediately before an HA attempt and through the manual Validate action.

## Operations and recovery

The Backup page is the normal control surface. It shows the companion's redacted
`ecu_online`/parked state, device pending count, copy throughput, queue states, the current
HA import, authentication/configuration status, last success and last error.

The same controls are available over the authenticated API:

```text
GET  /api/obd/status
GET  /api/obd/bundles?state=retry_wait
POST /api/obd/bundles/{id}/validate
POST /api/obd/bundles/{id}/retry
POST /api/obd/queue/rebuild
```

- Use **Validate** after investigating a retained copy. A failed revalidation moves the
  bytes to quarantine and disables HA retry. Pre-registration rejection rows are explicitly
  marked as untrusted: validating a repaired or newly supported copy promotes that same row,
  updates its observed hash, and transactionally stores all samples/diagnostics before it
  becomes ready. A trusted immutable identity must still retain its original SHA-256.
  Validate and Retry return 409 while a copy, validation or HA import claim is active.
- Use **Retry** after fixing Home Assistant auth, schema/profile or network configuration.
  Imported identities are not sent again; retrying one reports it already imported.
- Use **Rebuild queue** after restoring `/data/obd/verified` independently from the
  database. It registers valid orphans idempotently and quarantines invalid ones; it never
  deletes an archive.

Useful deployment checks after migration are the current Alembic revision, OBD state counts,
one representative drive's sample count, a successful HA response, and confirmation that no
token appears in logs or `/api/obd/status`. Preserve `/data/obd` and `dashcam.db` together in
backups.

When an existing local SQLite database is stamped at an older Alembic revision, startup first
creates and integrity-checks
`/data/backups/pre-migration-<old>-to-<head>-<UTC timestamp>.db`. The schema upgrade begins
only after that atomic snapshot succeeds; a backup or integrity-check failure aborts startup
with the original revision untouched. A new database and an already-current database do not
create a migration backup, so ordinary restarts do not accumulate redundant copies.

## Rollback

Disable ownership/logging in the Android companion and allow its pending count to reach zero.
Stop the server or remove the three `HA_*` settings to stop delivery, then re-enable the old
Home Assistant BLE owner only after the companion reports disabled. Removing HA configuration
does not delete server samples or verified archives; queued rows remain visible for a later
retry. Do not delete `/data/obd/verified`, quarantine files or the OBD database tables as part
of routine rollback.

Offline unit tests prove parsing, hashes, transaction rollback, retry classification and API
recovery. They do not prove Android scoped-storage visibility, BLE behaviour, engine-off
closure, the physical transfer window or a live Home Assistant import. Record those as separate
deployment acceptance checks rather than treating a build or synthetic bundle as hardware
evidence.
