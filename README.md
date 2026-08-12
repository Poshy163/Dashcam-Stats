# Dashcam Analyser

Self-hosted dashcam footage analysis. Point it at a folder of recordings and it will index
them, recover the GPS/speed telemetry, detect vehicles and read licence plates on your
iGPU, group the recordings into journeys, and give you a searchable web UI with maps.

Runs as a single container. Everything is configured in the browser — there are no
dozen-environment-variable deployments here.

---

## Quick start

1. Mount your dashcam share on the Docker host (e.g. `/mnt/Vault/dashcam`).
2. Save this as `docker-compose.yml`:

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

3. Start it:

```bash
docker compose up -d
```

4. Open `http://SERVER-IP:8098`.

That is the whole setup. The first scan starts automatically; everything else — scan
interval, detection thresholds, retention limits, map tiles — is on the Settings page.

---

## What the mounts mean

| Path | Purpose |
| --- | --- |
| `/dashcam` | Your raw footage. Mounted `:ro` above, which is the recommended default. Drop the `:ro` only to enable [Backup](#backup-pulling-footage-off-the-dashcam) or automatic deletion. |
| `/data` | Database, settings, thumbnails, plate crops and models. **Never** touched by retention. Back this up. |
| `/dev/dri` | The iGPU, used for hardware video decode and AI inference. Optional — without it everything still works on CPU, just slower. |

Deployment variables are `DASHCAM_DATA_DIR`, `DASHCAM_FOOTAGE_DIR`, `DASHCAM_PORT`,
`DASHCAM_LOG_LEVEL`, `TZ`, plus the optional `DASHCAM_AUTH_USERNAME` and
`DASHCAM_AUTH_PASSWORD`. Authentication stays disabled unless both credentials are set.

---

## Hardware acceleration

The container detects what is available at start-up rather than assuming anything, and
degrades gracefully at every level. There is no CUDA dependency anywhere.

* **Decode** — FFmpeg VAAPI on the render node, falling back to software decode.
* **Inference** — OpenVINO on the iGPU, falling back to OpenVINO CPU, then ONNX Runtime.

The **Settings → Advanced** page shows exactly what was detected: GPU name and driver,
which decoder is in use, which inference device is active, and the realtime factor the
current job is achieving.

Access to `/dev/dri` needs the container account to be in the group that owns the render
node, and that group ID differs between hosts. The entrypoint reads it at start-up and
joins the right group automatically, so the plain `devices:` mapping above is enough. If
you override `user:` in compose, you take that on yourself and will need `group_add`.

Verify it is working:

```bash
docker compose exec dashcam vainfo
```

---

## What it extracts

**Telemetry.** Many dashcams — including the one this was built against — write no GPS
metadata at all. Instead the date, coordinates and speed are *burned into the video image*
as an overlay. This app reads that overlay with OCR, which is why GPS works at all on such
footage. Coordinates are recovered at the overlay's own 1 Hz update rate.

Because the source is an overlay rather than a sensor feed, be aware of what that means:
coordinates carry the precision the camera prints (typically ~11 m), and **heading and
distance are derived** from consecutive fixes rather than measured. If your camera does not
report G-force or event markers, the app does not invent them.

If your dashcam *does* embed proper GPS metadata, that path is not implemented yet — see
[ARCHITECTURE.md](ARCHITECTURE.md) for where it would slot in.

**Detections.** Cars, trucks, buses, motorcycles, bicycles and pedestrians, tracked across
frames so that twenty seconds of following the same car is stored as *one* encounter rather
than hundreds of unrelated rows.

**Licence plates.** Detected on tracked vehicles, read with OCR, and voted across several
frames of the same vehicle. Australian plate formats are normalised, and the **raw OCR
output is always kept alongside the normalised value**. Confidence is shown everywhere a
plate appears — the UI never presents an uncertain read as fact:

```
ABC123
OCR confidence: 94%
```

---

## The UI

| Page | What it is for |
| --- | --- |
| **Dashboard** | Totals, disk usage, processing throughput and recent activity |
| **Recordings** | Filterable grid with thumbnails, camera, GPS availability and detection counts |
| **Viewer** | Video player with telemetry, a detection timeline, and click-to-seek |
| **Journeys** | Grouped drives with route, distance, speeds and detection counts |
| **Heatmap** | Every road ever driven, on OpenStreetMap: heat for how often, traced lines for which |
| **Plates** | Searchable plate database — full or partial (`ABC` matches `ABC123`) |
| **Vehicles** | Vehicle sightings independent of plates |
| **Queue** | What is processing now, what is queued, what failed, with retry. **Settings → Scanner → Reprocess all footage** starts again from scratch: the queue is emptied, anything in flight is stopped, every missing thumbnail is generated first, and the full analysis then runs oldest footage to newest |
| **Backup** | Copying recordings off the dashcam itself — progress, backlog and history |
| **Logs** | Application and per-job logs |
| **Settings** | Everything above, editable without a restart |

---

## Backup: pulling footage off the dashcam

Optional, and off until you turn it on in **Settings → Backup / Ingest**.

Dashcam head units are usually Android boxes with no battery, which means they are only
powered — and only on WiFi — while the engine is running. That is a window of a minute or
two on the driveway, and it has to be enough to move a day's recordings. The app watches
for the unit and pulls; nothing is installed on the unit and nothing is scheduled there,
so there is no state in the car to get lost when the engine stops.

Getting the data out fast enough is the whole problem, and it is a transport problem.
Measured against a live unit (Unisoc UIS7861, Android 15, not rooted):

| Path | Rate |
| --- | --- |
| WiFi link (Wi-Fi 5, 80 MHz, −54 dBm) | ~50 MB/s capable |
| TF card sequential read, both lenses recording | 60 MB/s |
| `adb pull`, four parallel streams | 4 MB/s |
| `adb exec-out`, single / `-P8` | 8.3 / 10.0 MB/s |
| **`tar` over a plain TCP socket** | **34.3 MB/s** |

Everything routed through `adbd` funnels into one daemon and caps around 10 MB/s no matter
how many streams it is given. So ADB is used only as a control channel — connect, list the
card, start a listener — and the recordings travel over their own socket. At 34 MB/s a
two-minute window moves about 4 GB, against 1.2 GB through `adbd`.

What that means in practice:

- **Nothing is installed on the head unit.** It already has toybox `tar`, `nc`, `setsid`
  and `timeout`; the tar stream is unpacked in Python on this side.
- **Interrupted transfers are normal.** Files land in `<footage>/.ingest_staging` and only
  a byte-complete file is moved into the footage directory, so the scanner never sees a
  partial. Whatever did not arrive is fetched next time.
- **The delta is size-based.** A file cut short when the car pulled away has the wrong
  size and is simply re-fetched — nothing has to remember that it was partial.
- **The recording being written right now is skipped**, because copying an open segment
  produces a truncated file that looks complete.
- **Nothing is deleted from the card** unless you explicitly enable it, and then only
  after a verified copy has been committed.

Transferred files land in the same directory the scanner already watches, so they are
analysed like anything else with no further configuration.

Home Assistant integration — a REST sensor, a webhook for phone notifications, and
optional MQTT discovery — is documented with copy-paste config in
[`examples/homeassistant/`](examples/homeassistant/README.md).

**First run:** the head unit authorises an ADB *key*, so the first connection puts an
"Allow USB debugging?" prompt on the dashcam's own screen. Accept it with "always allow"
while the car is running. The key is kept on the `/data` volume, so rebuilding the image
does not ask again. Until it is accepted the Backup page says "Not authorised".

To check the connection from the host without moving anything:

```bash
docker exec dashcam-analyser python /app/backend/scripts/ingest_smoke.py \
    --address 192.168.1.122:5555
```

---

## Storage retention

The footage directory grows forever, so retention keeps it under a configurable limit
(**default 150 GB**, set in the UI — never in `docker-compose.yml`).

**By default nothing is deleted.** With the share mounted `:ro` as recommended, retention
runs in report-only mode: it calculates what it *would* remove and shows you the plan.
Turning on actual deletion requires both an explicit setting and a writable mount.

Deletion refuses to run unless every safety check passes:

* the footage directory exists and is a real mount point, not an empty stand-in;
* it contains at least a minimum number of recognised files;
* what is on disk is consistent with what the database has indexed;
* every candidate path still resolves inside the footage root after symlink resolution;
* the plan does not exceed the maximum fraction of the library allowed in one run.

**An unmounted, empty, or unexpected `/dashcam` is treated as a fault, never as permission
to delete.** `/data` is never a deletion target. This behaviour has dedicated tests.

---

## API

A REST API backs the whole UI, documented at `/api/docs` (OpenAPI at `/api/openapi.json`),
so Home Assistant or anything else can query it.

```
GET  /health                      liveness + component health, used by the Docker healthcheck
GET  /api/status                  totals, processing state, hardware
GET  /api/recordings              filter by date, camera, journey, state, GPS, detections
GET  /api/recordings/{id}         details, telemetry and detections
GET  /api/journeys                journeys with route geometry
GET  /api/map/heatmap             grid-aggregated GPS fixes, weighted by time spent
GET  /api/map/coverage            per-journey bounds and distance
GET  /api/map/routes              every drive as polylines, split at signal gaps
GET  /api/recordings/{id}/osd-debug     what the overlay reader saw at one frame
GET  /api/recordings/{id}/osd-debug.png frame + cropped strip + thresholded mask
GET  /api/plates?q=ABC            full or partial plate search
GET  /api/plates/{id}/observations every sighting, with location and confidence
GET  /api/vehicles                vehicle sightings
GET  /api/jobs                    queue state; pause, resume, retry, cancel
GET  /api/ingest/status           live backup progress — also the Home Assistant sensor source
POST /api/ingest/run              pull from the head unit now
POST /api/ingest/cancel           stop the running transfer
GET  /api/ingest/history          past transfers
GET  /api/settings                the settings catalogue and current values
POST /api/scan                    scan now
POST /api/retention/plan          evaluate retention (report-only unless enabled)
```

The default deployment assumes a trusted LAN. Optional HTTP Basic authentication can be
enabled by setting both `DASHCAM_AUTH_USERNAME` and `DASHCAM_AUTH_PASSWORD`; use it behind
TLS because Basic credentials are encoded, not encrypted. Do not expose the app directly
to the internet without a TLS reverse proxy. The `/health` endpoint remains unauthenticated
so Docker can monitor the container.

Settings > Advanced can download a consistent SQLite backup while analysis is running.
A restore upload is integrity-checked and staged, then applied on the next container
restart; the replaced database is retained in `/data/backups` as a pre-restore copy.

---

## Development

```bash
# Backend
cd backend
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
python -m app.main

# Frontend (proxies the API to the backend)
cd frontend
npm install
npm run dev
```

Tests generate their own synthetic fixture clips — the format is reproduced faithfully, but
no real footage is committed, since that would publish someone's location history and other
people's plates:

```bash
python backend/scripts/make_fixtures.py --out tests/fixtures   # needs ffmpeg
PYTHONPATH=backend pytest tests -v
```

If your dashcam's overlay uses a different font, relearn the glyph templates from your own
footage:

```bash
python backend/scripts/learn_osd_templates.py --footage /dashcam
```

[ARCHITECTURE.md](ARCHITECTURE.md) documents what the real footage turned out to contain
and why the system is built the way it is. Read it before making structural changes.

---

## Licence

MIT.
