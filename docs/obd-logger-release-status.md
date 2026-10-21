# OBD logger companion — current release status

Quick orientation for the Android app that runs on the head unit. The full design and
procedures live in [`obd-dashcam-logger.md`](obd-dashcam-logger.md); this page is the
"where are we right now" summary.

## Current version

| | |
|---|---|
| Version | **0.3.9** (`versionCode` 22) |
| Source candidate | `android/obd-logger/` (0.3.10 / code 23); installed version remains 0.3.9 until an in-place update is verified |
| Application ID | `com.dashcamstats.obdlogger` |
| Target / compile SDK | 34 / 35 |
| Status schema | `status.json` schema version 7 |
| Poll plan | 7; five-second target with measured optional-command admission |
| Deployment | 0.3.9/code 22 was installed in place on 21 October with the production certificate, preserved app UID and settings, and verified runtime revision `af7e532f4619`. |

### Candidate 0.3.10

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
- The physical feasibility probe decoded only 44.6 seconds within a 45-second budget.
  A later SoC reading was 94 C. The candidate refuses analysis at 85 C or above and fails
  open to ordinary upload. It is not yet installed or activated on the camera.
- Local candidate validation: 244 Android tests passed, including the five private
  production-tracker replays, background-thread priority and cache-write failure
  containment. Both Android lint/build variants passed (zero errors, 56 warnings).
  All 206 frontend tests, frontend lint and production build passed.
  Full backend regression passed: 3,162 tests with 33 platform/fixture skips; Ruff passed.

See [quiet-clip-transfer-policy.md](quiet-clip-transfer-policy.md) for the exact contract,
resource limits and remaining physical acceptance checks.

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

## How to build and install the candidate

First integrate/review the candidate source in the protected signing checkout, or configure a
protected build host to use the existing signing files. The original checkout may still contain
older source and unrelated local changes: do not blindly pull, switch branches or overwrite it.
Fresh worktrees do not contain the signing files. From the chosen source checkout, after verifying
`app/build.gradle.kts` declares version 0.3.9/code 22:

```powershell
$env:OBD_PRODUCTION_SIGNING = "true"
cd android\obd-logger
.\gradlew.bat --no-daemon clean :app:testDebugUnitTest :app:lintRelease :app:assembleRelease
$apk = "app\build\outputs\apk\release\app-release.apk"
& "$env:ANDROID_HOME\build-tools\35.0.0\apksigner.bat" verify --verbose --print-certs $apk
Get-FileHash -Algorithm SHA256 $apk
```

Check that the printed `Signer #1 certificate SHA-256 digest` equals the complete digest above.
Record that digest and the APK SHA-256. Deploy this candidate's server manifest and
maintenance support first (poll plan 7 remains required);
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
`app_version_name: "0.3.9"` and `app_version_code: 22` under `logger`. A cached status alone does
not establish the currently installed/running version.

## What is still left to do

Remaining work; these are not implemented or claimed deployed by this candidate:

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
5. **Movement and maintenance acceptance** — select DCIM, exercise optional location and
   folder permission loss, compare a new drive's observations with clips, and test a fresh
   native countdown, hold expiry/connectivity loss and radio recovery. Report-only operation
   is intentional; automatic footage filtering is not implemented.
6. **Rear recorder recovery** — during the parked 21 October inspection, front clips continued
   while the rear active file remained unchanged. The rear camera still supplied preview
   frames but lacked a recording encoder surface. Start-only and guarded graceful all-camera
   stop/start broadcasts did not recover it. No hardware fault is established. A controlled
   device power cycle/reboot after backup and radio cleanup is still needed to test recovery;
   no automatic restart loop or persistent recording configuration change was enabled.

The earlier backend reset warning was stale: `87d0c968` already serializes reset, claim/owner
registration and outcome transactions, and abort refuses replacement jobs until cancellation
finishes. All 45 focused worker/reset/abort/outcome tests passed during this review. The periodic
scheduler's serial tasks and 30 s floor are deliberate maintenance scheduling; the ingest webhook
starts a separate immediate run, so that floor is not a general webhook delay. The Compose
template has no resource limits; live deployment limits and workload sizing were not inspected.

Physical checks that remain from the cutover list (BLE ownership handover, sleep-window
verification on the real head unit) are tracked in
[`obd-dashcam-logger.md` → Rollback and remaining physical checks](obd-dashcam-logger.md#rollback-and-remaining-physical-checks).
