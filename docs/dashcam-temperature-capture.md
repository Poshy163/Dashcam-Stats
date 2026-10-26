# Dashcam temperature capture

Companion 0.3.20 adds a small SoC temperature collector to the existing logger
foreground service. The collector starts after foreground admission, including the
service's normal boot, package-update and process-recovery paths. It operates
independently of the OBD polling worker and ingestion handoff. The logger must be
enabled and have its required permissions. Android force-stop or disabling the
service stops capture until the service can run again.

The collector reads the `soc-thmzone` sensor every 30 seconds while the device
naturally runs. It does not acquire a wake lock, schedule wake alarms, alter radios,
extend the native countdown, or change recording. Sleep and powered-off intervals
remain gaps. Failed sensor reads are unavailable observations, never zero degrees.
The decoder's 95/100-degree policy is independent of this history.

## Local history and publication

The app retains at most 8,192 observations in its private no-backup directory,
split into at most 32 chunks of 256 observations. This is approximately 68 hours
of continuous awake capture. The active chunk is replaced atomically, rather than
rewriting all retained history for each observation. Retention eventually removes
the oldest chunks; it does not claim that missing history was backed up.

A read-only mirror is published below the existing public OBD directory:

```text
temperature/manifest.json
temperature/samples-<first_sequence>.json
```

The schema-version-one manifest names source `companion_temperature_v1`, sensor
`soc-thmzone`, the 30,000 ms interval, generation time, collector state, retained
count, latest observation and up to 32 chunk descriptors. Each descriptor contains
a restricted basename, first/last sequence, byte length and SHA-256. A manifest is
published only after its referenced mirror chunks have been written. It is bounded
to 16 KiB; each chunk is bounded to 128 KiB.

Each reading contains a positive sequence, random process-session identifier,
UTC capture time, elapsed realtime, integer millidegrees Celsius or null, and
quality `valid` or `unavailable`. Session identifiers are not kernel boot IDs.
Sequence/history survive ordinary service restarts and package updates. Capture
or storage failures have bounded categories; raw paths and exception text are not
public diagnostics. Independent ownership prevents overlapping collectors if an
older operation has not finished during service teardown.

An unavailable device clock is reported separately. Raw local observations retain
their stated timestamps, but the server does not plot captures with untrustworthy
dates or invent replacement UTC times. Valid readings in the same chunk can still
be imported after the clock recovers. A genuinely blocked filesystem operation is
not forcefully duplicated; stale capture evidence remains unknown until it resolves
or the process recovers.

The optional `temperature_capture` status block reports the collector state, last
capture time and elapsed time, retained count, interval and a bounded error code.
It grants no ignition, radio, wake or sleep authority. It is distinct from the
OBD ingestion handoff and its `resume_verified` evidence.

## Import and graph

While the camera is known to be parked, the server checks the new mirror during
normal presence polling and active footage backup. It reads changed chunks within
bounded work and time budgets, verifies their lengths and hashes, validates their
schema, and stores observations idempotently. A failed read or changed snapshot
is retried without stopping footage transfer. It never truncates or deletes the
camera's journal to acknowledge import.

The temperature history combines these readings with existing retained CarPlay
diagnostics. Companion readings take precedence where the two sources overlap.
UTC capture time drives the graph; receipt/import time is separate. Session changes,
unavailable readings and observation gaps remain visible rather than being filled
with interpolated temperatures. Chart aggregation can omit intermediate points;
distances between displayed extrema alone do not prove capture stopped.

The heavier CarPlay diagnostic sampler remains server-deployed. This change does
not turn that sampler into a boot service or restart it during bulk transfer.
Temperature capture no longer depends on its process being alive. Older companions
continue using the existing retained diagnostic history.

## Physical acceptance

Check the production signature and update in place, preserving the application
identity, permissions, ownership, immutable drive exports and original native OFF
bounds. Deploy the corresponding server before expecting the new history in its
graph. Do not interrupt an active ingestion lease to install the APK.

Verify a fresh companion status and valid sensor reading under the real application
UID, increasing chunk sequences, durable history across service recovery, and
server import of those exact readings. Confirm capture continues while the OBD
worker is quiesced for a real backup and both camera streams continue recording.
Check that native sleep still ends capture normally. A physical kernel reboot
acceptance check is separate from a package-update or service-recovery check;
do not claim it passed based only on source tests or a new APK installation.
