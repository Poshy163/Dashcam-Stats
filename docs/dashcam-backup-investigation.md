# Dashcam → NAS backup: transport, progress and recording-pause investigation

> **Device facts now live in [`head-unit-reference.md`](head-unit-reference.md)** — properties, storage layout, shell permissions, the ignition/sleep window and the benchmark results, captured from the live unit so this work can continue while the car is off the network.

Research only. No device was probed and no device-control code was written. Everything below
is either read out of this repository or validated against AOSP / toybox / platform-tools
sources, with the unverified parts explicitly marked as questions for the device-probing agent.

---

## 0. Correction to the brief

> "At the moment, the application connects to the Android dashcam using ADB and copies footage
> from the dashcam to the NAS."

That is no longer what the code does, and the difference matters for everything that follows.

ADB is used **only as a control channel** — connect, ask state, resolve the card path, list the
card, launch a listener, delete verified files. The recordings do not pass through `adbd` at
all. The bulk path is:

```
adb shell "cd <card> && tar c F1 F2 … | timeout 180 nc -l -p 9000"      (on the unit)
        ↓  plain TCP, port 9000
Python tarfile in streaming "r|" mode → <footage>/.ingest_staging/     (in the container)
        ↓  size check + atomic rename
<footage>/
```

The move off `adb pull` already happened, and the measured numbers are recorded in
[`backend/app/ingest/adb.py`](../backend/app/ingest/adb.py) and the README:

| Path | Measured |
| --- | --- |
| `adb pull`, 4 parallel streams | 4.0 MB/s |
| `adb exec-out`, single | 8.3 MB/s |
| `adb exec-out -P8` | 10.0 MB/s |
| **`tar` over a plain TCP socket (current)** | **34.3 MB/s** |
| TF card sequential read, *both lenses recording* | 60 MB/s |
| WiFi link, estimated capability | ~50 MB/s |

So the question is not "should we stop using `adb pull`". It is **"is 34.3 MB/s close to the
physical ceiling, and if so where is the remaining time actually going?"** — and my answer is
that the data path has modest headroom at best, while the *startup latency* and the *window
length* have a lot.

Device on record: **Unisoc UIS7861, Android 15, not rooted**, no battery, on the network only
while the engine runs — roughly a **60–120 second window**.

---

## 1. How the backup currently works

### Components

| File | Role |
| --- | --- |
| [`ingest/poller.py`](../backend/app/ingest/poller.py) | Presence tick, own loop (not the shared scheduler, whose 30 s floor would eat a third of the window) |
| [`ingest/adb.py`](../backend/app/ingest/adb.py) | Control channel only: connect, state, source probe, inventory, listener launch/kill, delete |
| [`ingest/transport.py`](../backend/app/ingest/transport.py) | The socket + tar receive loop, on a worker thread |
| [`ingest/puller.py`](../backend/app/ingest/puller.py) | Orchestration: delta → transfer → stage → commit → delete → persist |
| [`ingest/status.py`](../backend/app/ingest/status.py) | The single in-memory live snapshot everything reads |
| [`ingest/reporter.py`](../backend/app/ingest/reporter.py) | Home Assistant webhook + optional MQTT discovery |
| [`api/routes/ingest.py`](../backend/app/api/routes/ingest.py) | `/status`, `/run`, `/cancel`, `/history` |
| [`pages/Backup.tsx`](../frontend/src/pages/Backup.tsx) | Dashboard, polls `/status` at 1.5 s while running |

### Sequence of a run

1. Poller ticks every `poll_interval_s` (default **8 s**, floor 3 s); skips entirely while a
   run is in flight.
2. Each tick calls `probe_unit()` → `adb.describe()` → `adb disconnect` + `adb connect` +
   `adb get-state` + (if online) a `sh` source probe.
3. On the **transition** offline → online, `start_run(trigger="auto")` fires.
4. `run_pull()`:
   1. enabled check; `status.try_begin()` single-flight claim
   2. **`probe_unit()` again** — a second full disconnect/connect/get-state/source-probe
   3. `adb.inventory()` — one `adb shell` running `for f in *.ts; do stat -c '%s|%n|%Y' "$f"; done`
   4. `delta()` on a thread — per-file `stat` against the footage share, skip files younger
      than `skip_active_s` (15 s), skip names the library deliberately removed, size-compare
   5. `status.plan(plan)`
   6. `_footage_is_safe_to_write()` — DB session + `evaluate_safety` + `is_writable`
   7. **`await report_event("started")`** — HTTP POST to the HA webhook, 10 s timeout,
      plus optional blocking MQTT connect
   8. clean staging, `clear_listener()` (kill stray `nc`), `launch_listener()` (not awaited)
   9. `transport.receive()` on a worker thread — connect with retry, stream the tar, write
      each member into `.ingest_staging` in 1 MB chunks, check the cancel flag once per read
   10. kill the adb child, `commit()` (size check + atomic `Path.replace`), optional
       `adb.delete()` of committed names, classify state, persist an `IngestRun`, report

### Reliability properties worth preserving

These are load-bearing and I would not change any of them:

- **Size-based delta, never checksummed.** A file cut short when the car pulled away has the
  wrong size and re-fetches itself; nothing has to remember it was partial.
- **The actively-written segment is skipped** (`skip_active_s`).
- **Staging + size-checked atomic rename.** The scanner never sees a partial file.
- **Refusal to create the footage directory**, plus `require_mountpoint` — the one code path
  that can destroy footage with no dry run.
- **Filename allowlist before anything reaches a shell** (`_SAFE_NAME`, `_bare_list`,
  `_quoted_list`), with hostile-name tests.
- **Delete-after-verify only ever names committed files**, and is off by default.
- **Single flight** — the poll fires every few seconds inside a two-minute window.
- **Cancel checked once per megabyte**, so cancel works mid-file.
- **`exit 0` on the inventory loop** — an empty card is the steady state, not a failure.
- **Reconnect before every cycle** — the transport goes stale while still reporting `device`.
- **The tar reader rejects members with a path in them** (`../../etc/passwd` test).

---

## 2. Current bottlenecks and inefficiencies

Split into three groups, because they are worth very different amounts.

### 2a. Window time lost before a byte moves — *the biggest recoverable loss*

At 34.3 MB/s a 90 s window is ~3.1 GB, so **every second of startup latency is ~34 MB**.

| Cost | Where | Estimate |
| --- | --- | --- |
| Presence detection delay | `poller.MIN_POLL_S` / `poll_interval_s` = 8 s | mean 4 s, worst 8 s → up to **275 MB** |
| Duplicate `probe_unit()` | `poller._loop` probes, then `run_pull` probes again — including `adb disconnect`, tearing down the connection just established | ~1–3 s |
| **Awaited `report_event("started")`** | `puller.py:291`, before the listener is launched. `httpx` with a **10 s timeout**; the reporter's own docstring notes a mistyped URL fails silently | 0–10 s, **up to 340 MB** |
| Per-file `stat` exec in `inventory` | `for f in *.ts; do stat …; done` = one process spawn per file, ~140 on a full card, on a weak SoC | ~0.5–2 s (measure) |
| `_footage_is_safe_to_write` | DB round trip + `is_writable` on an NFS mount, on the critical path | ~0.1–1 s |
| `_deliberately_removed()` DB query | on the critical path | small |

Nothing here is a bug — each has a good reason — but they are all sitting *inside* the window
when several could sit outside it.

### 2b. Data path

- **The `nc` relay is a second copy.** Toybox `netcat`'s relay loop is
  `poll()` → `read(fd, toybuf, sizeof(toybuf))` → `xwrite()`, and `toybuf` is **4096 bytes**
  ([netcat.c](https://github.com/landley/toybox/blob/master/toys/net/netcat.c)). So the current
  pipeline is: card → tar's 4 KB buffer → **pipe** → nc's poll + 4 KB buffer → socket.
  That is ~5 syscalls and 2 copies per 4 KB, plus a process hop.
- **Toybox `nc` can exec the producer with the socket as its stdout.** From the same source:

  ```c
  if (toys.optc) {
    close(sockfd);
    dup2(in1, 0);
    dup2(in1, 1);
    if (FLAG(E)) dup2(in1, 2);
    if (in1>2) close(in1);
    xexec(toys.optargs);
  } else {
    pollinate(in1, in2, out1, out2, ohexwrite, TT.W, TT.q);   // ← the 4 KB relay
  }
  ```

  So `nc -l -p 9000 tar -C <dir> c F1 F2 …` removes the relay and the pipe entirely.
  **This is the single cheapest transport experiment available and it should be tested first.**
- **It is not zero-copy, though — do not expect miracles.** Toybox `tar` writes file data via
  `xsendfile_pad` → `xsendfile_len` → `sendfile_len`, and `sendfile_len` uses
  **`copy_file_range(2)`** when available, falling back to a `read`/`write` loop through the
  4096-byte `libbuf` ([portability.c](https://github.com/landley/toybox/blob/master/lib/portability.c)).
  `copy_file_range` does not accept a socket as the destination, so writing to a socket falls
  back to the 4 KB loop regardless. The win from exec mode is roughly **halving the syscall
  count and removing an inter-process pipe hop**, not eliminating the copy.
- **Single TCP stream.** On a lossy or high-BDP wireless link a single stream frequently
  cannot fill the pipe. 2–4 parallel streams are the standard remedy and are easy to test.
- **`timeout 180`** kills the listener at 180 s. Fine for a 90 s window, but if the window ever
  gets longer (see §4), this becomes a cap.

### 2c. Progress and observability

- `throughput_mbs` is a **cumulative average since run start**, not a recent rate. It reads
  artificially low early in a run and cannot support an ETA.
- **There is no ETA at all.**
- **There is no phase**, only `RunState` (`disabled/idle/running/ok/partial/error/offline/
  unauthorized/cancelled`). "Scanning footage", "Preparing transfer", "Verifying" do not exist.
- `IngestStatus.file_done()` sets `current_file = name` — the file that just *finished* — so
  the UI shows the previous filename until the next one starts
  ([status.py:126](../backend/app/ingest/status.py#L126)).
- `bytes_done` counts **socket** bytes (tar headers, 512-byte padding, the 10 KB end-of-archive
  blocks); `bytes_total` counts **file** sizes. Progress can therefore exceed 100 % slightly.
- `DeltaPlan.active_skipped` is computed and then never surfaced in `snapshot()`.
- No per-file byte progress within the current file.

### 2d. Host/container

- **Debian bookworm ships `adb` `1:29.0.6`** (bookworm-backports has `1:34.0.5`). That client
  predates:
  - **receive windowing**, added in platform-tools **33.0.3, Aug 2022** — "*Add receive
    windowing (increase throughput on high-latency connections)*";
  - `push`/`pull` compression (30.0.0), `-z any/none/brotli/lz4/zstd`.

  So the `adb pull` / `adb exec-out` numbers in the README were measured with a client missing
  the single most relevant throughput feature for a WiFi link. **That does not change the
  recommendation** — `tar` over a socket is still very likely the winner — but the claim
  "`adbd` caps at 10 MB/s however many streams it is given" is not currently proven, and
  re-testing with platform-tools 35+ costs almost nothing.

### 2e. Policy issue (not a bug)

`delta()` sorts `plan.files` by **name**, i.e. oldest-first. If the backlog is permanently
larger than one window, today's footage never arrives — every window is spent on the oldest
files. The `_deliberately_removed()` docstring already recognises the starvation shape in a
different context. Worth an explicit ordering setting (oldest-first / newest-first /
newest-first-then-backfill) rather than an implicit consequence of a sort key.

---

## 3. Potential ADB transfer improvements

ADB is off the bulk path, so most of this is "what to keep in reserve" plus control-channel work.

| Idea | Verdict |
| --- | --- |
| `adb pull -z zstd/lz4/brotli` | **No.** The payload is H.264/H.265 in MPEG-TS — already compressed. Expect ≤1–2 % size reduction for real CPU cost on a weak SoC. Compression on pull has been *off by default* since platform-tools 31.0.0 for exactly this reason. |
| Upgrade the container's adb to 34/35 | **Yes, cheap.** Restores receive windowing; re-validates the adb numbers; may reduce control-call latency. `bookworm-backports` or a pinned platform-tools tarball. |
| `adb exec-out "tar c …"` | **Keep as the documented fallback.** Binary-safe (no pty to mangle data), needs nothing on the unit beyond `tar`, and requires shell protocol v2 (Android 5.0+, fine here). 8.3 MB/s measured with old adb — worth re-measuring. This is the safety net if `nc` turns out to be missing or blocked. |
| `adb pull` with many parallel streams | Already measured worst (4 MB/s). Skip. |
| Batch the control calls | **Yes.** See §10 — one `stat` exec instead of ~140, and drop the duplicate probe. |
| Fewer adb process spawns while idle | Each poll tick spawns 3 `adb` processes (~32 k/day when the car is away). A cheap TCP connect to `host:5555` with a 300 ms timeout is a much better arrival detector, with the adb dance only once it answers. |

---

## 4. Alternative transfer methods

### Realistic options

| Approach | Assessment |
| --- | --- |
| **`tar` over a plain socket (current)** | Keep. Best measured, minimal on-device footprint, nothing installed. |
| **`nc -l -p P <command>` exec mode** | **Test first.** Removes the pipe and the 4 KB relay. Zero new dependencies. Also lets tar's stderr be captured (`-E`, or leave stderr on the adb session) instead of `DEVNULL`, which would make "the command failed" distinguishable from "the car left". |
| **N parallel streams on N ports** | **Test second.** Disjoint file sets, N tar/nc listeners, N receive threads, all writing into the same staging directory. Preserves the staged-commit discipline exactly; cancel must set one flag all threads observe. |
| **Read from the raw mount instead of the FUSE view** | **Test.** If `/storage/Tfcard` is an emulated/FUSE view over something like `/mnt/media_rw/XXXX-XXXX`, reading the raw path can be materially faster. Whether the `shell` user can read it is the open question (usually needs the `media_rw` group). Cheap to check, potentially the largest single storage-side win. |
| **Physically move the card** | Fastest of all (60 MB/s card + USB reader) and worth documenting as the recovery path if the backlog ever becomes permanently unrecoverable in-window. |

### Rejected, with reasons

| Approach | Why not |
| --- | --- |
| SMB / NFS / SFTP server *on the unit* | Android ships no `sshd`, `smbd` or `nfsd`. Every option means installing and keeping alive a persistent service on a device with **no battery that reboots with the engine** — which is precisely the property the current design exists to avoid. Adds an auth surface and scoped-storage problems for a non-system app. Only revisit if `nc` proves unavailable. |
| `rsync` | Not present, and its delta algorithm is worthless here: recordings are written once, closed, and never modified. The existing size-based delta already does the only useful comparison. |
| Reverse the direction (unit connects to the app) | No throughput benefit. The container is bridged and publishes only `8098:8080`, so it would need a new published port — a worse security posture for zero gain. The current "app connects out, all state on the always-on side" is correct. |
| Mounting the card from the NAS | Not possible; the card is inside the unit. |
| Compression anywhere | Already-compressed video. See §3. |

### The two levers that are probably worth more than any of the above

1. **Lengthen the window.** The whole design is shaped by a 60–120 s window. Many head units
   have a configurable ACC power-off delay / sleep-instead-of-shutdown setting. Going from 90 s
   to 15 minutes is worth more than any conceivable transport optimisation, and costs nothing
   if the setting exists. **Probe this.**
2. **Fix the RF link.** 34.3 MB/s is 274 Mbit/s of TCP goodput.
   - If the unit's WiFi is **1×1** 802.11ac 80 MHz (433 Mbps PHY — very common in head units),
     274 Mbps is **63 % of PHY**, which is at or near the practical TCP ceiling. **There would
     be essentially no software headroom left.**
   - If it is **2×2** (866 Mbps PHY), 274 Mbps is **32 %**, which is well below typical, and
     there is real headroom — from parallel streams, AP placement, band/width, or interference.

   **Which of these is true is the single most decisive unknown in this whole investigation**,
   and `dumpsys wifi` answers it in one command. Everything in §2b is worth doing only if the
   answer is 2×2.

---

## 5. Android notification / progress options

### What `cmd notification post` can actually do — verified against AOSP

From `services/core/java/com/android/server/notification/NotificationShellCmd.java`:

```
usage: cmd notification post [flags] <tag> <text>

flags:
  -h|--help
  -v|--verbose
  -t|--title <text>
  -i|--icon <iconspec>
  -I|--large-icon <iconspec>
  -S|--style <style> [styleargs]
  -c|--content-intent <intentspec>
```

| Property | Finding |
| --- | --- |
| Caller restriction | `callingUid == Process.ROOT_UID \|\| callingUid == Process.SHELL_UID` — **shell is allowed**, no root needed |
| Posting identity | posts as the calling package, i.e. `com.android.shell` |
| Channel | `shell_cmd` / "Shell command", `IMPORTANCE_DEFAULT` |
| Update in place | **Yes** — `enqueueNotificationWithTag(pkg, pkg, tag, NOTIFICATION_ID=2020, …)`. Re-posting the same **tag** replaces the notification. |
| Styles | `bigtext`, `bigpicture`, `inbox`, `messaging`, `media` |
| **Progress bar** | **Not supported.** No `setProgress` anywhere in the command. |
| `setOngoing` | **Not supported** — the notification is dismissible. |
| `setOnlyAlertOnce` | **Not supported** — so on an `IMPORTANCE_DEFAULT` channel, **every update re-alerts** (sound/heads-up). |

### What that means in practice

A real determinate progress bar is **not achievable through ADB alone**. What *is* achievable,
with zero installation, is a text notification that updates in place:

```sh
adb -s $U shell "cmd notification post -S bigtext \
  -t 'Backing up dashcam footage' dashcam_backup \
  '47 / 126 files
18.4 GB / 42.1 GB
████████░░░░░░░░░░ 44%'"
```

Mitigations for the re-alert problem, in order of preference:

1. **Update rarely** — every 5 % or every 5 s, not per file. ~20 updates per run.
2. **Silence the channel once, on the unit** — the user long-presses the notification and sets
   "Shell command" to silent. One-time, persists, and removes the problem entirely.
3. Post a start notification and a completion/failure notification only, and leave live
   progress to the dashboard.

### The other options

| Option | Assessment |
| --- | --- |
| **Open the dashboard on the unit's own screen** — `am start -a android.intent.action.VIEW -d 'http://<nas>:8098/backup'` | **Strong candidate.** The app already serves a live Backup page with a real progress bar; this gets it onto the head unit with nothing installed. Costs: needs a browser, takes over the screen, and must **never** be triggered while driving. Good fit for a parked backup. |
| **A small companion APK** | The only way to get a true determinate progress notification, an ongoing/non-dismissible notification, and a foreground service. ~200 lines. But it contradicts the "nothing installed, no state in the car" property, which is load-bearing given the unit has no battery and reboots with the engine. **Recommend only if notifications are the priority and `cmd notification` proves unusable on this build.** |
| **Tasker + AutoNotification** | Works and can render arbitrary notifications driven by an `am broadcast`, but it is still "install an app" with more moving parts, a licence, and a persistent service — strictly worse than a purpose-built APK. |
| **Termux + `termux-notification`** | Same objection, plus I could not verify from a primary source whether it exposes a determinate progress bar. Treat as unverified. |
| Toast / system overlay from shell | Not available without an app. |

### Version and vendor caveats to verify on the device

- **Android 13+ requires `POST_NOTIFICATIONS`.** Whether `com.android.shell` holds it on this
  vendor build is unverified and is a hard gate on the whole approach.
- **Head unit launchers frequently replace or hide the status bar and notification shade.**
  If this one does, notifications may post successfully and simply never be visible. This must
  be confirmed *by looking at the unit's screen*, not by a zero exit code.
- `cmd notification` may be absent or restricted in a vendor build.

---

## 6. Dashboard progress improvements

### What already exists

`/api/ingest/status` returns exactly: `state`, `unit_online`, `files_total`, `files_done`,
`bytes_total`, `bytes_done`, `throughput_mbs`, `current_file`, `backlog_files`,
`backlog_bytes`, `last_success_ts`, `last_error`. The Backup page polls it at 1.5 s while
running and 15 s otherwise, and already renders a bytes progress bar, files x/y, GB done/total,
speed, backlog and history.

So **most of the requested panel already exists.** What is missing is small and local.

### Gap analysis against the requested display

| Wanted | Status |
| --- | --- |
| `Status: Transferring` | ✗ — only `RunState`, which is an *outcome*, not a phase |
| `Progress: 43%` | ✓ (can exceed 100 % slightly — tar overhead) |
| `Files: 56 / 131` | ✓ |
| `Transferred: 17.8 GB / 41.3 GB` | ✓ |
| `Speed: 28.4 MB/s` | ~ — exists but is a run average, not a recent rate |
| `Current: …_camera_0.ts` | ~ — shows the *last finished* file between files |
| `ETA: 13m` | ✗ |

### Recommendation — all inside `status.py`, no new subsystem

1. **Add `phase` alongside `state`.** Keep `state` exactly as it is so the Home Assistant REST
   sensor, the webhook and MQTT contracts do not change. New field, additive:
   `waiting_for_unit / connecting / scanning / preparing / transferring / verifying /
   complete / partial / failed / cancelled` (plus `pausing_recording` / `resuming_recording`
   only if §7 is ever implemented).
2. **Add `speed_mbs_recent`** — EWMA or a 5-second rolling window — and keep `throughput_mbs`
   as the run average.
3. **Add `eta_seconds`** = `(bytes_total − bytes_done) / speed_mbs_recent`, only while
   transferring and only after a warm-up, `null` otherwise.
4. **Clear `current_file` in `file_done()`** (or add `current_file_index`).
5. **Surface `active_skipped` and `started_at`.**
6. **Clamp the displayed fraction at 1.0** in `Backup.tsx`.
7. **Do not add SSE or WebSockets.** 1.5 s polling of an in-memory snapshot is entirely
   adequate for a 90-second event, and the "one snapshot, everything reads it" property is
   worth more than the latency saving.

---

## 7. Feasibility of pausing recording during backup

### The evidence already in this repository says the upside is small

| Fact | Source | Implication |
| --- | --- | --- |
| TF card sequential read = **60 MB/s while both lenses are recording** | README / `adb.py` docstring | There is already ≥25 MB/s of *unused* card read bandwidth with recording **on** |
| Achieved transfer = 34.3 MB/s | same | The card is not the constraint |
| Recording writes ≈ 2 × (8–20 Mbit/s) ≈ **2–5 MB/s** | typical dashcam bitrates | ~5 % of card capability; encoding is on the hardware encoder, not the CPU |
| Recording uses **no WiFi at all** | — | If the link is the bottleneck (§4), pausing recording gains **zero** |

**Expected gain: low single-digit percent**, mostly IO-scheduling jitter — unless the device
probe shows something surprising. This must be measured before any device-control code exists.

### The risks are severe and asymmetric

- **Android 15 makes `am force-stop` genuinely dangerous here.** Force-stop puts the package in
  the *stopped state*: the system **cancels all the app's pending intents**, and the app stays
  stopped until something explicitly launches it; `ACTION_BOOT_COMPLETED` is redelivered only
  when it leaves that state. For a recorder that arms itself from a boot receiver or an alarm,
  that is exactly the failure you cannot accept.
- **`FLAG_STOPPED` survives reboot.** The engine cycle is *not* a fail-safe here. A backup that
  crashes after force-stopping the recorder can leave the dashcam **not recording through the
  next drive** — silently.
- **Force-stopping mid-segment truncates the open `.ts`.** MPEG-TS is comparatively forgiving,
  but the final GOP is lost and the segment's duration metadata will be wrong.
- **Vendor watchdogs** on head units commonly restart DVR services. Unknown behaviour; could
  mask a failure, or fight the pause, or produce a restart loop.
- **Camera exclusivity** — if the recorder is stopped uncleanly and something else grabs the
  camera, restart may fail.

### Gentler mechanisms, in order of preference

1. **The DVR app's own stop/start** — an exported broadcast receiver, activity or service, or a
   vendor settings toggle. Only this deserves to be called "clean". Discoverable from
   `dumpsys package <pkg>` **without invoking anything** (§13-I).
2. **`am kill <pkg>`** — kills background processes only, sets no `FLAG_STOPPED`, and the system
   may restart sticky services. Safer to recover from, but non-deterministic and will not touch
   a foreground or persistent service.
3. **`am force-stop`** — last resort, and only with all of §8's fail-safes in place.

### Verdict

**Do not implement pausing.** Run the A/B benchmark in §13-H first. Revisit only if *all three*
hold: measured gain > 15 %, a clean vendor-supported stop/start exists, and every fail-safe in
§8 is implemented and tested.

---

## 8. Risks and fail-safes

### If recording pause is ever implemented, all of these are mandatory

1. **Resume in a `finally` that runs on every exit path** — success, error, cancel, app
   shutdown, task cancellation. `run_pull` already has this shape; reuse it.
2. **Persist a "recording paused at T" marker outside process memory** (the DB or the data
   volume), so an app crash or container restart resumes recording the next time it sees the
   unit, rather than forgetting it ever paused.
3. **A hard deadline** — resume unconditionally after N minutes regardless of transfer state.
4. **A remote watchdog** — `timeout <N> sh -c '…; <resume command>'` launched *on the unit*, so
   recording restarts even if the app disappears entirely. This is the same trick the current
   listener already uses with `timeout 180 nc`.
5. **Positive verification that recording resumed** — a new `.ts` appears *and grows* between
   two listings, and `dumpsys package <pkg> | grep -i stopped` shows the stopped flag cleared.
   A zero exit code is not evidence.
6. **Never pause unless the vehicle is stationary.** The app has no live speed signal — it
   parses GPS/OSD from *recorded* footage, after the fact. The available proxies are
   "associated with the home AP" plus "the most recent segment's OSD speed is ~0". Both are
   weak; require both, and make the whole feature opt-in and default-off.
7. **A visible, unmissable dashboard state** when recording is paused, and an alert if a resume
   ever fails.

### Existing risks worth noting

| Risk | Current mitigation | Residual |
| --- | --- | --- |
| Unmounted share + delete-after-verify → total loss | `_footage_is_safe_to_write`, `require_mountpoint`, `commit()` refuses to create the directory, tested | Well covered |
| Right-size but corrupt file then deleted from the card | Size check only | Real but small. If it matters, add an optional hash **only on the delete-after-verify path** — but note it costs a second full card read, i.e. window time |
| `clear_listener` kills by `pidof nc` | — | Collateral if anything else on the unit uses `nc`. Unlikely; worth confirming during probing |
| Power loss mid-transfer | Staged commit; expected, not exceptional | Well covered |
| Card reformat changes volume id | `/storage/Tfcard` symlink probed first | Well covered |
| ADB key loss on rebuild | Key on the `/data` volume + `ANDROID_USER_HOME` + symlink | Well covered |
| Parallel streams (if added) | — | More concurrent card readers; per-stream staged commit; cancel must stop all streams; partial streams must not confuse `commit()` (it is already per-file size-checked, so this is fine) |
| `nc` exec mode (if added) | — | A failed command closes the socket immediately and looks like "the car left". **Capture stderr instead of `DEVNULL`** to tell them apart |

---

## 9. Recommended architecture

Keep the current shape. It is well-reasoned and the comments record why. Changes in order of
value-per-risk:

**Tier 0 — no code, do these first**
- `dumpsys wifi` on the unit → settles whether there is *any* transport headroom (§4).
- Check the unit for an ACC power-off delay / sleep setting → potentially worth more than
  everything else combined.
- Re-test `adb exec-out` with platform-tools 35+ → validates or retires the "adbd caps at
  10 MB/s" claim.

**Tier 1 — app-side, safe, no device dependency (§10)**
Reclaim startup latency and expose the progress data that already exists.

**Tier 2 — transport, needs one device test each (§11)**
`nc` exec mode → parallel streams → raw-mount read → capture listener stderr.

**Tier 3 — only with device evidence**
On-unit notifications; recording pause (probably never).

---

## 10. Changes that can safely be made now — **implemented**

All app-side, no device access, every reliability property in §1 preserved.

| # | Change | Where | Effect |
| --- | --- | --- | --- |
| 1 | Poller hands its `UnitInfo` to `start_run` instead of the run re-probing | `poller.py`, `puller.py` | Removes a whole `disconnect`/`connect`/`get-state`/source-probe from the critical path — including tearing down the link just proven good |
| 2 | `report_event("started")` is fired, not awaited | `puller.py` | Takes up to 10 s of webhook timeout (~340 MB) out from in front of the first byte |
| 3 | One `stat` exec for the whole card instead of ~140 | `adb.py` | `set -- *.ts && [ -e "$1" ] && stat -c … *.ts; exit 0`. Empty-card guard kept |
| 4 | Cheap arrival detection — `adb.is_listening()` does one TCP connect (400 ms) and only escalates to adb when the port answers; `MIN_POLL_S` 3 s → 1 s, default interval 8 s → 2 s | `adb.py`, `poller.py` | Recovers up to ~6 s (~200 MB) of window, and stops spawning 3 adb processes every 8 s all day |
| 5 | `phase`, `speed_mbs_recent`, `eta_seconds`, `active_skipped`, `started_at` in the snapshot; `current_file` cleared on `file_done` | `models.py`, `status.py` | §6. Purely additive — `state` keeps its exact meaning, so no HA/MQTT consumer changes |
| 6 | Dashboard shows the phase, recent speed, ETA, skipped count; progress clamped at 100 % | `Backup.tsx`, `api.ts` | §6 |
| 8 | `stop_listener` returns whether the session was still alive | `adb.py`, `puller.py` | "The head unit stopped serving first" is now distinguishable from "the car left", which previously produced identical messages |
| 9 | `ingest.transfer_order` — oldest-first (default) or newest-first | `settings_schema.py`, `puller.py` | §2e — a permanently oversized backlog no longer starves new footage |
| 11 | The two DB reads and the staging clean now run *concurrently with* the card listing instead of after it | `puller.py` | None of the three needs the card, and on a hard NFS mount that series was worth hundreds of milliseconds. Still awaited — and still gating — exactly where each result is needed |
| 12 | Connect retry interval 250 ms → 50 ms | `transport.py` | The interval is the error bar on the start of every transfer: whatever is left of the current gap when the listener comes up is dead window |
| — | `ingest.show_on_unit` — open this app's Backup page on the head unit's own screen when a transfer starts | `adb.show_url`, `origin.py`, `main.py`, `settings_schema.py` | §5. Off by default, URL allowlisted, fired not awaited, only when there is something to copy. The address is **learned from the browser**: a bridged container sees only its own 172.x interfaces, never the host LAN address and published port that actually reach it, so the address the dashboard was opened on is used. Learned from the SPA route only — never from an API call, because Home Assistant polls under a container name no car could resolve. `ingest.unit_display_url` overrides it for reverse-proxy setups. **Superseded in part — see §14: the address is now persisted, and an API key is needed when sign-in is on** |

### Deliberately not done

| # | Change | Why not |
| --- | --- | --- |
| 7 | Pre-flight/cache the footage-safety check | It guards the one code path in the app that can destroy footage with no dry run. Caching it means a share that unmounted between the poll and the run would not be caught — which is precisely the failure it exists to prevent. It saves well under a second. Bad trade. |
| 10 | Pin a modern adb in the Dockerfile | Real, but unproven: the bulk path does not use adbd at all, so the only certain gain is control-call latency. Adding a backports repo to the image risks the build for a benefit nobody has measured. Do it **together with benchmark H6**, where it can be shown to matter or dropped. |

---

## 11. Changes that require testing on the dashcam

| Change | Gate |
| --- | --- |
| `nc -l -p P <command>` exec mode | Does this unit's toybox `nc` accept a COMMAND in listen mode? (`nc --help`) — then A/B the throughput |
| N parallel streams | Only worth it if `dumpsys wifi` shows 2×2 (i.e. real headroom) **and** the parallel `dd` test shows aggregate > single-stream |
| Reading from the raw mount rather than the FUSE view | Does `/mnt/media_rw/…` exist and can the `shell` user read it? |
| Raising `listen_timeout_s` | Only if the window is actually longer than 180 s |
| On-unit notifications | Does `cmd notification` exist, does `com.android.shell` hold `POST_NOTIFICATIONS`, and does the launcher show a shade at all? |
| Opening the dashboard on the unit's screen | Is there a browser? Does `am start` work from shell? |
| Recording pause | Everything in §7 and §8 — and the measured gain must justify it first |

---

## 12. Information Needed From Dashcam

Grouped by what it decides. Anything marked **decisive** changes the recommendation.

### A. Platform identity
- Android version, SDK level, security patch, build fingerprint, build type
- `ro.product.model` / `device` / `manufacturer` / `board`, `ro.board.platform`
- Whether the build is debuggable, and `ro.secure`
- Shell uid/gid and supplementary groups (does shell have `media_rw`?)
- Host-side `adb version`

### B. Shell toolbox — **decisive for §2b**
- toybox version, and the full command list
- **The complete `nc --help` output** — specifically whether it lists a COMMAND form, `-L`, `-E`
- Presence of `tar`, `timeout`, `stat`, `dd`, `md5sum`/`sha1sum`, `pidof`, `setsid`, `busybox`
- `tar --help` — does it support `-C`?
- What `/system/bin/sh` actually is

### C. Storage layout — **decisive for the raw-mount idea**
- `/storage` contents; is `/storage/Tfcard` a symlink, and to what?
- `/proc/mounts` — filesystem type of the card (exfat / f2fs / vfat), and whether there is a
  FUSE/sdcardfs layer over it
- Whether `/mnt/media_rw/<VOLUME>` exists and whether the `shell` user can read it
- Card capacity, free space, file count, typical segment size and duration
- Whether recordings are only ever in `DCIM/Video`, or whether there are event/locked folders

### D. Network — **decisive for the whole transport question**
- **`dumpsys wifi`: negotiated Tx/Rx link speed, `MaxSupportedRxLinkSpeedMbps`, frequency,
  channel width, RSSI, standard (11ac/11ax)** — this is the single most important datum
- Interface counters: tx/rx packets, errors, drops, retries (`ip -s link`, `/proc/net/dev`)
- Round-trip latency and packet loss to the NAS over 20+ pings
- Whether the unit supports 5 GHz only or also 6 GHz / 160 MHz

### E. Power and window length — **decisive for §4 lever 1**
- Time from engine-on to ADB reachable, and engine-off to link loss (measured, several times)
- Whether the unit has an ACC power-off delay / sleep-instead-of-shutdown setting, and its range
- `dumpsys power`, `dumpsys battery` — is there any backup power at all?

### F. Recording subsystem — identification only, no control
- Full package list (`-f` and `-s`)
- Which process is actually writing the newest `.ts`
- The recording app's package name, its services, and which are foreground/persistent
- Its **exported** activities, services and broadcast receivers, and their intent filters
- Any vendor service that supervises it
- Whether there is a local HTTP/socket API on the unit
- Whether an on-screen vendor toggle for recording exists

### G. Notification capability
- Does `cmd notification` exist, and its help output
- Does a posted notification actually appear **on the unit's screen**
- Does re-posting the same tag replace it, and does each update make a sound
- Whether `com.android.shell` holds `POST_NOTIFICATIONS`
- Whether the launcher has a status bar / notification shade at all
- Whether a browser is installed and `am start … VIEW` opens it

### H. Benchmarks
See §13-H. The four-cell matrix is the core deliverable.

---

## 13. Exact tests and commands for the next device-probing agent

> **Ground rules**
> - Sections A–J are **read-only or additive** and safe to run with the car parked and running.
> - Section K is **destructive to recording state** and must not be run without explicit
>   sign-off, with a person present, the vehicle parked, and the recovery procedure to hand.
> - Set `U=192.168.1.122:5555` (or the current address) and `adb connect "$U"` first.
> - Capture full stdout for everything; do not summarise raw output away.

### A — Platform identity

```sh
adb version
adb -s $U shell 'getprop | grep -Ei "ro\.build\.version\.(release|sdk|security_patch)|ro\.product\.(model|device|name|manufacturer|brand)|ro\.board\.platform|ro\.build\.fingerprint|ro\.build\.type|ro\.debuggable|ro\.secure"'
adb -s $U shell 'uname -a; id; whoami'
adb -s $U shell 'getprop ro.adb.secure; getprop service.adb.tcp.port'
```

### B — Shell toolbox *(decides the `nc` exec-mode optimisation)*

```sh
adb -s $U shell 'toybox --version'
adb -s $U shell 'toybox'                       # full applet list
adb -s $U shell 'which sh tar nc netcat timeout stat dd cat md5sum sha1sum sha256sum pidof setsid busybox toolbox'
adb -s $U shell 'nc --help 2>&1'               # ← capture VERBATIM, this is the important one
adb -s $U shell 'tar --help 2>&1 | head -40'
adb -s $U shell 'dd --help 2>&1 | head -20'
adb -s $U shell 'ls -l /system/bin/sh'
```

### C — Storage layout *(decides the raw-mount idea)*

```sh
adb -s $U shell 'ls -l /storage/'
adb -s $U shell 'ls -l /storage/Tfcard'
adb -s $U shell 'ls -la /storage/Tfcard/DCIM/'
adb -s $U shell 'ls -la /storage/Tfcard/DCIM/Video | head -20'
adb -s $U shell 'ls /storage/Tfcard/DCIM/Video | wc -l'
adb -s $U shell 'df -h'
adb -s $U shell 'cat /proc/mounts'
adb -s $U shell 'stat -f /storage/Tfcard'
adb -s $U shell 'ls -l /mnt/media_rw/ 2>&1'                    # does the raw mount exist?
adb -s $U shell 'ls -l /mnt/media_rw/*/DCIM/Video 2>&1 | head' # can shell read it?
```

### D — Network *(the decisive measurement)*

```sh
adb -s $U shell 'dumpsys wifi | grep -iE "linkspeed|frequency|rssi|standard|width|mac|ssid|score" | head -40'
adb -s $U shell 'cmd wifi status 2>&1'
adb -s $U shell 'ip addr; ip -s link'
adb -s $U shell 'cat /proc/net/dev'
adb -s $U shell 'ping -c 20 <NAS_IP>'
adb -s $U shell 'cat /sys/class/net/wlan0/speed 2>/dev/null'
```

Report: negotiated Tx/Rx Mbps, `MaxSupportedRxLinkSpeedMbps`, frequency, channel width, RSSI,
and the retry/drop counters.

### E — Inventory cost *(quantifies §10 item 3)*

```sh
adb -s $U shell 'cd /storage/Tfcard/DCIM/Video && time sh -c "for f in *.ts; do stat -c \"%s|%n|%Y\" \$f; done > /dev/null"'
adb -s $U shell 'cd /storage/Tfcard/DCIM/Video && time stat -c "%s|%n|%Y" *.ts > /dev/null'
```

### F — Storage read throughput

```sh
adb -s $U shell 'cd /storage/Tfcard/DCIM/Video && ls -S *.ts | head -3'     # pick a big one
adb -s $U shell 'time dd if=/storage/Tfcard/DCIM/Video/<BIG>.ts of=/dev/null bs=1M count=1024'
# and via the raw mount, if C showed it is readable:
adb -s $U shell 'time dd if=/mnt/media_rw/<VOL>/DCIM/Video/<BIG>.ts of=/dev/null bs=1M count=1024'
```

Use a *different* large file for each run so the page cache does not flatter the result.

### G — CPU / IO load (run these **during** a transfer)

```sh
adb -s $U shell 'cat /proc/cpuinfo | grep -c processor'
adb -s $U shell 'top -n 1 -b -m 15'
adb -s $U shell 'cat /proc/loadavg'
adb -s $U shell 'cat /proc/pressure/io 2>/dev/null; cat /proc/pressure/cpu 2>/dev/null'
adb -s $U shell 'cat /proc/diskstats'
```

### H — The benchmark matrix *(the core deliverable)*

Four cells that isolate every stage. Run each 3×, report min / median / max MB/s.
`nc` on the NAS side; use `pv` if available, otherwise wrap in `time`.

**H0 — link only, no card, no relay** *(exec mode)*
```sh
# unit
adb -s $U shell "timeout 300 nc -l -p 9000 dd if=/dev/zero bs=1M count=4096"
# NAS
time nc <UNIT_IP> 9000 > /dev/null
```

**H1 — link + relay, no card** *(isolates the toybox `nc` 4 KB relay)*
```sh
adb -s $U shell "dd if=/dev/zero bs=1M count=4096 | timeout 300 nc -l -p 9000"
time nc <UNIT_IP> 9000 > /dev/null
```

**H2 — link + card, no relay** *(the proposed new transport)*
```sh
adb -s $U shell "timeout 300 nc -l -p 9000 tar -C /storage/Tfcard/DCIM/Video c F1 F2 F3 F4"
time nc <UNIT_IP> 9000 > /dev/null
```

**H3 — everything** *(the current transport, the control)*
```sh
adb -s $U shell "cd /storage/Tfcard/DCIM/Video && tar c F1 F2 F3 F4 | timeout 300 nc -l -p 9000"
time nc <UNIT_IP> 9000 > /dev/null
```

Reading:
- **H0 ≈ H1** → the relay costs nothing; do not bother with exec mode.
- **H0 ≫ H1** → the relay is real; exec mode is worth implementing.
- **H0 ≈ 34 MB/s** → **the link is the ceiling**; all transport optimisation is dead and the
  answer is RF or a longer window (§4).
- **H2 ≪ H0** → the card read (or the FUSE layer) is the constraint; retry via `/mnt/media_rw`.

**H4 — parallel streams** (only if H0 is well above 34 MB/s)
```sh
# unit: 4 listeners
for p in 9000 9001 9002 9003; do
  adb -s $U shell "timeout 300 nc -l -p $p dd if=/dev/zero bs=1M count=1024" &
done
# NAS: 4 concurrent receivers, measure aggregate wall time
for p in 9000 9001 9002 9003; do nc <UNIT_IP> $p > /dev/null & done; time wait
```

**H5 — NAS write path** (separate "off the unit" from "into the NAS")
```sh
nc <UNIT_IP> 9000 > /dev/null                                    # network only
nc <UNIT_IP> 9000 > /mnt/Vault/dashcam/.ingest_staging/probe.bin # incl. NAS write
dd if=/dev/zero of=/mnt/Vault/dashcam/.probe bs=1M count=8192 oflag=direct  # NAS alone
rm -f /mnt/Vault/dashcam/.ingest_staging/probe.bin /mnt/Vault/dashcam/.probe
```

**H6 — modern adb comparison** (with platform-tools 35+, not bookworm's 29)
```sh
time adb -s $U exec-out "cd /storage/Tfcard/DCIM/Video && tar c F1 F2 F3 F4" > /dev/null
time adb -s $U pull /storage/Tfcard/DCIM/Video/<BIG>.ts /dev/null
time adb -s $U pull -z zstd /storage/Tfcard/DCIM/Video/<BIG>.ts /dev/null   # expect no gain
```

**H7 — recording on vs off** — *do not run until §K is signed off.* Same file set, same
conditions, recording active then stopped. This is the only test that justifies §7 existing.

### I — Recording subsystem, identification only *(nothing is invoked)*

```sh
adb -s $U shell 'pm list packages -f' > packages.txt
adb -s $U shell 'pm list packages -s' > packages-system.txt
adb -s $U shell 'ps -A -o PID,USER,NAME' > processes.txt
adb -s $U shell 'dumpsys activity services' > services.txt
adb -s $U shell 'dumpsys activity activities | head -80'
adb -s $U shell 'dumpsys window | grep -i mCurrentFocus'
adb -s $U shell 'dumpsys media.camera | head -80'
adb -s $U shell 'ls -lt /storage/Tfcard/DCIM/Video | head -5'   # twice, 20s apart: what grows?
```

Then, for whatever package that identifies (call it `$PKG`):

```sh
adb -s $U shell "dumpsys package $PKG" > pkg.txt
adb -s $U shell "cmd package resolve-activity --brief $PKG"
adb -s $U shell "dumpsys package $PKG | sed -n '/Receiver Resolver Table/,/^$/p'"
adb -s $U shell "dumpsys package $PKG | grep -iE 'stopped|enabled|flags|persistent'"
```

**Report the exported receivers/services and their intent filters.** That list is what decides
whether a clean pause is even possible.

### J — Notification capability *(safe, additive)*

```sh
adb -s $U shell 'cmd notification --help 2>&1 | head -40'
adb -s $U shell "cmd notification post -S bigtext -t 'Dashcam backup' dcbk 'test 1 of 2'"
# same tag — does it replace, and does it ding again?
adb -s $U shell "cmd notification post -S bigtext -t 'Dashcam backup' dcbk 'test 2 of 2 — 50%'"
adb -s $U shell 'dumpsys notification --noredact | head -60'
adb -s $U shell 'cmd appops get com.android.shell POST_NOTIFICATION 2>&1'
adb -s $U shell 'dumpsys package com.android.shell | grep -i notification'
# browser route
adb -s $U shell 'pm list packages | grep -iE "browser|chrome|webview|firefox"'
adb -s $U shell "am start -a android.intent.action.VIEW -d 'http://<NAS_IP>:8098/backup'"
```

**Photograph or describe the unit's screen for each step.** A zero exit code proves nothing —
the question is whether anything is visible.

### K — Recording control *(⚠ explicit sign-off required; do not run unattended)*

Preconditions: vehicle parked, engine running, a person present, and time to complete the full
recovery. Start a log first:

```sh
adb -s $U logcat -v time > pause-test.log &
```

Procedure — stop at the first step that works, and **do not escalate past step 3 without a
second explicit approval**:

1. **Baseline.** Record the newest `.ts` name and size, `ps` line for `$PKG`, and
   `dumpsys package $PKG | grep -i stopped`.
2. **Vendor route.** If I-section found an exported stop receiver or a UI toggle, use that.
3. **`am kill`.** `adb -s $U shell "am kill $PKG"` — gentlest; sets no `FLAG_STOPPED`.
4. **`am force-stop`** — *last resort, separate approval.* Note that on Android 15 this sets a
   reboot-persistent stopped state and cancels the app's pending intents.

After each attempt:

```sh
adb -s $U shell 'ls -lt /storage/Tfcard/DCIM/Video | head -3'   # run twice, 20s apart
adb -s $U shell "ps -A -o PID,USER,NAME | grep -i ${PKG%%.*}"
adb -s $U shell "dumpsys package $PKG | grep -i stopped"
```

Then **verify the last segment is intact** — pull it and `ffprobe` it — and **restore**:

```sh
adb -s $U shell "am start -n $PKG/<LAUNCHER_ACTIVITY>"
adb -s $U shell 'ls -lt /storage/Tfcard/DCIM/Video | head -3'   # twice: a NEW file must GROW
adb -s $U shell "dumpsys package $PKG | grep -i stopped"        # flag must be clear
```

Finally, **cycle the engine and confirm recording starts on its own**, then confirm again after
a second cycle.

**Stop conditions.** If a new file does not appear and grow, or the stopped flag will not clear,
or recording does not resume after an engine cycle — **stop the investigation and report
immediately**. Recording must be restored before the vehicle is driven. Do not leave the unit
in a stopped state overnight under any circumstances.

---

## 14. Why the backup page never actually appeared (field diagnosis, 2026-08-16)

The feature shipped in `f744398` and had **never once fired** on the live deployment. Three
separate things were wrong, and only the third was visible from the car.

Everything the design assumed about the *device* held up. Probed against the live unit
(`192.168.1.122:5555`) during a real driveway window:

| Check | Result |
| --- | --- |
| Browser present | `com.android.chrome` + `com.google.android.webview` |
| `http` VIEW intent resolves | `com.android.chrome/…IntentDispatcher`, `isDefault=true` |
| `am start … -d 'http://192.168.1.16:8199/backup'` | `Starting: Intent { … }`, no `Error:` |
| Unit → app reachability | `ping` 0 % loss, ~21 ms avg; TCP 8199 `REACHABLE` |

So steps 6, 7 and 8 of the original checklist were fine, and the notification fallback in
§12 G was never needed.

### a. The learned address was empty at every single pull

`origin` kept the address in process memory only, on the reasoning that the cost of a cold
start was "one window's transfer". The logs say otherwise:

```
2026-08-15T13:55:27Z  ingest poller started          <- process restart, _origin = None
2026-08-16T05:01:38Z  pulling from the head unit     62 files, 13 495 MB   <- no page
2026-08-16T05:42:42Z  pulling from the head unit     62 files, 13 514 MB   <- no page
2026-08-16T05:44:15Z  learned the address …          origin=http://192.168.1.16:8199
```

The dashboard is opened when somebody wants to look at footage; the car arrives when
somebody comes home. There is no reason the first has happened since the last restart — and
here it had not, for sixteen hours and two full windows. "One window" was every window.

**Fixed** by persisting the learned address to `ingest.learned_origin` (read-only, shown on
the settings page so the operator can see where the car is being pointed). Memory still
wins, and any dashboard load still overwrites both, so the staleness argument that motivated
memory-only is intact.

### b. The failure was below the log level

The one line that would have explained it, `not showing the backup page: this app's own
address is not known yet`, was `log.debug`. The deployment runs at INFO, so a feature that
failed on every run left **no trace whatsoever** — the table in §12 lists that message as a
diagnostic, but it was unreachable in practice. Now a warning.

### c. With sign-in on, the car got the login page

Confirmed on the unit: Chrome loaded the URL and rendered the login form. The head unit has
no keyboard and nobody in the driver's seat, and a browser's first navigation cannot carry
an `Authorization` header — so there was no way for it to authenticate at all.

**Fixed** with `security.api_key`. `backup_url()` appends it as `?k=…`; `_redeem_api_key` in
`main.py` trades it for a `HttpOnly`/`SameSite=Strict` cookie and 303s to the clean path, so
the key does not stay in the browser history of a screen that lives in a parked car. Also
accepted as an `X-API-Key` header for scripts.

It is a **full-access** bearer credential — the owner's explicit choice over a scoped
read-only variant. Consequences, recorded deliberately: anyone holding it can read the
footage and change the settings, and revocation is blanking the field. The cookie is
included in the gate's `Origin` check (`_COOKIE_BORNE`) so a sibling subdomain cannot spend
it on an unsafe method, which is the one thing handing it out as a cookie would otherwise
have cost.

### Not the cause

The deployment hypothesis in the original brief — `release.yml` only tags `latest` on `v*`
while `docker-compose.yml` pulls `:latest` — did **not** apply here: the live container was
already running a `main` build (`/health` reports `"version":"main"`) and had
`ingest.show_on_unit` in its settings schema. The mismatch is still a live trap for anyone
following the committed compose file, but it was not this bug.


## 15. The benchmark matrix, run against the live unit (2026-08-16)

§13-H executed on the real device. The unit was on a bench on mains power rather than in the
car, so window length was not a constraint and every cell could be run to completion three
times. Receiver was a separate host on **10 GbE**, so nothing here is limited by the far end.

### The numbers

| Cell | What it isolates | min | median | max |
| --- | --- | ---: | ---: | ---: |
| **H0** | link only, exec mode, `/dev/zero` (2 GB) | 34.57 | **34.88** | 35.70 |
| **H1** | link + toybox `nc` relay, `/dev/zero` (2 GB) | 31.65 | **32.43** | 32.58 |
| **H2** | card + exec mode, no relay, real `.ts` (1.7 GB) | 28.12 | **30.99** | 32.64 |
| **H3** | card + relay — **the current transport** | 31.84 | **32.07** | 32.20 |
| **H4** | 4 parallel streams, aggregate | — | **33.51** | — |
| — | cold card read through FUSE (`dd`, recording live) | 54 | **55** | 56 |
| — | production, unit → NAS, staged and committed | 27.8 | **30.7** | 32.5 |

All MB/s. H2/H3 alternated run-by-run so cache state matched.

### What it says

**The radio is the ceiling, and the transport is already at 93% of it.**

```
Wi-Fi standard        : 5 (802.11ac)          Frequency : 5220 MHz (ch 44, 80 MHz)
RSSI                  : -48 dBm               Link speed: 433 Mbps
Max Supported Tx/Rx   : 433 Mbps  <-- the device cap, 1x1 VHT80
```

433 Mbps PHY is 54.1 MB/s raw; H0's 34.9 MB/s is 65% goodput, which is ordinary for 11ac.
`Max Supported` being equal to the negotiated rate means the radio is **single-stream** —
there is no better rate to negotiate. RSSI is already -48 with zero TX errors, drops or
collisions, so nothing about placement, channel or antenna is being left on the table.

Everything else has slack:

- **The card is not the constraint.** 55 MB/s cold through FUSE against 32 used.
- **The CPU is not the constraint.** During a full-rate transfer: `414% idle of 800%`,
  `tar` at 10.3%, `nc` at 0.0%.
- **The link is clean.** `ip -s link`: 0 errors, 0 dropped, 0 collisions.

### Optimisations that are now dead, with the measurement that killed them

| Idea | Verdict |
| --- | --- |
| **Exec mode** (`nc -l -p P COMMAND`, no relay) — §2b | **No gain.** H2 ≈ H3, and H3 was *more consistent*. The relay costs 7% on `/dev/zero` (H0→H1) because `dd` outruns the link, but with real files the FUSE read paces the pipeline and the relay has slack. `nc` does support the COMMAND form; it is simply not worth using. Note `tar -C DIR c F` fails on toybox (`Needs -txc`) — only `cd DIR && tar c F` or `tar cC DIR F` work. |
| **Raw mount** (`/mnt/media_rw/<VOL>`, bypassing FUSE) | **Impossible unrooted.** `ls: Permission denied`. The vfat mount is `gid=1023 (media_rw)`, `dmask=0007`; `shell` is in `sdcard_rw`/`sdcard_r` but not `media_rw`. And FUSE is not costing anything anyway — see the 55 MB/s read. |
| **Parallel streams** — H4 | **No gain.** 4 streams aggregate 33.51 MB/s against 34.88 for one. It is the same radio. |
| **Compression** (`-z zstd`, gzip) | **No gain.** 40 MB of `.ts` gzips to 97.6% of its original size. It is already H.264. |
| **A faster `adb`** — H6 | **Moot.** The bulk path has not touched `adbd` since the `tar`/`nc` transport landed. |
| **Pausing the recorder** — H7/§K | **No gain, and no clean mechanism.** See below. |

### Pausing recording is not worth it

The question was asked directly, so it was measured rather than assumed.

Recording costs, during a live transfer: about 10% of *one* core (`media.unisoc.codec2` 6.8%,
`media.swcodec` 3.4%) against 414% idle, and roughly 2 MB/s of card writes against a card
that reads at 55 and a link that caps at 35. **There is no contention to recover.**

There is also no clean way to do it. `com.zqc.camera` exports exactly one component —
`.ui.CameraActivity`, on `android.intent.action.MAIN` and `zqc.intent.action.camera`. Every
other receiver in its manifest is AndroidX WorkManager `ConstraintProxy` boilerplate
(`BATTERY_OKAY`, `ACTION_POWER_DISCONNECTED`, `DEVICE_STORAGE_LOW`, `CONNECTIVITY_CHANGE`).
There is no pause or stop action to send. The only routes are `am force-stop` or
`pm disable`, both of which stop the dashcam recording with no guarantee about what restarts
it — to buy nothing.

**Recommendation: do not implement §7. Close it.**

## 16. What is actually limiting the backup, and what to do

The transport is finished work. The constraint moved somewhere else entirely.

### The arithmetic

Measured from the library's own totals — 134.6 GB over 34.7 hours of recording across 1128
files, a mean of **8.62 Mbps**. With both lenses running that is **~2.16 MB/s of wall-clock
driving**. Transfers run at **~32.5 MB/s**.

```
window needed  =  driving time x (2.16 / 32.5)  =  driving time / 15
```

**One second of window pays for fifteen seconds of driving.** So:

| Window | Moves | Covers |
| ---: | ---: | ---: |
| 60 s | 1.95 GB | ~15 min of driving |
| 120 s | 3.90 GB | ~30 min of driving |
| 600 s | 19.5 GB | ~2.5 h of driving |

The median successful window in the run history moved **1.23 GB** — about 38 seconds of
transfer, covering **~9.5 minutes of driving**. Drive longer than that between windows and
the backlog grows permanently, which is exactly what the card shows.

### Ranked, by how much they actually change the outcome

1. **Extend the window. This is the only lever that changes anything.** `dumpsys battery`
   reports `present: false` — there is no backup power of any kind, so the unit dies with
   the ignition. A hardwire kit with an ACC delay, or any small 12 V holdover, converts a
   ~40-second window into whatever it is set to. Ten minutes of holdover moves 19.5 GB,
   which is more than the entire card. Nothing in software competes with this.
   (`wifi_sleep_policy=2` is already "never sleep" and `screen_off_timeout` is already
   effectively infinite, so there is nothing to win in settings.)

2. **Halve the data.** `ingest.camera_filter` = `camera_0` doubles effective throughput at
   the cost of the second lens. Lowering the recorder's bitrate in the vendor app does the
   same to both. 8.62 Mbps is generous for what this footage is used for.

3. **Use the whole window, not just its first forty seconds.** *(implemented)* The
   poller fired one pull on arrival and then nothing, however long the unit stayed. Fixed
   in `poller.py`: after a run that moved files it goes again immediately, and after one
   that found nothing it re-checks every `IDLE_RECHECK_S` (30 s, well inside the five
   minutes the camera takes to close a segment). This is worth the most on exactly the
   windows that matter most — the 13.5 GB run took seven minutes and left ~840 MB behind.

4. **Transport tuning: nothing left.** 32.5 of a 34.9 ceiling.

### Protected recordings were never backed up at all *(fixed)*

The camera **moves** a clip you protect into `DCIM/LockVideo` rather than copying it, so it
leaves `DCIM/Video` entirely — and `SOURCE_PROBE` only ever resolved to `DCIM/Video`. The one
recording anybody deliberately marked as worth keeping was the one recording the backup never
saw. On the live card that was two clips from five days earlier, sitting on a volume that was
96% full and recycling.

`ingest.include_locked` (on by default) now lists both directories. The transfer batches by
directory and runs one `tar` per batch, because `tar` is rooted where it runs — the
alternative, rooting it at `DCIM` so members arrive as `LockVideo/x.ts`, is refused by the
receiver, which rejects any member carrying a path because that is how a tar stream escapes
its staging directory. Deletes are grouped the same way, since `rm` also runs from the
directory. A filename appearing in both places is refused rather than merged: every step
downstream keys on the bare name.

### Separately, and more urgent than any of it: the card is eating footage

Not a throughput finding, but found while benchmarking and it matters more.

The card is **96% full with 1.4 GB free**, and the recorder is recycling the oldest files to
make room. This was observed directly rather than inferred: a benchmark file set shrank from
1729 MB to 1478 MB *between two runs minutes apart*, and both
`20260813230432_camera_1.ts` (the oldest file on the card at the start of the session) and
`20260813233520_camera_1.ts` (in the benchmark set) were gone by the end.

`ingest.delete_after_verify` is **off**, so the app never reclaims space on the card. The
recorder therefore decides what to delete, it deletes oldest-first, and it has no idea which
files have been copied. Any footage the recorder recycles before a window reaches it is gone
permanently.

Turning `delete_after_verify` on is the fix, and it is safe by construction: a file is only
removed after a byte-complete copy has been committed to the footage directory. With the
backlog permanently larger than one window, `ingest.transfer_order` = `newest_first` is also
worth considering, so today's drive is never the thing that gets recycled.

## 17. Recording to internal storage instead of the card

Asked because the card is the visibly weak part. It is — but not for throughput.

| | Removable card | Internal |
| --- | --- | --- |
| Filesystem | vfat (no journal) | **f2fs** |
| Size / free | 30 GB / **1.4 GB (96% full)** | 108 GB / **101 GB (7% used)** |
| Sequential read | ~55 MB/s | ~859 MB/s (cached) |
| Sequential write | — | **170 MB/s** (`conv=fsync`) |
| Mount | FUSE over `/dev/block/vold/public:179,1` | FUSE over `/dev/block/dm-47` |

**It cannot make the transfer faster.** The link caps at ~35 MB/s and the card already
reads at 55, so the storage has never been on the critical path. Moving to something three
times faster still leaves the radio as the ceiling.

**What it would fix is the thing that is actually losing footage.** 101 GB against 1.4 GB
free is the difference between a card that recycles the oldest recordings every few hours
and one that could hold roughly thirteen hours of two-lens footage — which is the data-loss
risk in §16, removed rather than mitigated. f2fs over vfat is worth having on its own for a
device that loses power without warning several times a day.

### Can it be done?

Two halves, and only one of them is ours.

**Reading from there: already supported.** `shell` can read `/storage/emulated/0` (it lists
fine; the FUSE daemon grants it via `sdcard_rw`/`sdcard_r`). `SOURCE_PROBE` now names
`/storage/emulated/0/DCIM/Video` explicitly — the existing `/storage/*/DCIM/Video` glob
could never have found it, because the user directory is one level deeper than the glob
descends. `ingest.source_path_override` remains available for anywhere else.

**Recording to there: unknown, and not answerable from the shell.** `com.zqc.camera` is a
system app at `/system/app/ZqcCamera/ZqcCamera.apk` and its data directory is
`Permission denied`, so its configuration is only reachable through its own UI on the unit.
There is no `DCIM/Video` in internal storage today, and the app exports no component that
would let a storage target be set remotely — its only exported activity is `.ui.CameraActivity`.

So this comes down to: **does the camera app's own settings screen offer a storage or path
option?** If it does, switching it and leaving `ingest.source_path_override` blank should now
work unchanged. If it does not, there is no unrooted way to redirect it — and the fallback is
the one in §16, which is to stop the card overfilling rather than to replace it.

## Sources

- [ADB man page — `push`/`pull` `-z` compression, `shell -T/-t`](https://android.googlesource.com/platform/packages/modules/adb/+/refs/heads/master/docs/user/adb.1.md)
- [SDK Platform Tools release notes — receive windowing 33.0.3, compression 30.0.0/31.0.0](https://developer.android.com/tools/releases/platform-tools)
- [Debian bookworm `adb` 1:29.0.6](https://packages.debian.org/bookworm/adb)
- [AOSP `NotificationShellCmd.java` — `cmd notification post`](https://android.googlesource.com/platform/frameworks/base/+/master/services/core/java/com/android/server/notification/NotificationShellCmd.java)
- [Android 15 behaviour changes — the stopped state](https://developer.android.com/about/versions/15/behavior-changes-all)
- [toybox `netcat.c` — the 4 KB relay and the exec path](https://github.com/landley/toybox/blob/master/toys/net/netcat.c)
- [toybox `portability.c` — `sendfile_len` / `copy_file_range`](https://github.com/landley/toybox/blob/master/lib/portability.c)
- [Android notification runtime permission (POST_NOTIFICATIONS)](https://source.android.com/docs/core/display/notification-perm)
- [`adb exec-out` / exec service — no pty, binary safe](https://android.googlesource.com/platform/system/core/+/5d9d434efadf1c535c7fea634d5306e18c68ef1f)
