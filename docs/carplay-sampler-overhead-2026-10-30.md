# CarPlay sampler overhead and path checks - 30 October 2026

Follow-up to `output/pdf/carplay-report-2026-10-30.pdf`, run alongside the Codex
sessions recorded in `carplay-fix-investigation-2026-10-30.md` and
`carplay-followup-2026-10-30.md`. Times are Adelaide (UTC+10:30). No CarPlay cure is
claimed. The unit was parked with ignition off; no phone was streaming.

## Fixed locally: the sampler was a large load of our own

Measured on the unit with `/proc/<pid>/stat` ticks (utime+stime+cutime+cstime of the
sampler and its workers), side by side with a sandboxed copy of the new script:

| Condition (parked, no CarPlay) | Production sampler | New sampler |
| --- | --- | --- |
| Three leftover STALE hotspot neighbours, 60 s | 4,383 ticks = 73% of a core | 697 ticks = 12% of a core |
| Same, before the activity fix (builtins only) | 4,084 ticks = 68% | 2,559 ticks = 43% |
| 30 s idle check earlier (0 neighbours) | 641 ticks = 21% | not measured |

Causes, all in `backend/app/ingest/carplay_timing.sh`:

- **Process starts.** On this unit a trivial `awk` costs ~70 ms of CPU and `printf`/`sleep`
  are external binaries (mksh R59). `clock_ms`, `deadline_delay`, `process_start`,
  per-process ticks, pressure, SoC temperature, `/proc` and sysfs reads now use builtins.
  mksh arithmetic is 32-bit: the clock is concatenated, deadlines compare by difference,
  and a wrapped negative deadline is parenthesised (`x--y` is a decrement in mksh; caught
  by running the helpers on the unit).
- **STALE neighbours made the sampler "active".** Entries outlive the phone by many
  minutes, so the full transport/frame probes ran while parked. Activity now needs a
  REACHABLE/DELAY/PROBE neighbour, or ignition on with ZLink running (unchanged).
- **Full dumps every pass.** `dumpsys wifi` (2 MB, ~0.5 s) ran every pass even idle, for the
  hotspot frequency only; now every 5 min or when the neighbour count/station channel
  changes. The full SurfaceFlinger dump (~240 KB on the compositor thread) for queued
  frames is at most every 5 min. `iw` (absent) is probed once.

Output fields and formats are unchanged. Tests: `tests/test_carplay_sampler_builtins.py`
(sh and mksh; CI installs mksh), plus all CarPlay suites (338 passed locally).

## Checked and ruled out (head-unit side)

- **Thermal clamping**: at 86-87 C parked, all five cooling devices at 0, VSP at its 384 MHz
  maximum, first SoC trip 95 C. Android "status 4" is a report, not a clamp here.
- **Wi-Fi background scans**: during drives the station does a full-band scan every 160 s
  (~2.4 s each). Frame intervals around them (71-141 ms) match baseline; only the
  CarPlay start-up minute has large gaps.
- **TCP window**: with no default network Android sets `tcp_rmem` max to 110 KB (2 MB at
  home), but the CarPlay video sockets set their own buffer (`rb172728000`, wscale 11),
  so that limit does not apply.
- **Relay buffering**: Wi-Fi bytes in vs ZLink loopback bytes in track within ~100 KB over
  whole drives; the gap grows at a constant ~4.6 KB/s (headers), not in steps.
- **Decoder throughput**: 29.1-29.7 frames/s per session, matching the 30 fps input.
- **Mid-drive freezes** (5.2 s on 30 Oct, 7.0 s on 20 Sep) are supply stops: hotspot bytes
  drop to ~4 KB, then resume at a normal rate with no catch-up burst.
- Stream rate while driving is 17-21 Mbit/s on good and bad drives alike; no head-unit
  metric separates the user's good and bad drives. All good drives were evening/night,
  all laggy ones daytime.

## Still open

Where the seconds of delay live: upstream of the unit (phone render/encode, or a send
queue on the phone), or ZLink's Java queue between its socket read and the decoder,
which no probe can see. Codex found ZLink drops the phone's presentation timestamps at
the native handoff, so ZLink cannot detect or discard stale frames. The cheapest
discriminator needs no tooling: when the picture is behind, press the unit's Home
button and reopen ZLink. Snapping back to real time points at a queue that a stream
restart clears (and gives a workaround); staying behind points at the phone (try
another iPhone, or the same phone cool and off a wireless charger).

OBD BLE uses `CONNECTION_PRIORITY_HIGH` (~11-15 ms interval) on the Marlin3 Lite
combined Wi-Fi/BT/GNSS radio that also hosts the hotspot. It began 1 September, just
before the first lag reports, but it is present on good and bad drives alike, so it is
at most a contributing load. Not changed.
