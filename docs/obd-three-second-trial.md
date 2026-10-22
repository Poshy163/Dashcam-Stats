# Three-second driving and fuel trial

Companion 0.3.12 adds an opt-in polling experiment. It does not enable a live server
stream: completed drive bundles still transfer through the normal backup path.
Install the matching server before enabling the companion trial, because earlier
servers reject the new timing metadata and poll-plan version.

## Priorities and timing

Poll plan 8 targets a fresh speed, RPM and mass-air-flow (MAF) request in each
three-second cycle. MAF supplies the existing estimated fuel rate. Load has a
fifteen-second target; the remaining dynamic auxiliary PIDs target sixty seconds.
Adapter-voltage checks have a five-second elapsed-time deadline, served at the next
cycle (normally six seconds on the trial), and retain the engine-stop safeguards.
Optional requests use measured remaining time, with fair
overdue selection rather than a catch-up burst. A target is not a promise that an
adapter or ECU can complete the work; missed readings and overruns remain visible.
Sparse diagnostic steps use a bounded reservation after fifteen seconds and then
at least sixty-five seconds between steps. This can make a full diagnostic scan
slower; a reserved step can exceed the nominal row target and remains visible.

The option starts disabled. Normal operation continues to use plan 7's five-second
target and nominal fifteen-second MAF rotation. Enabling or disabling the option
selects a plan at a normal drive boundary; an active drive never silently changes
its cadence contract.

## Response timestamps and calculations

New samples can include `quality.pid_response_age_ms`, a bounded map from uppercase
two-digit hexadecimal PIDs to integer milliseconds. An age describes the interval
from a successfully decoded reply to the row's monotonic-clock-derived UTC time.
It is a response-completion time, not evidence of when the ECU measured its sensor.
Only successful, present readings receive an age. Plan 8 requires this metadata;
older samples without it retain their original row-time interpretation.
If a long pause makes a reply older than the bounded timing contract permits, its
fields are omitted and marked missing. Fresh readings in the same partial row are
retained, and fuel estimates are recomputed from the retained inputs; ages are
never clamped to make an old reading appear newer.

Speed reply times determine distance intervals and the web page's acceleration.
RPM bands use RPM reply times. Fuel integration uses MAF reply times, independent
row-continuity and MAF-hold limits, and the existing missing-read, sequence and
transport boundaries. Missing data is never filled to make a faster curve. Both
the on-device exporter and server calculations follow the same rules.

Faster sampling improves temporal detail. Speed remains an integer-km/h ECU
reading, and acceleration is an average between speed observations. Fuel remains
the existing MAF-based gasoline estimate, not an ECU fuel counter or a calibrated
measurement. Increasing its frequency does not remove the model's assumptions.

## Fallback and rollback

Three consecutive completed cycles exceeding three seconds, or three consecutive
cycles with genuinely failed critical requests, disable the trial and record a
bounded reason. A malformed critical reply retains the existing elapsed-time
cooldown and receives a chance to make its scheduled plain-request retry. Those
intentional cooldown slots remain missing in the exported data; they freeze the
missing-request counter instead of pretending to be additional failed requests.
Other critical failures still advance that counter during the cooldown. The
recovery allowance expires fifteen seconds after the first completed row with the
critical reply absent;
another malformed reply cannot extend it. A missing timing proof or an unexplained
cooldown does not receive this allowance. A successful retained reply clears that
PID's recovery deadline. The three-second work-overrun limit is unchanged.
An uncertain transport failure also disables it before the safe reconnection path.
The trial segment is retained as interrupted. A subsequent safe
connection uses plan 7, keeping its original timing contract. A transport fault
also preserves the partial observations before normal reconnection. Deliberate
ingestion quiesce, service cancellation and an ordinary engine stop do not count
as failed trial evidence. Confirmed engine-off voltage and stopped/missing RPM
retain the normal shutdown grace instead of treating ordinary shutdown as a trial
failure. An operator can explicitly re-arm the option; saving unrelated settings
cannot re-arm a fallback that happened after the settings form was opened.

The trial does not change read-only serial ownership, response validation, parked
ECU gating, recording, thermal limits or backup safety. Recovery and cooldown
durations are interpreted in elapsed time rather than accidentally shortened by
the faster cycle. Old poll plans and bundles remain readable with their original
cadence and calculation behavior.

Auxiliary coverage uses elapsed-time nominal opportunities and retained successful
observations. Exact modulo-cycle missing-streak counts are not known for this
schedule and are shown as unavailable; the older plans retain those counts.

## Deployment and physical acceptance

1. Deploy the matching server and verify its revision and health.
2. Install the production-signed 0.3.13 update in place, retaining configuration and
   storage permissions. Verify the pinned signing certificate before installation.
3. Explicitly enable the trial, then use a normal drive. No accessory-power hold
   is needed for a driving test.
4. Inspect actual fresh-reading intervals, MAF coverage, missing readings,
   overruns, fallback reasons, temperature and both native recording streams.
5. Verify the exported bundle, server totals and web graphs agree. A successful
   build or import does not establish sustained physical polling performance.

The first physical trial on 22 October 2026 recorded seventeen rows over 57.311
seconds before falling back to plan 7. Speed and RPM succeeded on every request;
MAF had fourteen successful requests, one malformed reply and two intentional
cooldown skips. The old row-based failure counter stopped the trial before its
plain MAF retry. The follow-up recovery tests replay those retained row times and
verify that a real retry is allowed, that rejected and skipped readings stay
missing, and that failed retries, recovery expiry, overruns and transport faults
still stop the trial. The three subsequent baseline segments imported correctly.
All four camera-exported summaries matched the server's distance and fuel totals
within floating-point rounding. Sustained three-second performance remains
unverified; the short initial segment does not establish it.

Automated validation covers timing skew, changed row delays, missing MAF, sequence
and transport gaps, cadence-specific limits, overload fallback and unchanged
legacy behavior. Sustained three-second performance remains unverified until the
new package has completed a real drive.

Earlier 0.3.12 validation on 22 October 2026 passed 274 Android JVM tests, 3,320 backend
tests (33 platform/fixture skips) and 229 frontend tests. Android debug lint and
assembly, frontend typechecking/lint/production build, and Python Ruff checks and
formatting passed. The web view was also checked with clearly labelled synthetic
data: missing replies remain unavailable, per-PID times differ from row times,
and a reply at 9.8 seconds saved in a row at 12 seconds remains visible in a window
ending at 10 seconds. These checks are separate from APK installation and physical
camera/ECU acceptance.
