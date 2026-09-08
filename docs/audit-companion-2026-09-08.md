# Android companion, Docker and CI audit — 2026-09-08

## Scope and evidence boundary

This is a source-only audit of revision `1e258d04dc9590141a427e16da663d555fc802ed`.
It covers `android/obd-logger`, the Android/server OBD contracts, the root Docker image,
dependency declarations, and GitHub Actions. It did not connect to the head unit, use ADB,
inspect or install an APK, call the live server, build or run a container, deploy, commit, or
push. Existing edits in backend source/tests and the untracked `docs/agent-handover.md` were
left untouched.

The Android build used an isolated JDK 17 and Android SDK 35 toolchain already present under
the Codex temporary toolchain directory. The same five targets as CI passed: 124 debug JVM tests,
debug lint, release lint, debug APK assembly, and unsigned release APK assembly. The build emitted
one SDK tooling XML-version warning and the existing deprecated pre-Android-O notification-builder
compiler warning; neither failed validation. No APK was installed or published. This audit did not
query a current CI run.

## Component ownership and contracts

The code in this repository is an **OBD telemetry companion**, not the recording app.
`android/obd-logger/app/build.gradle.kts:81-85` identifies package
`com.dashcamstats.obdlogger`; the audited installed baseline was version `0.3.0` (code 13),
and the upgrade is version `0.3.1` (code 14), with min SDK 26 and target SDK 34.
Follow-up: it was installed on 8 September at 12:38:30 Adelaide, with APK hash,
foreground service and exported-data continuity verified. See
[the live investigation](carplay-investigation-2026-09-08.md#installed-device-verification).
The earlier read-only procedure below describes the initial audit phase; phone playback
and natural webhook dispatch remain untested.
The manifest declares Bluetooth LE as required and has no camera, microphone, or media-storage
permissions (`AndroidManifest.xml:3-11`). Its foreground service is typed `connectedDevice`
(`AndroidManifest.xml:38-42`). Conversely, the recorded device evidence in
`docs/head-unit-reference.md:207-221` identifies the separate vendor system recorder and says
it owns both cameras continuously. No recorder source is present in this checkout.

The companion owns one BLE/OBD connection only after the explicit enable and ownership gates.
At boot or package replacement, `BootReceiver.kt:39-77` reloads those gates and permissions
before starting the service. The service stops when configuration is invalid or permissions
are absent (`ObdLoggerService.kt:315-329`), uses a connected-device foreground notification,
and closes its BLE client, dynamic receiver, retry channel, sleep controller and coroutine
scope on destruction (`ObdLoggerService.kt:190-219`). Server-side ingestion preserves this
ownership contract: older companions without the file handshake keep both radios on
(`docs/ingest-radio-state-machine.md:74-76`).

The archive contract is consistent at schema version 1. `BundleExporter.kt:31-58` fixes the
manifest, summary, and four member names; `BundleExporter.kt:87-202` refuses zero-sample
exports, synchronizes payloads, validates member sizes/hashes, and atomically publishes the
ZIP. `BundleExporter.kt:432-514` validates the archive again. Raw history is pruned only when
the immutable archive still validates or an exact bounded server receipt proves the same
drive ID and SHA-256 (`BundleExporter.kt:575-674`). The server independently validates the
same schema and receipt flow (`docs/obd-server-import.md:34-50`). This separates export,
copy, validation, durable registration, acknowledgement, and device cleanup rather than
treating them as one success state.

Status compatibility is intentionally capability-based. The server requires status schema
4 for the current structured status parser (`backend/app/ingest/obd_transfer.py:330-331`) and
schema 6 for the coordinated radio/quiesce path (`backend/app/ingest/puller.py:330-331`), while
retaining the older `ownership_enabled=true` behavior. A later server can therefore ingest
older bundles without assuming the newer radio protocol. This remains source evidence; the
installed companion version and its emitted status schema were not inspected.

## Prioritised findings

### P0 — a non-empty webhook API credential is embedded in source

`LoggerConfig.kt:42` contains a non-empty default API credential. `LoggerConfig.kt:62-77`
loads that value whenever the preference is absent or blank, and `ObdLoggerService.kt:2013-2015`
sends it as `X-API-Key`. The old handover concern therefore remains. The value is deliberately
omitted here and should be treated as compromised: rotate/revoke it outside this repository,
remove the source default, make an empty value mean no credential, and configure each install
explicitly. Repository history should be regarded as containing the old value even after a
new commit removes it.

Implemented locally: the source and preference fallback are now blank. An explicitly saved
credential remains unchanged, so upgrading does not silently disable an existing installation.
Rotation/revocation is an operator action for the next phase and was not attempted here.

The UI also initializes a normal text field with the saved key (`MainActivity.kt:86`) and
preferences store it as a plain string (`LoggerConfig.kt:83-101`). At minimum, render it as a
masked password field and never echo the saved value after load. Whether on-device encryption
is worthwhile depends on the head unit threat model; it does not repair an already embedded
or transmitted credential.

### P1 — the webhook cannot use the network on a standard Android install

The manifest has no `android.permission.INTERNET` (`AndroidManifest.xml:2-11`), but ignition-off
dispatch opens a `HttpURLConnection` and writes a JSON POST (`ObdLoggerService.kt:1981-2030`).
Without the normal `INTERNET` permission, this path cannot establish the socket. The method
folds every exception into `false`, so the only visible result is a generic `webhook_failed`
event; the root cause is hidden.

Add `android.permission.INTERNET` and a regression check over the merged manifest. Retain the
bounded five-second connect/read timeouts and no-retry behavior on the drive-finalisation path;
the webhook is merely a hint to the server and must not block durable local export.

Implemented locally: the manifest now declares `INTERNET`; both debug and release manifest
processing, lint, tests, and APK assembly passed.

### P1 — the configured default transport conflicts with target SDK network policy

The configured default webhook scheme is HTTP (host and credential omitted). The app targets
API 34 (`app/build.gradle.kts:83`) and declares neither `usesCleartextTraffic` nor a network
security configuration (`AndroidManifest.xml:13-20`). Android documentation says apps targeting
API 28 or later default to rejecting cleartext traffic. Thus adding `INTERNET` alone still does
not make the supplied default work on standard Android 9+ behavior.

Prefer HTTPS with a certificate trusted by the unit. If the private deployment cannot provide
HTTPS, add a narrowly scoped Network Security Configuration for the exact private destination
rather than enabling cleartext globally, and document that the API key and vehicle metadata are
otherwise observable and modifiable on that network. Official references:

- https://developer.android.com/guide/topics/manifest/application-element.html
- https://developer.android.com/privacy-and-security/security-config

Implemented locally: a Network Security Configuration enables the platform HTTP stack, while
both settings validation and dispatch permit cleartext only for loopback or RFC1918 IPv4 literals.
HTTPS remains accepted for DNS names. This matches the current LAN deployment without allowing a
saved public-host HTTP URL to bypass the UI check. HTTPS remains recommended for any traffic that
can leave the trusted private network.

### P2 — network configuration accepts unsafe or unusable values without validation

`MainActivity.kt:138-157` constructs and saves configuration from free-form fields. The implemented
validator now rejects URL user-info, fragments and non-HTTP(S) schemes, restricts cleartext to
private/loopback IPv4, and the service rechecks the same policy immediately before dispatch.
Authenticated POSTs also disable automatic redirects, preventing a validated endpoint from
forwarding the API key to another origin. Follow-up hardening now requires the exact
`/api/ingest/webhook` path, rejects query strings, bounds URLs to 2,048 characters and keys to 512
characters, and requires an explicit nonblank key whenever delivery is enabled. HTTP handling emits
bounded reason codes for authorization rejection, other client errors, server errors, unexpected
responses, timeout, network, platform-security, and malformed-URL failures. It never records response
bodies, URLs, keys, or exception text. Remaining work is installed-unit validation of those reason
codes and HTTPS for any deployment outside the trusted LAN.

## CarPlay lag boundary

The companion cannot directly diagnose or repair the asymmetric CarPlay video path. Its manifest
has no camera, audio, media-projection, display-capture, Wi-Fi-change, or privileged ZLink permissions;
it owns only BLE/OBD and its own status files. Sampling its OBD loop would not measure outbound phone
rendering versus return touch/control traffic and would add background work to the same constrained
unit. The next-phase investigation should use the documented ZLink/SurfaceFlinger timing log,
per-direction radio/link metrics, and a bounded before/during/after resource sample while reproducing
the lag. No ZLink action, radio restart, or speculative companion sampler was added.

## Prepared upgrade artifact

The local signed release is
`android/obd-logger/app/build/outputs/apk/release/app-release.apk`, version `0.3.1` (code 14).
`apksigner` verifies both this artifact and the privately captured installed `0.3.0` APK, and their
signer certificate digests match. The digest itself is omitted. This makes the artifact eligible for
an in-place Android package upgrade, but it was not installed.

The application ID, preference file name, external-files layout, database schema, bundle schema and
status contracts are unchanged, so an in-place upgrade preserves configuration and stored telemetry.
An explicitly saved webhook key is intentionally preserved; the removal affects only fresh/default
configuration and does not rotate the exposed credential. Before installation, the operator must
rotate the server credential and then update the saved companion key. A legacy enabled webhook with
a blank key, a nonstandard path, query string, public cleartext host, or overlong value will now be
rejected safely and reported by bounded reason code rather than attempted.

### P2 — dynamic wake receiver exposure is broader than its behavior needs

The service dynamically listens only for platform Bluetooth, screen, user-present, and power
events (`ObdLoggerService.kt:222-234`), yet registers the receiver as `RECEIVER_EXPORTED` on API
33+. Android warns that exported receivers can receive unprotected broadcasts from other apps.
The receiver should be split if Bluetooth delivery on the vendor build requires export: keep
only the privileged system broadcast receiver exported and register screen/power/user-present
signals as not exported. Confirm delivery on the target OS before changing this because Android
notes that some highly privileged system broadcasters require `RECEIVER_EXPORTED`.

Official reference: https://developer.android.com/develop/background-work/background-tasks/broadcasts

### P2 — production build reproducibility is partial

The Android wrapper pins Gradle and the build pins direct Kotlin/coroutine/test versions, while
CI fixes JDK 17 and SDK/build-tools 35 (`.github/workflows/ci.yml:141-169`). However,
`android/obd-logger/Dockerfile:1` uses a mutable Android SDK image tag and Gradle dependency
verification/locking is not enabled. Backend runtime dependencies are mostly bounded ranges
(`backend/requirements.txt:8-78`) and the frontend lockfile is used by `npm ci`, so rebuilding
the same source later can resolve different Python transitive versions and a different Android
toolchain image. Record the resolved Python environment in release provenance, enable Gradle
dependency verification/locking, and pin the Android build image by digest when it is used.

### P3 — CI validates the main container well but not the companion container recipe

CI executes the companion wrapper directly and covers debug/release tests, lint, and assembly
(`.github/workflows/ci.yml:141-210`). The separate `android/obd-logger/Dockerfile` is never built,
so drift in that documented alternate build path will not fail CI. Either remove it in favor of
the wrapper workflow or add a low-frequency/container-specific build check pinned by digest.

The main image has useful controls: multi-stage builds, a non-root runtime user, `tini`, a real
OpenVINO inference smoke test, an iHD media-driver presence check, UI/health checks, container
HEALTHCHECK verification, and a process-owner assertion (`Dockerfile:1-237`,
`.github/workflows/ci.yml:212-363`). Release publishing waits for the reusable full CI workflow
and attaches build provenance (`.github/workflows/release.yml:18-87`). These are configuration
findings only; Docker was not executed in this audit.

## Lifecycle, retry, resource, and privacy assessment

The service recovers interrupted drives before opening a new session and preserves evidence-based
finish times (`ObdLoggerService.kt:272-313`). Its retry loop is bounded through controller state,
and configuration/permission failures stop rather than spin. Durable sample storage, finalisation,
export and acknowledgement are independent; a webhook failure does not erase or falsely acknowledge
telemetry. Destruction releases the Bluetooth client and coroutine-owned resources. The code does
not acquire a wake lock, so no unbounded wake-lock leak was found; whether the vendor sleep model
gives the foreground service enough execution time remains physical-device evidence.

Cloud backup and device-to-device transfer exclude root files, databases, preferences, and external
files (`res/xml/data_extraction_rules.xml:2-14`), and the application disables platform backup in
the manifest (`AndroidManifest.xml:13-16`). Event/error handling uses bounded redaction, but the
hardcoded credential and plaintext HTTP design override those otherwise sound disclosure controls.

Android 14 requires an explicit foreground-service type and its matching permission; both are
present for `connectedDevice` (`AndroidManifest.xml:8-9,38-42`). Boot-completed remains a documented
background-start exemption for this type at target 34, subject to device/vendor behavior. Official
references:

- https://developer.android.com/about/versions/14/changes/fgs-types-required
- https://developer.android.com/develop/background-work/services/fgs/restrictions-bg-start

## Exact read-only head-unit plan for the next phase (do not execute in this phase)

1. Establish identity without changing state: record OS/API level, boot-completed state, installed
   package path, version name/code, signing-certificate SHA-256, APK SHA-256, first-install/update
   times, and compare them with a locally built candidate and `BuildConfig.BUILD_GIT_SHA`. A mismatch
   means source conclusions cannot be treated as installed behavior.
2. Confirm component and permission state: inspect the installed manifest/package dump for
   `INTERNET`, Bluetooth, notification, boot, foreground-service permissions, enabled receiver and
   service state, target SDK, cleartext policy, and runtime grants. Evidence that either networking
   permission/policy differs from source changes the webhook diagnosis into a source/deployment
   divergence.
3. Inspect only allowlisted app outputs: list metadata (names, modes, owners, sizes and mtimes) for
   the resolved external-files `obd/ready`, `status.json`, `events.json`, `receipts`, `.partial`, and
   migration/restore markers. Read bounded status/events with identifiers redacted. Do not copy raw
   databases or bundles until separately approved.
4. Verify ownership without radio commands: capture process/service/foreground-notification state,
   Bluetooth connection-owner evidence, and existing bounded logs for boot start, permission failure,
   retry/backoff, storage unavailable, finalisation, export, and webhook reason codes. Do not toggle
   Bluetooth, Wi-Fi, hotspot, ignition, sleep, recorder state, or adapter state.
5. Verify recorder separation: record the vendor recorder package/version/signature, active camera
   ownership, output directories, and atomic `pre_*` to final rename evidence from existing files and
   logs only. Do not stop, force-stop, launch, or reconfigure it. If the recorder exposes no manifest
   or completion API, retain filename stability/rename as the server discovery contract.
6. Check contract compatibility using one already completed, non-sensitive bundle: compare member
   list, schema, build identity, timestamps, units, sample/diagnostic counts and stored whole-file hash
   with the server parser without transferring or deleting it. Redact vehicle, adapter, logger, drive,
   network and location identifiers. An unsupported schema or missing capability changes the server
   fallback and rollout order.
7. Observe restart history rather than causing a restart: use existing boot/service logs and current
   state to determine whether boot launch, `START_STICKY`, permission persistence, removable-storage
   availability and retry wakeups have previously worked. Any reboot, app restart, install, permission
   change, or ignition-cycle test requires a later explicit physical-test window.
8. End with hashes and an evidence matrix: installed/source/build match; each permission and path;
   recorder versus companion ownership; status schema/capabilities; ready/partial/receipt state; and
   every claim labelled observed, source-only, or still requiring an active test.

## Reproducible local checks for review

From `android/obd-logger`, with JDK 17 and Android SDK 35 available:

```powershell
.\gradlew.bat --no-daemon :app:testDebugUnitTest :app:lintDebug :app:lintRelease :app:assembleDebug :app:assembleRelease
```

Then inspect the merged debug and release manifests and check the release artifact for embedded
credentials and signing material. The configured default LAN URL remains in source; it is not a
secret and this change does not remove it. Do not confuse removal of a default credential with
revocation of an already distributed key. Run the existing
Python cross-contract tests for bundle, event, status, receipt and radio compatibility in an isolated
test database. Container checks should build the root image and run the same OpenVINO, health, UI,
HEALTHCHECK and UID assertions defined in CI. None of these local checks substitute for the bounded
head-unit evidence above.
