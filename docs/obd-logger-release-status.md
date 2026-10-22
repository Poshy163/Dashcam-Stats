# OBD logger companion — current release status

Quick orientation for the Android app that runs on the head unit. The full design and
procedures live in [`obd-dashcam-logger.md`](obd-dashcam-logger.md); this page is the
"where are we right now" summary.

## Installed version

| | |
|---|---|
| Version | **0.3.12** (`versionCode` 25), installed in place on 22 October 2026 |
| Source | `6875998e149268480f76f9a4ad9e633429ebb8a9` (`android/obd-logger/`), verified in the running companion |
| Application ID | `com.dashcamstats.obdlogger` |
| Target / compile SDK | 34 / 35 |
| Status schema | `status.json` schema version 7 |
| Poll plan | 7 reported while parked; the three-second plan 8 trial is armed for the next drive |
| Deployment | Production-signed 0.3.12/code 25 was installed in place on 22 October 2026, retaining configuration and permissions. The running companion's revision and version were verified separately from the APK. Both quiet-filter opt-ins remain enabled with unchanged thermal and proof gates. No physical three-second drive has been accepted. |

## 0.3.12 polling trial

Companion 0.3.12/code 25 from build `6875998` is installed. Its three-second trial is
armed for the next drive; the parked companion still reports baseline plan 7. No
physical three-second drive has been accepted.

The in-place installation on 22 October 2026 retained existing configuration and
permissions. The APK matched the pinned production signing certificate and had SHA-256
`a6f06bdec37d78ab579764d8b3724d2b4be0d594ddf945ab0b50593ce6e0a608`.

- An opt-in immutable poll plan 8 targets fresh speed, RPM and MAF reads every three
  seconds. Plan 7 remains the default; slower readings retain explicit opportunities
  and measured admission rather than being copied forward as fresh.
- Successful reply-completion ages accompany the retained sample. Distance,
  acceleration and fuel calculations consume their source times with plan-specific
  continuity and hold limits; legacy bundles retain their original interpretation.
- A failed trial records its reason, retains its interrupted segment, disables the
  option and returns to the baseline plan through a safe connection boundary.
- The accepting server must be deployed before the producer. See
  [obd-three-second-trial.md](obd-three-second-trial.md) for the full procedure,
  calculation limits and physical acceptance requirements.
- Local validation on 22 October passed 274 Android tests, 3,320 backend tests with
  33 platform/fixture skips, and 229 frontend tests, with lint/typecheck/build and
  Python formatting checks passing. Source-window, missing-data and reply-time UI
  behavior was visually checked with synthetic fixtures. These are implementation
  checks; no physical three-second drive has been accepted.

### Server maintenance retry update (physical acceptance pending)

The implementation now records explicit version-three `native_verified` provenance
after a witnessed countdown cancellation and durable same-boot post-gesture proof.
An operator's duration-only maintenance request can retry that verified cancellation
after fresh native OFF/awake/normal-policy and absent-countdown checks. The complete
durable marker must still match under the controller and process fences. Older or
provisional records are not automatically eligible; neither a cached flag nor an absent
popup can authorize copying.

Each successful retry creates a new bounded lease and retains the cancellation
tombstone. Expiry, stop or failure still closes admission and restores radios; it does
not establish a normal native sleep deadline or invent an ignition transition. The
30-second host evidence lifetime, 45-second on-device heartbeat lifetime and 60-second
restoration reserve are unchanged.

The UI snapshot helper now serializes its accessibility ownership with an on-device
kernel lock, and transient heartbeat transport failures may retry within the original
evidence budget. The Backup page explains retry prerequisites and still requires new
server verification. All 230 frontend tests, typecheck, lint and build pass. Live
acceptance of this maintenance fix, a resulting backup and subsequent requested sleep
remains pending; installation of 0.3.12 alone does not verify that server-only path.

Live investigation on 22 October reproduced a separate transport race: a normal
5,432-byte UI snapshot became an empty successful shell reply when discovery
disconnected ADB during the command, then became valid again after reconnecting.
The first deployed retry held admission correctly but lost its first heartbeat
before any files copied. Discovery now reuses responsive transports, and a shared
address guard prevents reconnects and daemon-root restarts during maintenance
preparation and active holds. Admission closes and the guard is released before
radio restoration, so a dead connection can still recover. Empty replies require
complete bounded reproof; malformed nonempty replies remain refused. The resumed
production poller and backup still need physical acceptance.

### What changed in 0.3.11

- Fresh native segments were three seconds apart. The 0.3.10 closest-peer limit was two
  seconds, and that transfer gate ran before decoder selection, so fresh otherwise
  eligible clips could never reach analysis. Version 0.3.11 schedules independent clip
  analysis before final transfer pairing.
- Exclusion requires all overlapping opposite-view segments to independently qualify
  and continuously cover the complete candidate interval. The previous overlapping
  segment is checked too; activity, locked or unknown views and any uncovered gap retain
  upload. This does not widen the old closest-peer tolerance.
- Independently quiet or pending boundary views may still provide their own validated
  evidence when retained for upload. The server verifies the candidate and witness
  identities and sampled hashes before exclusion; retained witnesses do not count as
  excluded or backed up. Fixed six-minute deferral and all thermal/resource limits remain.
- On 22 October the enabled 0.3.10 producer reported ready manifests and ordinary upload
  decisions, with zero genuine exclusions. Visual results were `not_analyzed`, so the
  observations do not establish a production decoder thermal rejection. The camera was
  above the 85 C guard. That pre-update observation is distinct from the production
  0.3.11 resource-guard result below.
- The production-signed 0.3.11 APK was installed in place on 22 October. Its certificate
  matched the pinned production certificate, and APK SHA-256 was
  `9fd436d3e3c156ffb223d96ae49f2ba5d3f7fb458ac4a3683f3168a890b23375`.
  The application UID and OS boot were retained; the logger restarted normally for
  the update. Ownership, read-only DCIM access and quiet-filter opt-in were retained,
  movement reporting was ready, and passive GPS remained disabled. Both native camera
  active files independently rolled and grew during the update. Newly observed camera
  boundaries were approximately six seconds apart, reinforcing the need for interval
  coverage rather than a wider fixed closest-peer threshold.
- Fresh 0.3.11 policy evidence around 01:47 UTC was ready and enabled, with 56 upload
  decisions and zero skips. Three production analysis results were `unavailable`, each
  with zero decoded or sampled frames. Fresh companion evidence was parked, ACC OFF,
  with ingestion state known and no ingestion request; the SoC was approximately
  107.0 C. This physically verifies independent scheduling and resource refusal that
  preserves ordinary upload. It does not prove complete quiet decoding, sustained paired
  capacity, a genuine exclusion or parked-activity transfer under the new policy.
- Final Android validation on 22 October: all 252 tests passed without skips or failures.
  Debug and release lint reported zero errors and 56 existing warnings each. This includes
  the staggered-view publisher, unsafe/missing overlap, fixed deadline and full-card
  report-truncation regressions; physical acceptance remains separate.
- Final backend regression on 22 October: 3,235 tests passed with 33 platform/fixture
  skips and unhandled-thread warnings treated as errors. Ruff checks and formatting
  passed for all 295 checked files. Physical quiet-exclusion acceptance remains separate.
- After deployment, a fresh same-boot accessory-power cycle cleared the cancellation
  marker and supplied a decreasing unit-derived countdown. Polling was active during
  this cycle, so it does not isolate the camera-recorded-only clearance path. Runs
  584–586 copied 105 files / 6,282,289,404 bytes; run 585 retained a partial cleanup result,
  with its logger-resume debt subsequently verified cleared. Run 586 finished `ok` with
  Bluetooth, hotspot and logger resumption verified. Its final inventory had no eligible
  backlog or policy deferrals. The automatic scanner found 95 new files without errors,
  then settled 25 changed files in a later pass; ordinary processing continued.
- Fresh device evidence at 02:15 UTC still measured approximately 107 C, with ready,
  enabled policy output and zero exclusions. Actual quiet exclusion and its downstream
  scan/job absence remain unverified. Temperature history coverage and its limitations
  are documented in [dashcam-temperature-history.md](dashcam-temperature-history.md).

### What changed in 0.3.10

- Experimental on-camera scene-change screening can omit quiet parked front/rear pairs
  from transfer after separate companion and server opt-in. Both default off. Locked,
  active, dark, uncertain or incompletely analysed clips upload normally; excluded clips
  stay on the card. Short analysis deferrals remain backlog and expire by clip end plus
  six minutes or earlier when the native sleep budget is insufficient.
- A separate durable exclusion ledger and Backup page distinguish decisions from copied
  files. Exclusions create no recordings or processing jobs; later uploads scan normally.
  Known omitted intervals cannot bridge automatic journeys without retained camera coverage.
- Backup commits coalesce an immediate scanner handoff, preserving scanner enablement,
  settling and automatic-processing settings. Historical ingest errors redact SQL parameter
  dumps as well as bearer credentials.
- Private real-footage grids replayed through the actual Kotlin v2 tracker classified both
  reviewed quiet views as quiet, the parked pedestrian and driving views as activity, and
  the dark view as unavailable. This covers those examples; small/distant activity and
  sustained decoder capacity still require broader physical validation.
- After installation, a front hardware probe completed 1,499 frames covering 60.033
  seconds in 55.740 seconds of wall time, reached both end-of-stream markers, and returned
  quiet. The rear attempt returned unavailable before decoding any frames
  (`soc_hot_or_unknown`). Both cameras' native active files independently rolled and grew
  after the front probe. This is one observed recording-continuity window, not sustained
  paired-decoder validation.
- A later shell SoC reading during normal recording/backup was 93.760 C. Further decoder
  tests were stopped. A production-signed, read-only private instrumentation probe then
  read both thermal nodes under the existing app UID and PID without restarting it.
  Expected UID/PID checks passed, the PID was unchanged before and after, and the SoC type
  matched. The app-UID reading was 92.995 C and the CPU guard correctly returned false.
  Thermal access is verified; the measured temperature prevents visual analysis.
- The packaged maintenance helper physically verified native countdown cancellation and
  a maintenance hold with fresh heartbeat evidence. Heartbeat evidence was lost around
  08:58 UTC, before the planned 09:07 UTC expiry; the exact cause is unproven. This is not
  evidence that the server forced native sleep. The hold was subsequently ended through
  the API and verified inactive.
- Backup run 580 copied 57 files / 3,486,902,740 bytes, with zero backlog at that run's
  final observed inventory. It ended partial after Wi-Fi dropped during restoration.
  Subsequent server checks verified Bluetooth, hotspot and logger restoration, with
  `recovery_required=false`; the transition remained failed/inactive. This is not a
  fully successful run or a claim that later recordings cannot add backlog.
- Both quiet-filter options were enabled on 22 October at the operator's request. The
  companion checkbox changed from unchecked to checked, and the server accepted
  `ingest.skip_parked_footage=true`. A SoC reading of approximately 102.9 C was above
  the unchanged 85 C guard, so no further decoder probe was launched. Resource failures
  preserve ordinary upload; enablement does not establish a genuine exclusion. Ordinary
  uploaded moving, stopped, quiet and parked-activity examples completed metadata,
  telemetry, detection and plate processing; the filtered end-to-end path still needs
  physical acceptance.
- Newly copied `20261021190938_camera_1.ts` was automatically indexed as recording 18165
  and queued without a manual scan. All four stages were pending on 21 October. A fresh
  22 October check found metadata, telemetry, detection and plate processing complete,
  with no error or missing-file flag, verifying ordinary automatic handoff and completion.
- Ordinary backup run 582 completed on 22 October, copying 239 video files /
  12,724,617,944 bytes and separately acknowledging one duplicate OBD bundle. Fresh checks
  verified Bluetooth, hotspot and logger restoration with `recovery_required=false`.
  The subsequent automatic ingest scan finished with 65 new recordings, 25 changed and
  no scan errors. These are ordinary-copy results; no genuine quiet exclusion was proved.
- Local release validation: 244 Android tests passed, including the five private
  production-tracker replays, background-thread priority and cache-write failure
  containment. Both Android lint/build variants passed (zero errors, 56 warnings).
  All 206 frontend tests, frontend lint and production build passed.
  Full backend regression passed: 3,162 tests with 33 platform/fixture skips; Ruff passed.

See [quiet-clip-transfer-policy.md](quiet-clip-transfer-policy.md) for the exact contract,
resource limits and remaining physical acceptance checks.

### Server-only recorded-cycle recovery (deployed; physical acceptance pending)

This backend update originally used the 0.3.10 companion's existing deadline evidence
when the camera records a real ON to OFF cycle but the server misses live ON. It does
not change the APK, version code, poll plan or permissions. The live server's
`source_revision` was freshly verified at `6c31999` on 22 October. Successful physical
clearance from a camera-recorded cycle remains unverified; deployment alone does not
establish that result.

On 22 October, a normal short accessory-power ON/OFF cycle cleared the public admission
gate (`requires_ignition_cycle=false`, `countdown_cancelled=false`) and fresh native OFF
evidence supplied a positive decreasing sleep budget. Backup run 582 subsequently
completed and restored radios/logger. The server may have sampled ON live, so this
result does not isolate or prove the recorded-only recovery route.

- A version-two cancellation marker retains admission blocking before the gesture,
  then durably records a verified post-gesture monotonic upper bound with one second
  of padding. Fresh native OFF and fresh companion OFF proof must share the exact boot
  UUID, and the recorded ON-sample lower bound must be strictly after that barrier.
- Clearing compares the complete expected marker, including generation and baseline,
  so concurrent updates cannot be removed by an old proof. Timed-out disk workers keep
  their process fence until they exit, protecting successor records. Legacy markers use their
  first fresh same-boot native OFF observation while maintenance is inactive and owns
  no fence only to save a future-cycle baseline; the cycle already carried by that
  observation is excluded. Corrupt records and unresolved version-two bounds cannot
  take this legacy path.
- Repeated OFF heartbeats preserve edge bounds and the captured window. Process
  restart, missing observations and invalid continuity cannot create another edge.
  Normal deadline ceilings, remaining-budget and radio-restoration gates still apply;
  no full countdown is inferred from the current configured duration.
- Android regression coverage exercises a captured five-second ON pulse, real JSON
  evidence with frozen OFF heartbeats, same-boot process restart and observation-gap
  recovery. These are local contract checks, not proof that a physical short pulse
  will always be sampled or that the recovery update has worked on the camera.

See [clip-movement-manifests.md](clip-movement-manifests.md#recorded-ignition-cycle-recovery)
for the exact safety boundary and pending physical checks.

Local validation on 21 October: all 3,214 backend tests passed with 33 platform/fixture
skips and unhandled-thread warnings treated as errors; Ruff passed. All 247 Android
tests were covered successfully (246 in the full run plus the optional private replay),
with both lint variants at zero errors and 56 existing warnings each. All 206 frontend
tests, frontend lint and the production build passed. Recorded-cycle tests include the
actual synthetic Android JSON producer output and blocked disk workers with a competing
process lock. These local checks do not establish a physical recovery result; live
deployment was verified separately as described above.

### What changed in 0.3.9

- **Report-only clip movement manifests**: a bounded private journal reuses validated OBD
  speed and five-second ACC observations, with optional permission-gated passive GPS speed.
  A read-only DCIM picker enables closed-clip indexing and bounded sampled fingerprints.
  Clock ambiguity, missing coverage, unstable footage and old clips remain unknown.
- **Server validation and Backup page evidence**: the server reads a bounded report before
  its ordinary transfer plan, matches it against the footage inventory, and exposes advisory
  class counts and bytes. Malformed, unavailable or stale reports cannot block copying.
  No classification can exclude or delete footage in this release.
- **Verified maintenance hold**: an operator can acquire a bounded hold only after the
  server witnesses cancellation of the supported native countdown on the same parked boot.
  Short-lived host/device evidence gates backup admission and detached radio recovery.
  Ending a hold restores radio safety; a real ignition cycle restarts the normal countdown.

See [clip-movement-manifests.md](clip-movement-manifests.md) for setup, classification
limits, endpoints and physical acceptance checks. Poll plan 7 and the immutable OBD bundle
and event contracts remain unchanged. New movement evidence begins after installation;
existing footage cannot be retrospectively labelled stationary from absent observations.

### Local 0.3.9 validation (21 October 2026)

- All 231 Android JVM/Robolectric tests passed, including real SAF provider indexing,
  read-only folder access, both lenses, locked/orphan files, clock and observation gaps,
  queue overflow with durable gap recovery, and permission-gated passive location.
- Debug/release lint completed with zero errors (46 warnings), and both APK assemblies
  passed. Candidate metadata is 0.3.9/code 22 with poll plan 7.
- Full backend regression: 2,973 passed and 32 platform/fixture skips. Final targeted
  maintenance, movement, deadline, radio and OBD transfer/import checks: 498 passed and
  16 platform skips, with thread warnings treated as errors. A pre-existing test timing
  race was fixed by awaiting its fake heartbeat's cancellation; Windows Python 3.14
  deprecation warnings remain.
- All 203 frontend tests passed, with frontend lint and production build successful.
  New cases cover advisory counts, stale/invalid and restored reports, proof expiry,
  and a usable radio-restoration retry action.
- The backend accepts a synthetic fixture produced by the actual Android SAF indexer and
  atomic exporter. The physical DCIM read grant is persisted and both lenses are indexed;
  a subsequent scan classified moving, sampled-stationary and ACC-off clips. The server
  verified the card identity and received these classifications. Passive GPS remains
  disabled; its physical behaviour and resource impact remain unverified.
- The first verified post-update drive contained 234 samples over 19 minutes 36 seconds
  and 14.66 km, with no polling overruns or connection losses. MIL-distance PID `0x21`
  succeeded on all 18 attempts; downstream oxygen PID `0x15` still failed on all 20
  attempts. Two MAF replies and one calibration-ID response also failed validation.
- Sixteen historical archives were regenerated after a boot without their exact receipts.
  Comparing every archive against its original verified server copy found identical sample,
  diagnostic and summary bytes; only the manifest export timestamp differed. These copies
  exposed a same-filename conflict in backup reconciliation, while the original history
  and the new drive remained stored and verified. This is independent of folder access.

### What changed in 0.3.8

- **Measured polling budget**: fast engine readings and adapter voltage retain priority.
  Optional due readings run only when the measured command cost and a completion margin fit
  the five-second cycle. Deferred readings stay missing, with fair admission on later due
  cycles; old values are not copied into a fresh sample. Reserved sparse diagnostic work
  remains bounded and an unexpectedly slow command can still cause an honestly reported
  overrun. Poll plan 7 keeps the nominal 5/15/60-second expectations of plan 6.
- **Per-PID command compatibility**: when a response-count-suffixed request produces a
  prompt-complete reply that fails ISO validation, only that PID switches to a plain request
  on its next scheduled attempt. This addresses the observed rejection path for PIDs `15`
  and `21` without an immediate extra bus request. Source, checksum and exact payload length
  remain mandatory; a malformed plain reply stays rejected. Reconnecting resets this
  session-local compatibility decision.
- **Historical provenance remains immutable**: existing drives retain their producer poll
  plans. Deploy server acceptance for plan 7 before installing this APK.

The rejected production replies were not retained as raw frames, so a specific decoder bug
has not been established. Unit tests reproduce the compatibility fallback and strict rejection
cases. A further drive on 0.3.8 is required to confirm the physical adapter's response and
measured cadence; fallback does not guarantee that an ECU will supply a valid reading.

### Local 0.3.8 validation (21 October 2026)

- All 213 Android JVM/Robolectric tests passed. New cases cover measured command and storage
  costs, optional-PID fairness, reserve recovery, sparse diagnostic completion, quiesce timing,
  per-PID plain fallback and strict rejection of malformed data.
- Debug/release lint and both APK assemblies passed; metadata reports 0.3.8/code 21.
- All 336 OBD server tests passed, with three platform/fixture skips on Windows; server plan 7
  admission preserves earlier plans and continues counting deferred observations as missing.
- All 196 frontend tests, frontend lint and the production web build passed. Focused Python
  Ruff lint/format checks and `git diff --check` passed.
- These software checks do not establish the physical adapter's next-drive cadence or
  recovery of the two rejected readings. The production-signed 0.3.8 update was subsequently
  installed in place on 21 October; a new drive is still required for those acceptance checks.

### Previous changes in 0.3.7

- **Encrypted webhook credential storage**: AES-256-GCM ciphertext in preferences, with a
  non-exportable wrapping key in Android Keystore. An existing plaintext key migrates on the
  first full configuration load; a synchronous atomic preference commit saves verified ciphertext
  and removes the old plaintext entry together. Gate-only startup reads do not access Keystore.
  There is no plaintext fallback. If migration or decryption fails, webhook delivery pauses,
  the stored value remains available for retry/recovery, and OBD logging continues. The form
  shows an actionable warning; a failed secure save does not reload or stop the service. Blank
  fields after a credential error cannot silently erase the stored encrypted value. Failed disk
  commits restore the previous in-memory preferences too. Disabling the logger does not require
  a readable webhook key.
- **Expandable diagnostics**: tap **Show metrics and sleep details** for pipeline counters,
  polling duty cycle, sleep-window target/readback/verification, ACC and ingestion hold evidence.
  The compact view remains readable. Configuration crypto and bounded status reads run off the
  UI thread. The reader chooses the newest valid card/fallback snapshot and marks clock-ahead
  status stale. Display fields are allowlisted; raw error text, credentials and drive IDs are
  not copied into the status panel.
- **Webhook events actually persist and import**: 0.3.6 added webhook emission without updating
  the event allowlists, so its webhook events were dropped locally. The producer and server now
  accept the exact webhook kind, outcomes and bounded reason codes, including unavailable
  credentials. Regression tests compare both complete allowlists and cover journal reload and
  server storage/API output. Deploy server acceptance before installing this APK; the event
  schema remains v1. Previously dropped events cannot be recovered.

The Android Keystore implementation follows the platform's
[Keystore guidance](https://developer.android.com/privacy-and-security/keystore). Hardware-backed
key storage and real boot behavior still require checks on this particular head unit.

### Previous changes in 0.3.6

- **Live status panel** at the top of the launcher activity: dark, large monospace text
  re-read from the published `status.json` every 2 s while the screen is in the foreground.
  Shows logger state and publish age (flagged `STALE` after 90 s, which is how a stopped
  service looks from the car), vehicle state, adapter/ECU connection, battery voltage, current
  drive, last sample age, bundles waiting for the server, Wi-Fi / sleep hold, and the last
  error indication. Keeps the screen on while open. Formatting is in `LiveStatusSummary.kt` with unit
  tests.
- **Ingest-ready webhook retries**: transient failures (`webhook_timeout`,
  `webhook_network_error`, `webhook_server_error`) retry with a 3 s / 10 s / 20 s backoff
  before giving up. Auth, client and `disabled` outcomes stay final on the first response. Each
  failure before a retry and the final outcome now persist in the event stream with the 0.3.7
  contract fix.

Previous installed release: 0.3.7 (`versionCode` 20), with fresh status and app-owned events verified
after the first production drive. The matching server revision was `6edc404`.

### Previous 0.3.7 local validation (20 October 2026)

- 199 Android JVM/Robolectric tests passed, including migration/write-failure recovery, AES-GCM
  tamper/wrong-key rejection, startup isolation, status selection and webhook event persistence.
- Debug/release lint and both APK assemblies passed. Output metadata confirms version 0.3.7/code
  20; the release verification artifact is `app-release-unsigned.apk`, not a deployable update.
- 54 Python event-contract/storage tests and 45 worker/reset/abort/outcome tests passed.
- Focused Ruff checks/formatting and `git diff --check` passed.
- Those local checks preceded production signing and the in-place installation. Subsequent
  live checks confirmed 0.3.7/code 20, successful authenticated webhook delivery, all produced
  samples stored, and a device verification receipt matching the server bundle. Cold-boot
  behavior and hardware-backed Keystore properties have not been established by those checks.

## Where the APK comes from

CI (`.github/workflows/ci.yml`, job *Android OBD logger*) runs the JVM tests, debug and release
lint, and assembles both variants on every push — but it does **not** publish an installable
artifact. Its release APK is unsigned and its debug APK uses the throwaway debug signer; neither
can update the installed production app.

The production APK is built locally with the existing signing key, which lives only in the
`C:\Github\Dashcam-Stats` checkout (not in fresh clones or worktrees):

- Keystore: `android/obd-logger/secrets/dashcam-obd-prod.p12`
- Credentials: `android/obd-logger/keystore.properties`
- Signer certificate SHA-256:
  `e9191adb8f04a7117d71763c5e1332e7c0e4639a63afa09aef9c9bb1684d408d`

Both files are Git-ignored. Never regenerate the key: the installed app only accepts updates
signed by this certificate, and replacing it means uninstalling and losing the on-device
database and preferences.

## How to build and install the companion

First integrate/review the release source in the protected signing checkout, or configure a
protected build host to use the existing signing files. The original checkout may still contain
older source and unrelated local changes: do not blindly pull, switch branches or overwrite it.
Fresh worktrees do not contain the signing files. From the chosen source checkout, after verifying
`app/build.gradle.kts` declares version 0.3.12/code 25:

```powershell
$env:OBD_PRODUCTION_SIGNING = "true"
cd android\obd-logger
.\gradlew.bat --no-daemon clean :app:testDebugUnitTest :app:lintRelease :app:assembleRelease
$apk = "app\build\outputs\apk\release\app-release.apk"
& "$env:ANDROID_HOME\build-tools\35.0.0\apksigner.bat" verify --verbose --print-certs $apk
Get-FileHash -Algorithm SHA256 $apk
```

Check that the printed `Signer #1 certificate SHA-256 digest` equals the complete digest above.
Record that digest and the APK SHA-256. Deploy the matching server manifest and
maintenance and plan 8 support before enabling the trial (baseline plan 7 remains the default);
then, with the head unit awake and explicitly selected on ADB:

```powershell
$deviceSerial = "<verified-device-serial>"
adb -s $deviceSerial install -r $apk
```

`-r` updates in place and keeps the app's database, preferences and logger ID. After install,
open the app once and confirm the panel updates without `STALE` in the current operating state
(for example `PARKED` or `ECU_ONLINE`). Check that an existing webhook key migrates without losing
other settings, the metrics toggle works, and `Bundles waiting for the server` drains on the
next Wi-Fi window. Verify webhook events reach the server and boot recovery still works.

After ingestion obtains a fresh status snapshot, `GET /api/obd/status` should report
`app_version_name: "0.3.12"` and `app_version_code: 25` under `logger`. A cached status alone does
not establish the currently installed/running version.

## What is still left to do

Remaining work and physical checks:

1. **No OTA / self-update path** — every update is `adb install -r` with physical or network ADB
   access. A signed-APK check against the server (same certificate pinned) would let the server
   stage an update for the next Wi-Fi window.
2. **Per-file resume for bundle transfer** — a dropped connection restarts the bundle from zero.
   Small today, but worth it as drives get longer.
3. **Remote checksum verification of dashcam footage** — deliberately deferred: the puller's
   size-only verification is documented as load-bearing, and a remote SHA-256 would cost a full
   read on the camera's weak SoC inside the sleep window. Revisit only with a cheaper
   verification primitive.
4. **Physical verification** — exercise migration/decryption with the real Android Keystore,
   reboot and in-place update persistence, newest status selection with an unavailable TF card,
   UI legibility/touch behavior and a complete webhook/ingestion cycle.
5. **Movement, filtering and maintenance acceptance** — exercise optional location and
   folder permission loss, sustained paired decoding and recording within the thermal limits,
   an actual quiet exclusion retained on the card, an activity upload, and their downstream
   scan/job state. App-UID thermal access, native countdown cancellation, fresh maintenance
   heartbeat evidence and subsequent radio/logger restoration are verified. Planned hold
   expiry, physical acceptance of the version-three same-boot retry and the scope of
   the early heartbeat-loss cause still need investigation.
   Advisory labels remain report only. Both visual-policy opt-ins were enabled on
   22 October at the operator's request, with unchanged resource and proof gates.
   The camera was above the 85 C limit; genuine exclusion, parked-activity transfer and
   sustained paired-decoder acceptance remain unverified.
6. **Recording continuity** — an earlier parked 21 October inspection found an unchanged
   rear active file despite live preview. After the 0.3.10 installation and front decode
   probe, both cameras' native active files independently rolled and grew. This verifies
   recording in that observed window; it does not identify the earlier stall's cause or
   establish sustained paired decoding. No automatic recorder restart policy was enabled.

The earlier backend reset warning was stale: `87d0c968` already serializes reset, claim/owner
registration and outcome transactions, and abort refuses replacement jobs until cancellation
finishes. All 45 focused worker/reset/abort/outcome tests passed during this review. The periodic
scheduler's serial tasks and 30 s floor are deliberate maintenance scheduling; the ingest webhook
starts a separate immediate run, so that floor is not a general webhook delay. The Compose
template has no resource limits; live deployment limits and workload sizing were not inspected.

Physical checks that remain from the cutover list (BLE ownership handover, sleep-window
verification on the real head unit) are tracked in
[`obd-dashcam-logger.md` → Rollback and remaining physical checks](obd-dashcam-logger.md#rollback-and-remaining-physical-checks).
