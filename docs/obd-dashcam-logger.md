# Dashcam OBD logger

The on-device logger is the Android companion under `android/obd-logger`. It is deliberately
separate from the server container while using the same version-one bundle contract and the
existing ADB arrival backup.

The exact producer/validator contracts are [obd-bundle-schema-v1.md](obd-bundle-schema-v1.md)
and [obd-app-event-schema-v1.md](obd-app-event-schema-v1.md).

## Why it is an APK

The verified unit is a locked, unrooted UIS7861 head unit running Android 14 (SDK 34). The
existing device component is a small shell health watcher copied to `/data/local/tmp` and
detached with `setsid`; the analytics application itself runs on the backup server. Android's
shell exposes no BLE GATT client, the unit has no Python/Bleak runtime, `/data/local/tmp` is not
a boot service, and a release APK cannot be accessed later with `run-as`. A shell or pure-Python
logger would therefore be test code, not something this unit can run.

The companion uses a `connectedDevice` foreground service, a low-importance persistent
notification, `BOOT_COMPLETED`, Android's Nearby Devices plus network-state permissions,
app-private SQLite in WAL mode, and app-specific removable external storage. Foreground promotion
happens before database migration or integrity work; database/exporter initialization then runs on
the service IO scope so a large upgrade cannot consume Android's foreground-start deadline. It
holds no indefinite wake lock. If boot finds Bluetooth permission missing it disables itself,
publishes a redacted error and waits for the operator to reopen the setup screen; it does not retry
in a tight loop.

Default host-readable paths (verify the removable-volume alias on the physical unit):

```text
/storage/Tfcard/Android/data/com.dashcamstats.obdlogger/files/obd/status.json
/storage/Tfcard/Android/data/com.dashcamstats.obdlogger/files/obd/events.json
/storage/Tfcard/Android/data/com.dashcamstats.obdlogger/files/obd/control/ingestion-request.json
/storage/Tfcard/Android/data/com.dashcamstats.obdlogger/files/obd/control/ingestion-ack.json
/storage/Tfcard/Android/data/com.dashcamstats.obdlogger/files/obd/ready/<drive_id>.obd2.zip
/storage/Tfcard/Android/data/com.dashcamstats.obdlogger/files/obd/receipts/<drive_id>.verified.json
```

The backup server's `DASHCAM_OBD_REMOTE_READY_DIR`, `DASHCAM_OBD_REMOTE_STATUS_FILE`,
`DASHCAM_OBD_REMOTE_EVENTS_FILE`, and `DASHCAM_OBD_REMOTE_RECEIPTS_DIR` settings are overrides
for a unit whose app-specific path resolves differently. Status schema v6 publishes
`ingestion_quiesce_v1`,
`voltage_only_audit_v1`, `controlled_voltage_only_mode_v1` and
`adaptive_sleep_window_v1`, current/last drive identity,
last sample, pending bundle count, the correlated ingestion request ID and fixed-schema
saturating pipeline counters. It also keeps adapter reachability, transient BLE connection, ECU,
engine and drive state independent; exposes the last reported BLE owner and producer update time;
publishes bounded sleep-policy target/readback/verification evidence; and publishes a bounded
ATRV value, source, original sample time, freshness and quality. Every
ordinary, error and internal-storage fallback payload also
identifies the installed artifact with `app_version_name`, `app_version_code`,
`poll_plan_version` and `build_git_sha`. The build revision is a validated 12-character lower-case
Git revision sourced from `GITHUB_SHA` or the checkout's `HEAD`; it is `unknown` only when neither
is available. Gradle injects it directly into `BuildConfig` without generating a tracked file.
These fields are passed through under `logger` by `GET /api/obd/status`. Status contains no adapter
address, VIN, credentials, arbitrary command/response payload or ECU telemetry. The only retained
response text is the validated numeric ATRV token, such as `12.7 V`.

The `app_event_stream_v1` capability means the app also owns a transition-only event ring. It
keeps at most 2,048 rows or seven days in its private store and atomically publishes the latest
512 in `events.json`. The exact document has schema/source/generated time/sequence bounds,
bounded producer build identity, and an ascending `events` array. Every event contains only a
persistent sequence, timestamp, random session UUID, an allowlisted kind/level/outcome/reason,
an optional safe drive ID, and allowlisted finite numeric timing/counter metrics. It has no
free-form message, hardware/network identifier, raw adapter response or exception text. The
source UUID is random and app-scoped, not an Android/hardware identity; the server hashes both it
and the session UUID, deduplicates by source hash plus sequence, and never returns either hash.
Missing or malformed event telemetry cannot block footage, OBD bundle, receipt or radio recovery.
The vendor ACC state is sampled every five seconds as a fallback wake edge, in addition to Android
Bluetooth, screen, user-present and power broadcasts, so a head unit that emits none of those
broadcasts does not sit through the parked retry interval after ignition-on. The normal
service-stop record is reserved outside the bounded producer queue and the event worker
gets a bounded drain window. An abrupt process or power loss can still prevent that final notice;
the next boot/recovery transition is the durable evidence for that boundary.
The server retains the validated mirror for 90 days up to 50,000 rows and exposes it through
`GET /api/obd/events`, the Backup activity timeline and each matching OBD drive page.

The companion writes and reads back a fixed 900-second countdown while Wi-Fi is connected or a
valid ingestion lease is active, and a fixed 300 seconds otherwise. Known ACC-on state prevents
the post-ingestion handoff from narrowing an active Wi-Fi window; it does not select 900 seconds
by itself. This policy is always on: the server exposes it and both values read-only in Settings,
coerces any older disabled toggle or duration override back to the fixed contract, and never lets
the puller branch on a stale cached toggle. The two controllers therefore cannot fight over the
persistent property. Managed writes use a small bounded retry schedule and never stop OBD
collection when the vendor property is unavailable. A fresh status advertising
`adaptive_sleep_window_v2` makes request removal the app's safe completion trigger, so the server
does not race the final 300-second write. Before accepting that evidence, the server directly
reads the current property; stale status falls through to its verified 300-second fallback write.
The server still widens to 900 before arrival recovery and uses the same fallback for v1, missing
or invalid app status.

## Build, sign and install

The Android project targets SDK 34 and compiles with SDK 35. CI runs the JVM tests, debug and
release lint, and both assemblies. Its release APK is deliberately **unsigned**, and the debug APK
uses the disposable Android debug signer. Both are development/build-verification artifacts only;
never deploy either as the production logger. A later APK can update the installed app only when
it has the same application ID and release certificate, so changing or losing the production key
would require uninstalling the logger and losing its app-private database and preferences.

Provision one stable signing key outside this repository on a protected workstation, keep its
password in a password manager, and store an encrypted offline backup of both the keystore and its
recovery instructions. Restrict the live file to the build account. For example, after selecting a
secure path and alias:

```powershell
keytool -genkeypair -v -keystore D:\protected\obd-logger-release.jks `
  -alias obd-logger -keyalg RSA -keysize 4096 -validity 10000
```

Use environment variables (or an ignored `android/obd-logger/keystore.properties`) for the build.
Setting `OBD_PRODUCTION_SIGNING=true` makes missing or partial input a clear build failure rather
than silently emitting an unsigned production candidate:

```powershell
$env:OBD_PRODUCTION_SIGNING = "true"
$env:OBD_RELEASE_KEYSTORE_PATH = "D:\protected\obd-logger-release.jks"
$env:OBD_RELEASE_KEYSTORE_PASSWORD = "<from password manager>"
$env:OBD_RELEASE_KEY_ALIAS = "obd-logger"
$env:OBD_RELEASE_KEY_PASSWORD = "<from password manager>"

cd android\obd-logger
.\gradlew.bat --no-daemon clean :app:testDebugUnitTest :app:lintRelease :app:assembleRelease
$apk = "app\build\outputs\apk\release\app-release.apk"
& "$env:ANDROID_HOME\build-tools\35.0.0\apksigner.bat" verify --verbose --print-certs $apk
Get-FileHash -Algorithm SHA256 $apk
adb install -r $apk
```

Before each deployment, save the APK SHA-256 and the `Signer #1 certificate SHA-256 digest` from
`apksigner` in the release record. Compare that certificate digest with the previous production
release before installation. Preserve and reuse the same signer for every update; never regenerate
it merely because a build host changed. The equivalent ignored properties are
`obdProductionSigning`, `obdReleaseKeystorePath`, `obdReleaseKeystorePassword`,
`obdReleaseKeyAlias`, and `obdReleaseKeyPassword`. The repository also includes
`android/obd-logger/Dockerfile` for an isolated unsigned verification build. No SDK path, keystore,
MAC address, password or token belongs in `local.properties` or source control.

On first launch, enter a lower-case stable vehicle ID and the BLE adapter address. The app creates
and persists a bounded random logger ID on first run; it does not derive it from a raw Android or
hardware identifier. The value can be replaced manually with another bounded deployment ID.
The setup screen also persists the engine-on/off voltage hysteresis (both bounded to 10–16 V,
with off strictly below on), the 0–300 second off grace and a 15–3600 second parked interval.
Controlled voltage-only mode is an absolute fence that permits `ATRV` but never reset,
initialisation or ECU commands, even if voltage crosses the engine-on threshold. A protected build
can force the same fence with `-PobdVoltageOnlyAudit=true` for a physical audit, then be replaced by
the ordinary production build. Saving a changed runtime mode cancels and joins the active worker,
closes its BLE client, then starts a new worker under the new immutable command policy. Invalid
values disable the logger.
The app asks only for Nearby Devices (`BLUETOOTH_CONNECT`) plus notification permission on
Android 13+. It uses direct configured-MAC GATT and never calls a Bluetooth scan API, so it does
not request `BLUETOOTH_SCAN` or location.
Do not enable the persistent ownership checkbox until the cutover below is complete.

## Ownership cutover and safety proof

1. Turn off `switch.nissan_tiida_obd2_connection` in Home Assistant and confirm it remains off.
2. Stop every phone OBD scanner and confirm Home Assistant no longer has a GATT connection.
3. Open the companion, check the explicit ownership-transfer box, enable logging and grant the
   requested permissions.
4. Confirm the notification/status moves through `parked` and `probing` to `ecu_online` only after
   a checksum-valid `0100` response. Voltage alone is not ECU proof.
5. Complete a short drive, then verify one final `.obd2.zip` and no matching `.partial` file.
6. Run the ordinary arrival backup and confirm the server validates the bundle before deleting it
   from the dashcam.

Parked checks issue exactly one adapter-local `ATRV`; they do not reset or configure the ELM and do
not send a Mode 01 request while voltage remains below the configured 13.2 V start threshold. In
controlled voltage-only mode they cannot progress past this command at any voltage. Otherwise,
only after that cheap gate passes does the logger run the known reset/configuration sequence and
require a checksum-valid ECU response. Live stop uses the 13.0 V threshold, a 30-second grace, and a recent
RPM above 300 veto. Every command passes a read-only allowlist. Mode 04/08, raw monitors,
security/programming/reset requests and persistent ELM writes are absent and refused.

Before device ingestion takes Bluetooth away, the controller atomically writes the exact
five-field schema-v1 `control/ingestion-request.json` (`schema_version`, safe `request_id`,
`action=prepare_for_ingest`, `requested_at_utc`, `deadline_at_utc`). The logger checks between
commands: it schedules no new PID after seeing the file, lets the current bounded command finish,
persists any non-empty partial sample, records final pipeline metrics, finalises atomically, exports
and validates the immutable bundle, performs a FULL WAL checkpoint, then closes BLE. Only then does
it atomically publish the exact nine-field acknowledgement containing the correlated ID, state,
ready time, nullable drive/sample/bundle identity and nullable bounded error. A parked logger can
acknowledge with null drive metadata. A stale startup request first recovers and exports any
`recording`/`finalising` drive. `deadline_at_utc` is the overall quiesce hold lease: its requested
duration must be 1–600 seconds, with only 60 seconds of bounded clock-skew tolerance. An expired
lease, an overlong lease, or a request materially in the future is atomically removed with its
correlated acknowledgement and treated as absent, so a controller crash cannot pause logging
forever. A malformed request blocks only while its safe file timestamp is within the same bounded
lease-and-skew horizon; an older crash remnant is atomically removed with its acknowledgement. A
reboot invalidates any prior hold before polling. While a current request persists (including
fresh malformed input), the logger neither reconnects nor polls the ECU; controller removal or
safe lease expiry clears the acknowledgement and resumes.

Every allowed command is classified as `adapter_local` or `vehicle_bus` before the FFF1 write. The
public metrics distinguish requested/sent/completed commands, adapter-local and vehicle-bus
counts, target resolution, GATT establishment, notification subscription, successful/failed
voltage reads, connection failures, invalid voltage replies, disconnects, median/maximum timings,
total connected time and polling duty cycle. Strict ATRV parsing requires exactly one numeric `V`
token in the conservative 9.0–16.5 V range; empty, `?`, `NO DATA`, suffix-free, ambiguous and
out-of-range replies are invalid rather than clamped.

The single FFF1 command stream is prompt-delimited and bounded. A missing prompt, failed protocol
search, write uncertainty, overflow, multiple prompts, non-whitespace trailing bytes or an idle
notification taints the session without carrying bytes into the next command; the service closes GATT and starts again
with a fresh `ATZ` only after bounded exponential backoff with jitter, capped at 30 seconds. A
checksum-valid ECU proof resets that escalation immediately, even though the active drive remains
inside the same service call. Bluetooth-on, screen-on, user-present and power-connected broadcasts
interrupt a pending retry or parked-probe wait, while a new ingestion lease preempts either wait
within 250 ms. This prevents failures from earlier in a long-running service process delaying the
next post-sleep connection by minutes.
Sparse diagnostic scans read Modes
03/07/0A, Mode 01 readiness/MIL, supported Mode 09 calibration evidence, and correctly framed
Mode 02 freeze-frame number 0 (including explicit empty/no-data evidence). Strict parsing requires
the complete `48 6B <source>` ISO header, learned ECU source, exact length and checksum. A malformed
reply is recorded as a parser failure; it is never treated as an empty PID or cleared DTC list.
Because `command()` only returns once the next ELM prompt arrives, a malformed but prompt-complete
reply to an optional live PID leaves the command stream synchronized. That PID enters a bounded
per-PID cooldown and is retried; a transient MAF failure can recover, while a repeatedly malformed
advertised PID backs off to at most one retry per 12 sample cycles. Strict source, length and
checksum validation is unchanged. The v3 plan polls engine load, RPM and vehicle speed in every
five-second cycle. Timing, MAF, throttle and the remaining medium PIDs are phase-distributed
across three cycles; slow PIDs use phases 0/4/8 of each 12-cycle window. This keeps the
driving-critical stream inside the target cadence on the serial ISO 9141 adapter instead of
regularly overrunning it while attempting every optional command. App `0.2.3` identifies this
plan and hardened bundles declare `poll_plan_version=3`; the server retains the v2 cadence contract
for historical drives.

App `0.2.8` declares plan **v4**. It was designed from a measured drive rather than a guess:
under v3 the cycle was already at the five-second ceiling (six commands took ~4.36 s, seven
~5.05 s and an overrun), so nothing could be added to it. v4 changes the cost of a command
instead of the count. Every live mode-01 request now carries the ELM327 expected-response-count
suffix (`010C1`), so on this single-ECU K-line car the adapter returns the moment the one reply
lands instead of sitting out its adaptive timeout; an adapter that answers `?` to the suffix turns
it off for that session and the request is repeated in the plain form, so the worst case is
exactly v3. The forced 100 ms inter-command gap drops to 40 ms (the ELM enforces ISO 9141 P3
itself). The two PIDs that cannot change during a drive -- `0x13` which O2 sensors exist and `0x1C`
the OBD standard -- leave the rotation, are read once after the ECU proof, and their answers are
carried into every sample, so downstream shapes are unchanged; the server treats them as `static`
provenance and never counts them toward measured completeness. The slow slot they free takes
`0x01`, the one supported PID this car has that earlier plans never asked for, decoded as
`mil_on` and `dtc_count`. Driving-critical cadence is deliberately untouched; a tighter medium
rotation waits for the suffix to prove itself on a real drive.
Only transport-level faults (missing prompt, overflow, failed write, disconnect) still taint and
reconnect, and a fatal fault mid-cycle first persists the partial sample already gathered, marked
`failed_after_partial`/`partial`, so observed values survive the reconnect.
The scan is interleaved at no more than one diagnostic command after each committed fast sample.
It starts only when at least two seconds remain in that cycle's five-second budget; otherwise the
step stays queued for a later cycle instead of making an existing cadence gap worse. The serial
adapter can still take longer than that reserve, but an in-flight ELM command is never cancelled
because doing so would taint prompt ownership and require a reconnect. The scans never send Mode
04/08 or clear DTCs.

## Local data and bundle v1

SQLite stores drives, samples and sparse diagnostics transactionally with stable UUIDv7 drive IDs,
stable `<drive_id>-<sequence>` sample IDs, UTC timestamps, explicit values and parser/transport
quality. WAL uses `synchronous=FULL`: an externally powered head unit can disappear without a clean
shutdown, so a committed sample is not reported as persisted until SQLite has crossed the
power-loss durability boundary. Each connection captures one UTC anchor and advances drive/sample/response/notice time
only from Android elapsed realtime, so NTP or manual wall-clock changes cannot regress a drive's
sequence timestamps. Recovery clamps later lifecycle clocks to the final sequence sample when the
wall clock was corrected backward, and bundle creation is likewise ordered after finalisation.
Diagnostics deduplicate only an unchanged consecutive value of the same kind; a real
`A → B → A` transition keeps all three observations and timestamps. Startup recovery closes an
orphaned `recording` or `finalising` drive at its final persisted sample timestamp (not at the
later notice time). It then prioritizes the first adapter voltage/ECU check over background ZIP
repair; terminal unexported drives are drained once the unit is confirmed parked, at the next
drive terminal, or synchronously before an ingestion acknowledgement. Only an actual
`BOOT_COMPLETED` launch labels a stale recording `recovered` with `device_restart`;
ordinary service/process recreation labels it `interrupted` with `process_terminated`, while a
pre-existing `finalising` marker keeps its original reason. Live connection, command, parser, ingestion, administrative and process
faults end as `interrupted`; only the engine-off gate produces clean `complete`. Finalisation first
persists a `finalising` marker and then atomically fixes the terminal status, reason, evidence-based
finish, last sample, real last prompt-complete response, later notice time and finalisation time.
Repeating either finalisation or startup reconciliation leaves those clocks and export identity
unchanged. This also retries a crash after finalisation and prior export failures.
Zero-sample crash remnants are retained in SQLite with
`export_status=not_exportable_zero_samples`; no server-invalid empty bundle is published.
Every terminal drive also receives a bounded `pipeline_metrics` diagnostic covering command,
notification/fragment/frame, timeout, checksum/parser, sample/persistence/drop, BLE disconnect,
failure-triggered reconnect and direct-path queue depth counters without raw payloads.
`status.json` keeps the latest sampled timestamp, bounded metrics snapshot and immutable build/poll
identity, but durable status writes occur immediately only for state/ownership/drive/request/error
changes and otherwise on a one-minute heartbeat. SQLite sample commits remain independent,
avoiding an extra TF-card fsync and atomic rename on every five-second polling cycle.

Before an on-device schema upgrade, the logger forces a complete WAL checkpoint, validates the
source, then publishes a synced main-only migration snapshot with a ready marker; SQLite SHM is
never treated as durable backup data. Restore copies and validates a staged main file before an
atomic replacement, keeps a synced full-set restore marker until stale WAL/SHM/journal files are
removed and the restored database passes `quick_check`, and replays that marker after power loss.
The live main file is never pre-deleted. A valid retained backup is also recovered before open when
the main file is absent or corrupt.

Each final `<drive_id>.obd2.zip` is `ZIP_STORED` and contains exactly:

```text
manifest.json
samples.ndjson.gz
diagnostics.json
summary.json
```

The exporter writes `<drive_id>.obd2.zip.partial`, flushes the payloads and archive, validates
member names/sizes/SHA-256 hashes, then renames it atomically. The manifest records schema version
1, identity/times, `completion_status=complete`, `interrupted` or `recovered`, the separate
`clean_end` flag, lifecycle clocks/reason, exact units, counts, and a
size/hash/count map for all three payload members. The whole-file SHA-256 is recorded in SQLite
and recomputed by the server after copy; it cannot be embedded in the archive it hashes. The
server is the primary high-resolution history. Home Assistant receives the final sample identity,
the latest non-null value for every telemetry field with its original observation timestamp, and
historical aggregate statistics under their original UTC timestamps.

The manifest also carries the drive's non-negative `error_count`; the server validates and stores
it rather than replacing it with zero. Ready bundles are never written to internal-storage
fallback. If the removable TF volume is not mounted yet after boot, logging/export waits, the
foreground notification and fallback status report `storage_unavailable`, and completed drives
remain `waiting_for_backup`. Once TF mounts, the normal loop drains them into the configured ready
directory. This prevents an exported bundle from being stranded outside the server's ADB scan.

Raw SQLite retention is intentionally conservative. The most recent 16 completed/exported drives
remain available for local re-export. On each drain pass, at most 16 older candidates are examined
and at most four are removed, in deterministic `finish_time_utc, drive_id` order. Recording,
complete-but-unexported, zero-sample quarantine, malformed-receipt and recent rows are never
removed. Child diagnostics and samples are deleted before their drive in one foreign-key-enabled
transaction, and the persisted bundle SHA is rechecked in that transaction. If the immutable ready
archive still exists, it must validate and still match its persisted SHA; a corrupt or replaced
file blocks pruning. If it is missing, the local export record alone is not proof of backup. Pruning
then requires the server's exact acknowledgement, written atomically only after its copy and raw DB
registration are durable and before it removes the device bundle. The acknowledgement is at
`receipts/<drive_id>.verified.json`, is at most 512 bytes, and contains exactly
`schema_version=1`, the safe drive ID and its 64-character lower-case bundle SHA-256. Symlinks,
non-regular files, path escapes, invalid UTF-8, duplicate/extra keys, malformed JSON and any field
mismatch are rejected. A valid acknowledgement is removed only after the matching raw DB prune
commits.

This is a safety bound, not permission to discard the only copy: a recording or export backlog can
grow beyond it, and a corrupt ready archive deliberately keeps its raw rows. New databases enable
incremental auto-vacuum before schema creation; pruning also performs a separate bounded passive
WAL checkpoint and incremental page reclaim. Existing databases are not converted with a blocking
full `VACUUM`, so their freed pages are reused and the file may not shrink on disk.

## Rollback and remaining physical checks

Disable logging in the companion and wait for its status to show `disabled`, or uninstall it after
the server has verified every pending bundle. Then turn the Home Assistant connection switch back
on. Never run the two owners concurrently.

The source, JVM tests, Python contract tests and build path can be verified offline. Deployment is
not proven until the physical unit is online and these hardware-specific checks pass: APK install
and boot start, runtime permission persistence, ADB visibility of the app-specific removable path,
actual FFF1 notification/write behavior, valid `0100`, engine-stop disconnect, a real drive export,
and transfer/delete only after server verification. If Android restricts ADB access to the chosen
external-files directory on this vendor build, select another app-specific external volume and set
the two server path overrides; do not request broad storage access as a shortcut.
