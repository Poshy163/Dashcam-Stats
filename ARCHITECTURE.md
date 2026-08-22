# Dashcam Analyser — Architecture

This document records what the real footage actually contains, the decisions that follow
from it, and how the system is put together. It was written **after** inspecting the live
corpus, not before.

---

## 1. Findings from the real footage

674 files / 68 GB / ~72 hours were inspected directly over SMB before any code was written.

### 1.1 Container and naming

| Property | Value |
| --- | --- |
| Container | MPEG-TS (`.ts`) |
| Layout | One flat directory. No subdirectories, no sidecar files of any kind. |
| Filename | `YYYYMMDDHHMMSS_camera_N.ts` — local wall-clock start time |
| `camera_0` | **Front** camera |
| `camera_1` | **Rear** camera |
| Segment length | ~120 s nominal (~100 MB, ~7.2 Mbit/s) |

### 1.2 Streams

| | Front (`camera_0`) | Rear (`camera_1`) |
| --- | --- | --- |
| Video | H.264 **Baseline**, 1920×1080, `yuvj420p`, no B-frames, GOP ≈ 25 | same |
| Frame rate | ≈ 30 fps | ≈ 25 fps |
| Audio | AAC-LC, 8 kHz, mono, ~12.6 kbit/s | **none** |

`r_frame_rate` reported by the container is **not trustworthy** — 32 files report `90000/1`,
others `299/12`, `50/3`, `24000/1001`. These are ffprobe estimating from PTS deltas on short
or damaged segments. Real frame rate is established by counting decoded frames over a known
interval, with the container value used only as a hint.

### 1.3 Telemetry — the decisive finding

Every plausible metadata carrier was checked and found empty:

* PAT/PMT enumerated on multiple files — only the A/V elementary streams exist. No private
  data PID, no `stream_type` 0x06/0x15 metadata stream, no subtitle track.
* Every H.264 **SEI** payload in sampled files was parsed — no `user_data_unregistered`
  (type 5), no `user_data_registered_itu_t_t35` (type 4). In fact no SEI NALs at all.
* No sidecar files (`.gps`, `.nmea`, `.txt`, `.json`) exist anywhere on the share.
* Byte-level marker scans for NMEA sentences, Novatek/Ambarella `freeGPS` blocks, GoPro GPMF,
  and vendor strings produced only random coincidences inside compressed video payload.

**All telemetry is burned into the video as pixels.** Every frame of both cameras carries an
on-screen display along the bottom edge:

```
2026-08-04 17:44:38   E:138.6769 N:-34.8088  68 km/h
```

Properties established by sampling:

* Updates at **exactly 1 Hz** — five consecutive frames 0.2 s apart all read `17:44:14`,
  then the next reads `17:44:15`. Sampling telemetry faster than 1 fps yields nothing.
* `E:` is **longitude**, `N:` is **latitude**; both are signed decimal degrees at 4 dp
  (≈ 11 m resolution). Southern/western hemispheres appear as negative values.
* `E:00.0000 N:00.0000` means **no GPS fix**. It must never be stored as coordinates (0, 0).
* Identical OSD on front and rear, so telemetry can be recovered from either camera.
* Format is stable across the whole corpus and both device firmware generations observed.

**Fields that do not exist in this footage:** heading, G-force, accelerometer, and
event/emergency/parking classification. Heading and distance are *derived* from consecutive
GPS fixes and are labelled as derived in the UI. G-force is not obtainable and is not shown.

Consequence: telemetry extraction is an **OCR problem over a fixed screen region**, not a
metadata-parsing problem. See §4.

### 1.4 Real-world damage

About 2.5 % of the corpus is broken, which the pipeline must absorb without stalling:

| Failure | Count | Behaviour required |
| --- | --- | --- |
| Zero-byte files | 3 | Mark `Invalid` and leave the queue — see §5.3. Marking them `Failed` is what kept handing them fresh attempts |
| Unparseable stream table | 1 | Detect missing video stream |
| Decodes to green garbage (mis-detected as HEVC, broken PPS refs) | 4 | Survive decoder errors |
| **PTS wraparound** — reports 95,377 s duration for a 9 MB file | 2 | Clamp against 2³³/90000 ≈ 95,443 s |
| Sub-5 s truncated segments | 11 | Process normally, just short |

### 1.5 Front/rear pairing and journeys

Front and rear filenames are **not** identical — deltas run −3 s to +2 s, only 106 of 354
front files match a rear file exactly, and 44 front files have no rear counterpart at all.
Pairing is therefore done on a time window, never on filename equality.

Clustering front-camera segments on a 5-minute inter-segment gap yields 45 journeys across
12 days, ranging from 1 to 55 segments. This matches the observed driving pattern, so the
gap threshold is the primary journey signal, refined by GPS continuity.

---

## 2. Decisions that follow

| Decision | Rationale |
| --- | --- |
| **Single container** | Everything fits comfortably; a second container buys nothing here. |
| **SQLite (WAL)** over PostgreSQL | Peak load is ~260 k telemetry points, ~100 k detections for 72 h of footage. Even at 10× that, SQLite in WAL mode with one writer and many readers is well inside its envelope, and it keeps deployment to one image with no credentials to manage. The data layer goes through SQLAlchemy so a future Postgres move is a URL change plus a migration run. |
| **Durable queue in SQLite**, not Redis/Celery | The queue is low-volume (one row per recording) and must survive restarts. A `processing_jobs` table with atomic claim gives durability without another service. |
| **FastAPI + React SPA served by the same process** | One port, one image; the SPA is built at image-build time and served as static files. |
| **OpenVINO as primary inference runtime** | Target host is an i9-13900H (Iris Xe, 96 EU). OpenVINO's GPU plugin uses the same `/dev/dri` render node as VAAPI and needs no CUDA. ONNX Runtime CPU is the fallback. |
| **VAAPI for decode** | H.264 Baseline 1080p decodes on the iGPU essentially for free, leaving CPU for inference and OCR. |
| **Telemetry by glyph template matching**, OCR as fallback | See §4 — the OSD font is fixed and the region is fixed, so template matching is both faster and more accurate than a general OCR model. |
| **Retention is report-only by default** | The share is mounted read-only by choice. The planner computes what *would* be deleted; deletion requires an explicit setting **and** a runtime writability check. |

---

## 3. Deployment shape

```yaml
services:
  dashcam:
    image: ghcr.io/poshy163/dashcam-analyser:latest
    restart: unless-stopped
    ports:
      - 8098:8080
    devices:
      - /dev/dri:/dev/dri
    volumes:
      - dashcam-data:/data
      - /mnt/Vault/dashcam:/dashcam:ro
volumes:
  dashcam-data:
```

```
/data
├── dashcam.db          SQLite + WAL
├── media/              thumbnails, plate crops, vehicle crops (content-addressed)
├── models/             OpenVINO IR models
└── logs/
/dashcam                raw footage (read-only)
```

Environment variables are deliberately minimal — `DASHCAM_DATA_DIR`, `DASHCAM_FOOTAGE_DIR`,
`DASHCAM_PORT`, `DASHCAM_LOG_LEVEL`. Everything else is a row in `app_settings`, edited
through the UI and applied without a restart.

Authentication used to be the exception, as `DASHCAM_AUTH_USERNAME` and
`DASHCAM_AUTH_PASSWORD`. It is not any more, and section 9 explains why that mattered.

---

## 4. Telemetry extraction (the critical subsystem)

Because telemetry is pixels, this subsystem determines the quality of journeys, the map, and
every location attached to a detection. It is built for accuracy first.

**Pipeline**

1. **Region resolution.** The OSD strip is stored as *fractional* coordinates in an
   `osd_profiles` row, not fixed pixels, so non-1080p sources work. A calibration pass
   samples frames, locates the bright text band along the bottom edge, and stores the region.
2. **Sampling.** Decode at 1 fps via VAAPI (`-vf fps=1`), cropping to the OSD strip in the
   same FFmpeg graph so only a thin strip is ever transferred back to system memory.
3. **Binarisation and text-band isolation.** The text is near-white on dark, so a single
   global threshold suffices — measured on real strips the background sits at 1–28 and the
   strokes at 255. Column-local thresholding was implemented, measured and removed; see
   below. What the strip *does* need is removal of scene ink, because the crop is a fixed
   rectangle that also catches lit bonnet edges and lane markings.
4. **Glyph classification.** The font is fixed across the corpus, so each glyph box is matched
   against a bundled template set (`0-9`, `-`, `:`, `.`, `E`, `N`, `k`, `m`, `/`, `h`, space).
   Normalised cross-correlation gives a per-glyph score; the minimum across the field becomes
   that field's confidence.
5. **Parsing and validation.** Timestamp, position and speed are matched **independently**,
   so damage to one never costs the others. A point is rejected unless |lat| ≤ 90,
   |lon| ≤ 180 and speed ≤ 400 km/h; `0.0000/0.0000` sets `fix = false`; the timestamp must
   parse and stay monotonic within the segment, but a failed clock no longer discards a
   good fix.

### 4.1 Why the fix rate was intermittent

Worth recording, because the symptom pointed away from the cause. Coordinates were plainly
legible in frames the app reported as having no GPS fix, and the same recording would
"click between seeing it and not".

The cause was **horizontal scene ink**, not thresholding or classification. Glyph
segmentation splits on empty columns, so a bright streak crossing the strip — a sunlit
bonnet edge, a lane marking — fills every gap between characters and welds a whole field
into one run wider than the width filter allows. That run is then dropped in its entirety
and silently: the decode still succeeds, just with a field missing, which is why
`E:138.7158 N: 64 km/h` looked like a plausible reading rather than a bug.

`isolate_text_band` removes it in two passes, because streaks come in two shapes. One
detached from the text splits the strip into row bands, and the text band is identified by
counting glyph-shaped column runs (a streak is much ink in few runs; text is the reverse).
One *touching* the text leaves a single band, so the cleanly segmented glyphs vote on the
line's top and bottom edge and the mask is clipped to it.

Measured on real footage, 680 frames across 17 recordings and both cameras:

| | before | after |
| --- | --- | --- |
| GPS fix | 69% | 96%¹ |
| Speed | 86% | 98% |
| Implausible coordinate jumps | 5 | 0 |

¹ Excluding frames where the camera itself reported `E:00.0000 N:00.0000`. Those are real
satellite outages — GPS acquisition at the start of a drive, or an indoor park — and
reporting them as "no fix" is correct, not a miss.

**Two things measured and rejected.** A column-local threshold using a *mean* background
made results markedly worse (the mean is dragged up by the stroke it passes through); one
using a low percentile was sound and changed nothing at all, because a bright background
was never the problem. Neither is in the code. Lowering the brightness floor to admit the
anti-aliased halo around each stroke thickened glyphs enough to turn `5` into `6`, taking
the fix rate to zero while the decodes still looked superficially plausible.

**Coordinates are never invented.** Three distinct paths were found that silently produced
a *wrong* position, which is far worse than none — a missing fix costs nothing when the
overlay repeats every second, whereas a wrong one drags journey bounds across the planet.
A `-` misread as `.` flipped an Adelaide drive into the northern hemisphere; the timestamp
pattern ate the leading `1` of `138.7067` and returned a longitude off Nigeria; the tolerant
fallback spliced a seconds field onto a fraction. Each is now refused explicitly rather than
resolved by guesswork, and each has a named regression test.
6. **Derivation.** Heading is computed from consecutive fixes; distance by haversine with a
   jitter gate (points closer than the 11 m quantisation are not accumulated, otherwise
   stationary noise inflates distance).

A general OCR model (PaddleOCR via OpenVINO) is retained behind a setting as a fallback for
firmware whose font the templates do not cover, and the per-field confidence is always stored
so the UI can show uncertainty rather than asserting correctness.

---

## 5. Processing pipeline

Stages are independent and separately re-runnable, so "reprocess telemetry only" never
re-runs detection.

```
Settling → Discovered → Queued → Processing ─ Inspect ─ Telemetry ─ Detect ─ Plates ─ Summarise → Completed
    │                                 │
    │                                 ├→ Failed   (per-stage, attempt counter, retried)
    └→ Invalid                        └→ Invalid  (permanent: 0 bytes, no video stream)
```

Both ends of that diagram are load-bearing and neither used to exist. `Settling` is a file
the camera may still be writing; `Invalid` is a file no number of attempts can help. See
§5.3 — collapsing either into `Failed` is what let three zero-byte segments consume four
processing attempts on every bulk requeue, forever.

| Stage | Work | Cost control |
| --- | --- | --- |
| 1 · Inspect | ffprobe; codec, duration, resolution, real fps, audio, PTS-wrap clamp; thumbnail | Header read, and the thumbnail only if there is not already one |
| 2 · Telemetry | OSD OCR at 1 fps | 1 fps is the OSD's own rate — no gain above it |
| 3 · Detection | Vehicle/person detection on sampled frames + ByteTrack association | Default 4 fps sampling; tracks, not per-frame rows |
| 4 · Plates | Plate detection inside tracked vehicle boxes only, OCR on the best few crops per track | Never OCR every frame; per-track voting |
| 5 · Summarise | Roll up counts, distance, speeds; attach journey | Single pass |

**Models come from upstream, and so does their inference code.** Weights are fetched on
first use into `/data/models` and cached, so the image stays small and the container works
offline once warmed. They are published by two MIT-licensed projects rather than hosted
here — [open-image-models](https://github.com/ankandrew/open-image-models) for the RF-DETR
COCO detectors and the YOLOv9 plate localiser, and
[fast-plate-ocr](https://github.com/ankandrew/fast-plate-ocr) for plate text.

Their inference code is used too, which is the more consequential half of that decision.
This repository previously hand-rolled letterboxing, anchor decoding, NMS and CTC decoding
against release assets on its own GitHub that were never published — so every model 404'd,
detection and plate reading were silently unavailable on every run, and none of that code
had executed against real weights even once. Each of those steps is a place to be quietly
wrong rather than loudly broken: a transposed output layout yields plausible boxes in the
wrong places, and an alphabet one character out of step yields confidently misspelt plates.
Two concrete mismatches were found and avoided while wiring this up — the COCO detectors
label on the 91-entry COCO ID space rather than the contiguous 80-name list, and the OCR
model's config declares RGB input where frames here are BGR. Both would have degraded
results without raising anything.

Verified on real footage before merging: 5–10 vehicles per urban frame at 0.85–0.94
confidence with correct car/truck labels, and plate `S945CDX` read at 1.00 confidence
across five separate frames of one drive, normalising to a South Australian pattern. OCR
confidence tracks crop size honestly — a 128×53 crop reads at 1.00, a 35×17 crop at 0.15 —
so `plates.min_store_confidence` (0.3) discards the unreadable rather than storing guesses.

Inference runs directly on OpenVINO 2026.3. The upstream model packages still own image
preprocessing and output decoding; `app/ai/openvino_session.py` provides the small
`InferenceSession` surface they require while compiling the ONNX graphs with OpenVINO.
Plain ONNX Runtime remains a CPU-only emergency fallback. Its OpenVINO provider wheel is
deliberately excluded because its latest Linux build bundles OpenVINO 2025.4.1, and loading
that native runtime beside 2026.3 causes an ABI collision. Compiled blobs live under the
data volume, and shared models use OpenVINO's throughput hint plus one infer request per
worker thread so concurrent recordings can use multiple GPU streams without duplicating
weights.

**Object tracking.** ByteTrack over sampled frames produces one `tracked_objects` row per
physical vehicle with first/last seen, rather than hundreds of independent detections.
Individual `detections` rows are retained sparsely for the timeline.

**Plate association.** A plate is read from crops belonging to a tracked vehicle. Multiple
reads across the track vote on the final text, weighted by OCR confidence and crop size;
the winning text becomes a `plate_observations` row linked to both the track and the
`plates` record. This is what turns "20 seconds of the same car" into one encounter.

**Australian plate normalisation.** Raw OCR is always preserved. Normalisation upper-cases,
strips separators, and resolves position-dependent confusions (`O`↔`0`, `I`↔`1`, `S`↔`5`,
`B`↔`8`) against known AU state patterns. A normalised value is only accepted when it matches
a plausible pattern; otherwise the raw value is kept and flagged low-confidence. The UI always
shows the confidence alongside the text.

---

## 5.1 The heat map

`GET /api/map/heatmap` answers "where do I actually drive", and the two decisions that make
it work are both consequences of the sample rate rather than of cartography.

**Aggregation happens in SQL, not the browser.** One second of footage is one telemetry
point, so a few days of driving is already tens of thousands of coordinates and a year is
millions. Returning raw fixes is a payload that grows without bound, and the browser ends
up aggregating anyway. Rounding coordinates onto a grid and counting bounds the response by
the *area* covered instead of the *time* spent covering it — a commute driven two hundred
times is the same number of cells as one commute, only hotter. Precision is capped at four
decimal places because that is what the overlay prints; offering more would invent
resolution the source does not have.

**Stationary time has to be filtered, and the remaining spread compressed.** The camera
samples at 1 Hz whether or not the car is moving, so an hour parked is 3,600 fixes in one
cell. Measured on a synthetic 40-trip commute with an hour parked at each end: the busiest
cell held **101×** the weight of the busiest road cell, which on a map is a single
incandescent dot at home and nothing else. A `min_speed_kmh` filter removes it — and even
then the faintest road cell sits at 0.028 of the brightest on a linear ramp, which is
invisible, so intensities are compressed logarithmically to 0.19. Both numbers came from
measurement; neither problem was visible from the code.

Null speeds are kept by the filter rather than dropped: an unreadable speed field says
nothing about whether the car was moving, and discarding those fixes would punch holes in
real routes. Rows are also required to have non-null coordinates and not merely `has_fix` —
a flagged row with null coordinates would round to a cell at Null Island.

Blur radius is derived from how large a grid cell currently is on screen rather than fixed,
because cells are a fixed size on the *ground*: a constant radius collapses to specks when
zoomed out and beads into disconnected blobs when zoomed in.

### Tracing the routes as well as the heat

The heat layer blurs by design, so at a readable zoom a road and the car park beside it are
one warm smudge: it answers *how often*, not *which road*. `GET /api/map/routes` returns the
paths themselves as polylines, drawn thin and semi-transparent so overlapping drives
accumulate — a commute taken two hundred times reads brighter than a road taken once, which
gives the overlay its own sense of frequency without a second colour scale.

One property matters more than the rest: **a drive is several lines, not one.** The camera
loses its lock in tunnels, rejected readings leave holes, and a journey stitches together
clips minutes apart. Joining across any of those draws a road that does not exist. Measured
on a synthetic drive with a 60-second dropout, joining blindly produced a 1,055 m chord
through whatever the vehicle actually went around; splitting first caps the longest drawn
step at 17 m. On one journey that is a visible glitch, and on an overlay of every drive ever
taken it is a spray of false chords indistinguishable from real roads.

Splitting must also come *before* simplification, or Douglas-Peucker treats the two ends of
a gap as collinear with everything between them and collapses the dropout back into a single
straight line. The simplification itself is iterative rather than recursive: the textbook
formulation recurses once per split, and a nearly straight motorway trace — exactly the case
that produces the most points — approaches one stack frame per point.

At this library's scale (45 journeys, both cameras) the default 15 m tolerance sends 26 KB.

### Seeing what the overlay reader sees

Every telemetry defect in this project was found by pulling a frame, cropping the strip and
looking at the thresholded mask. That loop lived in throwaway scripts pointed at a mounted
share. `GET /api/recordings/{id}/osd-debug.png` puts it in the application: the frame with
the region outlined, the cropped strip, and the mask the classifier actually reads.

Three panels because the failures live between them. A clean strip and a solid-black mask
means the threshold is wrong; a clean mask that decodes to nonsense means the templates are;
a strip showing road instead of overlay means the region is. The final text alone cannot
distinguish those, which is why each of those bugs previously required a frame dump.

It loads the *same* active region and learned templates the telemetry stage uses, so a
discrepancy is real rather than an artefact of the tool — a debug view that quietly does
something slightly different sends you looking in the wrong place.

### The write lock is the scarcest thing in the process

SQLite allows one writer. In WAL mode readers never block, so the only real contention is
how long any single transaction holds the write lock — and `busy_timeout` does not save you,
because a transaction that already holds the lock is not going to release it just because
someone else is waiting.

"database is locked" has surfaced three times here, each from a different route to the same
mistake:

1. **Reading before writing.** `claim_next` selected a candidate then updated it. A lock
   *upgrade* returns `SQLITE_BUSY` immediately rather than waiting, so it failed instantly
   under load. Fixed by making the claim a single `UPDATE ... WHERE id IN (SELECT ...)`.
2. **Holding it across the work.** Marking a recording `PROCESSING` flushed, and the worker
   kept that transaction open across every decode. Fixed by committing between stages.
3. **Autoflush holding it across the work.** Setting a stage to `RUNNING` leaves the ORM
   object dirty; the stage's *first query* then autoflushes it, taking the lock before the
   long work rather than after. With a model compiling for the iGPU that is two minutes,
   during which the other worker cannot claim a job, the scheduler cannot reclaim stale
   ones, and the log sink drops entries. Fixed by committing the marker immediately.

The third is the subtle one and it is worth stating as a rule: **an uncommitted ORM change
is a write lock waiting to be taken.** It is not taken where the assignment is written, but
at the next query anywhere in the call stack — which may be inside a function that has no
idea a transaction is open. Anything that assigns to a mapped object and then does slow work
should commit first.

The corollary for tests: a fake stage that does no database work cannot reproduce this. The
concurrency test only started catching it once its stage issued a query, which is what every
real stage does.

### What was actually holding the lock

The three routes above were all inside a worker. A fourth was not, and it is the one that
produced `database is locked` on `DELETE FROM tracked_objects` and `DELETE FROM
telemetry_points` — each the *first write* of its stage, i.e. exactly where a transaction
opens.

`JourneyBuilder.refresh` took the write lock with its first `UPDATE` and then did all of
its reading: every telemetry point of the journey, three separate times. `rebuild` called
it for all forty-five journeys inside one transaction. That is minutes of held lock, and
`busy_timeout` is thirty seconds, so both workers' next write failed outright.

Two changes, and they are not alternatives:

* **Read everything, decide everything, then write.** `refresh` now makes one pass over
  the journey's telemetry and derives the outliers, bounds, distance and endpoints from it
  in memory — fewer queries than the four passes it replaces, and a write phase measured in
  statements rather than seconds. `rebuild` commits per journey.
* **Contention is not a failure of the recording.** The stage write phases are wrapped in
  `write_with_retry`, which re-runs delete-then-insert from data already in memory, and a
  lock that survives that is reported to the queue as `transient` — requeued with the
  attempt **refunded**, bounded by `MAX_CONTENTION_RETRIES` so a permanently locked
  database still surfaces. Two recordings had been marked permanently failed at 4/4 without
  ever being given a reason that had anything to do with the footage.

`commit_with_retry` deliberately does *not* roll back between attempts. Rolling back and
committing again is the obvious shape and it is catastrophic here: it discards the work,
commits an empty transaction, and reports success — §5.2 again, one level down.

### One recording, one worker

`enqueue(force=True)` — what every bulk reprocess uses — created a second job for a
recording a worker was already running, and the claim had no opinion about that. Both runs
then deleted and rewrote the same rows, and whichever delete landed between the other's
delete and its inserts took those inserts with it.

The claim now carries a correlated `NOT EXISTS` against `RUNNING` jobs for the same
recording, in the same statement for the same reason the claim itself is one statement.
`force` supersedes queued jobs rather than stacking them, so three presses of "reprocess
everything" no longer decode the library three times.

The other half of the pair had no owner at all: `reclaim_stale` returned the *job* but left
the *recording* stamped `processing`, and `queue_unprocessed` does not look at that state.
A hard restart mid-run stranded that recording permanently. Both halves are now healed, and
a stage left `RUNNING` counts as pending — treating it as done let a reclaimed recording
finish as `completed` with the stage it died in silently skipped.

---

## 5.2 Failures that reported success

A theme worth naming, because it has now happened often enough to be the thing this
codebase is actually prone to. Almost every serious defect found here has been a **write or
a stage that did nothing and said it worked** — not a crash, not an exception, but a green
log line over an empty result. They are invisible to smoke tests, which check that an
operation returns, and to type checkers, which check that it could.

The ones found so far, each caught only by comparing what the code claimed against what the
database or the deployment actually held:

| What reported success | What actually happened |
| --- | --- |
| `get_session` on every API write | Never committed; the reprocess the user asked for never ran |
| Migration 0003's first version | `sa.JSON` applies its bind processor to the assignment but not the comparison, so the `WHERE` matched no rows |
| `JourneyBuilder.rebuild` | Deleted every journey; SQLite reissued the freed row id, so the reassignment was a no-op to the unit of work and no `UPDATE` was emitted |
| The PTS-wrap clamp | Tested for a duration *above* the wrap period, which a wrap never produces |
| The detection stage | Weights 404'd on every attempt; recordings still finished as `completed` with zero objects |
| Logs/Plates pagination | Set the page then deleted it on the next line |
| Job diagnostics | Only the heartbeat wrote them, and it is cancelled before the final values exist |

The lesson that generalises: **a test that asserts an operation returned proves nothing.**
Every fix above is now pinned by a test that was first watched to fail with the fix
reverted — and twice that check found the *test* was wrong rather than the code, which is
the same class of error one level up.

The design rule that follows: where a write must happen, express it as a statement whose
effect does not depend on what the ORM believes, and assert on the resulting state rather
than on the call. Where a count is shown to a user, carry enough context to say whether the
thing producing it was working — see `FeatureStatus` in `/api/status` and `emptyStateFor`
in the recording viewer, both of which exist so a zero cannot masquerade as a measurement.

---

## 5.3 Discovered, settling, invalid

A file being written and a file with nothing in it look identical from one `stat()` at the
wrong moment, and the pipeline treated both as "try again later". For the three zero-byte
segments in this corpus that meant a permanent place in the retry population: they failed
on attempt 1 of 4 with `is empty (0 bytes)`, and every bulk requeue handed them a fresh
four.

`RecordingState` now distinguishes them, and the distinction is the whole point:

| | Means | Consequence |
| --- | --- | --- |
| `settling` | Still moving, or not yet seen twice | Asked about again next scan; never queued |
| `failed` | An attempt did not work | Stays in the retry population |
| `invalid` | The source cannot be processed at all | Leaves the queue; excluded from bulk requeues |

The test for readiness is *stability*, not delay: the mtime must be older than the settle
window **and** the size and mtime must be the ones the previous scan recorded. Nothing
sleeps — the second observation arrives with the next scan, whenever that is. Only once a
file has proved it is not moving is its size read as a verdict, so a zero-byte file written
a second ago is a copy that has just started and a zero-byte file unchanged since the last
scan is a zero-byte file.

A first sighting is not automatically held back, because an initial import is hundreds of
files whose mtimes are days old and making each wait a scan interval would be a delay with
nothing behind it. A future mtime — clock skew against the share — is deliberately not
treated as "recently written", which would stall such a share forever; it falls through to
the cross-scan comparison, which needs no clocks to agree.

Finding this also turned up its mirror image. A file first seen mid-write is stored with no
fingerprint, which is the marker for "never read" — but if it was then never touched again,
its size and mtime matched on every later scan and the cheap-path early return fired before
anything noticed the fingerprint was still missing. That recording was never processed at
all, with no error and nothing in the UI to suggest anything was wrong.

---

## 5.4 Where a sighting happened

Every position on a track or a plate observation is chosen from the recording's own
telemetry, and the choosing was one line:

```sql
ORDER BY abs(t_offset_s - ?) LIMIT 1
```

which has no tolerance, no interpolation and no validation. Three consequences, all of
which produce a confident coordinate rather than an error:

* A recording whose lock died ten seconds in still had a "nearest" fix for a detection two
  minutes later — the last known position, up to a couple of kilometres away.
* The point *between* two fixes a second apart is a better answer than either of them; at
  60 km/h picking one is up to 8 m of avoidable error on every sighting.
* `has_fix` was the only filter, so a row carrying the flag with a corrupt coordinate was
  as eligible as any other.

`app/osd/locate.py` answers the same question with a fourth possible answer: **no idea**.
It brackets the moment, interpolates when the two fixes either side are close enough
together and consistent with each other, falls back to a single fix within
`telemetry.max_fix_age_s`, and otherwise returns nothing. A sighting with no location is
honest and the UI already has a place for it; a sighting with someone else's location is a
wrong answer that looks exactly like a right one.

Two related mistakes went with it. Plate observations copied the track's coordinate, which
belongs to the frame the vehicle was *first* seen in rather than the frame its plate was
read in — up to twenty seconds apart on a followed vehicle. And `first_seen_at` and
`last_seen_at` were stamped with the same telemetry timestamp, so every track in the
library claimed to have been last seen at the instant it was first seen.

### Coordinates are refused, not repaired — and refusal has to reach every copy

`app/osd/validate.py` is now the single definition of what may be stored as a position:
finite, in range, and not the camera's `00.0000` no-fix marker. Four layers had grown their
own version of that question and none of them checked for NaN, which passes every magnitude
comparison ever written.

The sequential jump check also has a blind spot worth naming. The walk trusts its starting
point absolutely: the first fix becomes the anchor with nothing to check it against, so
when *that* one is the misread it is kept and every good fix after it is rejected for
disagreeing with it. A median cannot be dragged by the outlier it is looking for, so the
whole recording now gets a say before the walk starts, and a walk that condemns a majority
is treated as evidence of a bad anchor rather than a field of outliers.

### A parked session has nothing to disagree with

Every check above is *relative*: does this fix disagree with its recording, or with the rest
of its drive? That question has no answer for a car parked in a garage. The camera reports
no lock for almost every sample, the handful it misreads are the same corrupted placeholder
to the last printed digit, and the median centre lands on the corruption — so the journey is
reported as a clean drive to wherever the corruption points.

Two live journeys were exactly this: 24 fixes at `0.1, 0.0` and one at `0.0, 161.0`, each
reported as a journey whose *entire bounds* were the stray point. Four independent things
had to be wrong at once:

* **The no-fix epsilon was exclusive.** `00.0000` misread one digit at a time lands on
  `00.1000` more often than anything else, and `abs(0.1) < 0.1` is False. The bound is now
  inclusive; `00.0900` from the same footage was always caught, which is what gave it away.
* **A destroyed clock let the date be read as a coordinate.** Coordinates are searched for
  from the end of the timestamp precisely so a mangled date cannot become a position, but
  that guard only existed when the timestamp matched. `2026-08-16 1 .0000 N:00.0000` has no
  readable time, so the search restarted at zero and spliced the day digits onto the wreck
  of the `E:` field: `161.0000`. The date is now stepped over on its own.
* **The candidate merge preferred a position over "no fix".** Two frames per overlay-second
  read the same printed characters, so they cannot both be right; taking the positioned one
  unconditionally let one corrupt read beat a clean no-fix read every time. Ties now go to
  no-fix — overruling the camera's own report is the claim that needs the evidence.
* **Nothing judged the session as a whole.** A journey whose camera reported no lock at
  least ten times as often as it reported a position, and whose every position is the same
  coordinate to the last digit, is now read as a parked session rather than a place. A real
  receiver wanders further than the overlay's 11 m quantisation within a minute; a constant
  repeated exactly is one misread rendered the same way every time.

The ratio is what keeps this off real data. Idling at a level crossing also produces
identical coordinates — but it does not produce a camera reporting no lock, and discarding
a real position is the worse error.

### Tracing `-34.8040, -8.6845` on the live library

Worth writing down in full, because the obvious explanation was wrong and the measurement
said so.

That coordinate sits on **110 tracked objects and one plate observation** in
`20260803130528_camera_0.ts` (recording 268) — every sighting in the clip, at one identical
position. The recording stores **zero** telemetry fixes while its rollup still claims
`has_gps` with `gps_point_count = 1`.

Re-decoding all 121 frames through the deployed reader shows why the value is *shaped* the
way it is: this clip's overlay loses the leading digits of its longitude constantly.
`138.6845` comes back as `38.6845`, `28.6845` and — at t=2 — exactly `-8.6845`, against 92
frames that read it correctly. So the misread is real and reproducible.

But running both the deployed and the fixed derivation over those 121 real frames keeps the
same 92 good fixes and rejects the same bad ones. **The anchor bug did not produce this
value**; an older parser did, at the time this recording was processed, and the parser fixes
recorded in `app/osd/parser.py` have since closed that route. Claiming otherwise would have
been a tidy story that the evidence contradicts.

What is still live, and what actually kept the wrong coordinate on the map, is the pair
below:

1. **No tolerance.** Whatever single fix survived was stamped on all 110 tracks by
   `ORDER BY abs(t_offset_s - ?) LIMIT 1`, from t=0 to t=120, because nothing asked how far
   away in time it was.
2. **No cleanup of the copies.** The journey builder later recognised that fix as impossible
   and cleared it from `telemetry_points` — leaving the recording with zero fixes — but
   `tracked_objects` and `plate_observations` keep their own coordinate and nothing has ever
   gone back for them.

Measured across the whole library: **2,575 of 84,329 sightings** sit at a coordinate that
is physically impossible for this footage, and in **44 recordings** every sighting shares a
single position. Both are now judged by the same journey centre and radius as the telemetry,
and migration 0005 applies that to the rows already stored.

With the fix, S192DKX in recording 268 is placed by interpolation between the fixes 0.75 s
either side of the frame its plate was read in, and moves from `-34.8040, -8.6845` to
`-34.8040, 138.6845` — 11,565 km, onto the road the rest of that journey is on.

Nothing in any of this knows where this dashcam drives. The reference is always the drive's
own centre and its own elapsed time, so the same rules hold anywhere on Earth.

---

## 5.5 Reprocessing has to clean up after itself

Every stage deletes its own rows before writing, so duplication was never the risk. What
was left behind was the other half — rows derived from the rows just deleted:

* `plate_observations` point at `tracked_objects` with `ON DELETE SET NULL`, so reprocessing
  detection alone left every observation pointing at nothing while still carrying the
  coordinates and offsets of tracks that no longer existed. Stage selections are now closed
  over their dependants: re-running telemetry re-runs detection and plates, and re-running
  detection re-runs plates.
* Plate rollups were refreshed only for plates the recording *still* sees, so a plate whose
  reading a reprocess corrected kept a count including the observation just deleted, and a
  plate left with no observations anywhere stayed in the catalogue forever. The plates about
  to lose an observation are collected before the delete, and one with nothing left pointing
  at it is removed — unless a user has flagged it or written a note, which is not ours to
  discard.

---

## 5.6 The rear channel is mirrored, and the picture is not the coordinate space

The orientation vote already existed and is what lets the recogniser read a mirrored rear
channel at all. What it was never applied to was the *picture*: OCR read a flipped copy of
the crop and the crop was saved exactly as the camera produced it, so the plate page showed
a backwards plate above correctly-read text — which reads as the text being wrong.

The flip stops at the saved crop. Flipping the frame instead would put every stored bounding
box on the wrong side of a video the player still shows unmirrored, so detection, tracking
and every `bbox` stay in the source's coordinate space and only the standalone preview image
is turned round. The observation records that it was, so the two can never be mistaken for
the same space.

The vote also had to be settled earlier. It was decided after eight sampled readings, and
the crops are written after the loop — so a recording with fewer readable plates than that
reached the write phase with the question still open and saved every preview unflipped. On a
short rear clip that is every preview in the recording.

---

## 5.7 What only the running system could show

Four defects below were invisible from the source and obvious from a live server. They are
recorded together because they turned out to be one chain: a misread coordinate splits a
drive, the split makes the scheduler rebuild for ever, the rebuilds hold the write lock, and
the lock is what was failing recordings.

**A frozen coordinate decides where drives split.** `recordings.start_lat` is written once,
by the telemetry stage, from that recording's first fix — and nothing ever revisits it.
Cleaning `telemetry_points` updates the *journey's* rollups and never the recording's, so a
rear segment whose latitude lost its minus sign keeps a start position 7,700 km from the
front segment recorded at the same instant. Clustering reads exactly that field, so the pair
is never put in the same journey. This is the third copy of a coordinate that nothing
revisited, after `tracked_objects` and `plate_observations`, and it is the expensive one.
`rebuild` now re-derives every start position from the surviving telemetry *before* it
clusters: derive, then decide.

**The staleness test could never be satisfied.** `needs_recluster` asked whether two
automatic journeys sat closer together in time than the gap. `_cluster` splits on time
**and** GPS continuity — so a pair the rebuild had deliberately separated over a 7,700 km
jump looked, to a time-only test, exactly like a pair that ought to be joined. The old
docstring claimed "this cannot loop". On the live library it had looped 12,626 times: every
scan deleted and recreated all 85 journeys, and the write lock went with it. The check now
runs the clustering and compares the grouping against what is stored, which cannot disagree
with the rebuild it triggers.

That loop is also the answer to a question §5.1 left open — what was holding the lock long
enough to blow past a thirty-second `busy_timeout`. It was this, on a permanent cycle.

**A rebuild deletes journeys out from under the workers.** Every stage stamped
`journey_id` on the rows it inserted, from `recording.journey_id` — a value read into memory
when the job started. The scheduler deletes and recreates every automatic journey while
workers are mid-recording, so that number routinely names a journey that no longer exists
and the insert fails with `FOREIGN KEY constraint failed`. No stage writes it now:
`_attach` owns it, `stage_summarise` backfills it, and a derived value with two writers
always has one working from a stale copy.

**And the failure handler failed.** A stage that dies inside a flush leaves the session
refusing everything until it is rolled back, so `recording.state = FAILED` was discarded —
SQLAlchemy says so, in a warning nobody was reading: *"Session's state has been changed on a
non-active transaction - this state will be discarded"* — and the commit after it raised
`PendingRollbackError`, which escaped `run_stages` entirely. The job was never marked failed
at all: it stayed RUNNING with no worker until the heartbeat reclaimed it, then decoded the
whole recording again to fail the same way. Seven of those, each logged as a bare "worker
loop error" naming neither the recording nor the job.

The repair has a trap of its own worth recording, because the first version of it fell in:
rolling back expires every mapped object, so reading `recording.id` afterwards fires a lazy
load, and under the async engine that raises `MissingGreenlet`. The id is captured before
the rollback for that reason.

### Stopping a loop can freeze the thing it was churning

Deploying the above and probing the server two minutes later found the fix incomplete, in a
way that is worth recording because it is a general hazard.

`needs_recluster` now compares the stored grouping against what clustering would produce,
so once they agree it stops asking — which is the point. But a library grouped *consistently
with wrong start positions* satisfies that check: 85 journeys, 31 holding a single
recording, and clustering agreeing with every one of them. The correction lived inside
`rebuild`, and nothing was left to trigger a rebuild. The grouping was now stable, and
stably wrong.

The fragmentation then hides its own cause. Rejecting an impossible coordinate is
journey-scoped, because a whole clip can be sign-flipped and agree with itself — so a
recording marooned in a journey of one has no reference frame at all. That is precisely why
migration 0005 cleared nothing: recording 268's journey held one recording with no fixes, so
the pass had nothing to judge against and skipped it. The 110 sightings it was written to
clean were invisible to it.

Correcting the start positions is therefore a *self-healing repair* on the scan, alongside
`repair_stale` and `repair_durations`, not a step inside the rebuild. Ordered before the
staleness check, it breaks the deadlock in one pass: the damaged clips lose their bogus
start coordinate, cluster back with their neighbours on time alone, and the ordinary journey
refresh then has the neighbours' telemetry to recognise their sightings as impossible.

The general shape, which this codebase keeps rediscovering: **a correction that only runs as
a side effect of some other trigger is not a correction.** It has to be something the system
asks itself unconditionally, or the one state that stops the trigger firing is the one state
it was needed for.

## 5.8 One drive, one journey, one path

The Journeys tab showed one Aug 3 drive as **five** journeys — 12:45→13:05 with nineteen
recordings, then three holding a single recording each, then a pair. It was not
reprocessing and it was not the front/rear split, which were the two obvious suspects.

Two clips in the middle of that drive had exactly **one** surviving GPS fix each, and that
fix was a misread. `recordings.start_lat` therefore held a coordinate thousands of
kilometres away, and the GPS-continuity check cut the drive on both sides of each of them.
One damaged clip makes two cuts, which is precisely the "three journeys for one drive".

The deeper mistake was what the continuity check believed. It exists for the case where the
car genuinely moved without recording — parked in a garage, driven onto a ferry, GPS
reacquired a suburb away — and all of those are journeys the vehicle could have made. A
latitude that has lost its minus sign is 7,700 km away in under two minutes, which is not a
journey, it is a bad digit. **A jump that is physically impossible for the elapsed time is
evidence about the coordinate, not about the drive**, and cutting a drive on it is the worse
of the two available mistakes.

### The line, the pins and the distance were three different answers

Each consumer of a journey's telemetry coped with the two cameras differently, and no two
agreed:

| | What it used | What went wrong |
| --- | --- | --- |
| Distance | every stored fix | both cameras measured, median **2.05×** the distance the drive can have covered |
| Drawn route | front camera only, ordered by clip | a stretch the front could not read vanished from the map |
| Start/end markers | first and last row by `captured_at` | 10 of 59 journeys had a marker >500 m from the end of their own route, worst 4.7 km |

So a journey reported 37.7 km for a 21-minute drive averaging 49 km/h — which is 17.7 km of
road — drew a line that stopped short of the pin at the end of it, and put the pin somewhere
neither the line nor the distance agreed with.

`app/journeys/track.py` is the single answer all three now share: **one position per second
of the drive, in time order, from whichever camera saw it.** The front wins a tie because it
is the more useful picture; the rear fills any second the front could not read. It is the
same idea the heat map already relied on — counting distinct seconds rather than rows —
applied to everything else that describes a journey. Distance on that drive comes to 11.8 km.

Ordering had two wrong answers before this one. By clip puts a whole rear segment after the
front segment covering the same minutes, so the walk runs to the end of the road and jumps
back to the start of it. By `captured_at` fails differently, and worse.

### A clock is read glyph by glyph too

`captured_at` comes off the same overlay strip as everything else, so one misread digit
moves a sample a day out of its own recording. A single fix in this library reads
`2026-08-08 05:49:20` inside a clip recorded on `2026-08-05` — one wrong day digit — and
because it then sorted after every other point in its journey it became that journey's end
marker, 3 km from where the drive actually finished.

Nothing had ever compared a sample's clock against the clip it came from. The recording's
own start plus the sample's offset is the cross-check, because it owes nothing to the
glyphs, and where the two disagree by more than the clock could plausibly drift the offset
wins. Applied both at the source, so new rows are never stored wrong, and in the journey
builder, so the rows already stored heal without a reprocess.

### What the counts actually count

`journeys.vehicle_count` sums `tracked_objects` rows: one per vehicle, per clip it appears
in, per camera that saw it. A car followed across two clips and seen by both cameras is
four. The aggregation is correct — it matches the sum of its recordings exactly — but the
label said "Vehicles", which promises unique vehicles, and 3,575 of them on a twenty-minute
drive reads as an error rather than as 3,575 sightings.

Nothing in this pipeline re-identifies a vehicle between clips; the `vehicles` table has no
writer, which §5 already says. So a unique count is not something the data can answer, and
the honest fix is the label rather than the number: **vehicle sightings**, and **video
files** rather than "recordings" for a count where one minute of driving is two files.

---

### Measured on the live library

| | |
| --- | --- |
| Sightings at a coordinate this footage cannot reach | 2,575 of 84,329 |
| Recordings where every sighting shares one position | 44 |
| Journeys, against what clustering says | 85 vs 44 |
| Journeys holding a single recording | 31 |
| Rebuild/recluster log entries | 12,626 |
| `database is locked` in the worker loop | 128 |
| Jobs spent on three zero-byte files | 9, across three bulk requeues |

---

## 5.9 "Reprocess all footage" means start again

The action used to **add**: every previously-analysed recording was enqueued on top of
whatever was already in the queue. `enqueue(force=True)` had stopped it stacking duplicates,
but everything else survived a press of it — yesterday's failures stayed failed, a job the
pool was halfway through stayed running, the counters above the queue carried the old run's
arithmetic into the new one, and the order the library came out in was inherited from
whatever happened to be queued before.

It now **replaces**. `app/workers/reset.py` empties the queue, stops the runs in flight,
opens a new run, and derives the work from the recordings that exist rather than from the
rows that were in the table:

```
press → clear queue → stop in-flight runs → new run epoch → discover footage
                                                                  │
                        ┌─────────────────────────────────────────┘
                        ▼
        THUMBNAIL PASS  every recording with no usable thumbnail, priority 0
                        ▼
        ANALYSIS PASS   the whole library, priority 200, oldest → newest
```

**Thumbnails are a phase, not a second queue.** A thumbnail is a couple of ffmpeg seeks
against minutes for a full analysis, so making every missing one first gives the library
pictures in the time it would take to analyse a handful of clips — which is the difference
between a recording imported this morning being visible immediately and being visible after
the eight hundred older ones in front of it have been re-analysed. A recording holds exactly
one job throughout: its analysis job is created when its thumbnail job *finishes*
(`queue._follow_up_after_thumbnail`), because queueing both up front would put every
recording in the queue twice and double every count on the page. The successor is created in
the same transaction as the outcome that triggers it, and on failure as well as success — a
recording whose thumbnail could not be made still has to be analysed, and the analysis pass
is what probes the file properly and reaches a real verdict about it.

**Missing is asked of the disk.** Media lives on the data volume rather than in the
database, so a restored backup or a pruned volume leaves rows pointing at files that are not
there. The old check looked only at whether `thumbnail_path` was set, which meant those
recordings could never get a picture again; `stage_inspect` now asks the same question the
reset does.

**Four things stop a stopped worker.** Cancelling the job rows is not enough on its own, and
is in fact the more dangerous half: the claim refuses to start a second job for a recording
that is `RUNNING`, so cancelling that row removes the very guard keeping the replacement job
off a recording still being decoded and rewritten. So the reset cancels the rows, *then*
`WorkerPool.abort_active` cancels the tasks and waits for them to stop, and only then is any
replacement work queued. A run that finishes anyway re-reads its row before recording an
outcome and declines when it finds it cancelled.

**Nothing is deleted.** Jobs are retired, not removed: `log_entries` references
`processing_jobs` with `ON DELETE CASCADE` and foreign keys are enforced, so a `DELETE` here
would take the log trail of everything the previous run did with it. The counters read as a
fresh run because they are scoped to the run instead — a durable epoch beside the pause
marker, so a restart does not hand the current run the previous one's failures back. The
pause flag itself is *not* cleared: it is a decision about the machine rather than state
belonging to a run, so a reset says it is paused rather than overriding it.

**Only the full action resets.** "Failed only" and "outdated only" are targeted repairs of a
queue the user wants to keep, and wiping it to service them would discard the waiting work
they were not asking about.

---

## 6. Retention safety

Retention refuses to act unless **every** guard passes:

1. The configured footage directory exists and is a directory.
2. It is a **mount point** distinct from the parent filesystem (guards an unmounted share).
3. It contains at least a configured minimum number of recognised media files.
4. The database's view of the directory is consistent with what is on disk (a share that
   suddenly appears empty is treated as a fault, never as permission to delete).
5. The resolved path of every deletion candidate lies inside the footage root after symlink
   resolution.
6. `/data` is never a deletion target under any circumstance.
7. The mount is writable **and** the user has explicitly enabled deletion.

Failing any guard produces a report and a log entry, never a deletion. This is covered by
dedicated tests, including the "share unmounted / empty / wrong filesystem" cases.

**The scanner needs the same posture, and did not have it.** Retention refuses to delete
files from a directory it cannot vouch for, but the scanner could do equivalent damage from
the other side: marking a recording `file_missing` is a destructive conclusion drawn from an
absence, and an absence is exactly what a broken mount looks like. Because `/dashcam` is a
bind mount, an empty or unmounted host source still presents as an existing directory — the
existence check passes, the walk yields nothing, no exception is raised, and every indexed
recording is stamped missing with a `deleted_at` while the run is recorded as a clean
success.

The second-order effect is worse than the first. `evaluate_safety` counts only rows *not*
flagged missing, and guard 4 passes trivially when the index is empty. So zeroing the index
disarms the one retention check designed to notice a wrong mount, using precisely the event
it exists to detect.

The scanner now declines to reconcile deletions at all when its view of the share is not
worth trusting: any directory it could not read, a walk that found nothing while recordings
are indexed, or a file count that has fallen below half the index. It records why on the
scan run rather than reporting a green scan, because "found nothing" and "could not look"
must not look the same to an operator either.

---

## 7. Data model

```
cameras ──< recordings >── journeys
                │
                ├──< telemetry_points
                ├──< tracked_objects ──< detections
                │          │
                │          └── vehicles
                └──< plate_observations >── plates

processing_jobs, scan_runs, app_settings, log_entries, osd_profiles
auth_credentials, auth_sessions
```

Media files are never stored in the database — only paths under `/data/media`.
Migrations are managed by Alembic.

---

## 8. Hardware acceleration

At startup the app enumerates `/dev/dri/renderD*`, reads the PCI vendor/device IDs to name
the GPU, probes FFmpeg for a working VAAPI decode of each codec, and queries OpenVINO for
available devices. The resolved capability set drives decoder and inference selection and is
surfaced in the UI (GPU name, decoder in use, inference device, realtime factor, current job).
Every acceleration path degrades to CPU independently — a failed VAAPI probe does not disable
GPU inference, and vice versa. No CUDA anywhere.

---

## 9. Optional sign-in

Off by default. The deployment this was built for is a trusted LAN, and a password there
buys nothing at the cost of a login page in front of everything. It exists so that putting
the app on a public hostname is a decision rather than a mistake.

### Why it is not two environment variables any more

It was: `DASHCAM_AUTH_USERNAME` and `DASHCAM_AUTH_PASSWORD`, a middleware, and the browser's
native Basic prompt. Three things were wrong with that, and only the first is obvious.

* **The password was in the compose file, in clear text, and turning it on meant a
  restart.** Everything else a user would reasonably want to change is a UI setting; this
  was the one thing that was not, and it was the one thing people would want to change
  after the fact.
* **There is no way to sign out of a Basic prompt**, and no way to stay signed in either.
  A "remember me" is not expressible in it at all.
* **It was all-or-nothing at the edge of the process**, which meant the login page could
  not exist — there is nothing to render when the challenge is the browser's own dialog.

So: an account in its own table, sessions in another, a cookie, and a page.

### The shape

`app/auth/` is four small modules and a rescue tool. `passwords` hashes with
`hashlib.scrypt` — memory-hard, standard library, no new wheel in an image that already
carries OpenVINO and two model runtimes. `service` owns the account, the sessions and the
caching. `ratelimit` throttles. `gate` is the ASGI middleware.

Pure ASGI, not `BaseHTTPMiddleware`. Starlette's wrapper puts an anyio task and a memory
stream around every response, and the two hot paths here are a recordings grid firing fifty
thumbnail requests and a scrubber firing range requests as fast as it is dragged. Nothing in
the gate needs to see a response, so nothing in the gate should be in its way.

### Three decisions that carry the design

**Lockout is the worst outcome, so the dangerous state is made unreachable rather than
handled.** `security.require_login` cannot be switched on without an account — enforced in
`SettingsService.set_many` beside `_require_containment`, not in the route, because a guard
on one door of several is not a guard. Deleting the account switches the setting off in the
same operation. If the pair somehow comes apart anyway — a hand-edited database, a restored
backup — `sign_in_required()` fails *open* and says so in the log once a minute, and while
in that state it re-reads the account row every thirty seconds, which is what lets
`recover-login` reopen a running container without a restart.

**The check has to be free, and revocation still has to be instant.** A verified token is
cached in-process for a minute, misses are single-flighted so fifty simultaneous thumbnail
requests behind one cold cookie cost one query rather than fifty, unknown tokens are
remembered as unknown, and `last_used_at` is written in its own transaction after the read
scope closes — never as an upgrade of it, because SQLite has one writer and section 5.1 is
about what happens when the API queues behind it. Correctness does not depend on any of
those TTLs: every entry carries an epoch, and anything that invalidates a session bumps it,
retiring the whole cache in a single assignment.

**Being expensive on purpose makes it a weapon.** A scrypt derivation is 32 MiB and an
eighth of a second, `asyncio.to_thread` uses the loop's default executor, and that executor
is shared with the detection stage, the ffmpeg probes and the ingest transfers. The
`Authorization: Basic` path is reachable by anyone who knows the hostname, so unthrottled it
is both a password oracle and a way to take the thread pool away from the pipeline. Every
derivation goes through a two-permit semaphore; the Basic path is rate-limited and caches
its rejections as well as its successes.

The rate limiter's own shape follows from the same worry. Only the per-address bucket ever
returns 429, because it is the only one an attacker fills for themselves. A per-username or
global bucket that refused would hand any stranger a way to lock the *owner* out: four bad
guesses a minute against a global ceiling keeps it permanently full, and the owner typing
the correct password is refused along with everyone else. Those two buckets add bounded
delay instead.

### What the tunnel changed

Three things only became wrong once the app had a public hostname, and all three were
already in the code before any of this was added:

* `/media` was served `Cache-Control: public`. A CDN keys on the URL and not on a cookie,
  thumbnails and plate crops sit at sequential zero-padded `.jpg` paths, and Cloudflare
  caches that extension by default — so one signed-in look at the recordings grid would
  have published a week of footage stills to an edge cache that the gate never sees. Now
  `private`, unconditionally, because objects cached while sign-in was off outlive the
  moment it is switched on.
* CORS was `allow_origins=["*"]`, whose own comment asked for it to be tightened before the
  app left a private network. It is gone rather than narrowed: the SPA is same-origin so it
  was never subject to CORS, and Home Assistant and `curl` are not browsers and never were.
  What the wildcard did buy was any page the owner visited being able to read
  `/api/map/routes` out of their browser.
* `SameSite=Lax` is scoped to the registrable domain, so every other host under the same
  apex is same-site to this one and can post here with the cookie attached. The gate checks
  `Origin` on cookie-authenticated writes, and over HTTPS the cookie is named
  `__Host-dashcam_session`, which no sibling host can write.
