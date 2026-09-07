# OBD bundle backup and raw history

The server collects immutable OBD bundles from the Android companion, validates them, and
stores both the original archive and every sample in its own database. The five-second
driving-critical values, slower telemetry tiers, diagnostic events, observation timestamps,
and measured/derived provenance remain available through the OBD drives UI and API.

The companion and one-owner BLE cutover are documented in
[obd-dashcam-logger.md](obd-dashcam-logger.md). The archive contract is
[obd-bundle-schema-v1.md](obd-bundle-schema-v1.md).

## Device paths

The defaults are:

```text
/storage/Tfcard/Android/data/com.dashcamstats.obdlogger/files/obd/ready
/storage/Tfcard/Android/data/com.dashcamstats.obdlogger/files/obd/status.json
/storage/Tfcard/Android/data/com.dashcamstats.obdlogger/files/obd/events.json
/storage/Tfcard/Android/data/com.dashcamstats.obdlogger/files/obd/receipts
```

Confirm all four through ADB on the physical unit before relying on an arrival window.
`DASHCAM_OBD_REMOTE_DIR`, `DASHCAM_OBD_STATUS_PATH`,
`DASHCAM_OBD_REMOTE_EVENTS_FILE`, and `DASHCAM_OBD_REMOTE_RECEIPTS_DIR` override them when
the removable-volume alias differs. Missing or malformed status and event files fail soft;
they never fail bundle or footage backup.

While `status.json` reports `ownership_enabled: true`, footage pulls leave Bluetooth and
the hotspot alone in every logger state. The logger needs the radio while parked so it can
probe voltage and notice the next engine start. A transient status read failure does not
downgrade a previously observed positive ownership signal.

## Durable flow

1. Inventory accepts only `<safe-drive-id>.obd2.zip`, ignores `.partial` siblings, and
   copies oldest first into a unique staging directory.
2. Validation rejects links, unexpected or duplicate members, unsafe identifiers,
   excessive sizes or compression ratios, bad hashes, and malformed JSON. A missing or
   invalid derived summary is rebuilt from validated samples with a warning.
3. A valid archive is flushed and atomically renamed under `/data/obd/verified`. One
   database transaction stores its immutable identity, summary, diagnostics, and every
   sample. Replays are idempotent and a failed transaction leaves no partial history.
4. The server publishes a bounded receipt on the unit and reads it back in a separate ADB
   round trip. Only an exact receipt and bundle hash permit deletion of the device copy.
   Invalid bytes move to `/data/obd/quarantine`, while the device copy remains.

Bundle rows use `waiting_for_backup`, `copying`, `validating`, `stored`, `failed`, and
`quarantined`. Existing installations migrate previously verified delivery-queue rows to
`stored`; the migration preserves archives, samples, diagnostics, and unrelated settings.

Revision `0022` removes the retired delivery queue's retry metadata and result columns.
It renames existing notification settings to `ingest.webhook_url` and `ingest.mqtt_*`,
preserving their values; an already configured new key takes precedence. Deployment URL,
import-path, and token-file variables for the retired integration are no longer read.
Remove their unused environment entries and secret mounts from your deployment when
upgrading. No archive or raw telemetry is removed. Automatic pre-migration backups retain
the original schema for recovery; downgrading reconstructs the old columns without the
discarded delivery results.

## Operations and recovery

The Backup page shows the logger state, pending device copies, current transfer, stored
drive count, failures, last stored drive, and recent app events. The OBD drives pages expose
full-resolution history. Journey matching is a server-side UTC span-overlap join, with the
best overlap exposed in both directions.

Authenticated endpoints include:

```text
GET  /api/obd/status
GET  /api/obd/events?drive_id={drive_id}&kind={kind}&level={level}&since={ISO8601}
GET  /api/obd/bundles?state=stored
GET  /api/obd/drives
GET  /api/obd/drives/summary
GET  /api/obd/drives/{drive_id}/series
GET  /api/obd/drives/{drive_id}/bundle
GET  /api/obd/drives/for-journey/{journey_id}
POST /api/obd/drives/{drive_id}/reprocess
POST /api/obd/bundles/{id}/validate
POST /api/obd/storage/rebuild
```

The list returns stored rollups and storage state. `series` returns every retained sample,
ordered by sequence and original UTC timestamp, plus cadence/gap analysis and diagnostics.
`reprocess` idempotently rebuilds derived lifecycle and quality fields. `bundle` verifies
size and SHA-256 before returning the archive. Use Validate after investigating or repairing
a retained copy; invalid bytes are quarantined without erasing the database record.

**Recover stored bundles** on the Backup page registers valid archives that are missing
from the database, such as after restoring an older database backup. It reports registered,
duplicate, and quarantined counts. Startup also reconciles interrupted validations and
orphan archives; this recovery does not require the head unit to be online.

Preserve `/data/obd` and `dashcam.db` together in backups. Before upgrading an older local
SQLite database, startup creates and integrity-checks a timestamped pre-migration snapshot.
Failure aborts the upgrade with the original revision untouched.

Offline tests cover parsing, hashes, transaction rollback, migration, and API recovery.
They do not prove Android scoped-storage visibility, BLE behavior, engine-off closure, or a
physical transfer window. Record those as separate deployment checks.
