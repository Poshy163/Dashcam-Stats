# Application audit — 30 September 2026

This sweep reviewed the API, all 19 frontend page modules and their shared components, authentication, media delivery, ingestion, OBD storage, processing, retention, recovery, dependencies, and release configuration. It combined source inspection, isolated regression tests, and a signed-in walkthrough of `http://192.168.1.16:8199/`.

The initial sweep produced local fixes and a prioritized backlog. The follow-up implements that backlog and additional issues found during integration. Initial validation was local; the subsequent authorized delivery of commit `abd4816` is recorded below. Existing investigation documents and the pre-existing OBD test change were left alone.

## Evidence and coverage

- Local baseline: `f3e2c6e5bc44bd4df532a7955f9a88fe846a223f` on `main`, with existing uncommitted work.
- Live health: HTTP 200, database/scanner/worker all healthy. Version is `main`; database migration is `0024`.
- The live source revision was not retrieved. A branch name and migration number do not establish parity with the checkout. Local reproductions below are not presented as live exploit demonstrations.
- [Complete endpoint inventory](audit-2026-09-30-endpoints.json): **100 registered method operations, 94 paths, 93 OpenAPI operations**. Methods: 58 GET, 3 HEAD, 31 POST, 3 PUT, 3 DELETE, 2 PATCH. Static assets are recorded separately. The inventory includes handler names, source positions, and live-probe cautions.
- Live UI visited: sign-in, overview, recordings, recording detail, journeys, journey detail/player, map, telemetry health, OBD list/detail, plates/detail, vehicles, queue, backup, all 12 settings categories, server/unit/CarPlay logs, and global search.
- Browser capture retained 298 responses across assets and data calls, including **36 distinct normalized API/media path patterns**. Captured data responses were 200, with video byte ranges returning 206. The event buffer dropped an intermediate segment, so this is a lower bound, not a complete HTTP trace. No browser console errors were captured during the walkthrough.
- The recording and journey videos inspected reached media `readyState=4` with no media error. This checks loading/readiness; uninterrupted long playback, all codecs, and physical head-unit playback were not tested.
- The live walkthrough avoided state-changing controls: no reprocessing, deletion, restore, GPU retry, backup start, radio commands, credential changes, or device probes. Normal page loading can still update access logs, caches, and the app's learned address.
- Local browser regressions used a separate temporary database containing 36 synthetic plates, no real footage, no workers/poller/scheduler, and no production proxy.

### Endpoint coverage by area

| Area | Registered operations | Evidence |
| --- | ---: | --- |
| Authentication | 8 | Live sign-in/state; local auth, session, CSRF, and Host-poisoning tests |
| Recordings | 11 | Live list/detail/telemetry/detections/plates; local validation, export and visibility tests |
| Journeys | 6 | Live list/detail/motion quality/player; local grouping, dates and lifecycle tests |
| Plates / vehicles | 9 | Live lists/details; local representative, confidence and visibility regressions |
| Map / telemetry health | 4 | Live map and coverage UI; local geometry and telemetry tests |
| Jobs / scan / process / reprocess | 10 | Live queue reads; local worker/queue mutation and concurrency tests |
| Ingest / OBD | 22 | Live status/history/bundles/drives/events; local transport, durability and storage tests |
| Logs / unit logs / search | 5 | Live filters, diagnostic tabs and date search; local route tests |
| Settings / system / retention / status | 15 | Live settings, hardware and database reads; local mutation, safety and recovery tests |
| Media / stream | 2 | Live thumbnails/crops and video 206 responses; local range/path tests |
| Documentation / frontend / health | 8 | Live health, shell/cache header and unauthenticated API refusal; local asset/auth tests |

Enumeration and broad test coverage do not mean every parameter combination or destructive operation was exercised against production. The JSON inventory is the exact route list; this table groups it for review.

## Initial fixes implemented locally

P1 means a security or data-preservation issue; P2 means a correctness/reliability issue. Severity describes impact, not proof that it already happened on the live server.

| Priority | Problem and evidence | Change |
| --- | --- | --- |
| P1 | An unauthenticated request could overwrite the learned server address via `Host`. A later head-unit URL appended the master API key to that address. Reproduced locally for a protected installation. | Protected installations learn addresses only from authenticated requests. Authenticated SPA state refresh preserves normal discovery after login. See `backend/app/main.py` and `api/routes/auth.py`. |
| P1 | A previous backup chunk's commit scanned shared staging and deleted files already arriving for the next chunk. A controlled overlap test lost the next completed file. | `ingest/puller.py` now touches only names in the current chunk's inventory. Tests cover both incomplete and complete following chunks; stale cleanup remains at the run boundary. |
| P1 | GPU retry advertised a restart requirement but immediately cleared the failed runtime's disable state and caches. | `ai/openvino_session.py` removes the persisted verdict without rearming the current process. A real restart is required. |
| P2 | Cold hardware detection inside a worker could synchronously run subprocess probes on the event loop while startup warmup was still in flight. | Workers await asynchronous detection. A regression proves unrelated event-loop work continues during the probe. |
| P2 | GPU failure markers were written by truncating the destination; malformed JSON shapes could break startup/status, and failed persistence could prevent a later retry. | Publish a flushed temporary file atomically, preserve old data on failure, retry persistence, and keep inference disabled when a marker is unreadable. |
| P2 | Date-only bounds added 24 UTC hours, mishandling Adelaide's 23/25-hour days. Midnight timestamps also expanded into full days; global date search used UTC dates. | Preserve date versus timestamp input, convert successive local midnights to UTC, and use the same bounds in date search. |
| P2 | Hidden/reprocessing plate observations could supply representative images or qualify a confidence filter despite being excluded from displayed counts. | Representatives and minimum-confidence filtering use the shared visible-observation predicate. |
| P2 | JSON export of a camera-backed recording hit async lazy loading (`MissingGreenlet`, HTTP 400). GPS export also used obsolete raw quality instead of the authoritative verdict. | Eager-load the camera and export canonical telemetry quality, retaining the original observed verdict where relevant. |
| P2 | Live overview reported accelerated inference/decoding even though Advanced showed CPU inference and software decoding. | Dashboard uses the effective runtime and decode policy, distinguishes unknown/unavailable status, and gives a concise GPU fallback warning. |
| P2 | Live `/plates?page=2` reset itself to page 1 after mounting. | Only an actual search-term edit resets pagination. Local browser verification retained page 2 of 2 across the debounce interval. |
| P2 | Several user actions failed silently; some failed secondary queries appeared as empty datasets. | Visible failure states for queue controls, backup start/cancel, recording protect/reprocess, journey reprocess/merge, recording telemetry/detections/plates, and plate sightings. |
| P2 | Resetting a setting left its unsaved draft visible; Save could overwrite the just-reset default. | Reset replaces that field's draft/error while preserving other edits. Browser test: width draft 800 reset to 480; separate quality draft 75 survived, saved, and remained after reload. |
| P2 | Journey dates/distances used unconditional white text on light cards. | Use theme-aware text colors. |
| P2 | CI's root-process check assumed `ps` existed and could announce success after missing evidence. | A bundled Python `/proc` checker fails for root, missing app processes, or missing UID evidence. Tested against synthetic process trees. |
| Maintenance | Registry audit found high-severity transitive tooling advisories. | Compatible lockfile updates: brace-expansion 1.1.21/5.0.12, js-yaml 4.3.2, router 6.30.6 and its dependency. No forced major upgrade. |
| Validation | Frontend helper tests were not part of the normal npm/CI command set. | Added `npm test` and a frontend CI test step. Three new tests distinguish hardware capability from effective runtime; all 15 helper tests run in CI configuration. |

Relevant new regression files are `tests/test_audit_security_and_dates.py`, `tests/test_runtime_privileges.py`, and `frontend/tests/hardware.test.mjs`; transfer/GPU/worker regressions extend their existing clean test files.

## What the live server showed during the initial sweep

| Observation | Meaning / improvement |
| --- | --- |
| Overview: 7,667 recordings; library: 8,442; database: 11,894 rows | Different populations are intentional: available footage, visible history including missing files, then all rows. The labels do not explain this. Add an availability filter and label the dashboard count “Available recordings.” |
| 244 visible journeys, 2,609 visible plates, 156 stored OBD drives | Main browsing and cross-links rendered successfully. These are observations during the sweep, not a preservation baseline for a deployment. |
| Footage 459.4 GB versus a 200 GB configured cap | The UI shows 230% and “Report-only mode.” This is a configured retention cap, not proof that the physical disk is full. Surface it as an actionable capacity warning. |
| Static/empty and parked-session cleanup switches are enabled even though size-based deletion is off | “Report-only mode” is not a blanket guarantee that every cleanup rule is dry-run. The Settings prose explains this, but the dashboard should describe each active deletion policy explicitly. No cleanup was triggered during the audit. |
| 3,323 healthy, 4,241 degraded, 103 no-fix recordings; GPS coverage 89.1% | These are recorded-footage quality metrics, not evidence of current Android GPS/CarPlay lag. Add reason/date filters and pagination to triage the 4,344 affected recordings; only 250 are currently listed. |
| GPU capability present, but OpenVINO runtime on CPU, software decode selected, retained `CL_OUT_OF_RESOURCES` verdict | The dashboard's capability/runtime confusion is fixed locally. The underlying driver/GPU reliability issue remains; this audit did not re-enable or physically test GPU operation. |
| Backup reported an absent car, 107 files / about 5.9 GB remaining, and a previous transfer-port timeout | Treat the absent vehicle as an expected state while separately showing stale backup/error history. The local chunk race is proven, but it is not established as the cause of this live backlog. |
| Database file about 1.35 GB; current restore upload limit 512 MiB | A normal full backup of a database this size exceeds the restore route's limit. This is a high-priority recovery gap inferred from the measured database size and source contract; no production backup or restore was run. |

## Follow-up implementation

The original backlog below is retained as the acceptance record. All rows are now addressed locally; the final validation section records the evidence and deployment boundary.

| Area | Implemented behavior |
| --- | --- |
| Recovery | Uploads stream to unique files with a configurable 16 GiB default ceiling, declared/actual size checks, disk headroom checks, cancellation/disconnect cleanup, and serialized atomic publication. Validation checks integrity, a supported Alembic revision, columns, foreign keys and required indexes/unique constraints against an isolated historical schema. Older supported snapshots still migrate. Failed backups clean up partial output; applying a restore preserves the existing database until atomic replacement. |
| Dependencies | FastAPI/Starlette, Pillow, SQLAlchemy and test tooling are upgraded together. Router is upgraded to 7.18.4 with compatible frontend dependency updates. Python runtime/dev/build locks carry hashes for Linux CPython 3.12, and Docker base images are pinned by digest. OpenVINO 2025.4.1 and ONNX Runtime 1.27.0 remain deliberate compatibility pins. |
| Release | CI exports the image that passed its checks. Release verifies the archive checksum, immutable image ID and revision, then tags and pushes the loaded image without rebuilding. CI audits npm, Python locks and the installed image packages. See [dependency and release process](dependency-and-release-process.md). |
| Journey playback | Camera switching resolves the same absolute capture time across unequal clip timelines. A missing interval pauses at the nearest available footage with an explanation. Playback telemetry reads a precise clip time window instead of loading a whole drive. |
| Page recovery and navigation | A successful route-content commit clears the one-time chunk reload marker; a Suspense fallback does not. Auth gates status reads, and navigation tests exercise original deep links through sign-in, query history, encoded parameters, aliases and missing pages. Pagination can recover when a refresh shrinks the result set. |
| Action and query failures | Settings, plate review, map routes, OBD summaries/diagnostics and remaining actions show contextual errors. API validation errors identify fields without echoing submitted inputs; custom request headers are merged correctly. Edits made while settings save remain drafts, and reset is disabled during the save to prevent competing writes. |
| Plate browsing | Sorting, filtering and displayed counts/confidence use the same visible-observation aggregate, with deterministic page ordering. |
| OBD reads | Optional time windows, selected signals and bounded extrema sampling; streamed typed sample columns exclude raw producer JSON. Diagnostics are paginated; summary first/last queries use SQL LIMIT 1. Full-resolution samples and immutable original bundles remain available. Sampled charts explicitly identify representative points, label the last retained reading as "last shown", and do not infer source staleness or adjacent-sample statistics from them. |
| Operational UI | Overview identifies available recordings, configured-capacity excess, stale/failed backups and independent retention policies. Recording filters and cards expose missing footage. Telemetry issues have reason/date filters and pagination; an empty filtered or out-of-range issue page does not falsely report healthy telemetry. |
| Accessibility and diagnostics | OBD charts have keyboard controls and sample tables, vehicle counts describe sampled frames, filters/actions have labels, CarPlay diagnostics use the configured timezone, and Advanced exposes the source revision. Existing dates refresh when the configured timezone arrives. |
| Additional journey consistency fix | Rebuild updates now synchronize retained recording objects in the SQLAlchemy identity map. A regression that holds those objects across two rebuilds catches stale memberships that could otherwise trigger unnecessary repeated reclustering. |

The OBD performance regression uses 5,000 synthetic samples: selecting speed/RPM with a 200-point ceiling returns 193 samples and 62,664 bytes, versus 3,399,202 bytes for the full response (**98.16% smaller response**). Endpoints and extrema survive, and the battery result is identical. This measures response size, not production latency or process memory. Chart rows are streamed in batches of 512, but accurate battery estimation still retains a lightweight voltage list proportional to the source samples. Full-resolution requests intentionally retain their complete result.

### Original backlog and acceptance criteria

| Priority | Improvement | Evidence and acceptance criteria |
| --- | --- | --- |
| P1 | Make recovery work for the current database size | `api/routes/system.py:864` caps uploads at 512 MiB, buffers the body before checking actual size without Content-Length, and `db/backup.py:115` shares one temporary filename. Stream bounded uploads to unique files; serialize publication; choose a documented size policy that supports this library. Test concurrent uploads, disconnects, oversized streams and a database larger than 512 MiB. Validate migration/schema compatibility before staging; table names and SQLite integrity alone are insufficient. |
| P1 | Upgrade and audit the backend dependency stack | The local virtualenv audit found advisory entries for Pillow 11.3.0, Starlette 0.48.0 and development-only pytest 8.4.2. Runtime pins/ranges currently prevent the needed major upgrades. Resolve FastAPI/Starlette and Pillow as compatible sets, rerun media/auth/restore tests, then scan the built Linux image. See the dependency evidence below. |
| P2 | Migrate React Router with navigation regressions | Two moderate npm findings remain in router/router-dom; npm proposes v7.18.4, outside the current v6 major. Verify deep links, login redirects, query history, and camera/plate links before migration. SSR-only advisory applicability differs because this app is a client-rendered SPA; an audit finding is not proof of exploitability. |
| P2 | Preserve real timestamps when changing journey camera angle | `JourneyPlayer.tsx:92` retains compressed elapsed time across camera timelines. With a missing front minute, elapsed 75 s represents 00:02:15 in front but 00:01:15 in rear. Map by absolute time, with explicit handling when the other angle has no footage. Test unequal starts, gaps and endings. |
| P2 | Make lazy-chunk recovery truly bounded | `RouteBoundary.tsx:58` clears its reload flag when the boundary mounts, even while Suspense is still waiting. Persistent lazy-chunk failure can therefore trigger repeated reloads. Clear after successful route-content commit and test repeated chunk failures in a browser. |
| P2 | Complete action/query error presentation | Remaining surfaces include Settings scan/process/reprocess/retention, plate flag/dismiss/notes, map route loading and OBD summary. Every failed action needs visible context and retry; failed requests must not look like zero results. Add browser tests for offline/403/500 responses. |
| P2 | Sort plates by the same visible rollups being displayed | `content.py:986` still orders historical plate fields, then replaces display values with visible-only aggregates. Hidden/reprocessing sightings can distort “Most recent,” “Most sightings” and confidence order. Compute a shared visible aggregate for filtering, ordering and display. |
| P2 | Bound expensive read paths | OBD series loads all samples/diagnostics before formatting; first/last summary queries lack `LIMIT 1`. Add time windows/selected signals and chart downsampling while retaining full-resolution export. Measure with large real-shaped synthetic fixtures and inspect query plans before claiming a speedup. |
| P2 | Improve operational truth on overview | Include failed/stale backups, capacity excess, and explicit deletion-policy status alongside processing health. Make software fallback an explained state; a detected GPU is not an active GPU. |
| P3 | Improve navigation and accessibility | Add telemetry-health pagination, missing-footage filters, keyboard/sample-table access for OBD charts, clearer “sampled frames” wording for vehicle Seen counts, and consistently accessible labels for filters/buttons. Verify keyboard and light/dark/narrow layouts. |
| P3 | Use one timezone in diagnostics | CarPlay chart ticks/period endings use viewer-local formatting while adjacent dates use the configured camera timezone. Use the shared formatter and test a browser in a different timezone. |
| P3 | Make deployment identity and artifacts reproducible | Display `source_revision` in Advanced/About. Lock Python dependencies and base image digests; promote the tested image rather than rebuilding for release. Current branch-name version and separate builds are not byte-level release evidence. |

### Dependency evidence and limits

`npm audit` initially reported four affected package entries: two high, two moderate. Compatible updates removed both high findings; the follow-up Router migration removed the remaining findings. The scan describes the checkout, not deployed assets. Examples: [router navigation advisory](https://github.com/advisories/GHSA-wrjc-x8rr-h8h6), [router SSR advisory](https://github.com/advisories/GHSA-337j-9hxr-rhxg).

The original `pip-audit` inspected **65 packages in the local Windows virtualenv**, yielding **25 unique advisory IDs across three packages**, after merging duplicate feed entries by ID. The original evidence remains in [Python dependency audit](audit-2026-09-30-python-dependencies.json). Follow-up scans of the upgraded local environment, Linux runtime/dev locks and built Linux dependency image report no known Python vulnerabilities. The image scan additionally found an advisory-bearing pip seeded by the pinned Python base; a separately hashed bootstrap upgrade fixes that in the virtualenv and runtime base. These scans do not cover Debian packages, physical GPU behavior or the live server. Before/after JSON evidence is stored alongside this report.

## Validation

| Check | Result |
| --- | --- |
| Full backend suite, final code | **2,313 passed, 26 skipped**, 121.35 seconds, four workers, local Python 3.14.7 |
| Backend Ruff | Check passed; 235 files already formatted |
| Frontend | Typecheck, lint, production build passed; **35/35 tests passed** |
| Production frontend bundle | Final entry `index-B5C0r6K2.js`, 85.26 kB gzip. No before/after speed claim is made. |
| Recovery regressions | A 513 MiB streamed upload, supported historical migrations, schema rejection, cancellation/disconnect, concurrent publication, disk exhaustion and preservation of existing files are covered locally |
| Local browser regressions | Page-2 bookmark retained; missing-footage and telemetry filters/pagination worked; settings draft reset/save preserved other edits; injected plate 403 and settings/map/OBD 500 responses produced visible errors |
| Chunk failure in a real browser | A forced route asset failure caused exactly one automatic document reload, then a stable error and retry control; restoring the asset and retrying loaded the map |
| OBD keyboard and narrow layouts | Arrow keys changed the chart's announced timestamp/value. OBD and settings were inspected at a 390px viewport; document width was 382px with no horizontal overflow in those views |
| Linux CPython 3.12 | 220 focused API/auth/playback/security tests and 47 final restore/read-query/journey regressions passed; a real OpenVINO 2025.4.1 CPU inference passed |
| Final Docker image | Built successfully; an isolated tmpfs container served health HTTP 200 and the final SPA, reached Docker's healthy state, and ran the application as UID 1000. All 240 captured build-source files stayed stable; 157 backend Python files and all 27 compiled frontend files matched container bytes |
| Dependency audits | Zero known npm findings; zero known Python findings in the local environment (67 packages), Linux runtime lock (50), Linux dev lock (67), and installed runtime image (51) |
| Workflow | actionlint 1.7.12 passed both workflows; 15 release/lock/Android workflow gate tests passed; isolated privilege checks fail closed |
| Diff | `git diff --check` passed |

The final local image is `dashcam-analyser:audit-20260930`, immutable ID `sha256:3423cbc01a07a1a46e4019f88c8920e227263e7763078bdd4556b905f0d52738`. Its smoke-test container used no production data volumes and was removed afterward. Archive save/load identity was separately verified on the earlier dependency-stage image, not this final image. This is local build evidence, not deployment identity for the live server.

Commands used for final checks:

```powershell
.venv\Scripts\python.exe -m pytest tests -n 4 --tb=short --durations=8 -q --disable-warnings
.venv\Scripts\python.exe -m ruff check backend/app backend/scripts tests
.venv\Scripts\python.exe -m ruff format --check backend/app backend/scripts tests
# From frontend/
npm run typecheck
npm run lint
npm test
npm run build
```

The Python suite still reports upstream deprecation warnings, especially under Python 3.14; CI targets Python 3.12. Local Linux container checks supplement the Windows suite, but do not establish physical GPU/driver or head-unit behavior. No new GitHub CI run, Android APK build, deployment, production restore/reprocess, or physical head-unit test was performed. Normal CI and deployment verification are still required before these local fixes can be treated as live behavior. The retained GPU driver failure and the absent vehicle observed on the live server were not changed.

## Authorized delivery after the audit

The user subsequently requested a commit, application update and GPU/driver repair. Commit `abd4816af72a6f000a5496bfd741532a169d883a` was pushed to `main` without sign-off or co-author trailers. [Release run 36671742328](https://github.com/Poshy163/Dashcam-Stats/actions/runs/36671742328) passed all checks: Linux Python 3.12 reported **2,318 passed, 21 skipped**, the frontend passed, Android tests/lint/APKs passed, and the image passed inference, startup, privilege and dependency checks. The exact tested image was published as `main` and `sha-abd4816`, both with registry digest `sha256:3222fc271167ef504f9b1fd25da2e679e9a100cf32dc2a1165ec63070e4da49a`.

Before updating Dockge, a consistent database backup was created at `/data/backups/dashcam-20260930-050732.db`. The existing stack's mounts, port, environment and data volumes were retained. After restart, the container reported source revision `abd4816af72a6f000a5496bfd741532a169d883a`, and the authenticated UI loaded the new build. Migration `0024` and these counts were unchanged across the update:

| Table | Before / after |
| --- | ---: |
| Recordings | 11,909 |
| Journeys | 365 |
| Plates | 2,631 |
| OBD drives | 156 |
| OBD samples | 15,350 |

This delivery supersedes the initial local-only validation boundary above. GPU recovery work follows separately; this first update retained the saved CPU fallback.

## GPU runtime repair and hardware investigation

Commit `05487af06974065dece23fd996c1f21656f22c41` replaces the old Debian OpenCL runtime with checksum-pinned Intel NEO `25.13.33276.16`, IGC `2.10.8` and GMM `22.7.0`. These packages fit the retained Bookworm userspace; the previously attempted NEO 26.27 packages require newer GLIBC/GLIBCXX symbols. Installation now fails on incomplete packages, and CI eagerly loads the compiler, OpenCL ICD, GMM and iHD libraries. A `debian` build option remains for older GPU generations.

The application now guards GPU compilation, property access and inference against a failed native context. CPU recovery uses an independent ONNX Runtime CPU session instead of re-entering the poisoned OpenVINO core. A separate probe supervises each model in a disposable process with temporary caches, finite-output validation and a hard timeout. A native abort that occurs inside the application's own process remains outside Python exception containment.

Validation for this commit:

- Windows full suite: **2,344 passed, 26 skipped**; Ruff and formatting passed.
- [Release run 36673916546](https://github.com/Poshy163/Dashcam-Stats/actions/runs/36673916546): Linux **2,349 passed, 21 skipped**, frontend, Android, Docker, dependency audits and exact-image publication passed.
- Published `main` and `sha-05487af` resolve to `sha256:ed0edb46c3a4d06e0b3e6335e16b0a9fae9c3467db4f1c06766667f48fdecb42` with the expected source revision.
- The updated live container reported that exact revision and successfully loaded all pinned Intel libraries as UID 1000. The saved failure marker was retained while isolated hardware tests ran, and the previously idle queue was paused.

The actual host has an Intel i9-13900H / Raptor Lake GPU (`8086:a7a0`), i915 and kernel `7.0.14-15-pve`. Its request timeout is 20,000 ms, hangcheck is enabled and `enable_guc=-1` means automatic selection. The container has no cgroup memory limit and initially reported no OOM events. The readable DRM error state reported no collected error; kernel logs were not accessible from the container.

A read-only application-wrapper baseline used six frames from existing recording 11908, repeated ten times. All **60 CPU model calls** succeeded with finite raw outputs, recorded frame hashes and stable detections; the median after the first six calls was **568.86 ms**. Diagnostics use temporary data/cache directories, pre-existing models and a read-only database connection; no recordings or stored detections are reprocessed by these probes.

### Actual GPU results

All five installed models passed **100 changing-input GPU inferences plus three warmup calls each**, in fresh supervised processes. Each reported `GPU.0` execution and finite outputs:

| Model | Median inference time |
| --- | ---: |
| RF-DETR medium | 179.06 ms |
| RF-DETR small | 102.58 ms |
| RF-DETR nano | 55.52 ms |
| Plate detector | 13.34 ms |
| Plate OCR | 7.88 ms |

The application's own detector wrapper separately passed **60 real-frame GPU calls**, with exact CPU/GPU frame hashes, shapes and decoded timestamps. Repeated outputs were stable on each device. The GPU median was **187.60 ms**, versus **568.86 ms** on CPU (about 3.03 times faster for this sequential inference workload; not a recording-throughput benchmark).

All 33 CPU detections had same-class GPU matches, with intersection-over-union at least 0.9556. Most confidence differences were below 0.014; one was 0.0644. The GPU returned three additional detections at confidence 0.4054–0.4187, just above the 0.4 cutoff. This establishes useful output agreement, not bitwise or exact detection-count parity.

A subsequent **600-call real-frame GPU soak** passed with stable first/last detections, finite outputs and `GPU.0` throughout. Its median was 130.72 ms (78.76 seconds of measured calls). The separate runs are not a controlled thermal/power benchmark. The DRM error state still reported no collected error and all cgroup OOM counters remained zero. Together these probes exercised **1,175 successful GPU calls**, including the 15 model warmups, before re-enabling production GPU selection. This is bounded hardware validation, not a guarantee of indefinite stability.

After those tests, the previous failure marker was preserved at `/data/backups/gpu-inference-failed-before-05487af.json`, and the old compiled cache was renamed to `/data/openvino_cache_2025.4.1-before-05487af`. The application retry function cleared the active marker, and Dockge restarted the container. The authenticated overview then showed **OpenVINO · GPU** with software video decoding; health remained healthy. No probe/decoder child remained, and the five database table counts above were unchanged.

### Separate video-decoding finding

The current camera clip is H.264 Baseline profile 66, full-range 8-bit 4:2:0 at 1920×1080. The iHD driver loads successfully, but FFmpeg reports `Codec h264 profile 66 not supported for hardware decode`. With the application's NV12 output flags, FFmpeg silently falls back to CPU and returns success; forcing actual VAAPI surfaces and downloading them makes the unsupported path fail explicitly. A successful FFmpeg exit alone therefore does not prove hardware decoding. Software decoding remains the intended policy while GPU inference is active.

A bounded diagnostic inspected the stream headers (Baseline 66, constraint-set-1 flag 0, no slice groups or redundant-picture flag in the inspected headers), then explicitly allowed a profile mismatch for one disposable FFmpeg process. It decoded 120 output frames through real VAAPI surfaces and `hwdownload` successfully in 0.44 seconds. FFmpeg selected profile 100 and warned of possible incompatibility. This establishes that the new Intel media stack can decode this tested stream section; it does not justify enabling a profile override globally, and no such override was added to production settings.

The resulting decoder follow-up requires VAAPI surfaces paired with `hwdownload,format=nv12`, both in frame extraction and the device capability probe. The probe also requires one complete output frame. Unsupported profiles now receive an explicit software retry, and the decoder actually launched is reported even if media policy changes while waiting. Thumbnail retries write a temporary image and only replace an existing thumbnail after success. Bounded diagnostics retain FFmpeg's profile rejection without treating benign setup messages as errors. Local focused validation passed **129 tests**; an independent review passed **116 tests** and found no remaining blockers.

The follow-up also makes the clean-cache behavior automatic: after validating installed packages and shared libraries, Docker records a deterministic fingerprint of the actual NEO/IGC/GMM versions. GPU cache directories include that fingerprint and the OpenVINO version. CPU and non-Docker behavior remain unchanged; a malformed or unreadable present fingerprint disables persistent GPU caching. OpenVINO deliberately permits compatible ZeBin reuse across driver versions, so this extra separation ensures recompilation after an Intel stack change rather than asserting that all older blobs are invalid. The combined cache, GPU recovery and media regression suite passed **150 tests**; the build-checker suite passed **25 tests**.
