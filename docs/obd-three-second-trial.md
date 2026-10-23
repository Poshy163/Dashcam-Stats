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
2. Install the production-signed 0.3.14 update in place, retaining configuration and
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

## 23 October findings and 0.3.15 follow-up

The 0.3.14 trial recorded 14 rows over 46.410 seconds. All 42 critical requests
succeeded. Median reply intervals were speed 3.090 s, RPM 3.078 s and MAF 3.085 s,
but five cycles exceeded the target and the final three consecutive overruns caused
`sustained_overrun` fallback. Subsequent segments used plan 7. Two engine-load request
timeouts then separated moving observations by 35.069 s and 24.601 s; the zero
within-segment gap counts did not describe those recording boundaries.

The 0.3.15 source removes redundant control-file cleanup from active polling while
still checking for a new handoff request at each boundary. It computes expensive
rolling metric snapshots only when a status write is due. A timed-out optional PID
remains missing and enters cooldown; polling continues only after bounded strict
adapter-local prompt synchronization discards the late original response. Failure
to prove synchronization still taints and reconnects the transport. Critical PID
timeouts retain their existing reconnect behavior. The trial is not automatically
re-armed and the three-second overrun protection remains active.

The abort character is a space, not a carriage return: the latter can repeat a
completed command when its prompt was merely delayed. This follows the adapter's
[documented communication behavior](https://www.elmelectronics.com/wp-content/uploads/2017/01/ELM327DS.pdf#page=9).
Strict independent ATE0/OK and quiet-boundary checks remain required; documented
behavior alone does not establish that a particular adapter clone will recover.

Poll timing schema 2 reports exclusive critical-request, auxiliary-request, handoff,
persistence, diagnostic, status-publish and residual-work distributions. They account
for recorded cycle work and exclude cadence sleep; diagnostic-window flush overhead
after cycle accounting is not independently attributed to those stages. Deploy the
matching server before installing 0.3.15; old schema-1 windows remain readable.

The API and drive UI now show short, evidence-based gaps between adjacent interrupted
recordings separately from within-recording gaps. Logger and vehicle identities must
match, a compatible unclean interruption must precede the next segment, and intervals
must exceed 1.5 times the larger nominal cadence but not 120 seconds. Known shutdown,
deliberate backup, overlap and missing identity do not create reconnect evidence.
Moving observations on both sides are labelled likely reconnect; other admissible
boundaries remain explicitly unconfirmed. Neither distance nor fuel is filled across
the interval. Camera-reported missing time remains separately labelled, since its
definition can differ by exporter version and old discarded values cannot be recovered
from the derived projection alone.

### 0.3.15 physical result and 0.3.16 MAF retry

The two 23 October 0.3.15 drives sustained plan 8 for 18m46s/369 rows and
25m27s/499 rows. Median fresh speed/RPM/MAF intervals were 3.02-3.03 seconds;
no trial fallback, command timeout or connection loss occurred. All 868 samples
persisted. Four malformed MAF requests per drive still caused 12-15-second gaps:
the first malformed-PID backoff used two five-second buckets, independent of the
three-second row cadence. The rejected response bytes were not retained, so these
captures do not establish a specific checksum, framing or parser defect.

Companion 0.3.16 opts only plan-8 MAF into a single next-scheduled-opportunity retry.
The row sequence prevents a second command in the failed row even when consecutive
rows share a five-second bucket. A first refused reply remains missing and uses the
existing plain-syntax path; a repeated refusal returns to the existing exponential
backoff. Success resets the breaker. ISO source/mode/PID/length/checksum validation,
strict transport ownership, the 15-second health bound and sustained-overrun fallback
are unchanged. There is no retry of an uncertain critical timeout and no extra
same-cycle request. A successful next-row retry still leaves an honest roughly
six-second interval between valid MAF readings; hardware acceptance needs a new drive.

Earlier 0.3.12 validation on 22 October 2026 passed 274 Android JVM tests, 3,320 backend
tests (33 platform/fixture skips) and 229 frontend tests. Android debug lint and
assembly, frontend typechecking/lint/production build, and Python Ruff checks and
formatting passed. The web view was also checked with clearly labelled synthetic
data: missing replies remain unavailable, per-PID times differ from row times,
and a reply at 9.8 seconds saved in a row at 12 seconds remains visible in a window
ending at 10 seconds. These checks are separate from APK installation and physical
camera/ECU acceptance.
