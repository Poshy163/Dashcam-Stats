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

## Hotspot recovery credential

An AP passphrase is required only if the server dies after stopping an originally-on
hotspot. It is never written to the server database, database backup, API, status output,
diagnostics or logs. Before the stop, the server atomically writes a short-lived JSON
recovery capsule under `/data/local/tmp` on the head unit with `umask 077` and mode 0600.
That location is inside the same Android `shell` trust boundary already authorised for
ADB control. The database stores only its allowlisted opaque path. The capsule is read
only during recovery and deleted as soon as the hotspot baseline is positively verified.

Older logger builds do not understand the file handshake. Their existing explicit
`ownership_enabled=true` contract remains authoritative: ingestion continues with both
radios on rather than interrupting a drive.
