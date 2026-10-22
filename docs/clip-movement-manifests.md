# On-camera movement reports

Companion 0.3.9 adds a small movement journal and clip report on the camera. This first
version is **report only**: it cannot exclude a recording from transfer, delete footage,
change retention, or stop the recorder. Existing videos without observations remain
unknown. It does not retroactively decide whether last night's footage was useful.

The advisory report remains unchanged in 0.3.10. A separate, optional
[visual quiet-clip policy](quiet-clip-transfer-policy.md) can exclude guarded quiet clips
from transfer after camera and server opt-in; the movement labels themselves never do so.

## Setup

In the companion, select the recorder card's **DCIM** folder using the Android folder
picker. The app retains read permission only. Revoking that permission or removing the
card makes reports unavailable and leaves normal backup unchanged. Broad storage access
is not requested. Optional passive location requires a separate opt-in and Android
location permission; background access must be granted in Android settings to resume
that optional listener after a background or boot start. OBD logging does not depend on
location permission.

The listener consumes fixes other apps already requested. It does not start active GPS,
store coordinates, or request additional OBD commands. Speed observations come from
validated scheduled PID 0D replies. Ignition is observed every five seconds, including
unchanged and unavailable readings. GPS observations must be fresh, non-mock and have
bounded speed and position uncertainty; an unavailable fix is unknown.

## What each classification means

| Value | Meaning |
|---|---|
| `moving_observed` | A validated positive OBD speed or a pair of sufficiently confident passive GPS movement observations was recorded. |
| `sampled_stationary` | Paired zero-speed OBD samples cover at least 90% of the bounded analysis window. Events between samples remain possible. |
| `acc_off_candidate` | Paired ignition-off observations cover at least 90% of the window. This does not establish visual stillness or absence of an incident. |
| `unknown` | Timing, coverage, identity or observations are insufficient. |

Boot/service boundaries, clock changes, ambiguous local time, large observation gaps,
unstable files and orphan `pre_` recordings are treated conservatively. Locked clips are
identified and remain eligible for backup. Adjacent same-camera filenames bound an
analysis window; they do not prove the video's duration. The advisory report does not
decode video or claim to recognise a collision, person, vehicle or other visual event.

## Storage and server contract

The private SQLite journal is separate from immutable OBD bundles and bounded to seven
days or 100,000 observations. Optional observations batch for up to ten seconds to reduce
flash writes; normal service shutdown drains accepted observations within a bounded wait.
Persisted gap ranges keep affected windows unknown after queue or storage failures, while
later fully observed windows can recover. The clip indexer runs at background priority,
waiting up to one minute after a completed scan while the visual policy is disabled, or
five seconds while it is enabled. Visual decoding runs separately on a dedicated
background thread, so a slow decode does not block metadata and policy refreshes.
The advisory report reads directory metadata and bounded content samples for file
fingerprints. Whole-video visual analysis belongs to the separate opt-in policy.
A file must be unchanged across scans before receiving usable timing evidence.

The app atomically publishes UTF-8 schema version 1 at
`<obd-root>/movement/report.json`, limited to 512 KiB and 512 clips. The report includes
producer identity, hashed card-volume identity, generation and coverage times, report
status, truncation, relative clip path, camera, locked flag, size/mtime, optional sampled
SHA-256 fingerprint, time quality, classification, reason codes and bounded evidence
counters. No coordinates, credentials, hardware identifiers or raw adapter replies are
accepted by this contract. Sampled fingerprints are advisory identities, not full-file
integrity verification.

Only companions advertising `clip_movement_manifest_v1` trigger the additional bounded
read before the ordinary transfer plan. The server validates the exact schema and
matches card volume, relative path, size and mtime against the current inventory. A stale
report (over ten minutes), card mismatch, changed file, invalid timing or missing evidence
becomes unknown. Reader or persistence failure cannot change the footage plan. The normal
transfer verification still determines successful backup. Sampled movement fingerprints
do not add a remote full-video checksum.

`GET /api/ingest/movement-status` exposes bounded aggregate counts and bytes, report age,
coverage and setup status. The Backup page labels these as observations and keeps all
footage eligible. A retained diagnostic snapshot survives server restart; its original
generation time still governs freshness.

## Verified maintenance wake hold

The Backup page can request a 5, 15 or 30 minute maintenance window while parked. The
server supports durations from 60 to 1,800 seconds. It verifies the exact supported
SystemUI build and live ignition-off/awake state. For an initial hold, it long-presses a
visible native countdown, then verifies its disappearance on the same boot. An absent
popup alone is insufficient to acquire a hold. A new explicit request can instead reuse
a previously verified cancellation on the same boot, subject to the fresh checks below.

The server's small, hash-verified UI helper reads a fresh accessibility hierarchy without
waiting for an idle second (the countdown updates every second). Its source and reproducible
build are in `tools/maintenance-ui/`. Missing, changed or unsupported helper output still
refuses maintenance before any gesture; the helper itself only reads the screen.

The updated helper holds an on-device kernel file lock across its accessibility
connection, snapshot and disconnect, preventing concurrent snapshot readers from
evicting one another. The lock is released on process exit. Transient heartbeat
transport failures may retry only inside the original aggregate refresh and evidence
budget; a new countdown or invalid power proof still ends the hold. This implementation
does not establish the cause of every earlier heartbeat loss, and physical acceptance
of the updated helper and retry path remains pending.

On 21 October 2026, server revision `fd96348` physically verified native countdown
cancellation and a maintenance hold with fresh heartbeat evidence. Evidence was lost
around 08:58 UTC, before the planned 09:07 UTC expiry; the exact cause is unproven and
native forced sleep is not established. Following a Wi-Fi interruption, the server
verified both radios and logger restored with `recovery_required=false`; the transition
remained failed/inactive. Ending the hold through the API then returned maintenance
inactive. Planned expiry and diagnosis of the early evidence loss remain open checks.

`GET`, `POST {"duration_s": 900}` and `DELETE /api/ingest/maintenance` inspect, acquire or
retry, and end the hold. POST accepts only the bounded duration, never a client claim
that cancellation or power state is safe. Fresh host evidence expires after 30 seconds;
an on-device lease expires within 45 seconds without heartbeats. Backup admission reserves the final 60 seconds for
radio restoration. Lost evidence, ignition/boot changes, expiry or cancellation closes
admission and stops copying before reconciling radios. The detached recovery watchdog
accepts an extended deadline only with a valid same-boot on-device lease.

The boot-bound cancellation marker survives a server restart and the end of a lease,
so old companion countdown evidence cannot reopen backup admission. An unreadable marker
or missing boot identity cannot prove another safe window.

### Retrying a verified cancellation

Version-three cancellation records add the strict boolean `native_verified`. It is
saved as `true` only after the witnessed gesture completes, the native countdown is
absent, and same-boot post-gesture power checks and the durable monotonic bound succeed.
Provisional records and legacy baselines retain `false`. Version-one and version-two
records remain readable for normal ignition-cycle recovery, but their bounds do not
prove native cancellation and never automatically authorize a retry.

An operator can request a new bounded hold after a failed, expired or ended hold. The
server requires a verified version-three record, the same camera boot, fresh native
ignition OFF/awake/normal sleep-policy observations and a freshly absent countdown.
The controller and process fences exclude concurrent maintenance and pending record
workers. The complete durable record must still match before and after those checks.
Only then does the server publish a new lease token and deadline. This also applies
after a server restart; no cached flag or missing popup substitutes for fresh proof.

Retry does not clear or replace the cancellation tombstone, fabricate an ignition
transition, or turn the lease into a normal native sleep deadline. The tombstone remains
after failure, stop and expiry, and the normal deadline stays unknown until a genuine
recovery event establishes it. The unchanged 30-second host evidence lifetime,
45-second device heartbeat lifetime and final 60-second radio-restoration reserve
continue to gate copying. Implementation tests cover this contract; a successful retry,
backup and subsequent requested sleep still require separate physical verification.

### Recorded ignition-cycle recovery

The backend recovery update can accept a genuine ON to OFF cycle recorded by the
companion even when the server did not sample the ON state live. This capability was
introduced in 0.3.10 and is retained by the current release. It
uses the existing `sleep_deadline_evidence_v1` status contract; it needs no APK update,
new permission or motion-journal export. It has not yet been physically verified.

A version-three cancellation marker first blocks admission before the cancellation
gesture. Once that gesture has completed and same-boot post-gesture power checks pass,
the server atomically saves a monotonic cancellation upper bound,
`ceil(post_gesture_uptime_s * 1000) + 1000`, with one second of conservative padding.
The exact server-private marker is:

```json
{"schema_version":3,"boot_id":"<kernel boot UUID>","cancelled":true,"generation":"<random UUID>","cycle_after_elapsed_ms":null,"native_verified":false}
```

For a new cancellation, the bound replaces `null` and `native_verified` becomes `true`
only after verification and only if the complete expected record still matches. An
unresolved check/write keeps recovery blocked. Timed-out disk workers retain their
process fence until they actually exit, so a delayed rename cannot overwrite another
process's newer cancellation. The server must then read fresh native ignition OFF and
fresh companion evidence on that
exact boot UUID. The recorded `off_lower_elapsed_ms` must be strictly greater than the
cancellation bound: it is the start of a real ON sample after cancellation, not the time
of a later OFF heartbeat. Its first OFF upper bound and captured countdown duration
remain frozen while subsequent OFF observations refresh only observation freshness.

The marker is cleared only by an atomic compare-and-swap of the full expected record,
so an old observation cannot erase a newer cancellation or baseline. A legacy marker
has no trusted cancellation time. While maintenance is inactive and owns no maintenance
fence, its first fresh, same-boot native OFF observation can establish a durable,
padded baseline for a future cycle; it cannot authorize the edge already present in
that observation. A later recorded ON sample must begin after that baseline. Corrupt
records and unresolved version-two or version-three markers with a `null` bound cannot
use this legacy baseline path. Fresh live native ON or a verified new boot retain their existing
recovery paths.

Restart, unknown reads or observation gaps cannot manufacture a cycle. The companion
starts with no tracker history after a process restart; any previously written status
still has its original monotonic freshness limit. The existing five-second poll is a
nominal delay after each read, not a guarantee that every short ignition pulse will be
captured, and Wi-Fi connectivity alone proves no transition. Missing or rejected proof
continues to block ordinary countdown admission. Accepted proof supplies only the
conservative remaining portion of its captured window; operational deadline ceilings,
radio restoration and all other admission checks still apply. It never grants a fresh
full configured countdown or fabricates an ignition state.

Cancelling the native timer physically keeps this parked session awake. Ending the
server hold restores backup/radio safety; it does not force sleep or recreate the native
timer. A real accessory-power ON to OFF cycle is needed to establish another normal
countdown. The sleep-enable property selects sleep versus shutdown and must never be
used as a wake inhibit. No fake ignition state, forced sleep or recorder-stop policy is
part of this feature.

## Physical acceptance checks

After deploying the server and a production-signed in-place companion update, select
DCIM, exercise permission revocation/card removal, and confirm fresh report imports. On a
subsequent drive, compare positive-speed and parked windows against both camera clips.
Check a genuine ignition transition and visible native countdown before testing hold
acquisition, expiry, connectivity loss and radio restoration. Software tests alone do
not establish those device-specific results or make footage filtering safe.
For the recovery update, separately verify a short cycle captured by the companion
while the server misses live ON, rejection of the preceding cycle, recovery across a
server restart, and fail-closed behaviour after a companion restart or evidence gap.
