# Pipeline, metadata, and GPS audit — 2026-09-08

Scope: local source at `1e258d04dc9590141a427e16da663d555fc802ed`, limited to the
processing pipeline, OSD telemetry, and journeys. No server, head unit, API, deployment,
or media-library request was made during this audit. Existing untracked
`docs/agent-handover.md` was preserved.

## Lifecycle found in source

| Stage | Owner and durable evidence | Completion/failure behaviour |
| --- | --- | --- |
| Discovery and ingest | `app.scanner`, `app.ingest`; `recordings` identity, stat/fingerprint fields and ingest runs | Arrival/stability and fingerprint evidence keep a changed or growing source from being published as an analysed recording. |
| Metadata and thumbnail | `stage_inspect`; media fields, `probe_json`, thumbnail path, `metadata_state`/revision | ffprobe failures are classified as source-permanent only for conclusive demuxer errors; absent share/infrastructure failure remains retryable. Thumbnail-first does not mark analysis complete. |
| Overlay telemetry | `stage_telemetry`; `telemetry_points`, quality columns, recording rollups and telemetry revision | Decode infrastructure failures that yield no samples raise retryable `StageError` before deleting retained telemetry. The database replacement is one write unit after decode succeeds. |
| Detection and plates | `stage_detect`, `stage_plates`; objects, observations, recording counts and revisions | Telemetry reprocessing expands to detection, plates, then summary, so copied location data on dependent detections is rebuilt rather than left stale. |
| Summary and journeys | `stage_summarise`, `JourneyBuilder`; journey assignment/rollups/routes | Reprocessing preserves manually assembled journeys; normal rebuilds use the shared front-preferred/rear-fallback track and current revisions. |
| Publication/retry | `pipeline.orchestrator`; per-stage state and revision columns, `processing_jobs` | Stages are committed separately to release SQLite's write lock. A failed later stage retains earlier completed work; retained derived rows are hidden while their stage revision is `invalidated`. |

This separates a copied/indexed file, a completed metadata probe, a completed telemetry
decode, and a fully summarised recording. `pending_stages()` also treats a persisted
`RUNNING` state as unfinished after a worker crash.

## Metadata inventory

The metadata stage persists container, video codec/profile, dimensions, frame rate from the
stream and container, bitrate, pixel format, audio presence/codec/sample rate/channel count,
duration, timestamps, OSD-time provenance, and compact probe warnings/PTS-wrap evidence.
The telemetry stage retains the raw overlay text, OCR confidence, exact media offset,
canonical capture time, original overlay clock and delta, source/quality/reason, segment
breaks, speed and heading. Journey tracks resolve displayed time from a plausible overlay
clock or `recording.started_at + t_offset_s`, which avoids relying on a bad OCR date digit.

`probe_json` is deliberately compact rather than a raw ffprobe dump; it contains probe
warnings/PTS-wrap and durable handling markers. A read-only API/UI view should expose only
an allowlisted structured projection of those values. Container rotation/orientation and
embedded GPS were not found as persisted source facts; adding them needs representative
source-media inspection, and embedded GPS should take precedence over OCR only after that
validation.

## Confirmed fix: paired-camera GPS provenance

`recover_from_paired_camera()` previously used `quality_json["gps_status"]` to decide
whether a target hole could be filled and used JSON alone to identify a synthetic donor.
Migration `0009_gps_quality.py` can correct an old row without rewriting the historical
JSON: a rejected position has `has_fix = false` and `gps_quality = "rejected"`, while its
old blob can still say `"valid"`. The recovery path could therefore conceal the persisted
rejection behind a copied coordinate. A migrated/interpolated donor with no JSON could also
look independent.

The local change in `app.pipeline.telemetry_quality` makes the indexed `gps_quality` column
authoritative when it exists:

- `rejected` and `no_fix` targets are not repaired; legacy rows still fall back to their
  JSON status.
- A donor must be `valid` (or a genuine legacy row with no column) and must not declare a
  synthetic source. `interpolated` donors are never promoted to independent evidence.
- The existing conservative geometry check continues to veto copied coordinates that
  contradict the target camera's direct timeline.

This intentionally favours an explainable gap over a synthetic position when the local
quality pipeline has already made an explicit verdict. It does not change raw observations,
manual journey grouping, or existing direct legacy donors.

## Local evidence

After the change, the focused suite was run with the repository virtual environment and
normal pytest plugin loading:

```powershell
.\.venv\Scripts\python.exe -m pytest -q `
  tests/test_operational_enhancements.py tests/test_gps_reprocess.py `
  tests/test_gps_association.py tests/test_journey_integrity.py tests/test_osd_parser.py
```

Result: **107 passed** in 9.32 s. This includes two new regressions: a migration-corrected
row whose stale JSON says `valid` remains rejected, and a column-labelled interpolated donor
with no legacy JSON cannot seed another camera. `ruff check` and `ruff format --check` both
passed for the two changed files; `git diff --check` passed.

An initial run with `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` produced expected async-test plugin
errors (27 failures); it was not used as validation. The normal-plugin run above is the
valid result.

## Additional source audit: tags, caches, partial work, and media time

### Manual decisions through reprocessing

Manual recording protection, event type, and event notes live on `Recording` and are not
reset by any analysis stage. Automatic harsh-braking classification assigns `event_type`
only when it is empty. `Journey.manual` keeps manually split or merged journeys out of
automatic reclustering. Plate flag and notes already survived the plate-rollup cleanup.

One confirmed gap was fixed locally. The plate stage deletes a recording's old observations
before writing its replacement, then removes an empty `Plate` identity if it has no flag or
note. It did not treat `Plate.dismissed` as a manual exclusion. Consequently a dismissed
false positive could be removed after correction and, if seen again, recreated as a visible
plate. `_refresh_plate_rollups()` now retains a dismissed, zero-observation identity; the
new `test_reprocessing_keeps_a_dismissed_plate_without_remaining_observations` passes.

The API correction/merge paths now also OR a source plate's `dismissed` flag into the
target, just as they already do for `flagged`; the main audit integrated and tested that
route-level fix. There is no per-observation manual override model:
plate corrections are identity-level edits, so a future requirement to suppress only one
sighting needs a separate durable observation decision rather than overloading dismissal.

### Cache and configuration invalidation

Detector cache keys include model name and detection threshold; plate cache keys include
its threshold, and replacement discards the old compiled object before building the next.
The OSD template load is serialised to avoid two workers learning from the same library.
These reduce repeated model load, decode, and GPU pressure as intended.

Persisted analysis revisions are static code version identifiers. The settings service makes
the model/threshold and telemetry thresholds take effect for later jobs, but it does not
mark already-completed recordings stale when those semantic settings change. Neither the
stored template file nor its process cache is keyed by OSD region/profile or parser version.
This is a correctness/traceability limitation, not a safe small patch: addressing it needs
an explicit configuration fingerprint in stage provenance, targeted invalidation rules,
and migration/reprocessing UX. Do not claim a library was analysed under the current
settings merely because its static stage revision is current.

### Partial failures and empty results

The pipeline has important, tested distinctions:

- telemetry keeps valid partial samples from a truncated source but raises before replacing
  retained rows when no samples/infrastructure failure prove the decode did not complete;
- detection retains a partial decode only after frames were received, while infrastructure
  failure or zero unexplained frames is a retryable stage error;
- plate processing can skip an individual missing/damaged best-frame fallback and reports
  `missing_vehicle_frames`, while it clears stale observations if current detection has no
  tracks;
- stage states and revisions are committed at boundaries, and failed stages stop only that
  recording rather than falsely completing downstream summary work.

The limitation is visibility: a nonzero `missing_vehicle_frames` or a successful partial
decode is stage-result/log evidence, not a durable per-recording partial-completeness field.
It can be investigated, but the list view cannot distinguish a complete empty detection
from a partial detection pass without reading job output. Add a durable completion-quality
field only with API/UI contract work and migration coverage.

### Timestamp and decode observations

`ffprobe` validates suspect frame rates and recounts them; wrapped MPEG-TS PTS durations
are detected and recomputed. Sequential analysis uses FFmpeg's `fps=` filter, so output
offsets are deliberately uniform sampling times rather than raw input frame indices. This
is appropriate for the one-Hz overlay and sampled detection paths, including VFR inputs.

The plate fallback is less exact by design: it seeks to a track's sampled offset and the
first decoded frame is associated with that requested offset. It is used only when the
stored detection crop is unavailable; no actual decoded PTS is persisted. VFR or keyframe
seek error can therefore make a fallback crop an approximation, while its observation keeps
the original track offset. Validate this against representative source footage before
claiming sub-frame plate timing. The current code avoids repeated decoding by reusing saved
vehicle crops first and performs only bounded fallback seeks.

## Follow-up requiring source-media or device evidence

1. Compare a small paired front/rear sample against embedded streams and overlay timing to
   decide whether a camera-offset calibration is needed; current matching is wall-clock
   second based and defensively rejects inconsistent copies.
2. Inspect ffprobe stream side data on representative original files for rotation,
   creation-time metadata, and telemetry tracks. Do not add fields until their persistence
   and camera semantics are observed.
3. On authorised device access, collect read-only recorder finalisation evidence and a
   bounded file inventory, then correlate filename time, source mtime, first/last media
   PTS, and OSD clock. This is needed to validate capture-time and transfer assumptions,
   not to change the current safety rules.

## CarPlay sampler source audit and local instrumentation

The existing sampler used `ip neigh ... | grep -c REACHABLE` as both the timing gate and
the API's `phone_attached` signal. `STALE`, `DELAY`, and `PROBE` are normal neighbour
states for an active wireless peer, so that test could end a timing interval merely because
the cache transitioned state. It also did not identify the peer or prove CarPlay activity;
the old field name overstated the evidence. The local change retains that legacy field for
clients but records privacy-safe aggregate WLAN2 neighbour-state counts separately and emits
raw neighbour-present/absent observations. It persists no address, SSID, MAC, or handset ID.

The sampler previously divided received-byte deltas by the nominal configured interval. The
outer work can exceed that interval, so the bitrate was biased high or low. Zlink/OBD CPU,
received-byte, and AP-drop counters also assumed monotonic values: process replacement or a
netdev counter reset could report a negative measurement. The local script now uses elapsed
wall time for the byte rate and resets each baseline when the PID changes, a process vanishes,
or a counter goes backwards. The first reading after a reset is `na`, never a fabricated
zero or negative rate.

Historical timing data arrived only through the bounded, shared logcat capture. The sampler
already wrote a direct file but did not rotate or retrieve it, so a noisy logcat interval
could leave no durable record. The direct sampler log now rotates at 512 KiB with six older
generations (at most about 3.5 MiB). After the ingest status has a positive, read-only
ignition-off verdict, a later presence tick tails each fixed generation to 512 KiB,
tolerates absent rotations, caps parser rows, and non-destructively reads the result. This
recovery never runs during departure arming, where it could delay the first timing sample.
New sampler observations include an on-unit `sample=session-sequence`
identity, so the existing `UnitLogEntry` hash deliberately deduplicates a direct-file line
(`pid=tid=0`, whole-second time) against its logcat copy (real PID and millisecond time).
Pre-identity direct-file lines are skipped instead of guessed equivalent to historical
logcat data. Recovery/storage failure is logged but cannot stop a fresh sampler from arming.
No new table, identity data, or device command is introduced.

New script sessions use an epoch/PID token and are never inferred for older rows. Minute
buckets now include that token so reused SurfaceFlinger `#N` values cannot be averaged across
a sampler re-arm. The API returns `events` and `sessions` in addition to frames and minutes:
`surface_unavailable` and `surface_no_new_frames` are observations, not healthy zero-valued
timing. A candidate `SurfaceView[](BLAST)` identifier does not name an owning package, so
the code and head-unit reference describe its FPS as an observed surface measurement, not
proven Zlink or CarPlay frame rate. Package-named `com.zjinnova.zlink` SurfaceFlinger layers
are collected as additional `package_window` candidates while bare layers remain
`unattributed_surfaceview`; only numeric layer ID and that kind are retained, never a raw
window title. No candidate is preferred until an authorised live mapping can establish which
surface represents the complaint. `zlink_process_present` similarly reports only a `/proc`
presence observation; it is not a decoder diagnosis.

Focused local evidence after these edits: the CarPlay and unit-log suites passed **71 tests**
in 0.98 s, including a direct-file versus logcat copy with different timestamps/PIDs that
shares one canonical observation hash. Ruff and `git diff --check` passed for the changed
CarPlay files, and Git Bash `bash -n` passed for the shipped sampler. An attempted WSL bash
check was unavailable because that distribution could not launch `/bin/bash`; it was not
used as evidence.
No server, device, radio, Zlink process, or live request was made. The next comparison must
keep coverage, session, candidate-layer identity, and missing-frame observations visible;
it cannot infer a cause from departure/arrival correlation alone.
