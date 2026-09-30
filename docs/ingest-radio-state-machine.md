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

Unfinished restoration is also retried while the same unit remains online, independently
of footage retry limits and the number of backups allowed per visit. Recovery has a bounded
120-second budget and failed attempts are spaced 30 seconds from completion. A recovery
tick never starts another backup. Cancellation keeps the durable obligation available for
the next attempt.

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

The cryptographically approved production Zlink and FunctionCore system APKs may instead
use the separately enabled schema-2 `bluetooth_rearm` strategy. The historical mode name
remains compatible with existing capsules; restoration explicitly sends the attested
FunctionCore `action.start.tethering` action to start Android's saved profile. It contains
no network name or password. This path is accepted only when Bluetooth and a separate AP
were both positively observed on, both approved system-package paths, versions and APK
SHA-256 hashes match, and the operator enabled the Zlink hotspot recovery option.

Bluetooth being ON and a broadcast being accepted do not prove hotspot restoration.
Server recovery verifies the captured AP interface through a final stability window.
The detached watchdog allows up to ten seconds after the start request and requires the
captured interface in two successful scans at least one second apart. A different AP,
an unreadable interface inventory or a disappearing AP cannot acknowledge recovery.
An originally-off AP requires repeated readable scans showing no separate AP; a transport
interface is skipped and never stopped. Neither path bounces STA Wi-Fi or changes the saved
hotspot credentials.

The watchdog begins recovery 60 seconds before the head unit's sleep deadline. Its budget
covers two bounded six-second radio commands, six seconds for Bluetooth re-arm, ten seconds
of verification plus up to four seconds for final readbacks, and three bounded five-second
report attempts with two two-second retry pauses. The courtesy screen return runs afterwards
with its own six-second bound, so a slow launcher cannot prevent radio evidence from being
saved and sent. The watchdog must prove its command limiter works before publishing
readiness and allowing either radio to be disabled. A failed or unavailable readback
remains unverified; server recovery debt is retained for another reachable attempt.

The server accepts a successful ON report only for the captured hotspot interface.
Skipping a radio that was disabled cannot discharge its restoration obligation, and a
radio-only report cannot discharge an outstanding OBD logger resume obligation.

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
