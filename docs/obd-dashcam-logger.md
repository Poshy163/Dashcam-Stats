# Dashcam OBD logger

The on-device logger is the Android companion under `android/obd-logger`. It is deliberately
separate from the server container while using the same version-one bundle contract and the
existing ADB arrival backup.

The exact producer/validator contracts are [obd-bundle-schema-v1.md](obd-bundle-schema-v1.md)
and [obd-app-event-schema-v1.md](obd-app-event-schema-v1.md).

Companion 0.3.9 adds optional, report-only on-camera clip movement manifests and server
maintenance wake holds. Setup, classification limits and physical checks are documented in
[clip-movement-manifests.md](clip-movement-manifests.md). These reports do not filter or delete
footage, change the OBD poll plan, or stop the recorder.

Companion 0.3.10 adds an experimental, opt-in visual quiet-clip transfer policy. It requires
both views, complete guarded analysis and fresh server verification; exclusions stay on
the card and remain separate from backed-up or processed files. See
[quiet-clip-transfer-policy.md](quiet-clip-transfer-policy.md) for resource limits, deferred
uploads, scanner handoff and the physical checks required before activation.

Companion 0.3.12 adds an opt-in three-second speed, RPM and MAF trial with per-PID
response-completion timing, matching calculation changes and a recorded fallback to
the baseline poll plan. Deploy the matching server first. See
[obd-three-second-trial.md](obd-three-second-trial.md) for activation, rollback and
physical acceptance; it does not establish a live web telemetry stream.

Companion 0.3.20 adds bounded on-device SoC temperature history owned by the enabled
foreground service, including its normal boot/update recovery. Capture is independent
of OBD quiescing and the server-deployed CarPlay sampler. It records only while the
device naturally runs; it neither holds the camera awake nor extends a sleep window.
See [dashcam-temperature-capture.md](dashcam-temperature-capture.md) for the storage
contract, import path and capture-versus-import health evidence.

Companion 0.3.21 also hardens settings and recovery. Configuration reloads wait for
the actual previous logger to finish; repeated Apply requests coalesce. Invalid
drafts preserve the saved logger configuration, while explicit OFF saves only the
disabled setting. Trial-setting storage runs away from the UI thread and retains
revision-checked fallback protection. Successful interrupted-drive recovery clears
stale active-drive state, and export reconciliation uses consistent pagination for
equal SQLite timestamp sort keys.

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
`adaptive_sleep_window_v1` and `adaptive_sleep_window_v2`, current/last drive identity,
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
The vendor ACC state has a nominal five-second poll as a fallback wake edge, in addition to Android
Bluetooth, screen, user-present and power broadcasts, so a head unit that emits none of those
broadcasts does not sit through the parked retry interval after ignition-on. The normal
service-stop record is reserved outside the bounded producer queue and the event worker
gets a bounded drain window. An abrupt process or power loss can still prevent that final notice;
the next boot/recovery transition is the durable evidence for that boundary.
The server retains the validated mirror for 90 days up to 50,000 rows and exposes it through
`GET /api/obd/events`, the Backup activity timeline and each matching OBD drive page.

The companion writes and reads back a fixed 1200-second countdown while Wi-Fi is connected or a
valid ingestion lease is active, and a fixed 300 seconds otherwise. Known ACC-on state prevents
the post-ingestion handoff from narrowing an active Wi-Fi window; it does not select 1200 seconds
by itself. This policy is always on: the server exposes it and both values read-only in Settings,
coerces any older disabled toggle or duration override back to the fixed contract, and never lets
the puller branch on a stale cached toggle. The two controllers therefore cannot fight over the
persistent property. Managed writes use a small bounded retry schedule and never stop OBD
collection when the vendor property is unavailable. A fresh status advertising
`adaptive_sleep_window_v2` makes request removal the app's safe completion trigger, so the server
does not race the final 300-second write. Before accepting that evidence, the server directly
reads the current property; stale status falls through to its verified 300-second fallback write.
The server still widens to 1200 before arrival recovery and uses the same fallback for v1, missing
or invalid app status.

### Recovery after native countdown cancellation

The server-only recovery update uses the installed 0.3.10 companion's existing
`sleep_deadline_evidence_v1` proof to recognise an actual ON to OFF cycle even if the
server missed live ON. No APK version change or reinstall is required. The Android poll
waits five seconds after each read; property work and scheduling can lengthen that
interval, so neither a short pulse nor a connected Wi-Fi session guarantees capture.

After a completed cancellation gesture and same-boot power verification, a version-two
server marker durably records a monotonic cancellation upper bound plus one second of
padding in `cycle_after_elapsed_ms`; a pre-gesture `null` bound remains blocked.
Fresh native OFF, fresh companion OFF proof and the exact same boot UUID are
required. The proof's `off_lower_elapsed_ms`, the start of its last actual ON sample,
must be strictly after that barrier. Repeated OFF observations cannot qualify an older
cycle: they refresh observation time while preserving its original lower/upper bounds
and captured window. Companion 0.3.17 saves that genuine edge in an atomic, app-private
checkpoint excluded from Android backup. A brief process restart or package update can
restore it only after a fresh OFF/property read on the exact same kernel boot, within
the existing 15-second observation-continuity budget. The original captured deadline
cannot advance. Invalid, stale or missing checkpoints still require a new observed ON
before another edge; unknown reads and fresh ON invalidate saved OFF proof. A checkpoint
never clears a maintenance cancellation marker using a preceding cycle.

Clearing compares the full expected marker, including its generation and bound, so
concurrent cancellation or baseline updates cannot be erased. Legacy markers first
need a fresh same-boot native OFF observation while maintenance is inactive and owns
no fence to save a padded baseline for a future cycle; the edge already present is
excluded. Corrupt records and unresolved version-two bounds cannot use this legacy
path. Existing live-ON/new-boot recovery is retained. Successful
recorded-cycle validation uses only its conservative remaining deadline and preserves
operational ceilings and radio-restoration gates. This backend update has not yet
been physically verified; see [clip-movement-manifests.md](clip-movement-manifests.md)
for the complete recovery and acceptance conditions.

## Build, sign and install

The current source/candidate status and remaining work are tracked in
[obd-logger-release-status.md](obd-logger-release-status.md); a source version is not proof of
installation on the physical unit.

The Android project targets SDK 34 and compiles with SDK 35. CI runs the JVM tests, debug and
release lint, and both assemblies. Its release APK is deliberately **unsigned**, and the debug APK
uses the disposable Android debug signer. Both are development/build-verification artifacts only;
never deploy either as the production logger. A later APK can update the installed app only when
it has the same application ID and release certificate, so changing or losing the production key
would require uninstalling the logger and losing its app-private database and preferences.

This workstation's production signing key is at
`android/obd-logger/secrets/dashcam-obd-prod.p12`, with its build credentials in
`android/obd-logger/keystore.properties`. The properties file uses
`obdReleaseKeystorePath=secrets/dashcam-obd-prod.p12`, relative to the Android project root.
Both files are Git-ignored and excluded from the root and Android Docker build contexts.
Their locations are also documented in the root `AGENTS.md` for future agents.
Fresh clones and worktrees do not contain the signing files; reuse the existing production key
from the original checkout. Preserve its file permissions and an encrypted offline recovery
backup of the key, credentials and recovery instructions.

Use the ignored properties file for local builds, or environment variables on another protected
build host. Gradle command-line properties and environment variables override the local file;
remove stale overrides when using the relocated key.
Setting `OBD_PRODUCTION_SIGNING=true` makes missing or partial input a clear build failure rather
than silently emitting an unsigned production candidate:

```powershell
$env:OBD_PRODUCTION_SIGNING = "true"

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

Version 0.3.7 stores the ingest webhook key as authenticated AES-256-GCM ciphertext in private
preferences, using a non-exportable Android Keystore wrapping key. The first full configuration
load migrates an existing plaintext value only after encryption and round-trip verification;
one synchronous preference commit installs ciphertext and removes plaintext together. Failure
retains the stored value and reports `webhook_credential_unavailable`; it does not fall back to
plaintext or stop OBD collection. An unreadable encrypted key cannot be erased by an unrelated
save with an empty key field. Entering a replacement key can recover delivery when Keystore is
usable. The setup screen performs secure load/save off the UI thread and shows a persistent
credential warning. Boot/service permission gates read only non-secret configuration before
foreground promotion, so Keystore work cannot delay that deadline. Android backup remains
disabled; the wrapping key is independent of the production APK signing key.

The live panel has an expandable metrics/sleep view with bounded pipeline counters, duty cycle,
target/readback/verification, ACC and ingestion hold. It reads at most 64 KiB per candidate file
off the UI thread, chooses the newest timestamp-valid card/fallback status, and flags future
timestamps stale. It displays allowlisted values rather than raw error bodies or identifiers.
The additive v1 webhook event codes require the matching server update before the new APK;
0.3.6 attempted to emit these events but its producer allowlist dropped them.
Do not enable the persistent ownership checkbox until the cutover below is complete.

## Ownership cutover and safety proof

1. Stop every existing OBD client and confirm it remains disconnected.
2. Stop every phone OBD scanner and confirm no other client has a GATT connection.
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

App `0.2.9` declares plan **v5** and exists because v4 failed in the field. The logger's local
SQLite `samples` table is fixed-column -- every sample value is a column -- and v4 added the
`0x01` values without their columns, so the first `0x01` poll of every drive (the fifth cycle)
failed its insert, the drive ended as `database_fault`, and a three-minute trip became six
twenty-second fragments. 0.2.9 adds `mil_on INTEGER` and `dtc_count INTEGER` (database version 4),
binds whole numbers and booleans as integers on write, and exports them with the JSON types the
server validator requires (boolean, integer). The same drives also settled two open questions:
the response-count suffix saved essentially nothing on this adapter (its adaptive timing was
already short), while the 40 ms gap took the six-command cycle from ~4.36 s to ~4.0 s; and the
eight-command cycle -- a slow PID landing on a four-medium phase -- still overran at ~5.4 s every
third cycle. v5 therefore moves the slow PIDs to phases 1 and 5, where the medium rotation carries
three PIDs, so no cycle ever needs an eighth command. v4 keeps its own cadence map on the server
for the bundles the 0.2.8 logger exported.
Only transport-level faults (missing prompt, overflow, failed write, disconnect) still taint and
reconnect, and a fatal fault mid-cycle first persists the partial sample already gathered, marked
`failed_after_partial`/`partial`, so observed values survive the reconnect.
The scan is interleaved at no more than one diagnostic command after each committed fast sample.
Ordinary cycles require the connection's learned command reserve to fit in the remaining
five-second budget. That reserve can grow to six seconds, which previously starved all queued
diagnostics on a busy bus. Version 0.3.5 / poll plan v6 reserves cycle 3 and then at most one cycle
per 13 cycles for primary speed/RPM/load plus one read-only diagnostic step. It defers the rotating
PIDs on those cycles and starts the diagnostic only after a persisted sample, no bus-silence
condition, and primary work finishing within five seconds. Ordinary diagnostic progress postpones
the next reservation. The existing command timeout still bounds the diagnostic; a reserved cycle
can exceed the sample target. Coverage counts deferred observations as missing. The serial adapter
can still take longer than the learned reserve, but an in-flight ELM command is never cancelled
because doing so would taint prompt ownership and require a reconnect. The scans never send Mode
04/08 or clear DTCs. These changes require an APK update; refreshing server-managed Wi-Fi helper
scripts does not replace the companion APK.

App `0.3.8` / poll plan **v7** adds measured admission for optional live readings. The
five-second target, critical load/RPM/speed reads and adapter-voltage evidence are retained.
Before each optional command, the scheduler checks its learned cost plus a completion margin
against the remaining cycle budget. Deferral rotates fairly among the due PIDs and remains
visible as missing data; no old dynamic values are copied forward. The nominal medium and
slow expectations remain 15 and 60 seconds, so coverage continues to expose deferred work.
Reserved sparse diagnostics keep their existing bounded exception. Unexpected latency can
still overrun a cycle; in-flight commands are not cancelled to make timing look better.

The same release adds a per-PID response-count compatibility fallback. A prompt-complete
suffixed reply failing ISO validation is rejected, and only that PID uses the plain form on
its next normally scheduled attempt. This does not issue an immediate retry or weaken source,
checksum or exact-length validation. A malformed plain reply stays missing. The per-session
decision resets on reconnect, while adapters returning `?` retain the existing whole-session
unsupported-suffix handling. Production raw frames were not retained, so the fallback must be
confirmed on the physical adapter in a subsequent drive rather than assumed to repair every
rejected reply.

## Local data and bundle v1

SQLite stores drives, samples and sparse diagnostics transactionally with stable UUIDv7 drive IDs,
stable `<drive_id>-<sequence>` sample IDs, UTC timestamps, explicit values and parser/transport
quality. WAL uses `synchronous=FULL`: an externally powered head unit can disappear without a clean
shutdown, so a committed sample is not reported as persisted until SQLite has crossed the
power-loss durability boundary. Each connection captures one UTC anchor and advances drive/sample/response/notice time
only from Android elapsed realtime, so NTP or manual wall-clock changes cannot regress a drive's
sequence timestamps. Recovery clamps later lifecycle clocks to the final sequence sample when the
wall clock was corrected backward, and bundle creation is likewise ordered after finalisation.
Database version 5 adds a nullable producer `poll_plan_version` per drive. New drives record v7;
an additive migration assigns only Git-verified historical release mappings. Unknown legacy
producers retain no asserted plan and export the supported legacy v1 manifest/summary shape.
Delayed exports use the recorded producer's plan, and valid ready archives for supported plans
2–7 remain immutable when the APK changes.
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
The 0.3.3 logger adds a drive-scoped `poll_timing` diagnostic without changing the v5 poll plan,
five-second target, BLE configuration, sample values or row timestamps. It writes at most once
per 60 seconds while recording, plus a nonempty final or interrupted window. After 64 periodic
windows it keeps aggregating for the final window, so a drive emits at most 65 such records.
This caps output size rather than discarding later requests. Process death can still lose the
current unflushed window; a failed diagnostic database write is best effort and cannot interrupt
collection or replace the original transport error.

Each version-one window reports its index, elapsed start relative to drive start, duration,
flush reason, target cycle time, started/completed cycles, overruns, cycle work and cycle-start
interval distributions. Work ends before the diagnostic write and cadence sleep; it includes
live requests, voltage, sample persistence and any sparse diagnostic/recovery work. An interrupted
cycle contributes work and an overrun if applicable, but is not completed. Completed cycles have
a persisted full row and reach the normal cycle end. The start-interval distribution includes the
interval crossing a window boundary. Elapsed time includes Android suspend and is clamped against
clock regression; it does not depend on wall-clock corrections.

Each observed live PID has request attempts and separate success, empty decoded result, malformed,
timeout, transport-error, cancellation and cooldown-skip counts. Skips are not attempts. Request
time includes existing command pacing, lock wait and parsing, so it is not pure ECU response
latency. Only successful decoded requests receive an age at the row timestamp, measured immediately
beside that timestamp before the database write and retained only if the row was inserted. A
persisted partial row can retain ages for its earlier successful requests; its failed request has
no value age. This measures completion-to-row age, not the unknown sensor measurement time. Static
values carried forward from startup, ATRV and sparse diagnostic commands have no per-PID age entry.

Every timing distribution has total count, retained count, median, nearest-rank p95, maximum and
total milliseconds. Median and p95 describe the last 256 observations; maximum and total cover the
whole window. Empty median/p95 are null and other empty statistics are zero. Counts saturate at
one million, individual observations at 24 hours, and elapsed fields/totals at 10^12 milliseconds.
No response text, sensor values, vehicle identity or addresses enter `poll_timing`. The historical
`pipeline_metrics.polling_duty_cycle_percent` keeps its existing definition; use the new cycle work
and cadence distributions to assess whether faster polling has measured headroom.

Companion 0.3.15 extends this diagnostic to payload schema 2 with bounded exclusive
`cycle_stages_ms` distributions. The fixed stages identify critical and auxiliary
requests, handoff checks, persistence, diagnostic work, status publishing and residual
cycle work. They account for recorded cycle work without raw values or messages;
cadence sleep and the later diagnostic-window flush are outside stage attribution.
Deploy the accepting server before the companion update. Schema-1 windows and the
outer version-one bundle contract remain supported.

Every terminal drive also receives a bounded `pipeline_metrics` diagnostic covering command,
notification/fragment/frame, timeout, checksum/parser, sample/persistence/drop, BLE disconnect,
failure-triggered reconnect and direct-path queue depth counters without raw payloads.
`status.json` keeps the latest sampled timestamp, bounded metrics snapshot and immutable build/poll
identity, but durable status writes occur immediately only for state/ownership/drive/request/error
changes and otherwise on a one-minute heartbeat. SQLite sample commits remain independent,
avoiding an extra TF-card fsync and atomic rename on every five-second polling cycle.

The launcher activity reads the same `status.json` every two seconds while it is in the
foreground and renders a dark, large-type summary above the configuration form: logger state and
age of the last publish (flagged `STALE` after 90 s, which is how a stopped service looks from the
head unit), vehicle state, adapter/ECU connection, battery voltage, current drive, last sample age,
bundles waiting for the server, Wi-Fi/sleep hold and the last error. It keeps the screen on while
open. There is no service binding; the panel is purely a reader of the published file, so it shows
exactly what the server would see over ADB.

The ingest-ready webhook retries on transient failures (`webhook_network_error`,
`webhook_timeout`, `webhook_server_error`) with a bounded 3 s / 10 s / 20 s backoff before giving
up; auth, client and `disabled` outcomes are not retried. Each attempt is recorded in the event
stream.

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
server is the primary high-resolution history, retaining every sample and its original
observation timestamp.

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

If a retained drive is re-exported after its archive and acknowledgement are missing, the
export timestamp changes its archive hash. The server accepts this as an equivalent export
only after validating both archives, proving the original still matches its trusted stored
record, and comparing every manifest field except `created_at_utc` and the exact sample,
diagnostic and summary member bytes. Changed telemetry, source identity or lifecycle metadata
remain conflicts. The original archive and database history remain unchanged. The exact new
archive is retained under `verified/reexports/<new-sha256>/<original-filename>`, flushed and
read back before the server acknowledges the new hash and performs hash-guarded device
reclamation. Radio admission also verifies this retained alternate against the original;
neither a matching drive ID nor an unverified receipt can authorize backup shutdown.

This is a safety bound, not permission to discard the only copy: a recording or export backlog can
grow beyond it, and a corrupt ready archive deliberately keeps its raw rows. New databases enable
incremental auto-vacuum before schema creation; pruning also performs a separate bounded passive
WAL checkpoint and incremental page reclaim. Existing databases are not converted with a blocking
full `VACUUM`, so their freed pages are reused and the file may not shrink on disk.

## Rollback and remaining physical checks

Disable logging in the companion and wait for its status to show `disabled`, or uninstall it after
the server has verified every pending bundle. Only then may another BLE client claim the adapter.
Never run two owners concurrently.

The source, JVM tests, Python contract tests and build path can be verified offline. Deployment is
not proven until the physical unit is online and these hardware-specific checks pass: APK install
and boot start, runtime permission persistence, ADB visibility of the app-specific removable path,
actual FFF1 notification/write behavior, valid `0100`, engine-stop disconnect, a real drive export,
and transfer/delete only after server verification. If Android restricts ADB access to the chosen
external-files directory on this vendor build, select another app-specific external volume and set
the two server path overrides; do not request broad storage access as a shortcut.
