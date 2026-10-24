# Quiet parked clips before transfer

Companion 0.3.10 adds an optional visual check alongside the advisory movement report.
The camera reads the video; the server receives a small decision manifest and bounded
fingerprint hashes before building its transfer list. Both the companion's quiet-clip
option and `ingest.skip_parked_footage` must be enabled. Both default to **off**.

This is a scene-change check, not person or vehicle recognition. Its thresholds are
experimental. A quiet decision does not establish that a scene contains no objects or
that an event could never have occurred. Do not enable it solely because unit tests pass.

## Transfer rules

- Only stable, unlocked, closed clips in the selected card's `DCIM/Video` can qualify.
- Ignition must be continuously observed OFF throughout the clip and the two-minute
  buffer on each side. Unknown states, clock discontinuities, gaps over 7.5 seconds,
  observed movement, or insufficient history preserve ordinary upload.
- Every overlapping opposite-view clip must independently qualify, and their intervals
  must continuously cover the candidate's whole interval. Recorder boundaries can be
  staggered, so the previous and next opposite-view segments may both be required.
  Active, locked, unknown or missing overlapping views preserve upload; no gap is bridged.
  Independent analysis runs before this transfer gate so a retained boundary view can
  still provide its own evidence for another candidate, without recursive dependencies.
- Every decoded frame is evaluated. Both input and decoder end-of-stream, increasing
  timestamps, matching frame counts and bounded gaps are required. Partial, unsupported,
  dark, overexposed, interrupted or failed analysis preserves ordinary upload.
- A newly closed, strongly ignition-OFF clip can wait briefly for its future guard,
  stability and visual analysis. The deadline is fixed at clip end plus six minutes.
  Activity releases it immediately; expiry or inadequate verified sleep time uploads it.
  Deferred files remain pending backlog, distinct from exclusions and successful copies.
- Each exclusion requires fresh matching card/path/size/mtime evidence and independently
  checked sampled content hashes. The server commits an audit record before changing
  the transfer plan. Missing, stale, malformed, mismatched or unwritable evidence uploads
  the clip normally.

Excluded files stay on the memory card. They are never marked copied, reclaimed by the
verified-backup deletion path, or counted as backed up. The recorder's own eventual loop
overwriting still applies. Disable either policy option to make them eligible again.

## Analysis before copying

The server changes accompanying companion 0.3.16 give an opted-in parked camera a bounded analysis
opportunity before claiming the ingestion lease. The Backup page displays **Analysing
parked clips before copying**, its reason and the latest permitted finish time. During
this phase the logger is not quiesced, radios are not quieted and no bulk footage copy
starts. The existing camera resource guards and complete paired-view proof requirements
still decide whether a clip can be excluded.

The first opportunity ends no later than eight minutes after the actual recorded
ignition-OFF edge. That anchor is retained across repeated runs and server restarts;
starting another backup cannot create another eight-minute allowance. Later pending
clips can wait at most three minutes, and their original clip-end-plus-six-minute
deadline always wins. Both opportunities leave at least four minutes of native sleep
time for transfer and restoration. The server refreshes power, logger and inventory
evidence while waiting and can only shorten the initial deadline.

The handoff requires both quiet-footage opt-ins, radio coordination, fresh native OFF
evidence and an awake compatible logger without an active drive or ingestion hold.
Activity or a terminal refusal resolves the affected opportunity; an unrelated or
already-uploaded activity clip cannot block a valid pending pair. Unsupported or stale
evidence, insufficient headroom, expiry or an operator cancellation ends the wait. A
refusal or timeout preserves ordinary upload; pending analysis never counts as quiet
evidence.

If a new eligible paired candidate appears during a long backup, the server finishes
the current copy chunk, retains the ordinary backlog and restores the logger and radios.
The next run can use the candidate's remaining original deadline. The decoder never
runs concurrently with the ingestion hold, and the handoff does not extend native sleep
or change accessory-power state. Previously uploaded or expired clips are not made
newly eligible by this scheduling change.

## Visual check and resource limits

The hardware decoder reads one clip at a time at background priority, without keeping
images. A 160 by 90 luminance grid samples each decoded frame. The validated native
timestamp strip (bottom four grid rows) is excluded. Version 2 compares adjacent frames
and a six-frame rolling window; spatially coherent, persistent change avoids treating
isolated compression noise as activity. The exact metric and thresholds are included in
the proof and validated by the server. Version 1 proofs cannot authorize an exclusion.

All frames must meet exposure and contrast checks. The decoder has a 90-second wall
budget and stops for renewed ignition, ingestion or memory pressure. Companion 0.3.14
starts or restarts analysis only below 95 C; a running analysis stops when a resource
check observes 100 C or more. Generic Android firmware permits moderate thermal status
and stops at severe or worse. Companion 0.3.15 adds a narrowly pinned compatibility
profile for the retained Q30 firmware: exact display build, model, hardware, SDK and
fingerprint family must match. That profile permits aggregate SEVERE only with valid
measured CPU checks; CRITICAL or worse and unknown status still refuse wherever the
Android thermal-status API is available. A changed or
unknown build keeps the generic rule. CPU temperature is checked before each decoded frame; full resource
checks are scheduled about once per second and again before accepting a complete quiet
result. This software policy does not control the physical temperature between
observations. Battery temperature of 42 C or more,
missing or invalid thermal probes, and the existing memory limits still refuse analysis.
Firmware thermal protection remains active; no OS status override or threshold setting
is written. The profile addresses the observed firmware aggregate SEVERE at measured
79.735 C, not a general claim that Android thermal status is incorrect. Resource-policy
cache version 3 invalidates earlier cached refusals; visual algorithm version 2 and its
proof requirements are unchanged. Companions before 0.3.14 used the 85 C limit.

Companion 0.3.15 retries only transient CPU/system/battery heat or memory-pressure
refusals with no observed activity. A persisted four-attempt per-clip budget and shared
30/60/120-second backoff prevent cycling through every hot clip. The existing six-minute
clip-end deadline is never extended, including after process restart or clock rollback.
While eligible, the zero-frame `not_analyzed/resource_cooldown` report explains the
bounded wait; it is not quiet evidence. A retry starts the complete analysis again.
Exhaustion, expiry, insufficient sleep headroom, driving, backup or unsafe evidence keeps
ordinary upload. Completed activity, darkness, decoder faults and unknown probes are not
cooling retries. Refused or interrupted analysis cannot authorize exclusion.
These limits preserve recording priority and still require
physical validation of both recording streams under sustained analysis.

Companion 0.3.18 uses resource-policy cache version 4 and separates cooling analysis
from upload deferral. The transfer deadline stays fixed at clip end plus six minutes;
an otherwise guarded retained clip can receive background analysis for at most
24 hours from that original clip end. This grants no wake lease or additional
server wait. A safe result can be used before a later upload opportunity; footage
already uploaded remains visible and is processed normally.

Zero-input/zero-decoded-frame admission refusals keep the shared persisted cooling
backoff without consuming one of the four started-decode attempts. Queued input or
decoded output spends an attempt. Completed or terminal results do not restart
when the bounded proof cache evicts them during the same process; only present
identities are tracked, and changed identities are evaluated conservatively anew.
Restart may reconsider a result within the same fixed horizon, with all guards
still required. Fresh six-minute candidates and opposite-view completion receive
priority over older retries; decoder candidates must survive preliminary report
truncation. Full pairing is checked again on the final bounded report. Expired,
unsafe or insufficient evidence always preserves ordinary upload. The thermal
thresholds, recording priority and complete paired proof contract are unchanged.

## Scanning, processing and journeys

Successful backup commits notify the scanner, coalesced after the batch. Existing scanner
enablement, settling time and automatic-processing settings still apply. Partial transfers
also notify after files have actually committed. No files are invented for exclusions:
they create no `Recording`, processing job, detection, telemetry or successful-copy count.
If a previously excluded clip is uploaded later, it is scanned and processed normally.

The separate exclusion ledger records exact identities, proof and observation times.
`GET /api/ingest/clip-exclusions` provides a paginated audit, including later local copies.
`GET /api/ingest/movement-status` includes current versus historical aggregate decisions.
The Backup page shows stale evidence explicitly. A decision is not a receipt or a promise
that the video was never copied by some other operation.

Known excluded intervals cannot silently bridge automatic journeys. The same uncovered-gap
rule applies during incremental assignment and full regrouping. Coverage from another
retained camera can bridge a gap; manual journey membership remains under user control.

## Calibration and physical acceptance

On 21 October 2026, visual review covered moving footage, traffic-light stops, quiet front
and rear driveway views, a parked rear view with pedestrians, and a very dark front view.
This confirmed that `sampled_stationary` and `acc_off_candidate` cannot alone identify junk.
The initial first-frame comparison classified shadows and foliage in the quiet views as
activity, so it was replaced instead of enabling a rule that could never skip those clips.
Private grids and actual presentation timestamps from five clips were then replayed
through the production Kotlin v2 tracker: both quiet views passed, pedestrians and driving
were retained as activity, and the dark view was unavailable. Those results validate the
reviewed examples, not every small or distant activity the grid might miss.

The first physical hardware decode used `c2.unisoc.avc.decoder`, decoding approximately
44.6 seconds in a 45-second budget without reaching end-of-stream. Both cameras continued
creating segments. SoC readings increased from approximately 79 C to 84 C; a later parked
reading was 94 C, near the firmware's 95 C passive trip point. Further decoder load was
held. This does not establish sustained decoder capacity or safe concurrent recording.

The production-signed 0.3.10/code 23 APK was then installed in place, with its running
revision and the server's `source_revision` separately verified at `fd96348`. A front
hardware probe completed 1,499 input, decoded and sampled frames covering 60.033 seconds
in 55.740 seconds of wall time, with both end-of-stream markers, and returned quiet.
The rear attempt returned unavailable with `soc_hot_or_unknown` before decoding any
frames. Both cameras' native active files independently rolled and grew after the front
probe. This confirms one observed recording-continuity window, not sustained paired
decoder capacity.

A later shell SoC reading during normal recording/backup was 93.760 C, so no further
decoder load was attempted. A production-signed, read-only private instrumentation probe
then read both thermal nodes under the existing app UID and PID, with matching expected
UID/PID and no PID change before/after. The SoC type matched, the app-UID temperature was
92.995 C and the CPU guard returned false. Thermal access is verified; the camera remains
too hot for visual analysis.

The packaged maintenance helper verified native countdown cancellation and a hold with
fresh heartbeat evidence. That evidence was lost around 08:58 UTC, before the planned
09:07 UTC expiry, for an unproven reason; native forced sleep is not established. Backup
run 580 copied 57 files / 3,486,902,740 bytes and observed zero backlog in its final
inventory, but ended partial after Wi-Fi dropped during restoration. Subsequent checks
verified Bluetooth, hotspot and logger restoration with `recovery_required=false`, while
the transition remained failed/inactive. The maintenance API subsequently reported
inactive after the hold was ended. These results do not make run 580 fully successful or
establish that later recordings cannot add backlog.

Representative ordinary uploads from moving, stopped, quiet and parked-activity views
completed metadata, telemetry, detection and plate processing. The new ordinary copy
`20261021190938_camera_1.ts` was automatically indexed as recording 18165 and queued
without a manual scan. Its four stages were pending on 21 October; a fresh 22 October
check confirmed metadata, telemetry, detection and plate processing complete, with no
error or missing-file flag. This verifies ordinary automatic discovery and processing.

On 22 October, the operator requested activation and both opt-ins were enabled: the
companion's quiet-footage checkbox was changed from unchecked to checked, and the server
accepted `ingest.skip_parked_footage=true`. The SoC reading was approximately 102.9 C,
above the unchanged 85 C analysis limit. No additional decoder probe was launched.
Enabling the policy does not override resource guards or turn advisory movement labels
into exclusion authority; unavailable analysis preserves ordinary upload.

Fresh producer output confirmed both policy options enabled with a ready, non-truncated
manifest, ordinary upload decisions and zero genuine exclusions. It did not prove a
thermal rejection by the production decoder: every visual result was `not_analyzed`,
with no decoder attempt observed. The historical backlog had passed the six-minute
analysis deadline. Fresh front and rear segments were also three seconds apart; the
0.3.10 two-second closest-peer gate changed pending decisions to upload before selecting
any decoder candidate, preventing their independent analysis even on a cool device.

The 0.3.11 update separates per-clip analysis eligibility from final transfer
coverage. It checks all overlapping opposite-view segments and exact continuous coverage
instead of widening the closest-peer tolerance. Retained `peer_coverage_required` and
`peer_analysis_pending` decisions can carry independently validated witness evidence;
they still upload, and the server rechecks their identities and fingerprints before
using them to support another exclusion.

The production-signed 0.3.11/code 24 APK from `b348066d24ed` was installed in place on
22 October, with its certificate and running revision verified. The application UID,
OS boot, ownership, read-only DCIM grant and quiet-filter setting were retained; passive
GPS remained disabled. Both native active camera files independently rolled and grew
during the update. Subsequent native boundaries were approximately six seconds apart;
the coverage rule handles staggered segments without changing a timing threshold.

Fresh production output around 01:47 UTC was ready and enabled, with 56 upload decisions
and zero skips. Three actual 0.3.11 analysis results were `unavailable`, each with zero
decoded and sampled frames. The logger was freshly parked with ACC OFF, ingestion state
known and no ingestion request; the SoC reading was approximately 107.0 C. This verifies
that independent analysis now reaches the resource guard and preserves ordinary upload
without decoding on the hot device. It does not establish complete paired quiet analysis
or a genuine exclusion. The server was still at `6c31999` at that observation; the new
coverage validator was subsequently deployed and verified at `53346a6` on 22 October.

After deployment, runs 584–586 copied 105 files / 6,282,289,404 bytes. The first run's
temporary 49 MB analysis deferral subsequently resolved to ordinary upload. Run 585's
radio cleanup needed a retry; durable logger resumption was later verified and its
partial history retained. Run 586 completed successfully with all radio/logger
restoration verified and no eligible backlog or deferrals in its final inventory.
The first automatic scan found 95 new files without errors. These are ordinary-upload
and temporary-deferral observations, not evidence of a genuine quiet exclusion.

Ordinary backup run 582 completed on 22 October: 239 video files / 12,724,617,944 bytes
were copied, with one duplicate OBD bundle acknowledged separately. Fresh restoration
checks verified Bluetooth, hotspot and logger restoration with `recovery_required=false`.
The subsequent automatic ingest scan finished with 65 new recordings, 25 changed and no
scan errors. This is ordinary-copy and scanner-handoff evidence; it does not establish
a quiet exclusion or the filtered end-to-end path.

Guarded enablement does not complete physical acceptance. Sustained paired decoding and
recording continuity within the thermal limits, a genuine quiet exclusion retained on the
card, a parked-activity upload, and downstream scan/job behavior for those decisions
remain unverified. No genuine exclusion has yet been established. The ordinary-upload
processing checks do not establish the filtered end-to-end path. Synthetic tests and
server-side calibration do not replace these checks.
Private footage, coordinates, plate images and credentials must stay out of repository
fixtures and release artifacts.
