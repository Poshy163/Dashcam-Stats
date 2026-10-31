# Head unit system survey, 31 October 2026

A full read-only survey of the head unit's hardware, firmware and software. It covers
what else could be flashed, and which improvements are left. Captured live over ADB on
31 October 2026, 16:15–16:25 local time, with the car parked and the ignition off. Nothing
was changed on the unit during the survey. Device identifiers are omitted because this
repository is public. The earlier reference is [head-unit-reference.md](head-unit-reference.md).

## Summary

- The unit is a **portable 10.26-inch CarPlay screen with a built-in dashcam**. It uses a
  Unisoc **T606** SoC; `UIS7861` is the automotive name burned into the efuse. Every core
  maxes out at **1.612 GHz**, which is the chip's rated clock, not a firmware cap.
- **Heat is the largest problem.** Since 29 October, about 90 % of Android's thermal
  readings were *critical* (status 4). The SoC reached **96 °C** today while parked, above
  its first trip point (95 °C), yet nothing throttled. At that temperature the recorder
  crashed twice today, the first recorder crash anywhere in the server's retained logs.
- **No safe alternative firmware exists.**
  - The stock updater has never been offered a package.
  - There is no open ROM for this chip.
  - A GSI would delete the recorder and ZLink.
  - Rooting this exact board family has worked once, on an **Android 12** sibling. An
    **Android 14** sibling (ours is Android 14) was bricked trying, and no Android 14
    recovery firmware has been published.
- The improvements left, in order: physical cooling and making sure the unit sleeps when
  parked; easing the shared video engine (a rear-camera resolution test); verifying the
  1664x624 CarPlay resolution after the next drive; and a ZLink update through the seller.

## 1. Hardware

| Part | What it is (measured) |
| --- | --- |
| SoC | Unisoc **T606** (`ro.soc.model`), chip id `UMS9230-AC`, efuse `UIS7861`, board `uis7861_6h10`, 12 nm |
| CPU | 2× Cortex-A75 (`0xd0a`, cpus 6-7, capacity 1024) + 6× Cortex-A55 (`0xd05`, cpus 0-5, capacity 544), all **614 MHz–1.612 GHz**, governor `uscfreq` (Unisoc) |
| GPU | Mali-G57 (MP1), driver r41p0, OpenGL ES 3.2 + Vulkan (HWUI uses Vulkan), 384–850 MHz, `simple_ondemand` |
| Video engine (VSP) | One hardware block for **all** H.264/H.265 encode and decode, 256–384 MHz. It has spent **83 % of uptime at its 384 MHz maximum** (`trans_stat`) |
| RAM | 6 GB (`ro.boot.ddr_size=6144M`), 3.7 GB zram swap (lz4), ~2.5 GB available parked |
| Internal storage | **Samsung 128 GB eMMC** (`DV6DBB`, made 05/2022), wear estimate 0–10 % used |
| Card | SanDisk `SH32G` 32 GB microSD (made 07/2022), vfat |
| Display | MIPI-DSI panel, 720x1920 portrait rotated to 1920x720, **57 Hz only**, density 240, ~364 nits max |
| Touch | Jadard touchscreen controller (`jadard_ts`) |
| Cameras | 2 cameras, each 1920x1088, Camera2 *LIMITED*. The rear camera reaches the SoC through a **Techpoint TP2804 AHD decoder** (`tp2804_sub 2M`). SerDes driver modules are loaded (`ums9230_serdes`) |
| Radio | One combined Wi-Fi 5 (1x1, max 433 Mbit/s) + Bluetooth + GNSS chip. The GNSS service reports it as **UMW2652** (Marlin3 Lite family). It hosts the CarPlay hotspot, the home Wi-Fi link and the OBD adapter's Bluetooth link at the same time |
| GNSS | Built-in GPS (`gpsd`, `vendor.sprd.hardware.gnss-service`) |
| Audio | SC2730 PMIC codec, FM driver (`sprd_fm`), broadcast-radio HAL |
| Power | No battery. Powered over its USB power input (`power_supply/usb online=1`); the "battery" is a fixed 4.0 V / 65 °C stand-in. The firmware name says `CarCharger` |
| USB | One `musb` OTG controller, permanently in **device** mode (`b_idle`), used for power and ADB. Android's USB host manager has never seen a device (`num_connects=0`), and there is no USB-C role controller |
| Modem | LTE modem partitions exist (`l_modem`, `l_ldsp`, `l_gdsp`), but this unit has no SIM or cellular radio in use (`vendor.ril-daemon` stopped) |

### Thermal behaviour

- **Trip points:** `soc-thmzone` has passive trips at 95 °C and 105 °C and a critical trip
  at 120 °C. The cooling devices (`cpufreq-cpu0/6`, `thermal-cpu-*`, `devfreq-gpu`) all
  read `cur_state 0` at 96 °C, so nothing throttles before 105 °C.
- **Today:** SoC 96 °C, CPU top 94–95 °C, GPU 91 °C, board 82 °C, skin 82 °C, with the car
  parked and the ignition off.
- **Android's thermal status:** in the server's unit log, 1,014 of 1,113
  `ThermalManagerService` readings from 29–31 October were status 4 (*critical*). Lower
  levels appeared only on the evening of 30 October.
- **Where the heat comes from:** two minutes after the recorder stopped writing, the SoC
  had dropped from 96 °C to 88 °C. The recording pipeline is the main source.

### What runs while parked (ignition off, recorder alive)

| Process | CPU (100 % = 1 core) |
| --- | ---: |
| Camera HAL (`camera.provider@2.4`) | 134–137 % |
| `com.zqc.camera` (recorder) | 69–84 % |
| `media.unisoc.codec2` (VSP driver) | 27–37 % |
| SurfaceFlinger | 27–31 % |
| mediaserver | 25–31 % |
| MediaProvider (indexing the card) | 18–31 % |
| cameraserver, system_server, logd, hwservicemanager | 12–22 % each |

That totals about **3.5 of 8 cores** with nobody using the screen, and it is mostly the
dashcam. Load average is 17–23.

## 2. Firmware

| Item | Value |
| --- | --- |
| Display build | `QC_Q30_1025HJYEFPSL_U_Q30_MB_1025_CarCharger_user_2026060112` |
| Brand / model | QCTech (Qicheng) / Q30_1025HJYEFPSL_U |
| Android | 14 (`UP1A.231005.007`), SDK 34, security patch 2025-06-05 |
| System image | Unisoc generic `ussi_arm64` system with a device-specific product partition |
| Vendor / ODM | Android 13 (`TP1A.220624.014`), vendor patch **2024-04-05**, VNDK 33 |
| Kernel | 5.15.123 GKI, clang 14.0.7, built 1 June 2026 |
| Build | `user`, `release-keys`, built by `wming` on 29 May – 1 June 2026 |
| Updates | A/B with virtual A/B (compressed snapshots, `update_engine`), active slot `_a` |
| Verified boot | AVB 1.2, **locked**, state **green**, dm-verity enforcing |
| Unlock | `ro.oem_unlock_supported=1`, but `sys.oem_unlock_allowed=0` and the toggle is hidden |
| Root | none: `ro.debuggable=0`, `ro.secure=1`, `ro.adb.secure=1`, no `su` |
| SELinux | **Permissive** (why `setprop persist.*` works from the shell) |
| Encryption | file-based, metadata encryption on |
| File systems | system/vendor/product/odm are read-only **erofs** inside `super`; `/data` is f2fs (108 GB) |

**Partition layout (eMMC, Unisoc standard):**
- Boot chain: `spl_a/b` (in the eMMC boot areas), `sml` (Arm trusted firmware), `trustos`
  and `teecfg` (Trusty TEE), `uboot`, `hypervsior`.
- Kernel images: `boot`, `init_boot`, `vendor_boot`, `dtb`, `dtbo`.
- Verified-boot metadata: `vbmeta`, `vbmeta_system`, `vbmeta_system_ext`, `vbmeta_vendor`,
  `vbmeta_product`, `vbmeta_odm`, `avbmeta_rs`, `common_rs1/2`.
- Modem and DSP firmware: `l_modem`, `l_ldsp`, `l_gdsp`, `l_agdsp`, `pm_sys`, plus the NV
  partitions `l_fixnv*`, `l_runtimenv*` and `l_deltanv`.
- `super` (5.5 GB): all read-only images.
- Vendor data: `prodnv`, `miscdata`, `persist`, `calinv`, `qcdata`, `userprivate` (2 GB),
  `blackbox` (500 MB), `sysdumpdb`.
- Boot logos: `logo`, `fbootlogo`.
- `userdata` (108 GB).

**Updater:** the RedStone FOTA client (`com.abfota.systemUpdate` V9.4.1) is active. Every
check on record was answered `1500 Not found package`. It accepts a local
`/storage/emulated/0/update.zip` only if it is signed with this ROM's OTA key. See
[the 25 September update check](head-unit-update-check-2026-09-25.md).

## 3. Software

| Role | Package | Version |
| --- | --- | --- |
| CarPlay / Android Auto | `com.zjinnova.zlink` (`/system/app/CarZhiJian`), plus a root daemon `z-link -c qicheng` started by `/system/bin/zlink5.sh` | 6.1.02 (600102), targetSdk 26, runs as an ordinary app uid |
| Dashcam recorder | `com.zqc.camera` (system uid, persistent) | 1.0.0_2026052613 |
| Power / ACC | `com.zqc.functioncore` (persistent) | 1.0.5 |
| Vendor settings | `com.zqc.zqcsettings` | 1.1.0_2026052819 |
| Launcher | `com.android.launcher3/com.zqc.launcher.HomeActivity` (ZqcLauncher3) | — |
| Other vendor apps | NaviScreenSaver (persistent), USBMediaPlayer 2.0.0, VideoList, AudioRoute, user manual, ZXBluelet 1.2.5 (Bluetooth helper) | — |
| OTA | `com.abfota.systemUpdate` (persistent) | V9.4.1 |
| Google | GMS 26.37.37, Play Store 53.7, Chrome 151, WebView 155, Maps 25.41, Google app 16.43, Gboard, Speech Services | — |
| Unisoc tools | EngineerMode, LogManager, LinkTurbo, CameraCalibration, ValidationTools, SGPS, AIEngine, UniWifi, SilentReboot | — |
| Ours | `com.dashcamstats.obdlogger` | 0.3.23 (36) |
| Preloaded third-party | GPSTestPlus | — |
| Disabled | NFC, PowerSaveModeLauncher | — |

**Recorder settings in properties:**
- Both cameras record 1920x1080 at 25 fps (`persist.sys.zqc.record.resolution` and
  `.framerate`), 8 Mbit/s each (`persist.sys.zqc.record.bitrate=8:8:8:8`).
- Preview is 1920x1080.

The H.264 encoder declares 245,760 macroblocks/s, about one 1080p30 stream. The recorder
asks for two 1080p25 streams (~408,000/s). CarPlay's decoder runs on the same video engine,
which is pinned at its top clock. That is not proof of contention, but it is the one shared
resource the earlier decoder checks did not isolate.

**ZLink data cannot be exported without root.** The app has no allow-backup flag, so
`adb backup` returns nothing. Its hidden video panel (`fragment_video_adjustment`) and its
private settings stay out of reach.

## 4. Today's recorder crash (new)

- **16:17:58 and 16:18:45:** `com.zqc.camera` aborted twice with
  `Failed HIDL return status … DEAD_OBJECT`. The camera HAL had died underneath it. Both
  stacks start in `GpioManager$GpioHandler.handleMessage` →
  `CameraService$8.onAhdStateChanged` → `dealCameraPlugin` → `CameraInstance.setPlugin`,
  i.e. the rear AHD decoder reported a signal change. The SoC was at 96 °C.
- **Afterwards:** a new recorder and camera HAL came up at 16:18:42. Frames were flowing
  (`ObtainYuvRate` 20–23/s), but **no segment was written after 16:18**. Because the
  ignition was off (`acc_status=0`), the recorder restarted in its ignition-off state.
  Recording should resume on the next ignition-on. Check that after the next drive.
- **History:** these are the only two `com.zqc.camera` tombstones in the server's unit log
  history. The previous tombstone on the unit was `media.extractor` on 30 October.
- **Link to the resolution change:** the crash came 8 minutes after the 1664x624 change.
  The unit recorded normally for those 8 minutes, and the trigger was a hardware plug event,
  so heat is the more likely cause. If it happens again, revert the resolution
  (`wm size reset`) to rule it out.

## 5. What could be flashed

| Option | Verdict | Why |
| --- | --- | --- |
| Newer stock firmware | Ask the seller | The updater has never been offered a package. Only a package signed with this ROM's key installs. |
| A sibling's Android 14 OTA (posted on Hovatek for `Q30_1026EWT2SL`) | **Do not flash** | It is a different variant. If it shares the vendor key it may install and then fail to boot. |
| Root (Magisk-patched `boot`) | Possible in principle; **not recommended** now | See below. |
| GSI / custom ROM | Not viable | No LineageOS or other open ROM exists for UIS7861. A GSI replaces `/system`, which removes the recorder (system uid, platform-signed) and ZLink, i.e. the dashcam, the overlay telemetry and CarPlay. |
| Replacing ZLink with open-source CarPlay | Not possible on Android | CarPlay needs Apple's MFi chip. Open implementations run on Linux boards with a dongle. |
| Carlinkit-style dongle | Not possible on this unit | A dongle needs a USB host port, and the only USB controller is device-only. |

**Root: what is known about this board family.** A
[Hovatek thread](https://www.hovatek.com/forum/printthread.php?tid=49095) covers
`uis7861_6h10` units sold as W60N, Q30 (`Q30_1025EFPSL`, `Q30_1025E4GFPSL`,
`Q30_1026EWT2SL`) and Q90E.

- **The one success:** one owner unlocked and rooted an **Android 12** unit. They entered
  download mode with a button behind the reversing-camera jack, unlocked with Cheetah Tool
  Pro (a paid tool), patched `boot_a` with Magisk, and flashed it with fastboot. Re-signing
  vbmeta caused a boot loop.
- **The brick:** an **Android 14** Q30 owner bricked their unit by flashing Android 12
  bootloader files. Nobody has posted Android 14 firmware that would recover it.
- **The download-mode command:** another post says `reboot autodloader` puts these units
  into download mode over ADB. **Never run it here.** Without matching firmware the unit may
  not come back.
- **The open-source alternative:** the
  [CVE-2022-38694 unlock tools](https://github.com/TomKing062/CVE-2022-38694_unlock_bootloader)
  support several ums9230 phones, but they need device-specific files, and none exist for
  this unit.

Root would unlock ZLink's private video settings, CPU and thermal policy, Wi-Fi band
control and packet capture. The cost would be a likely `/data` wipe (our OBD app, ZLink
pairing and activation, the ADB setup) and a real chance of a unit with no recovery path.
The precondition for even considering it is a matching Android 14 PAC firmware from the
seller.

## 6. Improvements left, ranked

1. **Cool the unit (physical, highest value).** Use a reflective windscreen shade when
   parked. Keep the back of the unit clear of the glass and dash. A stick-on aluminium
   heatsink or a small 5 V fan on the rear housing would help. At 95 °C and above, the
   crashes, the CarPlay stalls and the card wear all get worse.
2. **Let it sleep after parking.** The normal 5-minute countdown stops the dashcam
   pipeline (~3.5 cores) and lets the unit cool. Maintenance holds cancel the countdown,
   and after one the unit stays awake until a real ignition cycle. It has been awake and
   hot since this afternoon's hold.
3. **Ease the shared video engine (a test, your call).** Record the rear camera at 720p,
   or 15–20 fps, for one daytime drive and compare CarPlay. This trades rear footage detail
   for headroom. Use the recorder's own settings screen, not `setprop`.
4. **Verify the 1664x624 CarPlay resolution** after the next drive (codec width and
   height, bitrate under ~25 Mbit/s, no connect-time freezes).
5. **Trim background load (safe, reversible).**
   - The Google app (`com.google.android.googlequicksearchbox`) runs three processes
     (~170 MB) and froze (ANR) at 03:00 today. A CarPlay unit doesn't need it:
     `pm disable-user --user 0 com.google.android.googlequicksearchbox`; revert with
     `pm enable`.
   - MediaProvider spends 0.2–0.3 of a core indexing the card. A `.nomedia` file would stop
     that, but it was rejected earlier because the recorder's clip browser may rely on the
     index. Test it before relying on it.
6. **ZLink update via the seller or KalosBox.** This is still the only route to the hidden
   video settings without root. Ask for 6.1.07 or newer for this exact build.
7. **Card:** replace the 2022 32 GB card with a high-endurance one. The heat makes card
   wear worse.

## Sources

- Live ADB survey, 31 October 2026 (raw output kept in the session scratchpad, not in the
  repository).
- Server unit-log history (`/api/unit-logs`, tags `ThermalManagerService` and `DEBUG`).
- [Hovatek: SPRD/Unisoc UIS7861 head unit firmware and root thread](https://www.hovatek.com/forum/printthread.php?tid=49095).
- [TomKing062/CVE-2022-38694_unlock_bootloader](https://github.com/TomKing062/CVE-2022-38694_unlock_bootloader).
- [Unisoc T606 specifications (Notebookcheck)](https://www.notebookcheck.net/Unisoc-Tiger-T7200-T606-Processor-Benchmarks-and-Specs.582689.0.html).
