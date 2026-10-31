# CarPlay lag: remaining fix options - 30 October 2026 (evening)

Offline follow-up to `carplay-sampler-overhead-2026-10-30.md` (head unit offline). Combines
saved evidence, static inspection of the installed ZLink 6.1.02 APK and web research. No
setting was changed. Ranked by evidence and cost.

## What the evidence now says

- Every user-labelled responsive drive was evening/night; every laggy one was daytime.
  Head-unit SoC temperature tracks that (good 35-62 C, bad up to 80-106 C).
- On the laggy afternoon trips the user reported Waze position jitter and no speed while
  connected, and Waze working instantly on the iPhone after disconnecting CarPlay.
- ZLink does not forward the unit's GPS: every saved session logs `platform_get_gps_on: 0`
  and never `StartLocationInformation`. The Waze jitter is on the phone.
- The phone renders and encodes CarPlay at 1920x720, 30 fps, about 17-21 Mbit/s. Nothing
  measured on the head unit (relay bytes, socket queues, decoder throughput, thermal
  clamps, scans, TCP buffers) differs between good and bad drives. ZLink's Java queue is
  not observable, and ZLink discards the phone's frame timestamps (Codex follow-up).
- Most consistent explanation: phone-side load and heat (CarPlay render/encode at a large
  resolution + Waze + GPS + charging + sun) on warm days. Not proven.

## Options

1. **Phone-side A/B (free, decisive).** On a warm-day drive: phone off any wireless/MagSafe
   charger and out of direct sun; Apple Maps instead of Waze (or Waze in 2D). Low Power Mode
   off; iOS and Waze current. Owners report Waze CarPlay lag that stops when Waze is closed
   or the phone leaves the wireless-charging spot (Apple Communities thread 253866476).
   Workaround and discriminator when it lags: Home on the unit, then reopen ZLink.
2. **ZLink hidden debug menu: lower projected resolution / frame rate.** The APK contains a
   settings debug panel (`layout_debug_view`, `layout_debug_new`) behind
   `dialog_factory_input_password` ("Please input the factory setting password"). Decoded
   labels: 分辨率 Resolution (width/height fields), 帧率 Frame rate (spinner), 像素密度 DPI
   (Android Auto only), audio/video stream save switches; plus a hidden HD ON/OFF switch
   (`item_hd_video`). A third-party guide opens it by long-pressing "Settings" for 5-10 s,
   code 3457 (unverified for this build). A Carlinkit owner fixed comparable CarPlay map
   lag by dropping the projection to 20 fps (ABRP feedback board). Candidates: 20 fps at
   1920x720, or 1440x540 / 1280x480 (keeps 8:3). Note the saved 24 Oct 17:01 session's 1808
   width was `init_video_res = 1` (driver-position alignment), not a resolution test.
3. **Reduce radio contention on the unit.** The OBD companion requests
   `CONNECTION_PRIORITY_HIGH` (~11-15 ms BLE interval) on the Marlin3 Lite radio that also
   hosts the hotspot. BALANCED (30-50 ms) cuts that load ~3x at a small OBD cadence cost.
   Present on good and bad drives alike, so a contributor at most. Needs a signed APK.
4. **Vendor.** Ask QCTech/ZJINNOVA for a matching ZLink newer than 600102 (owner reports of
   6.1.06; no changelog found) and cite the repeated-`0x25` send loop and dropped PTS.
5. **Bypass ZLink.** A wired-CarPlay comparison if the unit has a data-capable port, or a
   dongle with frame-rate control. Hardware change; last resort.

Already done today: sampler overhead fix deployed (`0c0d3fd`; 73% -> 10% of a core parked).

## Live check, 30 Oct 21:45-22:00 (parked, ACC on, CarPlay connected then disconnected)

- With CarPlay connected, ZLink's settings are unreachable (its home page sits under the
  CarPlay layer); the user disconnects CarPlay to open them.
- Settings -> long-press the "Setting" title ~6 s opens "Please input the factory setting
  password". **3457 works** and opens the *older* debug menu (`layout_debug_view`): Activation,
  Recording mic file, Mic loopback (RIGHT), AEC mode, Speaker gain 9, Call mic volume 2, Call
  car volume 3, Phone ring volume 1, Reset, Link type Auto. **No video controls.**
- The resolution/frame-rate panel is `fragment_video_adjustment` (res/YM.xml), a tab
  (视频调节) of the *newer* debug menu `layout_debug_new` (tabs: call parameters, volume, video
  adjustment, network check), which is GONE by default. Long-press of the debug title,
  8 taps on the version and 10 taps on the Device Info logo did not reveal it; its trigger
  (likely another code) is unknown and the Java is packed.
- Hidden settings item "Use Vehicle GPS" (`item_gps`, `setting_gps`) exists; ZLink currently
  does not forward vehicle GPS (`gps_on: 0`).
- Device Info: DISPLAY `1800x720-1920x720`, build V6.1.02 2026-05-19 QICHENG(S) Ultimate.
- `persist.zj.*` props hold the debug-menu audio values (apm.speakergain 9, apm.micgain 3,
  apm.delay 200 ms); none for video. Nothing was changed.
- The phone joined the hotspot from a different Wi-Fi MAC than before (private address
  rotation or a different iPhone).

## 31 Oct follow-up: the "almost 10 s" drive (15:29:50-15:47:05), logger 0.3.23 installed

- Balanced BLE (0.3.23) was active on all of today's drives; the bad drive still happened,
  so BLE airtime is not the deciding factor.
- At CarPlay connect (hotspot neighbour 15:29:54) the phone sent no video twice: 15:29:59
  (2.9 s, then 7.0 s) and 15:30:20-15:30:29 (7.8 s); hotspot bytes fell to ~4 KB per 3 s while
  the relay and ZLink kept up (rx queues 0). After 15:30:29 the stream ran at 21-23 Mbit/s,
  ~100-105 KB per frame, for most of 15:33-15:45.
- Across 2,038 polls the stream rate is flat from 15 to 24 Mbit/s and stops at a hard
  ~25 Mbit/s ceiling (p99 25.1, max 26.3). 100-105 KB/frame is exactly that budget at 30 fps:
  on this drive the stream sat at the ceiling with no headroom to send faster than real time.
- Mechanism consistent with everything measured (not proven): frames queued on the phone during
  the connect-time freezes cannot drain while the content needs the full ceiling, so the picture
  stays seconds behind for the drive while the unit presents a steady 27-28 fps and audio is
  instant. Head-unit side ruled out again: decoder max 0.46-1.2 s and avg 134-166 ms per session,
  relay bytes match, ZLink RSS rises and falls with bitrate (GC, not a queue), CPU unthrottled at
  77-86 C on the bad drive (the outbound leg hit 101.7 C and dipped to 1.536 GHz).
- Day vs night is not explained by bitrate alone: the good 21 Sep 19h drive ran at 19.6 Mbit/s.
- Levers that act on this mechanism: fewer bits per frame (lower projected resolution, simpler
  map content such as Waze night/2D) so the stream sits below the ceiling and a backlog can drain;
  and understanding the connect-time freezes. ZLink's resolution control is in the hidden
  `fragment_video_adjustment`; `wm size` (whole-UI, reversible with `wm size reset`) is the only
  other unit-side lever and must be tried with the user in the car to verify and revert.

## 31 Oct 16:10 - lower projection resolution APPLIED (for the next drive)

- `adb shell wm size 624x1664` (natural portrait; landscape 1664x624, 8:3, 75% of the
  pixels). Physical 720x1920 and density 240 unchanged; persists across reboots
  (`settings global display_size_forced` = 624,1664).
- ZLink picked it up live: Device Info DISPLAY changed from `1800x720-1920x720` to
  `1544x624-1664x624`, so the next CarPlay session should request 1664x624 (1544 with the
  driver-side alignment) from the iPhone. Not yet observed with a phone connected.
- Recorder unaffected: both cameras closed full ~61 MB segments at 16:10/16:11 after the
  change; server recorder health OK. ZLink home and Android UI lay out correctly (slightly
  upscaled).
- Verify after the next drive: decoder codec_summary width/height (expect 1664x624 or 1544x624),
  stream Mbit/s well below the ~25 Mbit/s ceiling, connect-time freezes, and the driver's lag.
- **Revert:** `adb -s 192.168.1.214:5555 shell wm size reset` (then ZLink shows 1800-1920x720 again).
- Also found: SystemUI's "Internet" Wi-Fi picker was open on the unit's screen, scanning every
  10 s while parked (since before 15:49). Closing it (Back) stopped the scans at 16:10:28.
  It did not affect today's drives (about 19 s of scan time across all driving).
