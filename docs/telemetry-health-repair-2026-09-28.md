# Telemetry health repair — 28 September 2026

The health page counted recordings, not individual GPS positions. The visible library
contained 7,573 recordings: 2,603 healthy, 4,868 degraded and 102 reporting no fix
throughout. Front and rear clips count separately. These measurements concern text read
from recorded footage; they do not measure CarPlay's live location delay.

## Corrected warning classification

The extractor selected timestamp, position and speed independently from candidate frames,
but kept parser warnings from discarded fields. A successful timestamp could therefore
retain `timestamp unreadable`. The field-aware filter now removes only known failures
superseded by successful selected fields. Repair provenance, clock/track rejection,
no-fix conflicts and unknown diagnostics remain visible.

Migration 0024 recounts the cached warning totals without changing telemetry rows or raw
provenance. Missing/malformed provenance and incomplete recordings are skipped. Startup
already requires a validated, atomic database backup before migration.

On a separate copy of the 1,330,356,224-byte production snapshot:

- 4,000 recording counters changed; 33,219 resolved problem samples were removed.
- A second application made no changes.
- All 766,554 telemetry rows, coordinates, raw text and provenance remained identical.
- All other recording fields remained identical; both database integrity checks passed.
- The updated status rules yielded 3,259 healthy, 4,212 degraded and 102 no-fix recordings.
  Three empty analyses previously counted as healthy now correctly require attention.

## GPS coverage and recovery

The page now separates GPS sample coverage from warning counts, identifies the units,
explains the reasons for a recording's status, and discloses the 250-row issue-table limit.
Later rejected positions and zero-sample analyses cannot count as healthy.

The persisted four-state quality column also represented ordinary OCR parse failures as
`rejected`. Paired-camera recovery consequently refused them, even when the other camera
had an independently accepted position at exactly the matching second. Recovery now
recognises the narrowly evidenced parse-only case. It still refuses genuine no-fix,
explicit rejection reasons, rejected clocks, segment breaks and contradictory coordinates.
Copied positions remain synthetic and cannot become donors or distance/sighting evidence.

Historical recovery is deliberately separate from the warning-only migration. The bounded
maintenance command defaults to preview with rollback, supports one-recording verification,
checks active jobs under a short SQLite write lock, and backs up before applying. It only
modifies the target recording and preserves the paired donor. It does not enqueue video
decoding or invalidate detection/plate results.

```sh
python backend/scripts/repair_telemetry_gaps.py --recording-id 11746
python backend/scripts/repair_telemetry_gaps.py --limit 10000
python backend/scripts/repair_telemetry_gaps.py --limit 10000 --apply
```

Use the normal `DASHCAM_DATA_DIR` configuration. The returned `last_id` supports bounded
continuation with `--after-id`. A remaining gap without an acceptable independent donor
is preserved. New footage uses the corrected recovery automatically.

## Bright-background overlay reading

A failure-only OCR fallback tries a small bounded set of brighter masks. It requires
complete valid telemetry, agreement between masks, strong individual glyph evidence and
unambiguous splits of fused digit runs. Existing accepted and explicit no-fix readings
take the original path unchanged. Coordinate and track validation remain in force.
The diagnostic JSON/image and production extraction use the same selected mask.

With the exact production template bundle, 16 saved crops covered daylight, night,
explicit no-fix and moving footage. Ten readable/no-fix controls were unchanged; five
unresolved failures stayed failed. One previously unreadable bright rear sample recovered
its complete timestamp, position and speed exactly, matching the paired front reading.
This is a verified improvement for that case, not a claim that every damaged overlay is
recoverable. Synthetic regression images cover disagreement, incomplete coordinates,
invalid clocks/speeds and ambiguous digit splits without publishing private footage.

Private database snapshots, frame crops, exact template bundle and machine-readable
validation evidence are kept under ignored `data/diagnostics/telemetry-health-2026-09-28/`.
They are not included in the repository or image.
