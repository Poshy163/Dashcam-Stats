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
later fully observed windows can recover. The clip indexer runs approximately once per minute at
background priority. It reads directory metadata and bounded content samples for file
fingerprints; it never reads whole videos for motion analysis. A file must be unchanged
across scans before receiving usable timing evidence.

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
SystemUI build, live ignition-off/awake state and a visible native countdown, long-presses
that countdown, then verifies its disappearance on the same boot. An already dismissed
popup is insufficient to acquire a new hold.

The server's small, hash-verified UI helper reads a fresh accessibility hierarchy without
waiting for an idle second (the countdown updates every second). Its source and reproducible
build are in `tools/maintenance-ui/`. Missing, changed or unsupported helper output still
refuses maintenance before any gesture; the helper itself only reads the screen.

`GET`, `POST {"duration_s": 900}` and `DELETE /api/ingest/maintenance` inspect, acquire and
end the hold. Fresh host evidence expires after 30 seconds; an on-device lease expires
within 45 seconds without heartbeats. Backup admission reserves the final 60 seconds for
radio restoration. Lost evidence, ignition/boot changes, expiry or cancellation closes
admission and stops copying before reconciling radios. The detached recovery watchdog
accepts an extended deadline only with a valid same-boot on-device lease.

The boot-bound cancellation marker survives a server restart and the end of a lease,
so old companion countdown evidence cannot reopen backup admission. An unreadable marker
or missing boot identity cannot prove another safe window.

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
