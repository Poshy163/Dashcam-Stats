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
- Both overlapping front and rear clips must qualify. An active, locked, unknown or
  missing companion view preserves both views. Small recorder start offsets are allowed;
  missing coverage is not inferred to be quiet.
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

## Visual check and resource limits

The hardware decoder reads one clip at a time at background priority, without keeping
images. A 160 by 90 luminance grid samples each decoded frame. The validated native
timestamp strip (bottom four grid rows) is excluded. Version 2 compares adjacent frames
and a six-frame rolling window; spatially coherent, persistent change avoids treating
isolated compression noise as activity. The exact metric and thresholds are included in
the proof and validated by the server. Version 1 proofs cannot authorize an exclusion.

All frames must meet exposure and contrast checks. The decoder has a 90-second wall
budget and stops for renewed ignition, ingestion, memory pressure, Android thermal
warnings, battery temperature of 42 C or more, or a measured SoC temperature of 85 C or
more. Missing supported SoC telemetry preserves ordinary upload. These limits preserve
recording priority; they can mean that no clips qualify on a hot camera.

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

Before activation, replay representative private clip grids through the actual Kotlin
tracker, verify retained activity and dark views, then test the production-signed APK on
the camera. Confirm permissions, temperature gates, complete decoding, both-lens recording
continuity, a real exclusion retained on the card, an activity upload, and downstream scan
and job state. Synthetic tests and server-side calibration do not replace these checks.
Private footage, coordinates, plate images and credentials must stay out of repository
fixtures and release artifacts.
