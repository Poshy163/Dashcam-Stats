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
| `/dashcam` | Your raw footage. Mounted `:ro` above, which is the recommended default. |
| `/data` | Database, settings, thumbnails, plate crops and models. **Never** touched by retention. Back this up. |
| `/dev/dri` | The iGPU, used for hardware video decode and AI inference. Optional — without it everything still works on CPU, just slower. |

The only environment variables that exist are `DASHCAM_DATA_DIR`, `DASHCAM_FOOTAGE_DIR`,
`DASHCAM_PORT`, `DASHCAM_LOG_LEVEL` and `TZ`, and all of them have working defaults.

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
| **Plates** | Searchable plate database — full or partial (`ABC` matches `ABC123`) |
| **Vehicles** | Vehicle sightings independent of plates |
| **Queue** | What is processing now, what is queued, what failed, with retry |
| **Logs** | Application and per-job logs |
| **Settings** | Everything above, editable without a restart |

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
GET  /api/plates?q=ABC            full or partial plate search
GET  /api/plates/{id}/observations every sighting, with location and confidence
GET  /api/vehicles                vehicle sightings
GET  /api/jobs                    queue state; pause, resume, retry, cancel
GET  /api/settings                the settings catalogue and current values
POST /api/scan                    scan now
POST /api/retention/plan          evaluate retention (report-only unless enabled)
```

The app currently has **no authentication** and assumes a trusted LAN. Do not expose it
directly to the internet — put it behind a reverse proxy with auth if you need remote
access. The API is structured so auth can be added without reworking the routes.

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
