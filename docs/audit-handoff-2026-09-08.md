# Review handoff — server/source phase complete

**Follow-up:** the operator requested a head-unit app update and investigation of laggy
home departures versus smooth returns. See [the CarPlay investigation](carplay-investigation-2026-09-08.md)
for today's measured comparison, companion 0.3.1 signed upgrade, diagnostics/history
changes and current validation. Companion 0.3.1 is installed with matching on-device hash,
foreground service and exported-data continuity verified. The phone was absent, so playback
testing remains pending. Live mapping proved the old timing layers were recorder surfaces;
the corrected selector passed Android execution and 74 focused tests. Server changes remain
local. Additional new files in this follow-up:
`docs/carplay-investigation-2026-09-08.md`, `frontend/src/lib/carplay.ts`, and
`frontend/tests/carplay.test.mjs`. Run the new frontend regressions with
`node --test tests/carplay.test.mjs` from `frontend`.

The operator has now authorised an unsigned commit and push to `main`, without co-author
trailers. Final pre-commit checks passed: 1,745 backend tests, 13 skipped; Python lint/format;
frontend typecheck/lint/build and five frontend regressions. The installed-device result
above and the linked investigation supersede the
initial audit snapshots below. Server deployment is not verified by a source push.

## Initial server/source audit snapshot

Read [the full evidence report](audit-2026-09-08.md) first. The supplied gym repository was
unrelated; the verified matching checkout is Dashcam-Stats at
`1e258d04dc9590141a427e16da663d555fc802ed`. The running server reports `main`/migration `0022`,
not an exact commit. No commit, push, PR, deployment, notification or head-unit access occurred.
All edits remain unstaged. The pre-existing untracked `docs/agent-handover.md` is unchanged.

## Final checks

| Check | Result |
| --- | --- |
| Untouched backend baseline | 1,706 passed, 13 skipped, 91.57 s |
| Integrated backend suite | **1,728 passed, 13 skipped**, 90.03 s; 1,411 deprecation warnings |
| Ruff lint / format | Passed; 195 Python files already formatted at final check |
| Frontend typecheck / ESLint / Vite build | Passed |
| Real-app visual check | Passed on isolated synthetic footage; real probe, migrated test DB and viewer; workers/scheduler disabled; preview stopped afterwards |
| Android | **122 tests**, 0 failed/skipped; debug/release lint and debug/unsigned-release APK assembly passed |
| Docker Compose | Configuration validated |
| Container build/runtime | Not run: Docker Desktop Linux engine pipe is unavailable; no daemon was started |
| Strict mypy | Still fails: baseline 336 errors in 52 files; final 327 in 52 files. Normalised file/message comparison found zero new messages and nine removed diagnostics |
| Git diff / staging | `git diff --check` passed; index empty; HEAD unchanged |

Backend skips cover platform/runtime-specific coverage; a Windows test run does not prove
Linux symlink/permissions, vendor shell behaviour, or VAAPI operation. Android unit/build
success is not evidence of installation or physical behaviour. The 1.54 s suite-time difference
is ordinary test-run variation, not an application-performance claim.

The dashboard GET used once on production has an existing possible temporary writability-probe
side effect; see the full report's explicit caveat. Its local implementation is now read-only.
No production stream/remux, raw export, database snapshot, reprocess, retention or device-control
endpoint was invoked. A private pre-existing thumbnail was inspected outside the repository.

## Reproduce locally (no production calls)

Run from the repository root:

```powershell
.\.venv\Scripts\python.exe -m ruff check backend/app backend/scripts tests
.\.venv\Scripts\python.exe -m ruff format --check backend/app backend/scripts tests
.\.venv\Scripts\python.exe -m pytest tests -n 4 --tb=short --durations=12 -rs
.\.venv\Scripts\python.exe -m mypy --config-file backend/pyproject.toml backend/app
git diff --check
docker compose config --quiet
```

From `frontend`: `npm run typecheck`, `npm run lint`, `npm run build`.
From `android/obd-logger`, with JDK 17 and Android SDK 35 configured:

```powershell
.\gradlew.bat --no-daemon :app:testDebugUnitTest :app:lintDebug :app:lintRelease :app:assembleDebug :app:assembleRelease
```

This run used the existing toolchain under
`$env:TEMP\codex-obd-android-build\jdk` and `android-sdk`. No toolchain or APK was installed
onto the head unit. Local test output is in `$env:TEMP\dashcam-audit-baseline.txt`,
`dashcam-audit-final-tests.txt`, and `dashcam-audit-mypy-{baseline,final}.txt`.

## Changed tracked files

- `Dockerfile`; `backend/app/config.py`: immutable source revision in runtime diagnostics.
- `backend/app/api/routes/content.py`, `routes/system.py`, `schemas.py`, `visibility.py`:
  dismissal preservation, stored metadata projection, final GPS verdict, read-only status.
- `backend/app/core/logging.py`: preserve different stages' timing/outcome events.
- `backend/app/ingest/adb.py`, `puller.py`: conditional reclaim, durability, collision safety.
- `backend/app/scanner/discovery.py`: fingerprint post-read stability check.
- `backend/app/pipeline/stages.py`, `telemetry_quality.py`: dismissal and GPS provenance fixes.
- `frontend/src/lib/types.ts`, `pages/RecordingViewer.tsx`, `pages/Dashboard.tsx`:
  metadata, units/provenance, accurate stage/coverage and unknown-writability wording.
- Android `AndroidManifest.xml`, `LoggerConfig.kt`, `MainActivity.kt`, `ObdLoggerService.kt`:
  network permission/policy, credential removal/masking and authenticated redirect protection.
- Existing tests: `test_dockerfile_layer_order.py`, `test_file_stability.py`,
  `test_ingest_adb.py`, `test_ingest_transport.py`, `test_operational_enhancements.py`.

## New files (distinct from the existing handover)

- `android/obd-logger/app/src/main/res/xml/network_security_config.xml`
- `android/obd-logger/app/src/test/java/com/dashcamstats/obdlogger/LoggerConfigTest.kt`
- `tests/test_audit_metadata_and_corrections.py`
- `docs/audit-2026-09-08.md`
- `docs/audit-handoff-2026-09-08.md`
- `docs/audit-transfer-2026-09-08.md`
- `docs/audit-analysis-2026-09-08.md`
- `docs/audit-companion-2026-09-08.md`

No database migration. External status clients must tolerate null `footage_writable`.
Fresh Android installs require explicit webhook credentials; saved values persist.
Existing same-size local footage no longer permits card deletion on size evidence alone;
card occupancy can increase. Conflicting staged arrivals are preserved for review.

## Approval boundary and next investigation

Stop for review before committing, deployment or key rotation. The operator subsequently
authorised read-only direct device investigation during its online window; that phase is
complete and documented in [the device supplement](audit-device-2026-09-08.md). It confirms
the installed companion lacks INTERNET, identifies its build/hash, observes a successful
72-file automatic backup and radio restoration, and measures irregular original-media PTS
and paired-overlay timing. No runtime code was changed in that follow-up; this supplement
is an additional new, untracked documentation file.
Key rotation is an outstanding **confirmed exposure response**, not completed by removing the
source default. Other open questions: exact deployed image/APK match, reused-name acquisition,
radio-watchdog failures, CPU fallback, historical configuration provenance, partial analysis
coverage, original metadata/sidecars and VFR alignment.

The original read-only device plan used the following command templates. A bounded subset
was subsequently executed under the new authorisation; see the supplement for exact coverage
and remaining limits. Replace `<approved-serial>` only with the
operator-authorised device. Do not run unrestricted `getprop`, dump preferences/secrets, or
use state-changing shell commands.

```text
adb -s <approved-serial> shell getprop ro.build.version.release
adb -s <approved-serial> shell getprop ro.build.version.sdk
adb -s <approved-serial> shell getprop sys.boot_completed
adb -s <approved-serial> shell pm path com.dashcamstats.obdlogger
adb -s <approved-serial> shell dumpsys package com.dashcamstats.obdlogger
adb -s <approved-serial> shell dumpsys activity services com.dashcamstats.obdlogger
adb -s <approved-serial> shell pm path com.zqc.camera
adb -s <approved-serial> shell dumpsys package com.zqc.camera
```

Filter package dumps to version/code, target SDK, permission/service declarations and install
times; redact device/vehicle/network identifiers. Hash only paths actually returned by `pm path`.
The recorder package is from existing documentation, not a fresh discovery; if absent, resolve
the installed recorder from the approved package inventory before proceeding.

For one operator-selected completed recording and its paired camera, inspect file name/size/
mtime and adjacent `pre_*` metadata only; compare source identity with the server's inventory.
Inspect the resolved companion external-files `obd/ready`, `status.json`, `events.json`, and
receipt metadata in bounded samples, plus existing radio-watchdog proof/lease logs. Do not
trigger a backup, receipt/delete, power transition, receiver broadcast, or recorder action.

Expected evidence: installed/source version alignment; actual networking permission/policy;
atomic recorder rename and stable generation; original embedded/data streams or sidecars;
clock offset and paired-camera timing; why watchdog health proof expired. Any mismatch changes
rollout order or requires a protocol design before deployment. See the companion report for
the full eight-part investigation and the distinctions between observation and active testing.
