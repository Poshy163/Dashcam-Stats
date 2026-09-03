# Head unit reference

Everything known about the dashcam head unit, captured from the live device so that changes
to the ingest code can be made and reviewed **without the car being on the network**. The
unit is reachable only while the engine runs, plus the sleep countdown (§6) — minutes a day.
Anything that would otherwise have to be learned by probing is written down here instead.

Captured 2026-08-28 from the running unit. Identifiers (IMEI, MAC, Wi-Fi SSID) are
deliberately omitted; this repository is public.

---

## 1. Identity

| | |
| --- | --- |
| Fingerprint | `UNISOC/uis7861_6h10_Natv/uis7861_6h10:14/UP1A.231005.007/…:user/release-keys` |
| Model | `Q30_1025HJYEFPSL_U` (whitelabel; vendor prefix `QCTech`) |
| Platform / hardware | `ums9230` / `uis7861_6h10` (Unisoc UIS7861) |
| Android | 14, SDK 34, patch 2025-06-05, `user` build |
| Screen | 720x1920 physical, presented landscape 1920x720 |
| RAM | ~5.8 GB, typically ~100 MB free (browser tab discards are common) |

## 2. Connecting

Network ADB only — there is no USB path in the car.

```sh
adb connect <unit-ip>:5555
```

- The IP is DHCP and **has moved before** (`.122` -> `.214` after a Wi-Fi re-add). Pin it
  with a DHCP reservation. `ingest.unit_adb_address` must match or ingest silently stops.
- `persist.adb.tcp.port=5555` is what makes ADB survive a reboot. The vendor Developer
  Options toggle only sets the runtime `service.adb.tcp.port`, which is cleared on every
  boot — so after a factory reset you must re-enable ADB by hand **and** set the `persist.`
  property, or it is gone again on the next engine start.
- Android randomises its Wi-Fi MAC per SSID, so the unit's MAC changes if the network is
  re-added. Do not identify it by MAC.

## 3. What the `shell` user can and cannot do

```
uid=2000(shell) groups=… 1015(sdcard_rw), 1028(sdcard_r), 1078(ext_data_rw), 3003(inet) …
context=u:r:shell:s0
```

**SELinux is `Permissive`.** That is why `setprop` on `persist.sys.*` works from an ordinary
ADB shell. There is no root, and none is needed for anything the app does.

| Action | Works? |
| --- | --- |
| Read/write/delete on the card via `/storage/Tfcard/…` (FUSE) | yes |
| Read the raw vfat mount `/mnt/media_rw/<VOL>` | **no** — permission denied (mount is `gid=1023 media_rw`, `dmask=0007`; shell is not in `media_rw`) |
| `setprop persist.sys.*` | yes (SELinux permissive) |
| `settings get/put global …` | yes |
| Hold a wakelock (`/sys/power/wake_lock`) | **no** — owned `radio:wakelock`, permission denied |
| `svc power forcesuspend` | yes |
| Read a system app's data dir (e.g. `/data/data/com.zqc.camera`) | **no** |
| Uninstall or reliably disable a `PERSISTENT` system app | **no** |

## 4. Storage

```
/dev/block/vold/public:179,N  /mnt/media_rw/<VOL>  vfat   gid=1023 fmask=0007 dmask=0007
/dev/fuse                     /storage/<VOL>       fuse   <- what shell uses
/dev/block/dm-47              /data                f2fs
```

- TF card: **30 GB, vfat**, no journal. Cold read ~55 MB/s through FUSE.
- Internal: **108 GB, f2fs**, ~104 GB free. Write ~170 MB/s.
- `/storage/Tfcard` and `/storage/sdcard0` are symlinks to the volume id (e.g. `0726-1708`),
  recreated at each mount. **The volume id changes when the card is reformatted** — it has
  rolled `EBDF-E4DD` -> `0726-1708` -> `public:179,25`. Never hard-code it; use the symlink.

### Footage layout

```
/storage/Tfcard/DCIM/
├── pre_<timestamp>_camera_N.ts   <- in-progress segment, written here
├── Video/                        <- completed segments (renamed in)
├── LockVideo/                    <- incident-protected clips (MOVED here, not copied)
└── Picture/
```

- The recorder **stages atomically**: it writes `DCIM/pre_*.ts` and renames the finished
  segment into `DCIM/Video/`. Anything in `Video/` is therefore already complete, so
  `ingest.skip_active_seconds` is belt-and-braces on this camera rather than load-bearing.
- A protected clip is **moved** into `LockVideo/`, leaving the ordinary listing entirely.
  `ingest.include_locked` exists because of this — without it, the one recording somebody
  deliberately marked as worth keeping was the only one never backed up.
- Filenames: `YYYYMMDDHHMMSS_camera_0.ts` (road) and `_camera_1.ts` (interior/rear).
- Segments are ~5 minutes, ~205–308 MB each. Measured mean across the library: **8.62 Mbps**,
  i.e. ~2.16 MB/s of wall-clock driving with both lenses.
- The card auto-recycles oldest-first when full. Confirmed by observation: files vanished
  between two benchmark runs minutes apart.

## 5. Network and the throughput ceiling

```
Wi-Fi standard: 5 (802.11ac)      Frequency: 5 GHz
Link speed: 351–433 Mbps          Max Supported Tx/Rx: 433 Mbps   <- the device cap
RSSI: -48 to -52 dBm              TX errors / dropped / collisions: 0
```

`Max Supported` equalling the negotiated rate means the radio is **single-stream 802.11ac
80 MHz**. There is no better rate to negotiate, so placement, channel and antenna work buys
nothing.

### Benchmark matrix (3 runs each, 10 GbE receiver)

| Cell | What it isolates | median MB/s |
| --- | --- | ---: |
| H0 | link only, exec mode, `/dev/zero` | **34.88** |
| H1 | link + toybox `nc` relay | 32.43 |
| H2 | card + exec mode, no relay | 30.99 |
| H3 | **card + relay — the current transport** | **32.07** |
| H4 | 4 parallel streams, aggregate | 33.51 |
| — | cold card read via FUSE | 55 |
| — | production, unit to NAS, staged and committed | 30.7 |

**The transport is at ~93% of the radio's ceiling. Do not spend effort here.** Specifically
ruled out, with the measurement that killed each:

- **exec mode** (`nc -l -p P COMMAND`) — H2 is not better than H3. The relay only costs
  anything against `/dev/zero`, where `dd` outruns the link.
- **parallel streams** — 33.5 aggregate against 34.9 for one. Same radio.
- **compression** — `.ts` gzips to 97.6%. It is already H.264.
- **raw mount** (bypassing FUSE) — permission denied, and FUSE is not the constraint anyway.
- **pausing the recorder** — costs ~10% of one core against 414% idle of 800%, and ~2 MB/s
  of card writes against a card that reads at 55. Nothing to reclaim. There is also no
  exported pause action; only `am force-stop`, which is not worth the risk.

## 6. Power and the ignition window

The unit has **no battery** (`persist.sys.device.with.battery=false`) and runs on the car's
constant 12 V, with ACC as a *signal* rather than the power source. On ignition-off it
**sleeps** (RAM retained) rather than powering off, then fully powers off later.

| Property | Value | Meaning |
| --- | --- | --- |
| `persist.sys.sleep.countdown.time` | **300 resting / 900 during backup** (shipped: 3) | **Seconds awake after ignition-off.** Drives an on-screen countdown. **The radio stays up for all of it.** |
| `persist.sys.state.system_sleep` | `true` | Sleep enabled |
| `persist.sys.time.system_sleep` | `24` | How long it stays asleep before full power-off (hours; from `array_system_sleep_time`) |
| `persist.sys.shutdown.countdown.time` | `3` | Countdown for a real shutdown |
| `persist.sys.fast.shutdown` | `false` | Leave alone — `true` would hard-cut |
| `settings global acc_status` | `1` / `0` | **Ignition on / off.** Flips within ~1 s of ignition-off and is readable while the unit is still awake |
| `settings global wifi_sleep_policy` | `2` | Never sleep Wi-Fi (already optimal) |

### The measurement that matters

| `sleep.countdown.time` | Reachable after ignition-off |
| ---: | --- |
| 3 (shipped) | effectively zero — gone before anything can connect |
| 300 (resting) | **5 min**, radio up throughout |
| 900 (managed backup/recovery) | **15 min** |

The production OBD companion carries the same fixed 900/300-second policy as a
device-side fallback: Wi-Fi arrival or a valid ingestion request selects 900, while a
definite Wi-Fi loss with no request selects 300. The policy cannot be disabled. The server
displays it and both values read-only, coerces older false/override rows back to the fixed
contract, ignores stale cached toggle values in the puller, and remains authoritative while
the unit is home and narrows the window only after footage, OBD and radio recovery are
all proven complete. The three Backup entries are read-only evidence of that contract, not
controls; there is no independent server-side off switch or duration override.

This is the **only** lever that changes how much footage a park is worth. At 32 MB/s, fifteen
minutes is ~29 GB. Before it was found the working window was ~40 s, and the conclusion
"sleep is a hard cutoff, nothing left in software" — which was wrong.

The app never forces suspend. Once the card is positively inventoried empty, OBD has
nothing waiting on the unit, and the durable radio transition proves Bluetooth, hotspot
and logger recovery complete, it verifies the five-minute resting property and lets
Android's normal countdown end the window. Unknown inventory or recovery state keeps the
15-minute window open. Radio reconciliation also fails closed if the 15-minute property
cannot be read back: it does not start a restore that may be cut off by the resting window.
A failed five-minute write/readback is logged and never followed by a forced suspend.

### Do not repeat these

- **`persist.sys.support.sleep.net.control=true` boot-loops the unit.** Tried on the guess
  that it would keep Wi-Fi alive during sleep. The unit cycled up/down and had to be
  recovered; settings including Wi-Fi and `persist.adb.tcp.port` were lost in the process.
  Leave it empty.
- Sleep itself **drops the radio completely** — no ping, no ADB. Extending
  `persist.sys.time.system_sleep` gains nothing for backups; only the *countdown* does.
- **Do not stop the hotspot without a protected recovery strategy.** The unrooted
  tethering binder can stop it, while the ordinary WiFi command and configuration reads
  are restricted. Measured before the guard existed: fifteen stops, zero explicit starts.
  The durable path now requires either an exact configuration capsule or the separately
  enabled Zlink Bluetooth-rearm capsule, and it must observe the serving AP return before
  declaring recovery complete.
- **Do not suspend early.** `svc power forcesuspend` works, but
  the camera keeps recording after ignition-off and closes a segment about every minute,
  which wakes the unit straight back up. The result is a suspend/resume loop — measured
  at roughly one cycle a minute for the whole countdown — that re-announces itself each
  time and puts a suspend in the middle of an active recording. Only change the bounded
  countdown and let Android enter sleep normally.
- There is no way to keep the unit awake indefinitely, and you would not want one: no
  battery, constant 12 V, and no voltage cutoff available to us, so a stalled sync would
  flatten the car battery. Always leave the countdown as the backstop.

## 7. Packages of interest

| Package | Path | Notes |
| --- | --- | --- |
| `com.zqc.camera` | `/system/app/ZqcCamera` | The recorder. `SYSTEM` + `PERSISTENT`, runs as `system`, holds **both cameras open continuously**. Only exported activity is `.ui.CameraActivity` (`zqc.intent.action.camera`) — **no pause/stop action**. Renders on a custom surface, so `uiautomator` returns empty containers and its UI cannot be driven reliably. |
| `com.zqc.functioncore` | `/system/app/FunctionCore` | Power model: `POWER_ACTION_SLEEP` / `BEFORE_SLEEP` / `WAKEUP` / `SHOCK_WAKEUP` / `POWER_OFF`, `com.zqc.action.system_sleep`, `SUPPORT_SLEEP_NET_CONTROL`. |
| `com.zqc.zqcsettings` | `/system/app/ZqcSettings` | Vendor settings UI (`.SettingsActivity`), readable by `uiautomator`. Sleep options sit behind **Factory Settings**, which is password-protected — but the underlying properties are writable directly (§3). |
| `com.abfota.systemUpdate` | `/system/app/Rsota` | OTA updater, see §8. `SYSTEM` + `PERSISTENT`. |
| `com.zjinnova.zlink` | `/system/app/CarZhiJian` | CarPlay / Android Auto. Packed with a commercial protector (`libshell-super`), so its strings are encrypted — static analysis will not reveal its endpoints. |
| — | port `8080` | `ZLMediaKit` streaming server (the vendor "Stream Media" feature). |

Both cameras are ordinary Camera2 devices (`Number of camera devices: 2`, back + front) but
are held open by the persistent recorder, so no third-party app can take them. The device
advertises no concurrent multi-camera support.

## 7a. Logging (disabled in firmware)

**The unit ships with Android logging switched off entirely**, which is the single biggest
reason the recorder can fail without leaving anything to read:

| Property / service | Shipped value | Meaning |
| --- | --- | --- |
| `persist.log.tag` | `S` | Silent. Global suppression of every tag. **Persists across reboot.** |
| `init.svc.logd` | `stopped` | The log daemon is not running; `logcat` returns `Logcat read failure: No such file or directory` |

Both are reversible from the `shell` user, but by *different* mechanisms and with different
lifetimes:

```sh
setprop persist.log.tag S            # keep the vendor's blanket silence (survives reboot)
setprop log.tag.AndroidRuntime E     # raise one tag; does NOT survive reboot
setprop ctl.start logd               # starts the daemon; does NOT survive reboot
```

`start logd` proper is refused (`Must be root`); setting `ctl.start` works because the shell
user holds that permission for this service. Neither the per-tag levels nor the `ctl.start`
half persist, so **both are re-applied after every ignition cycle** — which is why
`app/ingest/unit_logs.py` runs them on every arming rather than once.

**Do not clear `persist.log.tag`.** The first version of the collector did (`setprop
persist.log.tag ""`) and filtered on the reading side. That unleashed every native writer —
the media codec and camera server alone log hundreds of kilobytes a second — and the cost
is paid by the writers and by `logd` whether or not anyone reads the result. Measured back
to back with `top` over ten seconds, engine on, recorder running:

| | `persist.log.tag` cleared | `persist.log.tag=S` + allow-list |
| --- | --- | --- |
| `logd` CPU | 23.6 % of a core | 14.9 % |
| `main` buffer growth in 10 s | rolled over (>1,100 lines) | 0 lines |
| our own tags captured | yes | yes |

So the blanket silence stays and only an allow-list (`ingest.unit_log_allowed_tags`,
defaults in `unit_logs.DEFAULT_ALLOW_TAGS`) is raised with `log.tag.<tag>=E`. `log.tag` does
not gate the kernel buffer, so the reading-side filter is still needed as well.

### CarPlay frame timing, sampled on the unit

The CarPlay picture is drawn on a `SurfaceView[](BLAST)#N` layer of Zlink's, and the timing
of *that* surface — not Zlink's own views, which `dumpsys gfxinfo` reports — is where the
CarPlay lag lives. `dumpsys SurfaceFlinger --latency '<layer name>'` prints the display
period and the last 128 frames' desired/actual/ready present times in nanoseconds; the
intervals between successive actual-present values give fps, and any interval over about
2.5 periods is a frame that landed late. Measured with CarPlay in use: 23–26 fps delivered,
median interval 35 ms, p95 70 ms, worst 106 ms, a quarter to a third of frames late.

`app/ingest/carplay_timing.py` ships a toybox script (`carplay_timing.sh`) that does this
every few seconds while a phone is attached to the hotspot, emitting one line per surface
under the logcat tag `CarPlayTiming` (at error priority, because the collector keeps only
`*:E`) and to `/data/local/tmp/dashcam_carplay_timing.log`. Gotchas learned the hard way:
the layer names carry no package name (`--list | grep -i surfaceview`, skip "Background
for"); toybox `ps` shows the script as `sh`, so check `/proc/<pid>/cmdline` instead; and
inside a shell loop, redirect adb's stdin (`</dev/null`) or it consumes the loop's input.

### Volume, and why the capture is filtered

Measured live. The buffers are small (256 KiB each, `main` permanently full and wrapping),
so a poll-the-ring-buffer approach loses almost everything; the capture has to be continuous.

| Filter | Rate | Per hour |
| --- | --- | --- |
| `*:W` (warning and above) | ~22 KiB/s | **~80 MB** |
| Noise tags silenced, `*:E` elsewhere | ~0.34 KiB/s | **~1.2 MB** |

79% of warn-level output is two tags — `ParamSet` and `isp_alg_fw` — the camera ISP printing
tuning parameters frame by frame. Silencing them **on the unit** (a `TAG:S` filter spec, so
the line is never written) is what makes collection affordable.

### What is worth keeping

| Tag | Why |
| --- | --- |
| `ZQC-CamSubStream0` / `1` | The recorder's real per-camera frame rate (`ObtainYuvRate:16/s.cameraId 0`). A **direct** liveness signal per camera, unlike the newest-file-age heuristic in §4. |
| `UnisocWatchdog` | Watchdog firing |
| kernel `mmc` / `FAT-fs` / `blk_update_request` | The card developing errors — what `errors=remount-ro` turns into a silent stop |
| `ThermalManagerService` | 64-68°C on a desk; worse in a sunlit car |
| `BatteryService` | Prints `mIsAccCable=true`, a second opinion on ACC state. Silenced by default (~1 line/s) but useful to re-enable when chasing a power-window question. |

### Filterspec gotcha: long tags are silently ignored

**A `TAG:S` entry longer than about 25 characters is accepted on the command line and then
does nothing.** Measured on this unit: every deny entry of 25 characters or fewer was
honoured, while both 32-character entries (`SprdActivityDebugConfigsUtilImpl`,
`vendor.sprd.modules.thm@2.0-impl`) kept appearing at 126 lines per capture despite being
in the running `logcat` command line.

Two consequences:

- Every **privacy** tag must be kept short enough to be honoured, because those have to be
  stopped *on the unit* — dropping them after transfer is too late. All six networking tags
  are 18 characters or fewer, and a test asserts that.
- The deny list is therefore enforced a **second** time on the server
  (`unit_logs.drop_silenced`), so a long entry still means what it says.

Do not try to verify this with `log -p e -t "<32-char tag>"`: the `log` command truncates
the tag it emits, so the probe silently tests a shorter tag than you typed and the filter
appears to work.

Format note: use `-v threadtime -v year -v UTC`. Plain `threadtime` omits the year and uses
the unit's local clock, so a parser would have to guess both; the modifiers make every line
absolute. `logcat` also does its own bounded rotation (`-f FILE -r KIB -n COUNT`), so no
shell script is needed to cap the file.

## 8. Update / telemetry endpoints

`com.abfota.systemUpdate` is a rebadged **Redstone FOTA SDK** (`com.redstone.ota.sdk`,
OTA sw V9.4.1). Endpoints are plaintext in the APK's `assets/config.xml`:

```
https://fota.redstone.net.cn:7100/service/request     <- production (is_https=true)
https://fota.redstone.net.cn:7100/service/report
http://fota.redstone.net.cn:6100/service/request      <- production, plain
http://fota.mwhtml5.com:6100/service/request          <- test (is_test=false, unused)
http://fota.livedevice.com.cn:6100/service/uploadUrl  <- log upload
```

Config: `auto_check_cycle_selected=1` (**daily**), `auto_download=false`, `is_ab_mode=true`.
Each check sends IMEI, brand, model, firmware version and an SDK key. Its own log lives at
`/sdcard/Documents/fotalogs/RsOta_*.log`.

**To stop it:** block those hosts at the router or DNS. The app is `SYSTEM` + `PERSISTENT`
and cannot be removed or reliably disabled without root. (`dumpsys package` prints
`enabled=0` for it — that is `COMPONENT_ENABLED_STATE_DEFAULT`, i.e. *enabled*, not
disabled; it fires on `BOOT_COMPLETED` and holds JobScheduler jobs and alarms for the daily
check.)

**What it has ever been told (read from its own logs, 2026-09-03):** every check on record —
twenty of them across a week, the last one minutes after a reboot — was an OMA-FUMO session
(`"session":"FUMO-REQ"`, `"version":"A1.0"`) answered with
`{"code":"1500","msg":"Not found package"}`. The identity it sends is brand `QCTech`, model
`Q30_1025HJYEFPSL_U` (`ro.product.model`), firmware
`QC_Q30_1025HJYEFPSL_U_Q30_MB_1025_CarCharger_user_2026060112` (`ro.build.display.id`), OS
14, a dummy IMEI (no modem) and a utdid. There is no published firmware for this model, so
no OTA can arrive; the updater's logs are the update check, and nothing needs replaying.

**Local update path** (from the APK): `have_local_update=true`, so the updater's UI offers a
local update from the internal storage root — it looks for `/storage/emulated/0/update.zip`,
a standard A/B package (`payload.bin` + `payload_properties.txt`) staged to
`/data/ota_package/` and verified against `/system/etc/security/otacerts.zip`. Only a package
signed with this ROM's OTA key applies. Also `key_force_update=true`: if the seller ever
publishes, the updater is configured to force it.

**Zlink's own updater** (Settings → Check for Updates, in the app) exists on 6.1.02 and was
tried: Zlink's `zj` log shows `update error: … HTTP 404 Not Found`. No package there either;
a newer Zlink reaches this unit only as a seller-supplied file. The public APK mirrors carry
older 6.0.x builds, and the "Dofun Play" store other ZQC firmware uses is not on this unit.

## 9. Firmware / rooting feasibility

Treble-capable (`ro.treble.enabled=true`, dynamic partitions, virtual A/B, VNDK 33), but:

```
ro.boot.verifiedbootstate = green     ro.boot.flash.locked   = 1
ro.oem_unlock_supported   = 1         sys.oem_unlock_allowed = 0
settings global oem_unlock_allowed -> null   <- the toggle was never exposed
```

The bootloader is locked with no in-software unlock path. Unlocking would need BROM mode
plus this exact model's stock FDL1/FDL2 from its PAC firmware, which is not published for a
whitelabel unit. **And a GSI would replace `/system`, taking `ZqcCamera` and every vendor
app with it** — no recorder, no burned-in overlay, so the telemetry pipeline dies too. See
`dashcam-backup-investigation.md` §17.

**No open-source route exists either way.** There is no LineageOS or other open ROM for the
Unisoc UIS786x head-unit family — the community around these boards has only modded factory
ROMs, and those target FYT-based UIS7862 units, not this ZQC UIS7861 board. And CarPlay
cannot be replaced by open software on Android: it needs Apple's MFi authentication chip, so
every open implementation (LIVI, the pi-carplay lineage, Crankshaft/OpenAuto for Android Auto)
runs on a Linux SBC with either that chip or a dongle that contains it. The practical
replacement for Zlink on this unit is a Carlinkit-class dongle, which carries the MFi chip and
its own radio and hands the unit finished video.

## 10. Command gotchas

- `tar -C DIR c FILE` **fails** on toybox (`Needs -txc`). Use `cd DIR && tar c FILE`, or
  `tar cC DIR FILE`.
- `/tmp` does not exist. Use `/data/local/tmp`.
- `am start … VIEW` opens a **new browser tab every time** unless the intent carries
  `--es com.android.browser.application_id <id>`; with it, one tab is reused *and reloaded*.
- `am` reports "no browser" on **stdout with exit status 0** — check the reply text, never
  the return code.
- `uiautomator dump` returns empty containers for `com.zqc.camera` (custom surface). It does
  work for `com.zqc.zqcsettings`.
- Chrome's DevTools socket is the way to see what the car's browser actually did:
  `adb forward tcp:9222 localabstract:chrome_devtools_remote`, then
  `http://127.0.0.1:9222/json` for the tab list, and CDP over the returned WebSocket.
- From Git Bash on Windows, prefix `adb` commands that take absolute *device* paths with
  `MSYS_NO_PATHCONV=1`, or the path is rewritten into a Windows one.
