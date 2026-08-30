# Interrupted OBD drive audit — 2026-08-30

This note records the pre-change evidence for drive
`01a050f4-30b0-75a7-a36e-6ab43c2eb0b1`. Times below are UTC. The raw server database and
authenticated API responses were copied off the deployment before reconciliation. The database
snapshot was 435,679,232 bytes with SHA-256
`3ec3cd8caa6ffd45711cefe903d38caff8f18e082f7ea9ae400902eb52a043b`.

The snapshot and responses contain no API credential. The credential is not recorded here or in
source control.

## What happened

| Time | Evidence |
| --- | --- |
| 04:36:15.664976 | The logger created the drive after charging voltage and a checksum-valid `0100` response. |
| 04:36:27.858775 | First persisted sample, sequence 0. |
| 04:49:37.013984 | PID `0x10` (MAF) failed ISO header/length/source/checksum validation. The current logger suppressed it for the rest of the connection. |
| 05:02:07.831613 | The server saw the head unit and began an automatic ingest visit. |
| 05:02:09.885177 | The server recorded that the transfer link was 2.4 GHz. |
| 05:02:20.170794 | Bulk ingest began with 123 footage files (about 7,084 MB) planned. |
| 05:03:07.698612 | Last valid sample, sequence 244. |
| 05:03:08.739056 | Logger diagnostic: `connection_failure`, category `ble_or_elm`, message `FFF1 write was rejected`. |
| 05:03:08.768770 | The logger noticed/finalised the drive with `stop_reason=connection_lost` and `clean_end=false`. |
| 05:03:15.691957 | The concurrent automatic pull ended cancelled after a receive timeout. A partially copied active recording was discarded safely. |
| 05:03:24.262182 | The immutable 33,554-byte bundle was verified on the server with all 245 samples and 79 diagnostics. |
| 05:03:25.628267 | Home Assistant import completed with HTTP 200. |
| 05:03:32.173728 | The logger opened a new drive after reconnecting. |

The precise on-device cause below the rejected GATT write is not recoverable from the old status
and diagnostics. The transfer started 48.6 seconds before the failure on the same 2.4 GHz radio,
so coexistence pressure is a strong causal candidate, but it is not proof that Android disabled
Bluetooth. There is no radio-disable log at the failure, and the deployed server deliberately left
both radios alone whenever the OBD logger claimed ownership.

The lifecycle presentation defect is conclusive: the raw manifest says `connection_lost` and
`clean_end=false`, while the exported `completion_status` says `complete`. The logger mapped only
`device_restart` to `recovered` and mapped every other stop reason to `complete`. The API then
omitted `stop_reason`, preventing the UI from explaining the contradiction.

## Pre-change gap evidence

The raw sequence is complete and unique from 0 through 244. No sequence was dropped between the
logger database, bundle, server database, API, or Home Assistant queue.

Across the 1,599.840-second sample span:

- median sample interval: 4.976 s;
- 95th percentile: 11.656 s;
- 99th percentile: 11.972 s;
- maximum: 12.195 s;
- 123 intervals were at most 5 s, 40 were over 5 s and at most 8 s, and 81 exceeded 8 s;
- total excess above the nominal 5-second cadence was 433.268 s.

The pattern is deterministic rather than random database loss. Fast PIDs were requested every
cycle, all medium PIDs together every third cycle, all slow PIDs together every twelfth cycle, and
one diagnostic command was appended after each sample. On the serial ISO 9141-2 command stream,
the grouped medium/slow work and diagnostic commands overran the 5-second cycle. The 82 medium-tier
observations align almost exactly with the 81 intervals over 8 seconds.

Intentional tier spacing must not be counted as missing fast samples: medium signals were designed
for 15-second cadence and slow signals for 60-second cadence. The genuine avoidable losses were
the fast-cycle overruns, the permanent suppression of MAF after one malformed response (117 MAF
observations, ending at 04:49:32.466191), and the zero observations for advertised PID `0x15`
after its repeated framing problem. Validation must remain strict; recovery means bounded retry and
clear missing-data reporting, not accepting a bad frame or fabricating a value.

## Pre-change preservation and state

- All six server bundles were verified and imported; there were no waiting, failed, or quarantined
  OBD queue rows.
- The affected bundle had one import attempt, HTTP 200, no validation warning, and no queue error.
- The affected raw samples, diagnostics, manifest, and summary are retained in the pre-change
  database snapshot and the immutable server bundle. Missing values remain null; no synthetic rows
  or zero fills are present.
- There was no server restart around the drive. Startup recovery did not create or alter this
  bundle.

This is a pre-change record. Post-change acceptance results belong in the deployment report rather
than being retroactively written into this evidence.
