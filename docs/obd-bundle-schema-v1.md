# Dashcam OBD bundle schema v1

This is the versioned interchange contract between the Android logger and the backup
server. V1 is strict: unknown JSON keys, duplicate keys, non-finite numbers, path-like ZIP
members, or a unit spelling change are permanent validation errors. All text is UTF-8 and
all timestamps are RFC 3339/ISO 8601 strings with an explicit zero UTC offset.

## Archive and identity

The final filename is `<drive_id>.obd2.zip`; the in-progress sibling is
`<drive_id>.obd2.zip.partial` and is never discovered. `drive_id` matches
`[A-Za-z0-9][A-Za-z0-9_-]{0,63}` and `vehicle_id` matches
`[a-z0-9][a-z0-9_-]{0,63}`. The archive is `ZIP_STORED` and has exactly four regular,
unencrypted root members, with no directories, links or duplicate names:

```text
manifest.json
samples.ndjson.gz
diagnostics.json
summary.json
```

`manifest.json` contains exactly these keys:

| Key | V1 type/rule |
| --- | --- |
| `schema_version` | integer `1` |
| `bundle_format` | string `dashcam-obd` |
| `drive_id`, `vehicle_id` | canonical IDs above; drive ID equals filename |
| `adapter_id` | non-empty string up to 128 characters, or `null` |
| `logger_id` | non-empty string up to 128 characters |
| `logger_version` | non-empty string up to 64 characters |
| `start_time_utc`, `finish_time_utc`, `created_at_utc` | UTC timestamps; finish is not before start; span at most 31 days |
| `original_timezone` | IANA/original timezone string up to 128 characters, or `null` |
| `start_reason` | non-empty string up to 128 characters |
| `stop_reason` | non-empty string up to 128 characters, or `null` |
| `obd_protocol` | non-empty string up to 256 characters, or `null` |
| `completion_status` | `complete` or `recovered` |
| `clean_end` | boolean |
| `sample_count`, `diagnostic_count`, `error_count` | non-negative integers; counts equal payload records |
| `included_filenames` | the four member names above, exactly once each |
| `units` | the exact map below |
| `files` | exactly the three payload members; each value has exactly `size_bytes` (non-negative integer), `sha256` (64 lower-case hex), and `record_count` (non-negative integer) |

The manifest hashes the stored bytes of `samples.ndjson.gz`, `diagnostics.json` and
`summary.json`; it cannot hash itself. The whole archive SHA-256 is computed after the final
atomic rename and becomes the server/receipt/HA idempotency identity.

## Units and samples

The exact `units` object is:

| Field | Unit |
| --- | --- |
| `engine_rpm` | `rpm` |
| `vehicle_speed` | `km/h` |
| `coolant_temperature`, `intake_air_temperature` | `°C` |
| `engine_load`, `throttle_position`, `short_term_fuel_trim_bank_1`, `long_term_fuel_trim_bank_1`, `oxygen_sensor_1_short_term_fuel_trim`, `oxygen_sensor_2_short_term_fuel_trim` | `%` |
| `timing_advance` | `°` |
| `mass_air_flow` | `g/s` |
| `oxygen_sensor_1_voltage`, `oxygen_sensor_2_voltage`, `adapter_voltage` | `V` |
| `estimated_fuel_rate` | `L/h` |
| `estimated_fuel_consumption` | `L/100 km` |
| `distance_with_mil` | `km` |

`samples.ndjson.gz` is gzip-compressed NDJSON: one object and one newline per record. Every
sample requires `sample_id`, `drive_id`, `timestamp_utc`, `sequence`, `ecu_data_status`, and
`quality`. `sample_id` matches `[A-Za-z0-9][A-Za-z0-9_-]{0,95}` and is unique globally;
`sequence` is a non-negative, strictly increasing integer; timestamps are ordered and lie
within the drive; `ecu_data_status` is `live` or `last_known`.

All telemetry keys are optional and may be omitted when not observed. Numeric values may be
explicit `null`; non-null values are finite and within these inclusive ranges:

| Field | Range |
| --- | --- |
| `engine_rpm` | 0..20000 |
| `vehicle_speed` | 0..400 |
| `coolant_temperature` | -80..250 |
| `intake_air_temperature` | -80..200 |
| `engine_load`, `throttle_position` | 0..100 |
| `timing_advance` | -90..180 |
| `mass_air_flow` | 0..2000 |
| either fuel trim | -100..100 |
| either oxygen-sensor voltage | 0..5 |
| `adapter_voltage` | 0..40 |
| `estimated_fuel_rate` | 0..1000 |
| `estimated_fuel_consumption` | 0..10000 |
| `distance_with_mil` | 0..65535 |

The optional unitless fields are `fuel_system_1` and `obd_standard` (non-empty strings up
to 128 characters or `null`) and `oxygen_sensors_present` (unique integer indices 1..8 or
`null`). `quality` contains exactly `transport` and `parser` (non-empty strings up to 128)
plus `missing_pids` (at most 256 integer PIDs 0..255). Quality stays on the server and is
never forwarded to Home Assistant.

## Diagnostics

`diagnostics.json` contains exactly `schema_version: 1`, the matching `drive_id`, and
`events`. At most 4096 events are ordered chronologically. Each event contains exactly
`diagnostic_id`, `drive_id`, `timestamp_utc`, `kind`, and `payload`; IDs are unique and event
times lie within the drive. Payloads are strict:

| Kind | Exact payload |
| --- | --- |
| `confirmed_dtcs`, `pending_dtcs`, `permanent_dtcs` | `{codes:[...]}`; at most 128 unique canonical `P/C/B/U` plus four upper-case hex characters |
| `dtc_mode_status` | `{mode:3|7|10,status:ok|no_data|rejected|transport_error|malformed}` |
| `dtc_scan_complete` | `{modes:[3,7,10]}`; allowed only after all three modes succeeded/no-data in that scan |
| `mil_state` | `{on:boolean}` |
| `readiness` | `{supported:[string],incomplete:[string],complete:boolean,confirmed_dtc_count:0..127,ignition_type:spark|compression}`; incomplete is a subset of supported and complete means none incomplete |
| `readiness_scan_complete`, `mode09_support_scan_complete` | `{status:ok|no_data|rejected|transport_error|malformed}`; compact non-deduplicated observation evidence |
| `mode01_support` | `{supported_pids:[unique integers 1..64]}` |
| `mode09_support` | `{supported_pids:[unique integers 1..32]}` |
| `mode09_count` | `{pid:3|5,count:0..255}` |
| `mode09_probe_status` | `{pid:3|4|5|6,status:ok|no_data|rejected|transport_error|malformed}` |
| `calibration_id` | `{value:string}` up to 256 characters |
| `calibration_verification_numbers` | `{values:[string...]}`; at most 128 values, each up to 256 characters |
| `protocol_change` | `{protocol:string,protocol_number:string|null}`; maximums 256 and 32 characters |
| `freeze_frame_scan_complete` | `{status:ok|empty|no_data|rejected|transport_error|malformed}` |
| `connection_failure`, `parser_failure` | `{category:string,message:string}`; maximums 128 and 1024 characters |

`freeze_frame` uses one of three exact shapes: no data is
`{status:"no_data",frame:0,values:{}}`; an empty frame is
`{status:"empty",frame:0,dtc:null,values:{}}`; captured data is
`{status:"ok",frame:0,dtc:"Pxxxx",supported_pids:["01",...],missing_pids:[...],values:{...}}`.
PID strings are unique two-character upper-case hex. `values` accepts only the sample
telemetry fields and their same types/ranges. Value events may deduplicate unchanged data;
the compact completion/status events do not, so the server can preserve the newest actual
observation timestamp without repeating full payloads.

## Summary and implementation limits

`summary.json` has exactly these keys: `schema_version`, `drive_id`, `start_time_utc`,
`finish_time_utc`, `duration_s`, `distance_km`, `average_speed_kmh`, `maximum_speed_kmh`,
`average_rpm`, `maximum_rpm`, `idle_duration_s`, `estimated_fuel_used_l`,
`average_fuel_consumption_l_per_100km`, `maximum_coolant_temperature_c`,
`maximum_engine_load_pct`, `dtcs_observed`, `sample_count`, `missing_data_duration_s`,
`expected_sample_count`, `received_sample_percentage`, and `clean_end`. Identity, times,
count and clean-end match the manifest. `duration_s`, `missing_data_duration_s` and
`received_sample_percentage` are non-null; other aggregate metrics are numeric or `null` so
missing never becomes zero. Expected count is at least sample count, missing duration is at
most duration, percentages are 0..100, and DTCs are bounded canonical codes.

JSON members are capped at 2 MiB and a decompressed NDJSON line at 64 KiB. The deployment
also enforces configured archive size, total expansion, compression-ratio and sample-count
limits. Current defaults and overrides are server configuration, not permission for a
producer to rely on unbounded input. Any incompatible field, unit or semantic change needs a
new `schema_version`.
