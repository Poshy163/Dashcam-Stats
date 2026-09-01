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
and cannot be removed or reliably disabled without root.

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
