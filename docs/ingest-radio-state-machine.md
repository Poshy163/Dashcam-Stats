# Ingest radio state machine

Footage ingest uses ADB as its control plane and a plain TCP tar stream as its data
plane. OBD bundles use the same transport and are copied before footage. Radio control
therefore runs only on the head unit, after a capable OBD logger has acknowledged that
its active command, pending sample, drive finalisation and immutable export are durable.

The durable phases are:

```text
preparing
→ finalising_obd
→ transferring_obd
→ capturing_radio_state
→ disabling_radios
→ ingesting
→ restoring_radios
→ resuming_obd
→ complete | failed | recovery_required
```

Only one row in `ingest_radio_transitions` may be active. A short renewable lease
prevents two processes from issuing radio commands, while the partial unique index is the
database-level backstop. Intent is committed before every radio command and verification
is committed afterwards. On startup an expired non-terminal row is adopted and restored
before the ingest poller starts. If the unit is offline, the row remains active and the
arrival path retries it before allowing another pull.

Bluetooth is disabled before a separate hotspot because this head unit re-arms its AP
while Bluetooth is on. Restoration enables the original Bluetooth state first and then
enforces the exact hotspot baseline, including re-stopping an AP that was originally off.
An AP interface carrying the configured ADB/TCP address is recorded as `transport` and
is never stopped.

## Hotspot recovery capsule

Before stopping an originally-on hotspot, the server atomically writes a short-lived,
versioned JSON recovery capsule under `/data/local/tmp` on the head unit with `umask 077`
and mode 0600. That location is inside the same Android `shell` trust boundary already
authorised for ADB control. The database stores only its allowlisted opaque path. The
capsule is read only during recovery and deleted after the hotspot baseline is positively
verified.

Generic units use schema 1, which contains the exact SSID and passphrase needed to restart
the AP. Those values never enter the server database, database backup, API, status output,
diagnostics or logs.

The cryptographically approved production Zlink system APK may instead use the separately
enabled schema-2
`bluetooth_rearm` strategy. It contains no network name or password. This path is accepted
only when Bluetooth and a separate AP were both positively observed on, the exact approved
system-package path, version code and APK SHA-256 are present, and the operator enabled **Let Bluetooth
re-arm the Zlink hotspot after copying**. The on-unit watchdog restores Bluetooth; on this
head unit that was observed to be followed by the AP returning, though Zlink ownership of
the AP has not been independently proven. Normal recovery requires the same captured AP
interface to remain continuously present through a final stability window before the transition, watchdog and
OBD quiesce request are cleared. It does not call unvalidated Zlink broadcasts, bounce STA
WiFi or touch an AP carrying the transfer itself.

The watchdog is an ADB shell process, not an Android boot service, so a head-unit reboot
can remove it. The Android logger's durable quiesce lease and immutable exported bundle
still protect OBD data in that case. The server retains the transition and retries radio
verification and logger resume when the same unit becomes reachable; it never treats a
reboot as proof that either radio recovered.

After every durable pre-change checkpoint, the server proves the on-unit watchdog is armed.
Then, from inside the radio lock and immediately before the first radio command, it re-reads
the exact correlated OBD request and acknowledgement and requires the remaining Android
lease to cover the whole watchdog window plus recovery headroom. Quieting is capped at eight
minutes and the request is issued with 90 seconds of extra lease. If OBD copying, a database
wait or any preceding probe consumes that headroom, both radios stay on and the logger is
explicitly resumed instead of risking a BLE reconnect during shutdown.

Older logger builds do not understand the file handshake. Their existing explicit
`ownership_enabled=true` contract remains authoritative: ingestion continues with both
radios on rather than interrupting a drive.
