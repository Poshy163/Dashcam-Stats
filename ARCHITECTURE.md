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
| Zero-byte files | 3 | Fail fast, mark `Failed`, do not retry forever |
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
Discovered → Inspected → Telemetry → Detected → Plates → Completed
                  ↘ Failed (per-stage, with attempt counter) ↙
```

| Stage | Work | Cost control |
| --- | --- | --- |
| 1 · Inspect | ffprobe; codec, duration, resolution, real fps, audio, PTS-wrap clamp | Header read only |
| 2 · Telemetry | OSD OCR at 1 fps | 1 fps is the OSD's own rate — no gain above it |
| 3 · Detection | Vehicle/person detection on sampled frames + ByteTrack association | Default 4 fps sampling; tracks, not per-frame rows |
| 4 · Plates | Plate detection inside tracked vehicle boxes only, OCR on the best few crops per track | Never OCR every frame; per-track voting |
| 5 · Summarise | Roll up counts, distance, speeds; attach journey; write thumbnails | Single pass |

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

Inference runs on ONNX Runtime. The image ships the plain build, whose only provider is
CPU: `onnxruntime-openvino` installs under the same module name *and* bundles its own copy
of the OpenVINO runtime that hardware probing already loads, and two builds of one native
library in a single process is not a risk worth taking here. `app/ai/runtime.py` picks the
best available provider rather than hard-coding CPU, so installing that package later is
enough to move inference onto the iGPU with no code change. Cost measured on an i9-class
CPU: ~104 ms/frame for RF-DETR nano, so a 60 s clip sampled at 4 fps spends roughly 25 s in
detection.

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
