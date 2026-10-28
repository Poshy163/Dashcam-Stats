# OBD logger companion — current release status

Quick orientation for the Android app that runs on the head unit. The full design and
procedures live in [`obd-dashcam-logger.md`](obd-dashcam-logger.md); this page is the
"where are we right now" summary.

## Installed version

| | |
|---|---|
| Version | **0.3.21** (`versionCode` 34), installed in place on 27 October 2026 |
| Source | `b89848e4cca3a9edf6613d3994f249a919fc2d2f` (`android/obd-logger/`), verified in the running companion, installed APK readback and server |
| Application ID | `com.dashcamstats.obdlogger` |
| Target / compile SDK | 34 / 35 |
| Status schema | `status.json` schema version 7 |
| Poll plan | The existing trial OFF choice, revision 1 and no fallback reason were preserved. No automatic re-arm was performed. New-drive scheduling acceptance remains pending. |
| Deployment | Installed APK SHA-256 and production signer verified; app UID 10162, permissions, ownership, stored drive identity and original OFF bounds survived. Both cameras continued recording. Temperature capture advanced during OBD quiescing, and matching readings reached the server. Automatic backup resumed and completed with verified radio restoration. Restart evidence records `package_update` on the same kernel boot; no physical reboot test was performed. |

## Server update: verified existing-copy cleanup

The server can verify older same-name/size candidates with full content hashes before
removing their card copies. Bounded cleanup retains independent durable preservation
through remote deletion, leaves changed or unverified footage intact, and runs after
ordinary copying and radio restoration. It needs a server redeploy and no companion
update. See [verified-duplicate-cleanup.md](verified-duplicate-cleanup.md).

## 0.3.22 candidate: driving workload and diagnostic recovery

Source version **0.3.22/code 35** reduces optional clip-indexing work while the
car is in use. Immediate movement and power observations continue. Full clip
assessment, fingerprints and report publication require fresh parked evidence;
bounded driving metadata refreshes run on a slower cadence. A parked transition
wakes the normal analysis path promptly, and interrupted scans cannot publish
fresh skip decisions from incomplete evidence.

The matching server update fixes the CarPlay codec diagnostic guard. A persistent
kernel file lock serializes capture and recovery, while validated stale workspace
metadata can be recovered without deleting a new owner's files. Active owners,
active tracing and unreadable or unsafe filesystem state are preserved. Capture
outcome and lock status are reported separately; a skipped capture never means
zero decoder latency.

Deploy the server for the diagnostic helper update, and install the production-signed
0.3.22 APK for the indexing change. Neither change alters the saved OBD trial choice,
native sleep timing, recorder controls or backup enablement. Device installation,
recovery of the actual retained lock, and a subsequent CarPlay drive comparison
remain physical acceptance checks. These changes do not establish a fix for the
phone-to-screen lag observed on 28 October.

Local validation passed **3,854 backend tests** (144 platform/optional skips),
**450 Android tests** including the saved private-grid replay, and **254 frontend
tests**. Both APK assemblies and debug/release lint passed with zero lint errors
and the existing 62 warnings per variant. Python formatting/lint and frontend
type checking/lint/production build passed. Executable Linux `sh`/`mksh` recovery,
contention and crash fixtures are mandatory in CI; their Windows skips are explicit.
The helper also passed 84 focused checks in an isolated local Linux container,
including real `sh` and `mksh` execution with no skipped checks. This caught and
corrected Android-shell descriptor inheritance before image publication.
Independent review covered capture ownership, interrupted indexing, cache retention
and the bounded pre-backup readiness wait.

## 0.3.21 installed: settings and recovery hardening

Source version **0.3.21/code 34** includes the 0.3.20 temperature collector below
and addresses the pre-install app audit:

- Within a running service, configuration reloads wait for the old logger's finalization and
  coalesce into the latest replacement. They cannot cancel an intermediate join
  and start a second logger while the original is still cleaning up.
- Invalid unsaved settings leave the saved logger running. Explicit OFF bypasses
  invalid drafts, durably disables only the logger and preserves other stored
  settings, including an unreadable webhook credential. A committed save or OFF
  issues its service action even if the setup screen closes during disk IO;
  service-action failures are distinguished from settings-write failures.
- Trial-setting reads and writes run on the IO dispatcher. Pending Apply is
  guarded, captures an immutable revision-checked choice, and cannot clear a newer
  health fallback or let an older refresh overwrite its result. Save-error and
  stale-choice notices survive ordinary status refreshes until reviewed or changed.
- Successful interrupted-drive recovery clears stale in-memory active-drive
  identity and timing, allowing parked analysis to become eligible again without
  another drive or service restart.
- Export-recovery pagination now compares timestamps using the same SQLite sort
  key as its ordering, preserving all entries across a page boundary even when
  distinct sub-millisecond instants round to the same value.

Local validation passed **442 Android tests**, with no failures, errors or skips,
plus debug/release builds and lint. Lint reports zero errors and 62 warnings per
variant. Regressions exercise blocked old-worker cleanup, transient SQLite
finalization/recovery failures with retained samples, all 65 export-recovery rows
across a page boundary, stale trial fallback, UI responsiveness, persistent error
notices, Activity destruction during a real settings commit and denied service
starts. Independent review covered lifecycle, settings and storage behavior.

Installed on 27 October. The camera captured valid temperature observations about
30 seconds apart, including during a real OBD-quiesced backup, with exact timestamp
and value matches in server history. The follow-up backup completed 14 files and
verified logger/radio restoration. Native sleep admission, backup settings, thermal
limits and the saved trial choice were preserved. Physical kernel-reboot, next-drive
and sustained quiet-analysis acceptance remain separate pending checks.

## 0.3.20 included: companion-owned temperature capture

Source version **0.3.20/code 33** adds a lightweight temperature collector to the
existing admitted foreground service. It starts through normal boot/update service
recovery, independently of the OBD polling worker and ingestion quiescing. It does
not acquire a wake lock or change native sleep, radios or recorder state. Capture
requires the logger service to be enabled and permitted; force-stop and disabled
service states are not continuous capture.

Thirty-second awake observations have a bounded private journal and a read-only
removable-storage mirror. The server imports changed, integrity-checked chunks
while parked, including during a footage backup. Capture time, import time and
missing sensor readings remain distinct. Legacy CarPlay temperature history stays
available, without inventing values across gaps. See
[dashcam-temperature-capture.md](dashcam-temperature-capture.md) for the protocol,
retention, recovery boundaries and physical acceptance checks.

The investigation on 26 October confirmed that the existing shell sampler retained
no temperature observation between **10:37:28 am and 7:27:27 pm Adelaide time**.
Device uptime places a boot around 10:38 am. The shell sampler had no local boot
launcher; its new session began on returning home. During the later online session,
the sampler stayed alive through the 0.3.19 installation, with a maximum observed
temperature interval of 83 seconds. Stale graph readings in that session were
consistent with delayed imports, rather than the sampler stopping.

Local validation passed: **3,745 backend tests** (78 platform/optional skips),
**426 Android tests** and **253 frontend tests**. Python formatting/lint, frontend
type checking/lint/production build, both APK builds and Android debug/release lint
also passed. Android lint retains 61 warnings per variant and reports zero errors.
The real Kotlin-generated manifest, chunk digests, three readings across two
sessions and nullable sensor evidence passed the Python validators unchanged.
Independent review covered recovery fairness, clock correction, valid legacy
fallback, SQLite timeout restoration and native power/OBD separation. The new
capture/import panel was visually checked with explicitly synthetic data. Device
installation and physical boot/backup acceptance remain separate from these checks.

## 0.3.19 installed: durable trial intent, MAF priority and startup evidence

Source version **0.3.19/code 32** addresses the 25 October review. It was installed
on 26 October; physical drive acceptance remains pending.

- Trial intent has a separate, versioned, checksummed, atomically replaced record
  in app-private no-backup storage. One conservative migration retains the existing
  choice and any named fallback; a migrated missing/corrupt record cannot import an
  old armed preference. Unavailable storage is reported explicitly. A bounded
  eight-change audit records source, revision and known change time. Warm polling
  reads use the validated in-process snapshot without repeated file IO or writes.
  The checkbox stages a choice; explicit Apply performs a revision-checked update,
  so an old form cannot clear a newer fallback. A committed named health fallback
  remains sticky across restarts. A separate fallback barrier protects an old armed
  primary record when its replacement write fails. Failure of all independent
  storage writes cannot durably record new intent through power loss; the current
  process reports the failure and retains its existing connection safety latch.
- Baseline plan 7 retains its nominal fifteen-second MAF opportunities during fault
  diagnostic reservations. Due MAF is the first budget-checked optional request
  after speed/RPM/load and ATRV. Diagnostics must fit their measured command budget
  and the remaining full post-primary tail within five seconds. Diagnostic replies
  have a separate budget so a long multi-frame reply cannot starve single-frame MAF.
  Outlier reservations age out after sixteen genuine observations. On a persistently
  slow adapter, other auxiliary work and optional fault scans can be deferred.
  Missing replies stay missing; no extra retries, copied-forward values, catch-up
  burst, poll-plan version change or calculation change is introduced.
- A separate bounded startup journal distinguishes app process restart, package
  update and a newly observed kernel boot. It records at most eight sanitized
  observations, firmware reason categories and temporally correlated own-app exit
  categories where Android supports them. Its cached public snapshot exposes no
  additional boot UUID, process token, raw property, stack trace or exception text.
  Generic `reboot` remains an unknown physical cause. The asynchronous observer runs
  after foreground admission independently of native sleep recovery; it grants no
  ignition edge, wake lease or countdown authority.
- The server independently validates optional `trial_settings` and
  `restart_evidence` objects. Invalid advisory objects are omitted while ordinary
  status and backup remain usable. The dashboard shows the last reported trial
  state, bounded change history and startup observations, keeping saved intent,
  active poll plan, app exit and device reboot distinct. Older companions explicitly
  show that these diagnostics were not reported.

The historical OFF/no-reason state cannot be reconstructed. This installed release
retains the current baseline choice until an operator explicitly re-arms the trial.
Thermal limits, locked/activity/uncertain upload protection, read-only card access and
native sleep admission remain unchanged. New persistence, MAF and startup behavior
requires physical acceptance after installation; the old reboot cause remains unknown.

Local validation passed: **3,704 backend tests** (77 platform/optional skips),
**406 Android tests** and **245 frontend tests**, plus Python formatting/lint,
frontend type checking/lint/production build, workflow lint, both APK builds and
Android debug/release lint. Android lint reports zero errors and 61 warnings per
variant; the three additional warnings cover guarded optional property reflection
and two dynamic UI strings. The actual Kotlin public-status fixture passed both
server validators unchanged. The bounded diagnostics panel was also reviewed in
a synthetic browser preview. Independent reviews approved persistence, scheduling
and diagnostic boundaries. These checks do not establish physical-device behavior.

## 0.3.18 installed: physical review on 25 October

Source version **0.3.18/code 31** addresses the 24 October drive review. It is a
verified installed release. Its three-second trial was explicitly re-armed after
installation; the package does not automatically re-arm a previously disabled trial.

- Plan 8 still targets 3,000 ms and reports every raw target miss. Its separate
  health budget permits 500 ms of scheduling/storage margin; three consecutive
  completed, still-running cycles above 3,500 ms select baseline for the next
  connection. Missing-reading recovery and transport-fault safeguards remain.
  Optional diagnostics must fit the measured command plus full post-primary tail
  reserve. Deferred probes retry after 15 seconds, leaving auxiliary opportunities
  between reservations; actual probes retain the 65-second cadence. Only adjacent
  absent handoff checks can share a result for at most 100 ms, including read latency.
- Quiet policy cache version 4 separates the existing six-minute transfer hold from
  a fixed 24-hour background-analysis horizon measured from clip end. A naturally
  available safe idle window may analyse retained footage after cooling; this grants
  no wake lease and never renews the upload hold. Zero-input/zero-output thermal
  refusals retain persisted cooling backoff without consuming the four started-decode
  attempts. Current transfer windows, paired-view completion and candidates retained
  in the bounded report receive priority. Terminal results are not repeatedly decoded
  when the 128-entry proof cache evicts them during a large-card scan.
- Backup accounting preserves OBD and footage plans separately, unions completed
  identities into replanned totals, and makes completion callbacks idempotent.
  Retry-byte accounting survives replans. The observed 87-completed/80-total case
  now retains all seven OBD bundles as well as the 80 footage files. Footage backlog,
  durable history and raw tar-stream versus payload-byte semantics are unchanged.

The 95 C admission, 100 C observed running stop and Android CRITICAL/unknown guards
remain. At the measured 104 C/CRITICAL state, decoding must still be refused. Locked,
active, uncertain, stale, changed or incompletely witnessed footage uploads normally;
complete fresh paired full-frame/EOS proof remains necessary to skip a quiet clip.
Already copied footage is not hidden or deleted.

Local validation passed **3,668 backend tests** (77 Windows platform/fixture skips),
**347 Android tests** (no skips) and **240 frontend tests**. Ruff, frontend
typecheck/lint/build, npm audit, workflow actionlint, both Android APK builds and
debug/release lint passed; Android lint retained 58 existing warnings and zero
errors per variant. The large-card eviction test initially exceeded the default
one-minute coroutine-test limit; its real inventory now grows progressively and
passed in 41 seconds with an explicit bounded three-minute fixture timeout.
Nine backend regressions verify valid paired proofs after 10 minutes, one hour and
23 hours of cooling, and reject stale, changed, incomplete or active evidence without
renewing the original hold. Counter tests cover real OBD bundles, repeated handoffs,
retries and completion callbacks. Independent polling/counter and quiet-policy source
reviews passed. CI's ffmpeg package cache is now keyed by distribution, architecture
and runner image to prevent replaying Ubuntu 24.04 packages on a 26.04 runner.

The latest physical 0.3.17 drive retained 320 rows: 319 speed, 320 RPM and 318 MAF
readings, with median fresh intervals of 3.029/3.030/3.032 seconds. Both malformed
MAF replies recovered on the next scheduled row; there were no command timeouts or
transport failures. Camera, server and independent replay agreed on 14.649971944 km
and estimated 1.265352021 L. Three work cycles just above the old 3,000 ms threshold
caused fallback while stationary, about three seconds before actual accessory OFF.
The new health-budget and scheduling behavior require a new installed trial to
establish physical performance.

Normal backup run 619 completed 87 files / 4,505,697,497 bytes at 16.51 MB/s; later
chunks also completed with Bluetooth, hotspot and logger restored. Quiet exclusions
remained zero because the camera was hot. Tests establish the conservative late-proof
and scanner contracts; an actual quiet exclusion retained on the card and omitted
from downstream processing still needs physical acceptance in a safe cool window.

The following seven recordings contained 821 contiguous samples with no sequence
holes or duplicate IDs. Bundle/member hashes and 14,778 mapped server-series values
matched; independent replay agreed with producer/server distance, estimated fuel and
idle time. The four trial segments supplied 450 rows, including 427 operating rows:
speed/RPM were present in all 427, MAF in 422. All five malformed MAF replies recovered
on the next row, with 6.000-6.060-second valid-reading gaps. Median speed/RPM cadence
was 3.00-3.01 seconds. Four raw target misses were recorded, including one isolated
4.185-second cycle; recovered segments lacked final timing flushes, so their tails
are incomplete evidence.

The latest 14:05 Adelaide drive instead used plan 7: 259 rows, 15.032736667 km and
estimated 1.314614043 L. Its eight roughly thirty-second MAF gaps comprised seven
optional diagnostic reservations and one malformed reply. Fuel integration covered
about 91.3% of the observed row span; matching arithmetic is not complete or calibrated
fuel measurement. A 74.140-second restart boundary had moving observations on both
sides on 24 October; a 77.585-second morning boundary had stationary observations.
Boot count rose from 25 at installation to 27. The retained reason was generic
`reboot`, with empty accessible crash/pstore evidence, so power, thermal, firmware and
app causes cannot be established retrospectively.

Quiet exclusions remained zero. At 86.875 C the CPU was below the admission ceiling,
but Android CRITICAL independently refused decoding. BatteryService reported no
physical battery; the thermal HAL's 65 C battery category is not a verified battery
temperature. Three earlier activity results retained ordinary upload. The processing
queue continued with four running jobs and zero failures.

Backup remained enabled throughout the review and implementation. Runs 630-633
completed 140 files / 7,493,446,744 bytes. Run 634 copied another four files /
245,305,972 bytes and reported zero backlog, then its history was marked partial after
cleanup acknowledgement was interrupted near the native window's end. The later
radio snapshot reported restoration verified and no recovery required. The camera
went offline; no operator pause, cancellation, wake hold or forced sleep was applied
during this review.

## 0.3.17 installed and durable sleep timing

Source version **0.3.17/code 30** preserves a genuine observed native ON to OFF edge
across brief app restarts and package updates. A bounded, versioned checkpoint is
stored atomically in app-private storage excluded from Android backup. Restore needs
the same kernel boot UUID and boot count, a fresh known native OFF read, unchanged
last-observed property and monotonic continuity within the existing 15-second limit.
The original OFF bounds and captured window remain fixed; a restart never starts a
new timer or extends its deadline.

Missing, malformed, stale, expired or mismatched checkpoints preserve unknown timing.
Fresh ON, unknown power/window reads, clock rollback and reboot invalidate saved OFF
proof. The existing maintenance cancellation marker remains authoritative; a saved
pre-cancellation edge cannot clear it. No old shell-writable status file or movement
journal is imported as native deadline authority, and the status wire schema is unchanged.

The first update from 0.3.16 cannot reconstruct an already-lost checkpoint. Recovery
of the current backup uses the existing verified, bounded maintenance lease; future
native recovery requires a genuine recorded edge. Durable restart/update acceptance
is separate from successful copying under that temporary lease.

The one-time recovery completed without an accessory-power cycle: durable backup
run 616 finished OK at 10:07:21 UTC on 23 October, copying 44 files / 2,258,492,692
bytes at an average 6.94 MB/s. Increasing transfer counters and the new history row
were verified independently. Bluetooth, hotspot and logger restoration all passed,
with no recovery debt. This verifies the bounded maintenance path, not restoration
of a cancelled native timer. The current 0.3.16 server already accepts the unchanged
v1 proof contract used by the new checkpoint.

After that completed run, the temporary lease lost its screen heartbeat and was
revoked with `screen_timeout`; it did not reach normal expiry. The camera remained
parked with Bluetooth, hotspot and logger restored, no active transfer and no radio
recovery debt. Later footage remains behind the unknown-native-deadline gate until
another independently verified maintenance lease or the next genuine drive edge.

Validation passed locally: 3,651 backend tests (77 platform/fixture skips), 333 Android
tests and 240 frontend tests. Ruff, frontend typecheck/lint/build, debug/release Android
lint and both APK builds passed; Android lint retained its existing 58 warnings and
zero errors. Twenty new Android cases cover exact restart bounds, corrupted records,
revocation, write failure and slow-storage concurrency; seven backend contract cases
verify fresh re-emission without deadline extension. Independent source review passed.
Positive physical checkpoint continuity still needs an actual new native OFF edge;
the already-cancelled 0.3.16 window cannot supply that acceptance evidence.

Production-signed 0.3.17 was subsequently installed in place. The installed APK
matched SHA-256 `a9010928507c5db5a5466c34c98a4f76e80cfb59ec71435cc31f70d7a95c4500`
and the pinned production certificate. The kernel boot, app UID, runtime grants and
all 20 immutable export artifacts were preserved. Fresh companion and server status
reported the new source/version, ready movement analysis and ready quiet filtering.
Both camera streams grew and rolled new segments. Same-process read-only verification
confirmed retained ownership, enabled three-second trial with no fallback, webhook,
quiet filtering, one persisted read grant and zero write grants. The private no-backup
checkpoint existed with a valid checksum and revocation tombstone, as expected without
a new edge; the temporary verification helper was removed without restarting the app.
The complete source CI and tested-image publication passed in run
[`39938107753`](https://github.com/Poshy163/Dashcam-Stats/actions/runs/39938107753).

## 0.3.16 installed and physical review

Source version **0.3.16/code 29** addresses the remaining 0.3.15 drive findings.
It was installed after the matching server was deployed.

- Plan 8 grants MAF one retry on the next scheduled row after its first strictly
  rejected, prompt-complete reply. The rejected row remains missing, no extra command
  is added to that row, and repeated failures retain the exponential backoff and
  existing health/fallback limits. Other PIDs and baseline polling are unchanged.
  This removes the artificial first-error ten-second cooldown; it cannot guarantee
  that an adapter's next reply will be valid or eliminate all estimated-fuel gaps.
- An owned native sleep deadline produces a distinct orderly window-ending stop.
  Positive generation, process identity and monotonic deadline/restoration evidence
  are required. No ending/restoring watchdog is treated as healthy or renewed;
  unproven loss, stale/malformed evidence and failed restoration remain failures.
  Committed footage is retained, the rest stays pending, and explicit Cancel wins.
- Opted-in parked analysis receives a bounded opportunity before radio quieting and
  bulk copying. The Backup page exposes the analysis phase and its absolute cutoff.
  The individual clip-end-plus-six-minute deadline, paired full-frame/EOS proof,
  thermal/resource checks and safe-upload fallback remain in force.

Local validation on 23 October passed **3,644 backend tests** with 77 Windows
platform/fixture skips, **313 Android JVM tests** with no skips, and **240 frontend
tests**. Ruff lint/format, frontend typecheck/lint/production build, all three locked
Python dependency audits and the npm audit passed. Both Android APK variants assembled;
debug/release lint reported zero errors and 58 existing warnings each. Generated
watchdog scripts were executed under the available POSIX shell. The scheduling tests
exercise full paired proofs before any quiesce/copy, bounded fallback, completed-chunk
restoration, current versus historical refusals and exact backlog with camera filters.

The production-signed APK was installed in place with SHA-256
`a7cacf714391e6285a7052f9073145c054e80e2ae2f24f4b3d68fcc5bb9797c2`.
The installed APK matched those bytes and the pinned production certificate. A
same-process, read-only verification confirmed the armed trial, retained ownership,
one persisted read grant and zero write grants; its temporary helper was removed.
All 20 immutable export artifacts were retained. The native countdown remained
visible after installation, but the restarted app had no in-memory OFF bounds and
the server correctly withheld further radio quieting. No native timer was inferred
from the configured 1,200-second property.

Physical acceptance still requires real malformed-MAF recovery,
native deadline/restoration checks, actual quiet exclusions retained on the card and
their downstream scanner/processing omission. Local tests and historical replay do
not establish those future device outcomes.

## 0.3.15 installed and physical review

Source version **0.3.15/code 28** addresses the physical 23 October findings. Deploy
the matching server before installing the companion: new poll-timing diagnostics use
schema 2 with bounded, exclusive cycle-stage timing; schema 1 remains supported.

- A narrowly pinned profile for the known Q30 firmware allows aggregate Android
  SEVERE only while the measured SoC admission/continuation checks permit work.
  Generic firmware still refuses SEVERE, and CRITICAL or worse and unknown status
  are refused wherever Android exposes the thermal-status API. The 95 C admission and 100 C observed hard stop, battery,
  memory, recording and backup checks remain. No firmware thermal setting is changed.
- Transient resource refusals receive a persisted, bounded cooling retry budget with
  increasing delays and the original six-minute deadline. Exhausted, expired,
  uncertain or non-resource failures upload normally. Cache policy 3 invalidates the
  previous permanently cached thermal refusals; complete paired quiet proof is still
  required for an exclusion.
- Active polling avoids redundant control-file cleanup and unnecessary metric
  snapshots. Optional request timeouts may recover only after strict prompt
  synchronization; uncertain transports still reconnect. The trial keeps its
  three-second target and fallback safeguards and is not automatically re-armed.
- Derived API/UI evidence identifies bounded gaps between adjacent interrupted
  recordings separately from within-recording cadence gaps. It does not join trips,
  invent observations or integrate distance/fuel through an unobserved interval.
  Camera-reported missing time and the server's threshold-based definition are
  labelled separately; unavailable historical producer values remain unavailable.

The signed APK was installed on 23 October. All five native firmware identity fields
matched the narrow compatibility profile. The camera initially reported 106.255 C
and Android CRITICAL, correctly retaining the analysis block. Cooling diagnostics
reached the matching server, and expired cooling results returned to ordinary upload.
The three-second trial was re-armed through the app's existing preference API with
same-process read-back; the temporary signed helper was removed and the native
countdown was preserved. APK SHA-256:
`13f3dbf9db30aeb6ab9884b0b7c69ed25e30b53796d338087a2bdd3ecffddb4f`.

The two following drives sustained plan 8 with median fresh speed/RPM/MAF intervals
of 3.02-3.03 seconds, no command timeouts, no connection losses and all 868 samples
persisted. Independent production-accumulator replay exactly matched camera and
server distance and estimated fuel. Four malformed MAF replies per drive still
caused 12-15-second gaps; the second drive reported three slow row intervals with
6.192 seconds of excess. A backup retained 145 files/8.557 GB before losing watchdog
proof; a later retry completed 42 files/2.518 GB successfully. Quiet exclusions were
still zero. With the camera later at 80.485 C/SEVERE, long backups continued to block
analysis and consume the six-minute clip deadline. The first drive's 47 video clips
were fully processed; the second drive's processing was still incomplete at review.
Naturally cool paired analysis, actual quiet skips and their downstream omission
path remain physical acceptance work.

Local validation on 23 October passed **3,506 backend tests** with 33 platform/fixture
skips, **307 Android JVM tests** with no skips, and **238 frontend tests**. Ruff
lint/format, frontend typecheck/lint/production build, Android debug/release lint
(zero errors, 58 warnings per variant) and both APK assemblies passed. Private
replay imported the six actual 0.3.14 bundles in reverse order: all three boundary
gaps were exposed while distance and fuel totals remained unchanged. Actual UI
components were visually checked with clearly labelled synthetic timing data.
These checks do not establish physical BLE recovery or sustained decoder/polling
performance on the new companion.

The release dependency audit identified the existing OpenTelemetry API 1.45.0
tracestate parsing advisory. Runtime and development locks now pin the patched
1.45.1 release with an explicit source floor; the runtime, development and build
locks pass the dependency audit without exclusions.

## 0.3.14 installed and 23 October review

Source version **0.3.14/code 27** implements the operator's 95 C/100 C request. It
starts or restarts visual analysis below 95 C and stops running analysis when a
resource check observes 100 C or more. Moderate Android thermal status is permitted;
severe or worse still stops analysis. Battery temperature at 42 C, memory, unknown
probes, driving and backup guards retain their previous behavior. Native firmware
thermal protection is unchanged. Resource-policy cache version 2 prevents reuse of
the older 85 C/moderate refusals; the visual algorithm and exclusion proof stay v2.

Checks remain bounded during decoding and run again before accepting a complete
quiet result. Partial or resource-refused results preserve upload. The chart shows
separate 95 C/100 C reference lines explicitly labelled as the 0.3.14 policy; historical
85 C refusals are not relabelled as 95 C. These limits are a software policy, not a
manufacturer rating or a guarantee that the physical sensor cannot overshoot between
checks. Sustained paired analysis and recording at the raised limits require a new
physical acceptance test. The signed update was installed and verified on 22 October.

The 23 October check found 14 visual attempts refused by Android's thermal guard
before decoding any frame, and zero current or historical exclusions. A direct SoC
reading of 79.735 C was below 95 C while aggregate Android status remained SEVERE.
The three-second trial recorded 14 rows over 46.410 seconds, with all 42 critical
requests successful, before `sustained_overrun` fallback. Two subsequent engine-load
timeouts split moving observations with 35.069 s and 24.601 s intervals that were
absent from the within-segment gap counts. All six bundles imported and camera/server
distance and fuel totals matched; matching arithmetic does not fill those gaps.

Local validation on 22 October passed all 290 Android JVM tests, including the
private footage-grid replay, with no failures or skips. Debug/release lint had zero
errors and 58 warnings per variant; both APK assemblies passed. All 3,454 backend
tests passed with 33 platform/fixture skips, as did Ruff lint/format checks across
296 files. All 232 frontend tests, typecheck, lint and the production build passed.
The actual chart component was visually checked with labelled synthetic temperatures;
the 95 C and 100 C references remained distinct. Automated and synthetic checks do
not establish sustained analysis/recording performance at the raised limits.

## 0.3.13 installed

Source version **0.3.13/code 26** addresses the issues found in the 22 October drive
review. It was installed after the matching server was deployed; the polling trial
was explicitly re-armed in the native app. A new physical drive is still required
to accept the polling fix, and sustained quiet decoding remains unverified.

- Critical-reading health distinguishes an actual failed reply from a bounded,
  intentional parser cooldown. Missing values remain missing; the parser and transport
  validation are unchanged, and cooldown cannot defer fallback indefinitely.
- Journey derivation and recording-position repair avoid carrying SQLite write locks
  through expensive reads and calculations. Radio ownership and restoration remain
  fenced and fail closed; diagnostic errors retain the preparation or lease failure
  instead of disguising it as an operator cancellation.
- Quiet-analysis reports retain bounded refusal reasons. Thermal/resource refusal
  still preserves upload, with the 85 C processor and 42 C battery limits unchanged.
  A reason is diagnostic evidence and cannot authorize an exclusion.

Local 0.3.13 validation on 22 October passed 3,451 backend tests with 34 skips;
Ruff lint and formatting passed across 304 files. All 284 Android JVM tests passed, including
the optional private footage-grid replay, with no skips or failures. Debug/release
lint had zero errors and 58 warnings per variant; both APK assemblies passed. All
231 frontend tests, typecheck, lint and the production build passed. The actual
Backup-page component was visually checked with clearly labelled synthetic refusal
counts, including separate saved/stale wording. These checks do not establish
physical decoder capacity or sustained three-second operation on the camera.

The installed APK matched the production certificate and had SHA-256
`43fde5b843bd3bf042b7b96ce5093cbe76f2eba4ea907f697f51eb728157422a`.
Fresh 0.3.13 events confirmed automatic package-replaced startup and successful
authenticated webhook delivery. The camera's Wi-Fi address changed during maintenance;
after the operator selected its device MAC, the server address was restored to
`192.168.1.214:5555` and fresh 0.3.13 status was verified there.

Run 594 durably recorded 81 files / 4,954,339,372 bytes, retaining a partial outcome
because radio restoration initially needed recovery. The subsequent four-file /
246,366,856-byte catch-up run 595 finished `ok`, with Bluetooth, hotspot and logger
restoration verified. Automatic ingest was restored after the temporary maintenance
hold. A vendor-native sleep request was sent, but the loss of the previous IP did
not prove suspension: after the address change the suspend-success counter remained
716 and the camera was awake. Do not count that request as physical sleep acceptance.
New visual diagnostics reported eight `system_thermal_guard` refusals; they validate
the refusal reporting, not quiet exclusions or decoding capacity.

## 0.3.12 polling trial

The preceding companion 0.3.12/code 25 from build `6875998` was installed for a physical drive on
22 October exercised plan 8 from 16:53:02 to 16:53:59 Australia/Adelaide. Median
successful response intervals were approximately 3.10 seconds for speed, 3.12 for
RPM and 3.17 for MAF. One malformed MAF reply was followed by two intentional
cooldown skips; the trial counted the three missing rows and fell back before its
plain-request retry. The recorded reason was `critical_readings_missing`, not a
sustained-overrun or speed/RPM parser failure. This is brief physical operation,
not acceptance of sustained three-second polling.

The three drives produced four verified bundles because fallback split the first
drive. All 509 sample timestamps, quality metadata and critical readings agreed
between the exported bundles and server series. Distance and fuel totals agreed:
30.348 km and 2.566 L estimated. Later baseline speed/RPM median intervals were
approximately 5.1 seconds; missing speed/RPM rows occurred at shutdown tails.
PID 15 remained unavailable (40 malformed and two missing replies), and the retained
combined validation error does not identify a defective vehicle sensor.

Normal ignition-off backup run 592 copied 135 files / 7,680,620,244 bytes and finished
`ok`, with Bluetooth, hotspot and logger restoration verified. Both camera streams
have indexed clips covering the three driving intervals, with no start interval
above 61 seconds; this is file continuity, not visual review of every frame. The
automatic scanner found 131 new recordings with zero invalid, missing or scan errors.
Processing began, but concurrent database-lock errors caused retries and stopped
follow-up run 593 before copying. Its logger resumed and radios were never disabled;
the remaining nine clips were later parked footage. The exact live lock holder was
not established.

The enabled quiet filter made no genuine exclusions. Six fresh visual results were
unavailable before decoding any frames. Captured processor temperature reached
97.8 C after return, above the unchanged 85 C decoder limit, but 0.3.12 did not retain
the exact refusal reason. Do not infer successful junk removal from enablement or
advisory movement labels.

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

### Server maintenance retry update (deployed and physically exercised)

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
acceptance was subsequently performed separately from the APK installation, as recorded
below.

Live investigation on 22 October reproduced a separate transport race: a normal
5,432-byte UI snapshot became an empty successful shell reply when discovery
disconnected ADB during the command, then became valid again after reconnecting.
The first deployed retry held admission correctly but lost its first heartbeat
before any files copied. Discovery now reuses responsive transports, and a shared
address guard prevents reconnects and daemon-root restarts during maintenance
preparation and active holds. Admission closes and the guard is released before
radio restoration, so a dead connection can still recover. Empty replies require
complete bounded reproof; malformed nonempty replies remain refused. The resumed
production poller and backup were then exercised successfully on revision `09c9de8`.
Run 591 copied 210 files / 12,683,360,592 bytes and durably finished `ok` on 22 October
at 06:01:10 UTC. The bounded maintenance hold retained fresh verified evidence through
the copy without an accessory-power cycle. Bluetooth, hotspot and logger restoration
were verified before ending the hold. The guarded vendor-native sleep operation was
sent and the camera remained offline; a post-sleep kernel suspend counter was not
observed. Automatic backups were restored. A later normal drive cleared the
cancellation marker and supplied a fresh native ignition-off window for run 592;
that observation does not isolate the camera-recorded-only recovery path.

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
`app/build.gradle.kts` declares version 0.3.20/code 33:

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
`app_version_name: "0.3.20"` and `app_version_code: 33` under `logger`. A cached status alone does
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
   heartbeat evidence, the version-three same-boot retry and subsequent radio/logger
   restoration are verified. The discovery transport race was reproduced and guarded;
   that does not establish every possible cause of heartbeat loss. Planned hold expiry
   and isolated camera-recorded-only power-cycle recovery still need physical acceptance.
   Advisory labels remain report only. Both visual-policy opt-ins were enabled on
   22 October at the operator's request, with unchanged resource and proof gates.
   On 22 October the camera was above the then-current 85 C limit; genuine exclusion,
   parked-activity transfer and sustained paired-decoder acceptance remain unverified.
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
