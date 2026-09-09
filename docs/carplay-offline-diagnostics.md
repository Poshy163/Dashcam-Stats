# Offline CarPlay delay diagnostics

Sampler schema 2 adds evidence for video that is delayed even when frame presentation is
regular. It runs on the head unit, independently of server connectivity. The server deploys
the script when the unit is reachable; it must reach the unit at least once after an update.
An offline cold reboot cannot start a detached shell that was not restored by the platform.
The script continues across ordinary sleep/resume when Android preserves the process.

The existing four-second presentation cadence is unchanged. On the slower context cadence
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
