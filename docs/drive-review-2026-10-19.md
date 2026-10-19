# Drive review and follow-up fixes — 19 October 2026

This review follows the retained-log inventory in `application-audit-2026-10-19.json`.
It checks the new drive, its backup passes, subsequent processing and confirmed
implementation issues. Live evidence was collected from authenticated APIs and,
during the online window, native read-only radio checks. Credentials, locations,
vehicle identifiers and raw telemetry are excluded from this report.

The running server was revision `5c6f2f57a706deded0e2e8cb850ad175563fa05f`.
The fixes described below require a later deployment; the Android scheduling fix
also requires a separately signed companion APK update to version 0.3.5.

## Latest drive

The OBD interval was 22:07:12–22:29:32 Adelaide time: 22 minutes 20 seconds,
5.48 km, average 15 km/h and maximum 66 km/h. Engine-running idle accounted for
13 minutes 55 seconds. These are recorded measurements, not a mechanical assessment.

The server stored and verified the bundle. It contains 252 of 269 expected samples
(93.68%), with three gaps, a longest gap of 9.91 seconds and no recorded transport
connection losses. Signal completeness was 85.04%. Backup quiescing explains the
producer's `ingestion_requested` interruption marker.

All 21 ECU lamp/status observations reported the check-engine lamp on and one
stored fault. The user confirmed the dashboard lamp. The actual trouble code was
not captured. The 24 polling errors are separate: 23 rejected oxygen-sensor-2
responses and one rejected oxygen-sensor-1 response. There were no recorded PID
timeouts or transport errors. The retained evidence does not establish which
frame validation failed or the vehicle fault's cause.

The optional diagnostic queue made no progress during this drive. Its conservative
command reserve could exceed the entire five-second cycle budget; the measured
median cycle was 5.236 seconds. Merely waiting for spare time could therefore
starve read-only fault-code scans indefinitely.

## Backup and processing

The arrival pass completed 95 transfers / 5,112,867,800 bytes. Two later passes
completed six transfers / 307,975,208 bytes and eight transfers / 409,625,868 bytes:
109 copy operations and 5,830,468,876 bytes in total. The first two passes ended
with durable Bluetooth/hotspot restoration and OBD logger resume verification.
Native checks observed quiet radios while transfer counters advanced, then restored
Bluetooth and the hotspot interface after those passes. A transient quiesce renewal
warning recovered before completion. At 12:33:37 UTC, the final pass's durable
transition was complete/inactive, with radio disable/restore and logger
quiesce/resume all verified and no recovery required. Its final state was checked
through the server record; native evidence from earlier passes is not independent
verification of the final pass. The car was then offline and was not reconnected.

A normal scan was requested after the final files settled, with damaged-footage
handling set to hide and all deletion/cleanup options disabled. At 12:26:50 UTC,
108 newly indexed recordings were visible, 45 completed, two processing and 61
queued, with zero failed or settling recordings. Transfer operations and newly
indexed rows are different counters; these totals alone do not establish a missing
or duplicated recording. Processing continued after the car's online window.
The scan HTTP request exceeded its four-minute client deadline. It was not retried;
subsequent GET observations established that files were indexed and settling cleared.
Although the configured cap is two automatic backups per visit, logs show a new
arrival boundary between the first and second passes; the second visit then used
two passes. No cap violation was established. The exact cause of that boundary was
not retained, so these history rows do not prove three backups within one visit.

All original 329 drive IDs and 429 session IDs remained visible; the totals had
grown to 330 drives and 431 sessions. The new OBD interval linked completely to a
visible footage journey. Camera GPS distance/speed rollups remained provisional
while analysis was incomplete.

## Prioritized implementation findings

| Priority | Confirmed issue | Implemented change | Verification |
| --- | --- | --- | --- |
| P1 | Busy live polling can starve every fault-code scan. | Reserve an occasional primary-only cycle for one bounded read-only diagnostic step, after persisting the sample. Keep speed/RPM/load, defer rotating PIDs, retain ordinary spare-budget scans, and label the new producer plan v6. | Fake busy-drive regression reproduces zero old-policy scans and exercises readiness and confirmed/pending/permanent code progression; Android CI required. |
| P1 | An old backup's delayed history write could clear a newer run's live ownership and duplicate history; history also assigned its start timestamp at completion. | Keep the start and release state with the claiming task so late cleanup cannot finish a successor; persist the original run start through completion and cancellation. | Five real-database timestamp/overlap regressions and 280 focused ingestion tests. |
| P2 | An empty code list hid the difference between no captured codes and no returned codes. | Show observed lamp, highest observed stored-fault count, captured codes and scan evidence separately; disclose filtered/paginated/downsampled evidence. | Six functional frontend regressions. |
| P2 | A producer upgrade could relabel an older, unexported drive with the current polling plan or reject a valid ready archive. | Persist the producer plan per drive through an additive migration; export recorded provenance and continue validating supported older archives without rewriting them. Preserve historical recovery directories and use atomic retirement for superseded snapshots. | Migration, immutable archive, interrupted handoff/retirement and exporter regressions; Android CI required. |
| P2 | Final copied files can remain settling until the next 30-minute scan. | Coalesce at most three follow-up scans after the settling delay in the existing scheduler; respect scanner/processing settings and stop on errors. | 17 focused scheduler regressions and broader compatibility tests. |
| P2 | Slow or failed CarPlay presence work can overlap or suppress useful retries. | Keep one in-flight arm/recovery operation per address, retry failures sooner, start successful cooldowns at completion, bound recovery through storage, and await cancellation cleanup. Sanitize background failure reporting. | Nine lifecycle regressions, including real SQLite rollback preservation; 89 passed and two platform skips in the related suite. |
| P2 | Plate editor drafts/errors/page state can cross entity navigation, and late mutations can discard a newer editor or leave merged sightings stale. | Key detail state by validated entity ID; guard navigation after unmount; cancel old source/target sighting reads and refresh both affected caches. | Real renderer regressions for navigation, late corrections/merges and loaded/pending target caches. |
| P3 | Invalid detail IDs can issue meaningless API requests, and unchanged map bounds can reset user positioning. | Validate plate/journey/recording IDs before mounting queries; fit route maps using geographic bounds rather than array identity. | Invalid-link and viewport regressions within 28 detail-state tests. |

The diagnostic reservation deliberately trades a small number of rotating PID
observations for fault-scan progress. Coverage continues to count those observations
as missing rather than inventing values. Actual ECU response timing and restored
fault-code capture require a drive with the updated APK; this review does not claim
physical-device validation of that change or identify the existing fault code.

## Validation and remaining limits

Frontend: 165 tests passed, plus typecheck, ESLint and production build. Backend:
2,808 tests passed and 32 platform/environment tests skipped on Windows. The later
history/ownership fix additionally passed its 280-test focused ingestion suite.
Ruff lint and formatting passed across the project.
The initial backend run's two environment issues (missing `paho-mqtt` and an older
Ruff version) were corrected, and both affected test modules then passed.

Transient SQLite writer contention still occurred on the running server. The
existing retry/requeue path preserved progress without the previously masked
`PendingRollbackError` or `MissingGreenlet` signatures. This review does not claim
that all database contention has been eliminated. Historical NFS/GPU findings
remain subject to the limits documented in the earlier application audit.
The last OBD event mirror attempt at 12:12:03 UTC reported `storage_busy` and was
inactive with zero skipped attempts. Earlier attempts imported new events with no
sequence gap. A successful later retry was not observed before the car went offline;
the verified drive bundle is stored separately from this event mirror.

No server deployment, APK installation, code clearing or direct adapter command
was performed as part of these implementation changes. Unrelated working-tree
documents and changes were excluded from the commit.
