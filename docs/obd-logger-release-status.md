# OBD logger companion — current release status

Quick orientation for the Android app that runs on the head unit. The full design and
procedures live in [`obd-dashcam-logger.md`](obd-dashcam-logger.md); this page is the
"where are we right now" summary.

## Current version

| | |
|---|---|
| Version | **0.3.7 candidate** (`versionCode` 20) |
| Source | `android/obd-logger/` on `main` (0.3.7 / code 20) |
| Application ID | `com.dashcamstats.obdlogger` |
| Target / compile SDK | 34 / 35 |
| Status schema | `status.json` schema version 7 |
| Deployment | Production installation and physical checks have not been performed |

### What changed in 0.3.7

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

Previous source release: 0.3.6 (`versionCode` 19). The installed version has not been checked.

### Local validation (20 October 2026)

- 199 Android JVM/Robolectric tests passed, including migration/write-failure recovery, AES-GCM
  tamper/wrong-key rejection, startup isolation, status selection and webhook event persistence.
- Debug/release lint and both APK assemblies passed. Output metadata confirms version 0.3.7/code
  20; the release verification artifact is `app-release-unsigned.apk`, not a deployable update.
- 54 Python event-contract/storage tests and 45 worker/reset/abort/outcome tests passed.
- Focused Ruff checks/formatting and `git diff --check` passed.
- No production signing, installation, live server change or physical-unit test was performed.

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
`app/build.gradle.kts` declares version 0.3.7/code 20:

```powershell
$env:OBD_PRODUCTION_SIGNING = "true"
cd android\obd-logger
.\gradlew.bat --no-daemon clean :app:testDebugUnitTest :app:lintRelease :app:assembleRelease
$apk = "app\build\outputs\apk\release\app-release.apk"
& "$env:ANDROID_HOME\build-tools\35.0.0\apksigner.bat" verify --verbose --print-certs $apk
Get-FileHash -Algorithm SHA256 $apk
```

Check that the printed `Signer #1 certificate SHA-256 digest` equals the complete digest above.
Record that digest and the APK SHA-256. Deploy the matching server event-contract change first;
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
`app_version_name: "0.3.7"` and `app_version_code: 20` under `logger`. A cached status alone does
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

The earlier backend reset warning was stale: `87d0c968` already serializes reset, claim/owner
registration and outcome transactions, and abort refuses replacement jobs until cancellation
finishes. All 45 focused worker/reset/abort/outcome tests passed during this review. The periodic
scheduler's serial tasks and 30 s floor are deliberate maintenance scheduling; the ingest webhook
starts a separate immediate run, so that floor is not a general webhook delay. The Compose
template has no resource limits; live deployment limits and workload sizing were not inspected.

Physical checks that remain from the cutover list (BLE ownership handover, sleep-window
verification on the real head unit) are tracked in
[`obd-dashcam-logger.md` → Rollback and remaining physical checks](obd-dashcam-logger.md#rollback-and-remaining-physical-checks).
