# Ignition-on Wi-Fi joining delay

Companion 0.3.2 includes a shell entry point that pauses Wi-Fi station auto-joining for
30 seconds on an observed ignition off-to-on transition. It disconnects any station
association that won the wake-up race, without changing Wi-Fi power, Bluetooth,
the CarPlay hotspot, saved networks, credentials or hotspot channel settings.

Normal joining resumes after 30 seconds, or earlier when ignition becomes off or
unreadable. Remaining ignition-on does not retrigger the hold. Attaching a controller
mid-drive does not create a synthetic ignition edge. This is an experiment to reduce
home-network competition during CarPlay startup, not a guarantee of 5 GHz or smooth
CarPlay. A connection may briefly form before the 250 ms ignition poll catches wake-up.

## Device lifecycle and recovery

The helper runs as the existing ADB shell UID. The normal Android application does not
have the required Wi-Fi privilege. The tested firmware exposes the three-argument
`IWifiManager.allowAutojoinGlobal` API and a separate station `disconnect` method.
Ignition is read through the same external Settings provider used by Android's shell
`settings get`, without repeatedly spawning a command.

The controller takes no wake lock and is intended to remain suspended through normal
head-unit sleep. Full reboot or firmware termination can remove it. After that, the
server re-arms an opted-in unit once it is reachable and positively ignition-off; it
cannot prevent the first association after a cold boot. Real ignition/sleep/wake and
CarPlay performance still require a subsequent drive to validate.

Before blocking joining, the controller verifies that autojoin was enabled and starts
an independent recovery process. That process must acknowledge a bounded lease before
the block is applied. A monotonic deadline includes time asleep. The recovery process
restores joining even if the main controller is killed. Leases include the boot identity;
stale pre-reboot leases cannot override a subsequent user setting. File locks serialize lease
restoration and prevent duplicate watchers. An already-disabled autojoin setting is
left alone. Only a lease created by this helper authorizes a restoration.

## Explicit installation and opt-in

The private on-unit directory is `/data/local/tmp/dashcam_wifi_startup`, mode 0700.
Its `enabled` marker opts in this particular unit. The server never creates that marker
on a discovered unit. Install the signed companion APK first, then while parked:

```sh
mkdir -p /data/local/tmp/dashcam_wifi_startup
chmod 700 /data/local/tmp/dashcam_wifi_startup
touch /data/local/tmp/dashcam_wifi_startup/enabled
```

The server's `wifi_startup.arm` copies the installed companion APK privately and starts
`com.dashcamstats.obdlogger.WifiStartupGuard watch` with `app_process` in a detached
session. An existing watcher or outstanding recovery lease is left untouched.
The installed APK must contain this class. Helper updates take effect when re-armed
after the old watcher exits; ordinary server presence checks do not restart it.

To disable, remove only the opt-in marker. The watcher exits and restores an owned
hold; the recovery process also notices marker removal:

```sh
rm -f /data/local/tmp/dashcam_wifi_startup/enabled
```

Do not remove `guard.apk` or lease state while recovery is outstanding. Verify
`autojoin=true` and `hold=false` before removing remaining helper files.

## Evidence

### Wireless CarPlay reconnect on the Q30

Companion 0.3.30 adds an optional phone-specific retry to the existing ignition
watcher. On the 21 November return trip, ZLink's Bluetooth socket to the paired
iPhone closed before the phone received its hotspot address. ZLink was still
waiting five minutes later. On the outward trip the iPhone already had an
address when the socket closed, and video started within a second.

On an observed ignition start, the watcher checks once after 35 seconds. It uses
the existing ZLink and hotspot records to match the order of events and allows
at least 20 seconds after the phone receives its address for CarPlay to start.
Recovery requires the stock ZLink main screen, a disconnected CarPlay status,
and an idle decoder. The Bluetooth helper checks the same bonded smartphone,
hands-free call state and media playback before asking Android to disconnect
and reconnect that phone's profiles. A backup or ignition-off cancels eligibility.
The existing Bluetooth pairing is preserved. The single event
`zlink_phone_reconnect_requested` records that Android accepted the reconnect
request. A new ZLink video session confirms that projection actually recovered.

To enable on this Q30, install the signed update while parked and enable the
retry in the opted-in watcher's private directory:

```sh
touch /data/local/tmp/dashcam_wifi_startup/zlink_reconnect_enabled
```

The running watcher uses its original private APK until it exits. During a
parked update, verify ignition is off and there is no outstanding autojoin
lease, temporarily remove the ordinary `enabled` marker, wait for the watcher
to exit, then restore that marker so the server can re-arm it. Verify that
`guard.apk` matches the installed companion and that `watch.pid` belongs to
the new watcher. Removing only `zlink_reconnect_enabled` disables the phone
retry independently of the Wi-Fi joining delay.

### Wi-Fi guard diagnostics

`events.log` and one rotation each retain about 32 KiB of fixed-vocabulary events.
`WifiStartupGuard` also emits to logcat and is in the server's default tag allow-list.
No network names, keys, MAC addresses, network IDs or raw exception messages are logged.

```sh
CLASSPATH=/data/local/tmp/dashcam_wifi_startup/guard.apk \
  app_process /system/bin com.dashcamstats.obdlogger.WifiStartupGuard status
```

This reports autojoin, opt-in, outstanding hold and the current ignition reading.
`test-parked` exercises the same 30-second hold and recovery without changing ignition;
it refuses unless ignition is positively off. It must not run alongside the watcher.

Local validation: 129 Android unit tests, release lint and APK assembly passed;
180 focused server ingest, radio and diagnostic tests passed. The live parked tests
verified automatic restoration at the 30-second deadline, including after killing the
controller eight seconds into the hold. During that test the station interface was down
and the hotspot remained up; recording stayed enabled. Actual next-drive ignition
detection, survival through firmware sleep and CarPlay performance remain unverified.
