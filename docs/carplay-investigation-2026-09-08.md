# Head-unit update and directional CarPlay investigation

## Result and current boundary

The companion upgrade is installed and verified: **0.3.1, version code 14**. It fixes the
confirmed installed networking defects, removes the embedded credential default, masks
the configuration field, validates the destination and reports redacted failure reasons.
Its certificate matches the APK captured from the head unit. Installation completed at
**12:38:30 Adelaide / 03:08:30 UTC on 8 September** using a data-preserving package upgrade.
The operator clarified that the iPhone was absent; there was no reported connection
failure to reproduce. Foreground-service and stored-data checks passed. Actual CarPlay
playback and a natural webhook dispatch remain untested.

The server/frontend also have local CarPlay diagnostics and backup-history fixes. Nothing
has been committed, pushed or deployed to the server. Existing audit edits and the original
`agent-handover.md` have been preserved.

Today's existing context measurements support a difference between the two driving periods, but
**do not yet establish the cause of the reported CarPlay lag**. The strongest directional
differences are startup load and the hotspot frequency chosen for each session. An active
backup during departure is not supported by the server log. **Live parent/owner mapping
proved that both old sampled layers, #101 and #104, belong to the camera recorder. Their
frame timings cannot be used as evidence of CarPlay rendering performance.** The local
sampler now excludes these anonymous surfaces and selects the mapped ZLink app buffer.

## What was inspected

- Existing head-unit audit evidence, captured APK, and a new parked live device session.
- Matching source for companion, ingest/ignition/radio recovery, timing sampler, log
  retention/transport, API and diagnostics UI.
- Authenticated read-only server routes: bounded OBD drive/event lists, ingest status and
  history, filtered server/unit logs, thermal CPU log samples, and 24/72-hour timing windows.
  Credentials remained outside URLs and source files. No backup, rescan, notification or
  radio-control route was called.
- Official Android documentation/source for Wi-Fi concurrency, low-latency mode,
  TextureView composition, thermal status and MediaCodec cancellation.

The timing endpoint returned 328 samples across 29 per-surface minute buckets today;
72 hours contained 1,217 samples. Private response copies are outside the repository under
the temporary directory `dashcam-carplay-audit-20260908`. The operator was asked to confirm
the approximate trip times, wireless connection and whether the symptom was visual,
touch-response or audio lag. The following labels are based on the two latest stored OBD
drives and their order; that confirmation is still pending.

## Comparing today's latest two drives

Times are Adelaide local time (UTC+09:30), 8 September 2026:

| Observation | Outbound candidate | Return candidate |
| --- | --- | --- |
| OBD drive interval | 11:13:13–11:19:01 | 11:25:59–11:33:30 |
| Drive duration used for comparison | 348 s | 451 s |
| Surface #101 samples / summed intervals | 62 / 285.3 s | 90 / 409.7 s |
| Surface #101 interval-weighted cadence | 24.69 / s | 25.52 / s |
| Surface #101 maximum observed hold | **247 ms** | **88.3 ms** |
| Surface #104 samples / summed intervals | 63 / 300.4 s | 91 / 430.5 s |
| Surface #104 interval-weighted cadence | 21.76 / s | 22.31 / s |
| Surface #104 maximum observed hold | 194.0 ms | 194.1 ms |
| Sampled SoC temperature | 82.5–85.1 C | 87.4–98.1 C |
| Sampled load average | 20.99–48.27 | 18.69–21.53 |
| Median reported ZLink CPU | 57% of one core | 55.5% of one core |
| Median reported companion CPU | 3% of one core | 2% of one core |
| Reported hotspot frequency | 5240 MHz | 5180 MHz, then 5240 on arrival |
| Sampled aggregate netdev drops/errors | 0 | 0 |

Cadence above is `sum(new_frames) / sum(span_s)`. The live hierarchy identifies #101 and
#104 in this boot as **camera-recorder layers, not CarPlay video**. The table retains the
old measurements to document why their earlier interpretation was rejected. Counts do not include all
drive seconds: the first outbound timing arrives about 46 seconds after the OBD drive
starts, and the first return timing about 21 seconds after its start. Missing first moments
are particularly relevant to a departure complaint. Context is sampled more slowly than
surface timing. Distinct surfaces were never pooled into one frame-rate claim.

Surface #101 had its worst outbound hold early, around 11:14. Its per-minute maximum was
about 106 ms at 11:15 and 88 ms in later minutes. #104 retained a roughly 194 ms worst hold
in both periods. This is a more nuanced result than saying all surfaces were smooth on
the return.

At the first outbound sample, STA and AP both reported **5240 MHz**, with STA RSSI -91 dBm.
STA then disappeared from the available context while AP remained 5240 MHz. The return
mainly used AP 5180 MHz with no STA frequency; both reported 5240 MHz again near home.
Thus the outbound session retained a different AP frequency after losing the home link.
That is an observed asymmetry, not proof that either channel caused latency.

## Testing the competing explanations

### Backup interference: not supported during this departure

Server logs at 11:13:22 and 11:13:52 explicitly held the automatic backup because uptime
was only 60 and 87 seconds. The sampler armed at 11:13:23. The first logged arrival/pull
was on return at 11:33:18, and the previously inspected radio transition began at 11:33:27.
No radio-transition message appeared in the bounded matching departure history.

Source independently gates normal copying on known ACC-off. It can reconcile an old
pending radio restore before the departure uptime hold, which is a plausible general
home-only interaction; there is no matching evidence for that interaction today. Safety
recovery was not disabled to test this theory.

The stale ingest-history table is not reliable negative evidence. Its newest row was
September 5 even though today's memory status, radio row and companion journal showed a
72-file return backup. Source omitted material continuation runs and silently swallowed
history-write exceptions. Both defects are fixed locally. This does not retrospectively
recover the exception or prove which path omitted today's particular row.

### Startup contention and Wi-Fi session setup: leading candidates, unproven

The outbound unit had just booted and showed much higher initial load; the return session
had a settled system. Load includes runnable and uninterruptible work and cannot identify
whether CPU, storage, startup services or another process delayed video. A per-process
startup/decoder trace is needed to attribute that pressure.

Home departure also begins with a weak home STA association and a CarPlay AP on its
frequency, while leaving elsewhere starts without that STA association. AOSP documents
that some chipsets time-share when concurrent STA/AP interfaces use different channels,
and that this can hurt performance. However, the first observed home STA/AP frequencies
were equal. The old UI's assertion that merely seeing both roles proved channel hopping
was wrong and has been removed. See [AOSP STA/AP concurrency](https://source.android.com/docs/core/connect/wifi-sta-ap-concurrency).

No Wi-Fi disconnection, forced reassociation, hotspot reset or automatic ZLink restart
was added. Such changes could sever CarPlay or home recovery, and there is no validated
vendor contract yet for a corrective action. Android's low-latency Wi-Fi mode also requires
specific foreground/network/platform conditions and vendor support; a background
companion lock is not an established fix for ZLink's SoftAP path. See
[AOSP low-latency mode](https://source.android.com/docs/core/connect/wifi-low-latency).

### Thermal pressure: present in logs, does not explain the direction alone

The return was hotter despite the operator reporting it felt better. CPU-type thermal
log rows reported status 4 in all 20 sampled outbound records and all 30 return records.
Android defines 4 as critical thermal status. These are the vendor service's reports;
the complete thermal configuration and whether this vendor reports the value accurately
remain unverified. The parked live service subsequently reported status 4 with no override,
HAL ready, SoC about 86 C and GPU about 82 C. Both CPU policies exposed current, minimum,
maximum and hardware maximum frequency of 1,612,000 kHz; those values alone do not prove
or exclude vendor throttling. Neither hotter temperatures on a smoother
drive nor weak historic correlations rule out throttling. See
[Android thermal status](https://developer.android.com/reference/android/os/PowerManager#THERMAL_STATUS_CRITICAL).

No thermal policy, clock limit or protection was changed.

### Decoder messages: teardown evidence, not a demonstrated driving fault

ZLink logged `Pending dequeue output buffer request cancelled` at 11:19:22 and 11:33:43,
near the end of each period. Both stack traces include `MediaCodec.dequeueOutputBuffer`
and `SimpleTextureViewVideoDecoder.kt`. Android's MediaCodec source emits that message
when pending dequeue operations are cancelled; its presence at both endings does not
establish a mid-drive decode failure. See
[AOSP MediaCodec cancellation](https://android.googlesource.com/platform/frameworks/av/+/c1408e601dc91edab856b567d0c2d9163392b94b/media/libstagefright/MediaCodec.cpp).

ZLink's updater also logged a DNS lookup failure while away at 11:23:46. That is an
updater/network observation between drives, not evidence that the local CarPlay video
transport needed that internet host or stalled because of it.

The TextureView class name changes the measurement plan. TextureView is composited into
the application window rather than exposing the separate window used by SurfaceView.
The live dump confirmed the old anonymous layers were watching another component:
`#101 -> #100 -> #99 -> #98 -> com.zqc.camera#96` and
`#104 -> #103 -> #99 -> #98 -> com.zqc.camera#96`. Both camera buffers were 1920x1080.
The ZLink application buffer was its package/activity-named layer #255, with owner UID
10077, owner PID matching ZLink, and a 720x1920 RGBA buffer. The corrected selector
returned only that layer when executed on Android. It excludes anonymous surfaces and
ZLink container/input-sink layers. This establishes app-window ownership, not that every
presented frame is a decoded CarPlay frame; no phone was attached. See
[Android TextureView](https://developer.android.com/reference/android/view/TextureView).

## Changes implemented

### Android companion 0.3.1

- Required INTERNET permission and LAN HTTP policy, with shared validation at settings
  entry and dispatch; HTTPS remains supported.
- No embedded API key; explicitly saved credentials survive an upgrade and are masked
  in the UI. Removing the embedded default does not revoke the previously exposed key.
- Bound URL/key lengths, require the webhook path and a key when enabled, reject query
  strings, URL user-info/fragments and unsupported destinations; redirects remain disabled.
- Distinguish authentication rejection, client/server responses, timeout/network/security
  failure and invalid configuration using fixed reason codes. Do not log URL, key,
  response body or exception text. Existing bounded timeouts remain.
- Preserve telemetry databases, exported bundles, preferences, ownership and schema paths.

Signed artifact: `android/obd-logger/app/build/outputs/apk/release/app-release.apk`.
SHA-256: `f43449e58263f9c50dbbbe60590529a13c8dcfd4673b1ad33c158feb9ca94e4a`.
The certificate matches the captured installed APK; a matching signature is required for
a data-preserving package upgrade. No uninstall or data-clear is planned.

### CarPlay observability

- Bound and rotate direct sampler history independently of noisy logcat; recover retained
  modern observations non-destructively when the existing ingestion status confirms
  ignition off, even if logcat evicted them. Departure arming stays lightweight.
- Give each emitted observation a unique identity shared by file and logcat. Deduplicate
  across both transports. Legacy direct-file lines without an identity are deliberately
  not automatically imported to avoid doubling historical measurements.
- Distinguish sampler lifetimes and preserve raw aggregate neighbour states, ZLink process
  presence, radio-frequency changes, no candidate surface and no new presented frames.
  A neighbour is not automatically labelled as a connected CarPlay phone.
- Use actual elapsed time for bitrate; reset counters on missing/recreated interfaces or
  processes; handle a reset SurfaceFlinger timestamp baseline.
- Select the mapped package/activity buffer naming pattern and exclude anonymous
  SurfaceViews and window containers. Fail closed if the vendor naming changes; retain
  historical unattributed samples with their caveat. Store kind and numeric layer ID
  without private window titles.
- Expose events and sessions through the existing timing API, and keep minute aggregation
  separated by capture session as well as layer.
- UI selects observed periods, preserves chart gaps, keeps unknown metrics unknown, shows
  maximum holds and sampled duration, and explains measurement/ownership limits accurately.

Direct-file recovery is bounded to 512 KiB per retained generation and 20,000 parsed rows.
The script rotates after a context pass threshold; current plus six older files bounds
normal retention to roughly 3.5 MiB plus one pass of overshoot. This does not promise
unlimited drive retention. A genuine reboot away from the server can still end the shell
sampler; the companion is not granted privileged global tracing access by these changes.

### Backup history

Persist all material completed attempts, including continuation runs. Keep notifications
once per visit. Log history-write failures explicitly without converting a safely
completed media transfer into an apparent transfer failure.

## Validation and remaining live work

Android: **124 tests passed**, no failures/skips; debug/release lint and assembly passed.
APK signature/certificate and packaged INTERNET declaration verified locally.

CarPlay/unit-log focused tests: **74 passed** after the live mapping correction, including transport deduplication, bounded
parked recovery, observations, session separation and execution of the shipped AWK
against unchanged, overlapping and reset frame rings, and the shipped selector against
camera buffers, ZLink containers and the real app buffer name. Frontend regressions: **5 passed**
for unknown values, separated periods, chart gaps, same-channel wording and capture periods
that contain only missing-surface events. Frontend type
checking, lint and production build passed; the real app rendered synthetic periods,
unknown temperature and capture events in an isolated local preview without schedulers.
Git Bash validated sampler shell syntax. The layer-selector AWK also ran successfully on
the live Android unit. The complete new sampler has not been deployed or drive-tested.
Final pre-commit backend validation including the corrected selector:
**1,745 passed, 13 skipped**, 1,411 deprecation warnings in 93.51 seconds. Ruff lint and
format checks and frontend typecheck/lint/build plus all five regressions passed again.
The operator subsequently authorised an unsigned commit and push to main; this does not
verify server deployment or CarPlay playback.
The temporary preview server has been stopped. Full test output is in the local temporary
file `dashcam-precommit-tests-parallel.txt` (earlier run: `dashcam-carplay-final-tests.txt`).

## Installed-device verification

The unit automatically transferred 38 files while parked. Its radio transition completed
at 12:35:24 local with Bluetooth/hotspot restoration and OBD resume all verified. The
upgrade was performed only after that transition finished.

Before installation, the existing APK and 18 accessible exported OBD files were retained
privately under the local temporary directory `dashcam-install-20260908`. The old APK hash
matched the earlier capture. This is an exported-data backup, not a claim of access to the
app's private database/preferences. No uninstall, data clear or radio reset was performed.

The installed APK SHA-256 exactly matched the artifact above. Package manager reported
0.3.1/code 14 and INTERNET granted. The foreground service restarted automatically. Fresh
status reported `parked`, ownership enabled, ACC off, ingestion hold false, no pending
bundles and no error. The last-drive identifier was retained and all 16 existing verified
receipt files remained byte-identical. Saved webhook credentials were not extracted;
actual webhook authentication and next-drive ECU polling remain natural-use checks.

Remaining work, with the vehicle parked and the operator's phone connected:

1. Confirm the mapped ZLink window during actual CarPlay playback and capture symptom
   timestamps, connection state, startup pressure and AP frequency.
2. Verify the next natural ignition transition and webhook dispatch without triggering a
   backup solely as a credential test.
3. Coordinate sampler/server rollout: the currently deployed server periodically re-arms
   its bundled script, so copying a newer script alone is not a durable deployment.
   Server deployment remains unperformed under the original review boundary.
4. Compare the next natural home departure and away departure with the same phone/app,
   symptom timestamps, mapped ZLink window, startup load and AP frequency. Only then test
   a reversible targeted mitigation supported by the observed vendor controls.

No promise is made that a companion update alone fixes the vendor recorder's irregular
capture cadence, proprietary ZLink decoder behaviour, thermal hardware limits or all
wireless interference. Those are separate findings from the confirmed companion defects.
