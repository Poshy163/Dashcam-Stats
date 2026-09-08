# One-minute journey gap policy

The operator requested a new journey whenever neither camera records for more than one
minute, including historical footage. The live `journeys.gap_minutes` setting was changed
from 5.0 to 1.0. This is a persisted setting read by incremental assignment, clustering and
the scheduler's historical consistency check; no server restart is required.

The source default and Settings description now match. Exactly 60 seconds remains within
one journey; 60.001 seconds starts another. Overlapping front/rear footage extends the
covered interval, so a missing segment from one camera alone does not create a boundary.
Existing GPS continuity rules remain in effect. This policy describes recording gaps,
not necessarily ignition-off or parked time while the recorder continues running.

## Historical application and verification

- A complete SQLite backup was retained privately before the change; its integrity check
  passed. A second snapshot captured the completed rebuild.
- Preview found 54 qualifying gaps in 31 historical journeys. Neither manually corrected
  journey (837 outbound, 839 return) required another split.
- After saving the setting, the existing scan/maintenance route rebuilt automatic journey
  membership. Scan 1443 reported 5,317 files seen; zero new, changed, missing, damaged,
  deleted or queued recordings; zero errors. No media reprocessing was requested.
- Total stored journey rows increased from 203 to 257. There are 244 groups containing
  eligible, non-ignored recordings; historical hidden/empty groups are distinct from the
  visible journey count.
- All 31 affected journeys split as previewed, adding 54 groups. All 6,023 eligible
  recordings are assigned; none of their groups has an uncovered gap greater than
  60 seconds. Both manual journeys retained precisely their original membership.
- All 7,799 recording rows retained identical IDs, paths, filenames, camera IDs, sizes,
  start/end timestamps, ignored flags and missing-file flags. Both database snapshots
  passed their integrity checks.

Snapshots and the before/after membership mapping are private local files under the
temporary directory `dashcam-journey-gap-20260908`, outside source control. Automatic
journey IDs can change during a rebuild; recordings themselves are preserved.

## Tests

Added regressions for 59.999/60/60.001-second boundaries, coverage by the other camera,
and a previously merged historical group that splits and then remains stable. The older
four-minute-gap GPS regression now explicitly requests its original five-minute setting,
so it continues testing GPS repair independently of the new default.

Journey-focused suite: 36 passed. Full backend suite: 1,750 passed, 13 skipped;
1,416 existing deprecation warnings in 124.50 seconds. Changed Python files pass Ruff
lint and formatting checks. The live policy and retrospective correction are already
applied; publishing the source default is separate from restarting the server image.
