# Dashcam temperature history

The Backup page includes a **Dashcam temperature** graph for the camera processor
(SoC), separate from the engine coolant and intake-air measurements supplied by OBD.
It offers 1-hour, 6-hour, 24-hour and 7-day views, the latest captured observation,
and the selected range's minimum and maximum.

The graph reads retained on-device timing captures already copied to the server.
It includes parked heartbeat records as well as records associated with CarPlay
surfaces. Repeated surface context is deduplicated; downsampling retains temperature
peaks. Browser refreshes query stored data and do not start a device capture or add
OBD requests.

## Coverage and gaps

This is available capture history, not a continuous temperature monitor. The timing
sampler can keep recording after the camera leaves home Wi-Fi, but it must already
have been armed. It does not start itself after an offline cold boot. Existing unit
log retention is bounded, so selecting seven days does not guarantee seven days of
observations. Missing data cannot be reconstructed.

Missing, invalid, separated capture sessions and long intervals break the line.
Unavailable readings are never displayed as zero. Truncated results are identified;
the chart does not connect lines across observations omitted by the response limit.
Capture timestamps determine the horizontal position, rather than the time the
server received the logs. A stale latest observation remains historical evidence,
not a current camera-temperature claim.

The two dashed policy-reference lines show companion 0.3.14's **95 C start/restart**
threshold and **100 C running-analysis stop**. Earlier companions used 85 C. The legend
identifies the policy version; it does not claim that historical observations were
captured under the new limits or that the latest companion is installed. These are
software policy references, not manufacturer temperature ratings. Readings above them
remain visible, including the approximately 107 C measured on 22 October. Graphing does
not change the camera's thermal settings.

## OBD polling assessment

Poll plan 7 targets five seconds. The latest eight-minute drive had typical start
intervals around 5.1 seconds and typical cycle work around 4.3 seconds. Individual
serial PID requests usually took about 0.6–0.7 seconds. Speed, RPM and engine load,
plus the adapter-voltage read, precede optional sensors.

Faster core readings are possible only with a different command budget and coverage
tradeoff. Merely reducing the target cannot make the same complete query set fit.
A four-second core target would be 25% faster nominally, but can defer substantially
more optional readings. Offline replay of the existing admission formula at 610 ms
per serial request admitted three optional reads per five-second cycle and only one
per four-second cycle. At 850 ms, the corresponding counts were one and zero. The
four-second replay therefore cannot preserve current optional coverage; it is not a
verified improvement on the camera. A one- or two-second full cycle is unsupported
by the measured workload.

A faster plan requires an explicit new plan version, compatible timing/coverage
validation for old bundles, scheduler and slow-command tests, and comparison during
a normal drive. This graph change retains plan 7 and its five-second target, strict
reply validation, serial command ownership and parked polling policy.

The later 0.3.12 implementation adds a separate opt-in three-second plan for speed,
RPM and MAF with reduced auxiliary cadence, response timestamps and a recorded
fallback. It leaves the graph's read-only data path and the default five-second
plan unchanged. See [obd-three-second-trial.md](obd-three-second-trial.md); sustained
physical performance is still a separate acceptance step.
