# Maintenance UI snapshot helper

`FreshUiSnapshot.java` is the complete source of
`backend/app/ingest/assets/fresh-ui.dex`. It opens a fresh Android accessibility
connection, requests the current window root, writes one hierarchy, and disconnects.
It sends no input and changes no recording, radio, ignition, or power setting.

The normal `uiautomator dump` waits for one second without accessibility events,
with a ten-second overall idle wait. The native sleep countdown changes every
second, so that command can return without writing a hierarchy. This helper reads
the current root directly; a missing root, unsupported framework or timeout still
refuses maintenance.

The server validates the bundled and on-device DEX SHA-256 before execution. A
missing or different device copy is replaced through a unique temporary file.
Each snapshot uses a unique output filename, a five-second process timeout and an
eight-second total budget, including installation. Output is limited to 128 KiB
plus one overflow byte, parsed without logging, then removed. Server admission
still requires the attested SystemUI build, exact countdown resource and current
bounds, ignition OFF, normal sleep policy, and a durable cancellation marker
before any long press. This helper never authorizes a gesture by itself.

To rebuild using an **already installed** JDK and Android SDK (no downloads):

```powershell
python tools/maintenance-ui/build.py --android-sdk C:/path/to/android-sdk --java-home C:/path/to/jdk
python tools/maintenance-ui/build.py --android-sdk C:/path/to/android-sdk --java-home C:/path/to/jdk --check
```

The build also accepts `ANDROID_SDK_ROOT`/`ANDROID_HOME` and `JAVA_HOME`.
`--check` recompiles in a temporary directory and requires byte-for-byte parity
with the packaged asset. The recorded build uses OpenJDK 17.0.20.1+1,
`platforms/android-35/android.jar`, and build-tools 35.0.0, whose D8 reports
`8.6.2-dev` (build `abaab469b5ebd4dd2bb91ba0ed6f45277faae4ca`). Java compilation
targets release 8 without debug information; D8 uses release mode and min API 23.

The packaged 3,068-byte DEX has SHA-256
`0f331af612480bd7fa63ee65b16b3dd9a8cda4a5308daecf484a1a473cc8f5e4`.
An intentional source/toolchain update must regenerate the asset, inspect the
source, and update this digest and `UI_HELPER_SHA256` in `maintenance.py` together.

An earlier equivalent read-only prototype obtained the live countdown hierarchy
on the Android 34 camera while the ordinary idle-wait dump failed. On 21 October
2026, deployed server revision `fd96348` used the packaged helper to verify native
countdown cancellation and an active maintenance hold with fresh heartbeat evidence.
Heartbeat evidence was lost around 08:58 UTC before the planned 09:07 UTC expiry; the
exact cause is unproven and native forced sleep is not established. Subsequent server
checks verified Bluetooth, hotspot and logger restoration with `recovery_required=false`,
while the transition remained failed/inactive. The operator then ended the hold through
the API, which reported maintenance inactive. Planned expiry and diagnosis of the early
evidence loss remain separate checks; this sequence does not establish a fully successful
backup run.
