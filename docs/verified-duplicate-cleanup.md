# Verified cleanup of existing camera duplicates

The server can reclaim older card files that already have a complete server copy when
`ingest.delete_after_verify` is enabled. Filename and size are only candidate evidence;
neither a database record nor an old transfer result authorizes deletion.

Normal copying, OBD finalization and radio restoration take priority. After a successful
run, an optional cleanup pass attempts verification of at most four files and 256 MiB within a
45-second work budget. A duplicate-only run uses the existing admission checks without
starting a footage listener, changing Wi-Fi, quiescing OBD or switching radios off. The
camera must report ignition OFF and have a known native sleep window with at least
15 seconds held in reserve. Missing clock, storage, required-band or radio-recovery evidence
defers cleanup. No maintenance wake hold is acquired and no native deadline is extended
for this optional work.

## Verification and deletion

The server opens a regular, non-symlink source and binds its identity, size and change
times. It streams an independent copy into
`<footage>/.ingest-duplicate-preservation/`, computes the full SHA-256, flushes the file
and synchronizes its directory entries. The private files have non-media suffixes so
the scanner does not ingest them as new recordings.

The camera hashes its complete pinned file, rather than a sample. Its address,
directory, filename, inode, size, modification and change times must match the inventory.
The server then rechecks its original and preservation. A durable `delete_requested`
receipt is written before requesting removal. The camera hashes its full file again
and rechecks the pinned inode and current pathname before unlinking it. The second
hash protects against content changes hidden by coarse card timestamps.

A changed, missing, symlinked or mismatching object is retained. Unsupported commands,
timeouts and disconnections grant no deletion authority. Cancellation stops subsequent
work. A blocked server filesystem operation retains its optional IO slot until its
actual worker finishes, so later runs cannot accumulate overlapping cleanup workers.
As with ordinary filesystem pathname operations, the final remote stat and unlink
cannot be made an atomic conditional unlink by the Android shell; eligibility is
restricted to closed clips and the independent server preservation remains held.

Copied-file progress and transfer bytes exclude these already-stored duplicates. Logs
separately report attempted, verified and deleted files, deleted bytes, skipped candidates,
retained preservations and a bounded stop reason. Quiet-policy exclusions and raw-file
retention tombstones are not treated as proof of an existing server copy. Current camera
selection and locked-footage inventory settings continue to apply.

## Retained preservation and recovery

A confirmed remote deletion releases the temporary preservation only after the original
server file still passes a full content and identity check. If that original disappears,
changes, or the delete acknowledgement is uncertain, the independent `.verified` copy
and JSON receipt remain. Ordinary staging cleanup never removes them.

The receipt identifies the recording, byte count, SHA-256 and deletion phase. Restore or
reconcile that copy with the named library file before removing the preservation. Do not
overwrite a different existing recording or delete unresolved preservations as temporary
files. A pending preservation prevents repeated destructive attempts for that identity.

Deployment requires the updated server image. The installed 0.3.21 companion remains
compatible and needs no reinstall. Unit, orchestration and executable POSIX shell tests
validate the safety paths; a successful build or push alone does not establish that the
live server has performed cleanup.
