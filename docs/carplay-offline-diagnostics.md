# Offline CarPlay delay diagnostics

Sampler schema 4 adds evidence for video that is delayed even when frame presentation is
regular. It runs on the head unit, independently of server connectivity. The server deploys
the script when the unit is reachable; it must reach the unit at least once after an update.
An offline cold reboot cannot start a detached shell that was not restored by the platform.
The script continues across ordinary sleep/resume when Android preserves the process.

Presentation uses independent three-second deadlines. On the slower context cadence
(15 seconds by default), it additionally reads:

- ZLink UID TCP receive/send queue bytes and socket count, when proc access is permitted.
- Available memory, CPU/memory/I/O pressure averages, and current CPU policy clock range.
- ZLink memory/thread count and the shared Unisoc codec service's CPU consumption.

These are numeric aggregates. No network addresses, SSIDs, input events, screen images,
song titles or raw codec dumps are saved. Socket measurements are scoped to the app UID;
the codec service is shared with recording and must not be attributed solely to CarPlay.
Socket tables or pressure files blocked by Android produce unavailable values, not zeros.

SurfaceFlinger's third timestamp is buffer-ready and its second is actual presentation.
The sampler reports p95 and maximum ready-to-present delay for newly observed frames,
excluding overlap, invalid fences and future-ready timestamps. This can reveal a long local
display wait despite steady frame rate. It does **not** measure decode duration, tap latency
or phone-to-display frame age. A zero/short local wait does not rule out upstream buffering.
The timestamp contract is defined by
[AOSP FrameTracker](https://android.googlesource.com/platform/frameworks/native/+/cdb6b16dec3a541b455be99d075004cb2f0a0cd7/services/surfaceflinger/FrameTracker.cpp).

Capture continues with ignition on and ZLink present even without hotspot neighbours.
Separate context events retain resource evidence when no ZLink display layer is available.
The current file and eight rotations occupy approximately 9 MiB, plus at most the next
context pass before rotation; retention time depends on activity. Recovery is read-only,
deduplicated and gated on observed ignition-off. Under the line cap, the newest history
is preferred. Old imported observations keep missing diagnostic values as null.

The CarPlay timing page shows the local display wait and unread TCP queue for the selected
period. Detailed measurements are available in `/api/unit-logs/carplay-timing` samples and
events, and under the CarPlayTiming tag in unit logs. Merely seeing schema 2 does not prove
that every Android measurement is available; inspect the individual fields.

## Decoder and Android UI reports (schema 3)

An independent worker runs at most once per minute while ignition is on or a hotspot
neighbour is present, and for two minutes afterwards. It also runs once at sampler startup
to recover retained reports. Each Binder dump uses a two-second timeout; the worker never
blocks the four-second surface loop. It collects:

- `media.metrics` video **decoder** records whose owner is exactly `com.zjinnova.zlink`.
  Camera encoders, other apps and audio codecs are excluded. Average/minimum/maximum
  latency (microseconds), buffer count, session lifetime and low-latency toggle counts
  survive as `codec_summary` events. Only the newest eight sanitized reports are retained
  in a small deduplication snapshot; unchanged reports are not logged repeatedly.
- `gfxinfo com.zjinnova.zlink` renderer epoch, frame/jank totals, p95 render time and
  high-input-latency/slow-UI-thread counts as `graphics_summary` events. These are Android
  renderer statistics, not measurements of a touch reaching the iPhone. Totals reset with
  the renderer; compare the epoch before calculating deltas. The collector never resets them.
- `/proc/net/snmp` cumulative TCP retransmitted segments and UDP receive/send buffer
  errors as `network_summary` events. These are **device-wide**, not ZLink-specific or
  wireless retry counters. Counters can reset at reboot; do not subtract across resets.

Android can publish a decoder summary only after its session closes. `occurred_at` is
collection time; `codec_reported_local` preserves the device's month-day/time with no
invented year or zone. The UI lists reports separately from the selected capture period
to avoid assigning an old session to a new drive. Historical records available at update
are recovered, but records Android has already discarded cannot be reconstructed.

[AOSP MediaCodec](https://android.googlesource.com/platform/frameworks/av/+/refs/heads/android12-release/media/libstagefright/MediaCodec.cpp)
defines the latency units as microseconds and measures from sending a buffer to receiving
its corresponding output. This includes codec buffering and is neither pure decode
computation time nor end-to-end CarPlay latency. Low-latency on/off fields count requests;
zero does not prove a hardware mode is disabled or can be forced. Session maxima do not
provide the frame's time and may include startup/teardown, so compare repeated evidence
and reported symptoms before inferring a cause.

All three event types use the same bounded offline file, log transport and API as schema 2.
Raw dumps, app UID, window titles, addresses and media metadata are never retained. Removing
the sampler's files should also remove `.dashcam_cpt_codec_snapshot` and any interrupted
`.dashcam_cpt_codec_snapshot.*` temporary file.

## Video buffering and sampling coverage (schema 4)

The frame worker reads a small, atomically replaced context file. It schedules against
elapsed uptime, subtracts execution time from the next sleep, and skips missed deadlines
after an overrun instead of sending a burst of catch-up queries. Slow context reads no
longer run between frame reads. Frame queries have a one-second service timeout. Re-arming
terminates the previous worker; separate observation IDs/counters prevent races between
frame, context and summary emissions.

- `frame_poll_gap_ms` records actual starts of active polling passes, not the configured
  interval. `context_age_ms` makes the age of reused context explicit.
- `ring_overlap=0` means the oldest retained frame is newer than the last observed frame.
  `ring_gap_ms` measures that separation. It flags unobserved history, not a proven
  visible stall or exact dropped-frame count. Initial/reboot baselines are unavailable.
- `surface_unchanged_ms` is elapsed time since polling last observed a newer presentation
  timestamp. Static/hidden windows can legitimately stay unchanged. It is **not** the
  age of an iPhone-generated frame.
- A full SurfaceFlinger dump is filtered in memory on the context cadence for exact
  ZLink package buffer layers. Only matching layer count and maximum `queued-frames`
  are retained; anonymous camera buffers/window containers are excluded. No matching
  layer is unavailable, distinct from a matching layer with zero queued buffers.
- `ss -tine`, bounded to two seconds, contributes maximum RTT/RTO and retransmission
  statistics from established sockets with exactly the app UID. No endpoints, socket
  identifiers or raw dumps are logged. These sockets can include control/loopback traffic
  and are not identified as the CarPlay video channel. Zero matching sockets produces
  count zero and unavailable RTT. Lifetime retransmission totals are summed over the
  currently observed sockets and can decrease when sockets close; do not treat them as
  monotonic app counters. Omitted TCP_INFO fields remain unavailable.
- ZLink main-thread CPU runtime/runqueue wait nanoseconds and process-start ticks are
  retained. Compare deltas only within the same process lifetime. Runqueue wait means
  waiting to be scheduled, not blocking on I/O, and excludes other ZLink/decoder threads.

Field definitions: [ss TCP diagnostics](https://www.man7.org/linux/man-pages/man8/ss.8.html)
and [Linux scheduler statistics](https://www.kernel.org/doc/html/v6.12/scheduler/sched-stats.html).

Retained file recovery now also runs during a footage transfer when ignition is known off,
with the existing five-minute throttle and byte/time limits. This path does not restart
the sampler or reconnect the transport. Ignition-on/unknown still blocks recovery.

Schema 4 cannot reconstruct these fields for older drives, prove continuous coverage under
all loads/frame rates, or determine when the iPhone rendered a frame. Frame-worker context
and sequence files use `.dashcam_cpt_context_*` / `.dashcam_cpt_frame_seq_*`; normal exit
cleans them up. Per-layer `.fresh` markers accompany the existing `.dashcam_cpt_seen_*`
markers and contain only elapsed timestamps.
