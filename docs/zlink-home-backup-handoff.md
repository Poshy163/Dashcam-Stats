# Zlink and home-backup handoff

This note records the read-only investigation of the production head unit and the safe
shape of a future wireless-CarPlay-to-home-WiFi handoff. Network names, addresses, device
identifiers and credentials are deliberately omitted.

## What the unit exposes

- Zlink is the system package `com.zjinnova.zlink`, version 6.1.02.
- Its daemon, Bluetooth service and ZBT service remain alive when no CarPlay session is
  connected. Stopping the whole package is therefore broader than the handoff needs.
- The runtime registers receivers for:
  - `com.zjinnova.zlink.action.DISCONNECT`
  - `com.zjinnova.zlink.action.POWER_ON` and `.POWER_OFF`
  - `com.zjinnova.zlink.action.ACTION_APP_SHOW` and `.ACTION_APP_HIDE`
  - the vendor `acc_connect` and `acc_disconnect` events
  - Bluetooth-disconnect and hotspot-state changes
- `settings get system persist.sys.carplay.connect.status` is `0` while the current
  disconnected state is observed. It is a useful candidate signal, but an active-session
  transition still needs to establish its complete value contract.
- Zlink holds the privileged WiFi, tethering and Bluetooth permissions needed to manage a
  wireless CarPlay link. The observed architecture is most consistent with Bluetooth for
  discovery/control and a Zlink-managed soft AP for data. That remains an inference until
  an active session is captured.
- The Android framework says STA can coexist with either an AP or WiFi Direct interface.
  This proves hardware/framework concurrency, not which path Zlink chooses or whether it
  allows Android to autojoin the home network promptly during a session.

## Current home-network facts

The unit already autojoins a saved, non-ephemeral 5 GHz network and negotiates the radio's
433 Mbps ceiling. Only the normal STA interface was active during the audit; tethering and
WiFi Direct were not serving.

This Android build does not expose `cmd wifi connect-network`. An unrooted shell can inspect
status and scans, but cannot safely select an existing saved configuration. Never bounce
STA WiFi to make Android choose another band: disabling it persists the off state before
the control connection drops and can leave the unit unreachable after a power loss.

The supported arrangement is therefore:

1. Save a router-side 5-GHz-only network on the head unit.
2. Let Android autojoin it.
3. Set the server's `ingest.wifi_band` policy to `require_5ghz` if a slow-band transfer is
   worse than waiting for the next window.
4. Keep the server's two-second presence poll and existing 5 GHz verification as the
   authoritative start gate.

## Recommended handoff state machine

This should be coordinated by the boot-persistent OBD logger rather than by a server-only
ADB script. The logger already owns the engine-state evidence and participates in the
durable radio transition.

### Arrival / engine stopped

1. Require the logger's stable engine-stopped verdict. ACC-off may be an early hint only
   after it is physically re-correlated on the normal vehicle supply.
2. Record whether Zlink reported an active connection and arm a durable local restoration
   marker.
3. Send the package-scoped `com.zjinnova.zlink.action.DISCONNECT` broadcast. Do not spoof
   the vendor's global `acc_disconnect` event and do not force-stop Zlink.
4. For at most five seconds, verify the Zlink connection state becomes disconnected and
   any CarPlay AP or WiFi Direct group disappears.
5. Leave STA WiFi enabled and allow Android to autojoin its saved network.
6. Require both a negotiated frequency of at least 4.9 GHz and a successful private
   home-server probe.
7. Publish `home_handoff_ready`; the server can then run the existing OBD quiesce, exact
   radio-baseline capture and footage transfer.
8. If home cannot be proved within a short bounded window, restore the prior Zlink state
   and clear the marker.

### Departure / engine running

1. Withdraw `home_handoff_ready` and let an active transfer stop at a safe file boundary.
2. Require the existing radio coordinator to verify Bluetooth/hotspot restoration and OBD
   logger resume.
3. Send Zlink `POWER_ON` and wait for its background-connect behaviour.
4. If required, try `ACTION_APP_SHOW`; launch
   `com.zjinnova.android.zlink.features.main.MainActivity` only as a final fallback.
5. Verify the actual connection/foreground state and clear the restoration marker.

All waits need hard deadlines. A failed backup must not delay CarPlay indefinitely, and a
process death must leave enough durable state for the next logger start to finish or undo
the transition.

## Why ACC or uptime alone is insufficient

The read-only snapshot reported ACC as on while the battery-bank setup was physically
parked, and Linux uptime was already more than fourteen hours. The current server gate
assumes a freshly booted unit means departure and a high-uptime unit means arrival; retained
sleep or external power breaks that assumption. ACC-only logic also misclassifies the
battery-bank arrangement.

Use the logger's voltage/RPM/ECU evidence as the primary engine state. ACC can reduce
latency once a normal ignition-off transition has been revalidated, while successful
5 GHz association plus a private server probe proves that the unit is actually home.

## Controlled active-CarPlay validation

With the vehicle stationary and CarPlay connected, capture only sanitised state:

1. Zlink connection status and foreground activity.
2. STA frequency and link rate.
3. Serving tether/AP interface state.
4. WiFi Direct group state.
5. Bluetooth enabled state.
6. Time from a real ignition-off event to CarPlay disconnect, 5 GHz association, server
   reachability and backup start.
7. The same facts after the graceful-disconnect broadcast and after restoration.

Do not record network names, BSSIDs, MAC addresses, phone names or credentials. Do not use
`force-stop`, `POWER_OFF`, a global ACC broadcast, direct hotspot commands or a WiFi toggle
in this test.
