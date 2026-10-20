# OBD logger companion — current release status

Quick orientation for the Android app that runs on the head unit. The full design and
procedures live in [`obd-dashcam-logger.md`](obd-dashcam-logger.md); this page is the
"where are we right now" summary.

## Current version

| | |
|---|---|
| Version | **0.3.6** (`versionCode` 19) |
| Source | `android/obd-logger/` on `main` (commit `d28082f`) |
| Application ID | `com.dashcamstats.obdlogger` |
| Target / compile SDK | 34 / 35 |
| Status schema | `status.json` schema version 7 |

### What changed in 0.3.6

- **Live status panel** at the top of the launcher activity: dark, large monospace text
  re-read from the published `status.json` every 2 s while the screen is in the foreground.
  Shows logger state and publish age (flagged `STALE` after 90 s, which is how a stopped
  service looks from the car), vehicle state, adapter/ECU connection, battery voltage, current
  drive, last sample age, bundles waiting for the server, Wi-Fi / sleep hold, and the last
  error. Keeps the screen on while open. Formatting is in `LiveStatusSummary.kt` with unit
  tests.
- **Ingest-ready webhook retries**: transient failures (`webhook_timeout`,
  `webhook_network_error`, `webhook_server_error`) retry with a 3 s / 10 s / 20 s backoff
  before giving up. Auth, client and `disabled` outcomes stay final on the first response. Each
  attempt is recorded in the event stream.

Previous release: 0.3.5 (`versionCode` 18).

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

## How to build and install 0.3.6

From the `C:\Github\Dashcam-Stats` checkout, on `main`:

```powershell
git pull
$env:OBD_PRODUCTION_SIGNING = "true"
cd android\obd-logger
.\gradlew.bat --no-daemon clean :app:testDebugUnitTest :app:lintRelease :app:assembleRelease
$apk = "app\build\outputs\apk\release\app-release.apk"
& "$env:ANDROID_HOME\build-tools\35.0.0\apksigner.bat" verify --verbose --print-certs $apk
Get-FileHash -Algorithm SHA256 $apk
```

Check that the printed `Signer #1 certificate SHA-256 digest` equals
`e9191adb…d408d` above, then with the head unit on ADB:

```powershell
adb install -r $apk
```

`-r` updates in place and keeps the app's database, preferences and logger ID. After install,
open the app once and confirm the live panel shows `Logger: LOGGING` (or `IDLE`) rather than
`STALE`, and that `Bundles waiting for the server` drains on the next Wi-Fi window. Record the
APK SHA-256 and signer digest in the release record as described in the main doc.

The server can confirm the new build without touching the car: `status.json` now reports
`app_version_name: "0.3.6"` and `app_version_code: 19`.

## What is still left to do

Reviewed but not implemented in this round, roughly in priority order:

1. **API key storage** — the ingest webhook key is held in plain `SharedPreferences`. Moving it
   behind the Android Keystore (encrypt at rest, decrypt on use) is the obvious hardening step.
2. **No OTA / self-update path** — every update is `adb install -r` with physical or network ADB
   access. A signed-APK check against the server (same certificate pinned) would let the server
   stage an update for the next Wi-Fi window.
3. **Per-file resume for bundle upload** — a dropped connection restarts the bundle from zero.
   Small today, but worth it as drives get longer.
4. **Remote checksum verification of dashcam footage** — deliberately deferred: the puller's
   size-only verification is documented as load-bearing, and a remote SHA-256 would cost a full
   read on the camera's weak SoC inside the sleep window. Revisit only with a cheaper
   verification primitive.
5. **Config UI ergonomics** — the form is still a plain programmatic `LinearLayout`; the live
   panel is a good first step but a tap-to-expand metrics view (pipeline counters, sleep window
   evidence) would make the on-car diagnosis loop shorter.
6. **Backend side notes that affect the companion**: `_finish_under_barrier` in the worker can
   strand RUNNING jobs on reset, the scheduler loop is serial with a 30 s floor, and compose has
   no resource limits. None block the app, but they shape how quickly a bundle is processed once
   it lands.

Physical checks that remain from the cutover list (BLE ownership handover, sleep-window
verification on the real head unit) are tracked in
[`obd-dashcam-logger.md` → Rollback and remaining physical checks](obd-dashcam-logger.md#rollback-and-remaining-physical-checks).
