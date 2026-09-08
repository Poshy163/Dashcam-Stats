# Read-only head-unit audit — 8 September 2026

The operator separately authorised direct investigation of the online dashcam at
`192.168.1.214`. Device reads ran approximately 02:09:40–02:14:15 UTC
(11:39:40–11:44:15 Adelaide). Reads stopped when the server reported about 18 seconds
remaining before sleep. Sleep itself was not observed or triggered.

This supplements the [server/source audit](audit-2026-09-08.md). No APK was installed,
recording or radio setting changed, backup triggered, source file deleted, service
restarted, or commit/deployment performed. ADB was connected on the documented port
5555. Completed recordings and the installed APK were copied to private local temporary
storage for offline inspection; these copies are not repository fixtures.

## Findings and practical consequences

1. **The installed companion confirms the networking defect.** Version 0.3.0/code 13
   reports source identifier `bce049b7e961`, and the same identifier occurs in its DEX.
   Its installed package and extracted APK manifest both omit `INTERNET`. The manifest
   also has no cleartext override or network security configuration. This is direct
   installed-artifact evidence supporting the permission/LAN policy fixes already made
   locally. No webhook was sent, and those fixes are not installed on the unit.
2. **The most recent automatic backup and restoration completed.** Server state reported
   72/72 files, approximately 3.99 GB, idle/OK. Its latest radio transition reports
   Bluetooth and hotspot restoration verified. The companion event journal independently
   records resumption. This successful visit does not explain earlier watchdog failures.
3. **The original camera timing is irregular.** The front declares 30 fps but contains
   about 24.73 video packets/second across its PTS span. The rear declares 25 fps and
   measures 24.82. PTS gaps reach 167 ms and 120 ms respectively. Both files decode fully
   without warnings. Identical filename timestamps do not establish exact paired-frame
   alignment; displayed clocks differ by a second at three sampled offsets but agree at
   the fourth. Do not apply an unconditional one-second correction.
4. **Recorder staging was observed in operation.** Growing `pre_*` files and zero-byte
   future placeholders became completed files under `Video` in subsequent snapshots.
   Segments in this visit are approximately one minute, not five minutes. This supports
   excluding staging files; snapshots cannot prove atomic publication or crash durability.
5. **The companion was healthy in its parked state.** Foreground service running, ACC off,
   Wi-Fi connected, no queued OBD bundles, no reported error, verified 300-second managed
   idle policy. The engine was not running; ECU polling and moving-vehicle telemetry were
   not tested.

## Installed components and identity

| Evidence | Observed value |
| --- | --- |
| Platform | Android 14, API 34; boot completed |
| Companion package | `com.dashcamstats.obdlogger` |
| Companion version | 0.3.0, code 13, min SDK 26, target SDK 34 |
| Companion last update | 2026-09-03 21:24:42, device-reported local time |
| Companion source identifier | `bce049b7e961`, status JSON and APK DEX agree |
| Local source at audit entry | `1e258d04dc9590141a427e16da663d555fc802ed`, plus local audit edits |
| Installed APK bytes | 1,186,280 |
| Installed APK SHA-256 | `34614720a4341f5fabb1ee41b51f3a3f7c64368b12c0753b6b8960c8d8747d2f` |
| APK signing certificate SHA-256 | `e9191adb8f04a7117d71763c5e1332e7c0e4639a63afa09aef9c9bb1684d408d` |
| Recorder package | `com.zqc.camera`, system app at `/system/app/ZqcCamera/ZqcCamera.apk` |
| Recorder version | `1.0.0_2026052613`, code 2, min/target SDK 29 |

The APK hash matched between the installed path returned by `pm path` and the downloaded
copy. Local `apksigner verify` succeeded. Source identifier `bce049b7e961` resolves in the
local Git history. An embedded identifier establishes what the package claims to be built
from; it is not a reproducible byte-for-byte source build proof. The recorder's vendor
source is not present in this repository. No randomised install paths, credentials,
preferences, network identifiers, or raw DEX strings are included here.

The companion foreground service was `ObdLoggerService`, foreground notification ID 1107,
connected-device type, `startRequested=true`. Requested/granted permissions included
Bluetooth connect, foreground service/connected device and notifications; `INTERNET`
was absent. The package was not replaced by the locally built audit APK.

## Backup, radio recovery and OBD evidence

The latest automatic transition began at 02:03:27.690 UTC, before this investigation,
and completed at 02:09:30.104 UTC. Server flags report Bluetooth/hotspot disable and
restore attempted and verified, with no active transition or recovery requirement.
Logger quiesce and resume were also verified. Recovery evidence source/unit report/sleep
report fields were null: this completion was server-verified, not a fresh watchdog report.

Independent companion journal events, UTC:

| Time | Event |
| --- | --- |
| 02:03:31.959 | Ingest handoff entered quiesce |
| 02:03:31.973 | Drive lifecycle interrupted for ingestion; bundle export started |
| 02:03:32.062 | Backup-active sleep window verified |
| 02:03:32.235 | Bundle export completed |
| 02:03:34.394 | Ingest handoff acknowledged |
| 02:09:18.665 | Service resumed on Bluetooth-on event |
| 02:09:26.326 | Ingest handoff resumed, resume observed |
| 02:09:26.815 | Wi-Fi-connected sleep window verified |
| 02:09:31.095 | Adapter voltage read successfully |
| 02:09:31.146 | ECU session skipped because voltage was below start threshold |

These are existing application actions, not audit-triggered commands. Server ingest state
later showed 72 files complete, total payload counter 3,990,169,653 bytes and completed
counter 3,990,241,792 bytes. The counters differ by 72,139 bytes; their precise accounting
was not established by this device phase, so they are not treated as a checksum/integrity
proof. No complete server-to-card inventory reconciliation or remote footage hashing ran.

Only a stale `.dashcam_analyser_radios.watchdog_report`, dated September 5, remained in
the bounded watchdog path inspection. Its sanitised content indicated `lease_expired`
and Bluetooth/hotspot both on. It is evidence of an older fallback invocation, not of a
failure during this successful visit. No active base flag or token-specific watchdog
script was present after restoration, which is consistent with cleanup. The active
watchdog uses token-specific script/ready/lease paths; absence of generic legacy
`.watchdog_pid`/`.watchdog_lease` names would not diagnose its health. Tokens were not
printed. No watchdog was armed, killed or simulated.

Companion status schema 6 at 02:11:39.635 UTC reported parked, ownership enabled,
pending bundles 0, adapter reachable but disconnected, ECU disconnected, engine stopped,
Wi-Fi connected, ACC known/off, no ingestion sleep hold and no error. Managed-idle sleep
target and observed value were both 300 seconds, verified. A read of the vendor countdown
property independently returned 300. The OBD ready directory had 0 files, receipts 16,
and control was empty. A bounded journal sample contained no webhook event; absence from
that sample does not establish that no webhook was ever attempted.

## Recording lifecycle and original metadata

The card had approximately 31,158,272 KiB capacity, 560,960 KiB used and 30,597,312 KiB
available in the snapshot. The normal `Video` pair named `20260908113847_camera_*.ts`
remained unchanged in size while subsequent `pre_*` pairs grew and appeared as completed
files. Future `pre_*` placeholders were zero bytes. `LockVideo` was empty. A bounded
two-level DCIM inventory later contained 16 TS and 2 JPG files, with no other extensions.
This does not rule out metadata elsewhere, unrecognised private transport packets or
sidecars outside the inspected paths.

One completed pair was pulled, approximately 122 MB total. Observed ADB pull rates were
11.9 MB/s front and 12.7 MB/s rear. This is a two-file read observation, not a controlled
backup benchmark or evidence that production concurrency should increase. Source sizes
were checked again afterwards. No extra full media hash read was imposed on the card.

| Property from original MPEG-TS | Front, camera 0 | Rear, camera 1 |
| --- | --- | --- |
| File size, bytes | 60,637,708 | 61,520,556 |
| Video | H.264 Baseline, 1920×1080 | H.264 Baseline, 1920×1080 |
| Declared `r_frame_rate` | 30/1 | 25/1 |
| Declared `avg_frame_rate` | 0/0, unavailable | 25/1 |
| Video time base | 1/90000 | 1/90000 |
| First video PTS | 0 seconds | 0 seconds |
| Duration | 59.633322 seconds | 60.441133 seconds |
| Format bitrate | 8,134,741 bit/s | 8,142,872 bit/s |
| Audio | AAC LC, 8000 Hz, mono, 59.52 seconds | No audio stream declared |
| Rotation / data stream | None declared | None declared |

`ffprobe -show_packets -select_streams v:0` produced the following deterministic timing
measurements. Effective rate is `(packet count - 1) / (last PTS - first PTS)`; it is a
packet-timing calculation, not a sensor exposure measurement.

| Measurement | Front | Rear |
| --- | --- | --- |
| Video packets | 1,475 | 1,500 |
| Last PTS | 59.599989 seconds | 60.401133 seconds |
| Effective rate | 24.731548 / second | 24.817415 / second |
| Median PTS delta | 33.334 ms | 40.000 ms |
| 95th percentile delta | 66.677 ms | 40.067 ms |
| Maximum delta | 166.655 ms | 120.000 ms |
| Non-positive deltas | 0 | 0 |
| Gaps greater than 100 ms | 5 | 1 |
| Keyframe packets | 59 | 60 |

Each full video and available audio stream was decoded locally with FFmpeg to the null
muxer. Both exited 0 with no warning/error lines. Gaps in otherwise decodable media do
not by themselves establish whether capture, scheduling or timestamp generation caused
missing cadence. This small parked sample cannot establish driving performance.

Timestamp-only crops at four requested media offsets were manually read:

| Requested offset | Front clock | Rear clock |
| --- | --- | --- |
| 1 second | 11:38:49 | 11:38:48 |
| 10 seconds | 11:38:58 | 11:38:57 |
| 30 seconds | 11:39:18 | 11:39:17 |
| 50 seconds | 11:39:38 | 11:39:38 |

The visible overlays contain date/time, coordinates and speed; private coordinates are
excluded here. This is manual source observation, not a scored OCR/parser test. Clock
updates have second-level resolution, and seeking selects a frame near the requested
offset. The table does not justify a universal offset or frame-index correspondence.
One ADB clock comparison found about -0.625 seconds relative to the local UTC midpoint
with 102 ms round-trip time; the device command returned whole seconds, so this only
supports agreement within roughly a second, not a sustained drift measurement.

Current code already resamples normal analysis through FFmpeg's `fps` filter and derives
offsets from that output rate (`hardware/ffmpeg.py`); it does not label normal sampled
frames using the source's declared 30 fps. Telemetry derives each camera's origin from
its overlay with a filename sanity check (`pipeline/stages.py`), and paired recovery
matches absolute capture seconds (`pipeline/telemetry_quality.py`). These are relevant
guards, but this visit did not run a full production OCR/inference pass on the pair or
prove all fallback seeks. No speculative timing change was made from four screenshots.

## Resource observations and limits

A single process snapshot showed recorder CPU 73.3% of one core and resident memory
about 285 MB; companion CPU 0.0% and resident memory about 103 MB. Device load averages
were around 22/22/18 with eight cores; that is a pressure signal, not attribution of
the observed frame gaps. Wi-Fi information included 5240 MHz and link-rate reports in
the 260–433 Mbps range. Multiple dump entries can include history; this is not measured
application throughput. Network names, MAC addresses and vehicle identifiers were filtered.

Unverified: actual sleep/wake and next ignition cycle, driving/ECU data, a controlled
webhook with the fixed APK, recording continuity under changed transfer load, watchdog
failure reproduction, full metadata reverse engineering, exact server image identity,
source-to-server media hashes, physical crash durability, and GPU behaviour. Existing
successful backup and restoration evidence does not close these separate questions.

## Handoff

This phase adds this report and updates the original report/handoff links; no additional
runtime code was changed. The prior audit's passing local checks remain applicable; tests
were not rerun for documentation-only updates. Offline original-media decode and APK
manifest/hash/signature checks are the new validation.

Raw clips, APK and private diagnostic images remain outside the repository in the local
temporary directory `dashcam-device-audit-20260908`. Do not attach them to a public issue
or commit them as fixtures. Any regression fixture derived later should be synthetic or
sanitised. The next deployment should include the already-tested companion networking
fixes and coordinated removal/rotation of the previously identified embedded credential;
installation, credential changes and deployment remain unperformed.
