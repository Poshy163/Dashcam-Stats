# CarPlay visual lag, 17 September 2026

The saved logs confirm multi-second stalls in ZLink's video path. They do not yet
identify whether wireless delivery, ZLink buffering, or the vendor codec caused
them. The user reported that the song changed immediately while the visible
button animation and artwork lagged. No live CarPlay session was available for
this investigation; the user requested continued analysis of saved logs.

All times below are Adelaide local time (UTC+09:30). The server was running
`f75d9fb4e7b042f2c5a369923b1b2952886870cf`. Evidence came from the unit-log,
CarPlay-timing, OBD-drive, and journey APIs. Private raw captures remain outside
version control.

## Confirmed events

| Drive | Captured picture stall | Longest gap between presented frames | Maximum ready-to-present wait in that sample | Decoder session maximum / average |
| --- | --- | ---: | ---: | ---: |
| 09:39-09:53, journey 4842 | 09:50:58 | 3,846.6 ms | 27.4 ms | 3,976.477 / 163.898 ms |
| 11:59-12:16, journey 4843 | 12:05:58 | 1,958.5 ms | 36.8 ms | 2,086.867 / 161.830 ms |

Both incident samples came from the package-owned ZLink window, with overlapping
SurfaceFlinger rings and zero measured ring-coverage gap. The morning sampler
then returned to about 28 fps at 09:51:01. A missing poll does not explain either
stall. The decoder summaries were emitted after each session ended; their
maximum values have no per-frame timestamp. Their close agreement with the
presentation stalls is corroboration, not an exact event join.

The midday stall precedes the user's approximate 12:07 report by about a minute.
From 12:06 to 12:09, the largest captured presentation interval was 105.9 ms.
Regular frame presentation cannot exclude stale content arriving at a steady
cadence, so this does not disprove the report.

Android's [FrameTracker implementation](https://android.googlesource.com/platform/frameworks/native/+/cdb6b16dec3a541b455be99d075004cb2f0a0cd7/services/surfaceflinger/FrameTracker.cpp)
reports desired presentation, actual presentation, and frame-ready timestamps.
The small ready-to-present waits place the observed long delay before the final
compositor wait. The [AOSP MediaCodec implementation](https://android.googlesource.com/platform/frameworks/av/+/refs/tags/aml_go_art_330913000/media/libstagefright/MediaCodec.cpp)
measures buffer round trips in microseconds, including pipeline effects. These
values are not pure decode execution time or phone-to-screen latency; the
vendor's implementation has not been inspected.

The 12:31-12:39 drive's decoder maximum was 1,183.156 ms, with a 157.620 ms
average. The user cannot recall its symptoms, so it is not a confirmed smooth
comparison. Large initial presentation intervals on this and the midday drive
include the startup/waiting window; they were excluded from the incident claims.

## What the surrounding data supports

- Both reported drives used a 5 GHz hotspot at 5180 MHz (channel 36). No home
  Wi-Fi station association or channel change was recorded during either drive.
  The earlier 2.4 GHz/home-Wi-Fi startup theory does not fit these observations.
- The morning temperature context was 84.8 degrees C; the midday event was
  58.8 degrees C. Heat alone does not explain both. CPU frequency readings did
  not change, but the probe does not measure video-processor throttling.
- CPU pressure stayed near its surrounding baseline, about 32 percent. ZLink
  used about 52-55 percent of one core and the shared codec service about
  29-30 percent. Memory availability was about 2.5 GiB with negligible memory
  pressure. No corresponding ZLink main-thread scheduling spike was found.
- The midday hotspot receive-rate context fell to about 2.6 Mbit/s. It averages
  a slower context interval, and video bitrate also depends on content; it is
  insufficient evidence for a radio fault. Device-wide TCP retransmissions and
  UDP buffer counters did not establish an incident-specific network failure.
- All 1,654 successful peer-TCP observations in the saved 48-hour window matched
  zero sockets; 21 observations were unavailable. Zero queue sizes therefore
  describe an unmatched probe, not a healthy phone video connection. The probe
  currently requires an IPv4 hotspot neighbour and a socket owned by ZLink's UID.
  IPv6, UDP, or a different owner remain possibilities. App-wide TCP statistics
  cannot identify the video connection. The firmware also lacks `iw` station
  diagnostics, leaving signal and retry measurements unavailable.
- No retained vendor/thermal log entries were found for the narrow incident
  windows. Their absence limits diagnosis; it does not establish error-free
  operation. The direct sampler file provided the principal evidence.

The strongest finding is a video-path stall before final presentation, with a
similar codec round-trip maximum in each affected session. A live session will
eventually be needed to identify the real transport and distinguish input
starvation from receiver/codec stalls. No radio, firmware, decoder, or head-unit
settings were changed during this investigation.

## Fixed: truncated log copies prevented complete recovery

In one saved page of 200 afternoon frame observations, 130 messages were exactly
1,024 characters long and ended mid-field. Missing tails included ring overlap,
ring gaps, and unchanged-surface duration. The direct-file transport retains the
longer messages, but both copies intentionally share a sample identity.

The server previously ignored every identity conflict. If a truncated logcat
copy arrived first, the later complete direct-file copy was discarded forever
as a duplicate. This explains missing diagnostic fields despite an unchanged
sampler version. It is a loss of evidence, not a demonstrated cause of CarPlay lag.

Storage now atomically enriches an existing CarPlay observation only when the
incoming message is a strictly longer, exact prefix extension with the same
identity. It preserves the original timestamp, process metadata, and row ID.
Shorter, repeated, and conflicting copies cannot overwrite it. Ordinary vendor
logs and legacy sampler lines keep their existing deduplication behavior.

After deployment, the existing parked-unit recovery can automatically complete
historical rows whose full messages remain in the head unit's rotated sampler
files. No schema migration is needed. Already-rotated-away tails cannot be
reconstructed from the server's truncated copy. This changes server ingestion
only and adds no work to the driving sampler.

Validation: a regression reproduced the old truncated-first failure. All 107
unit-log, CarPlay sampler, and latency-diagnostic tests then passed, including
both arrival orders, repeated recovery, conflicting identities, capture metadata
preservation, and a truncation boundary inside a numeric field.
