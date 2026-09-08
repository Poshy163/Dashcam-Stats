# Transfer and scanner reliability audit — 2026-09-08

Scope was a source-only review at `1e258d04`: `app.ingest.puller`, `app.ingest.adb`,
the scanner modules, and their focused tests. No network requests, device connections,
deployment, commit, or push were performed.

## Reviewed stage map

1. **Presence and source resolution:** ADB reconnect/state, configured source override,
   mounted-volume probe, locked directory derivation, and orphan-partial discovery.
2. **Inventory and identity:** batched remote `stat`, filename allowlist, directory-aware
   `RemoteFile` records, duplicate-name rejection, unit-clock active-file filtering, and
   the second inventory that removes growing or vanished files from the plan.
3. **Delta and resume:** local size comparison, deliberately-removed exclusion, camera
   filtering, transfer ordering, per-file resume across interrupted windows, and top-up
   sweeps for files closed during the transfer.
4. **Transfer:** directory grouping, bounded chunks, stale listener cleanup, requested-name
   and exact-size tar validation, flat-path enforcement, byte bounds, cancellation, and
   progressive commit backpressure.
5. **Finalization and durability:** staging isolation, exact-size recheck, existing-file
   collision rules, atomic rename, interrupted-tail cleanup, and scanner visibility.
6. **Receipt/reclaim:** safe-name quoting, directory grouping, local/share safety gates,
   commit-before-delete ordering, lease checks, retry behaviour, and remote deletion.
7. **Scanner identity:** streaming walk, symlink/dot-directory policy, settling decisions,
   stat/fingerprint provenance, changed-content reset, missing-file reconciliation guards,
   queue eligibility, and scan serialization.

## Confirmed fixes

- **Remote replacement could be deleted without backup.** Reclaim previously issued an
  unconditional `rm` by filename. A recorder could replace/recycle that path after the
  inventory and the new bytes would be erased. Reclaim now compares the current remote
  size and mtime with the inventoried `RemoteFile` immediately before unlinking, in one
  shell invocation, and counts only confirmed deletions. Changed or missing paths remain.
  A failed remote `cd` exits before every comparison so no clause can run in the shell's
  original directory.
- **A same-size local filename was treated as proof of remote content identity.** The tar
  receiver writes a new local file and does not preserve the remote mtime; footage also has
  no digest receipt. Therefore an older local clip cannot be linked to a current remote
  object whose recorder-reused name and size happen to match. Delta still skips that
  needless download, but it no longer adds the remote object to the deletion-safe set.
  Reclaim is limited to files committed against the current run's inventory.
- **A same-size destination could race finalization.** If another writer publishes the
  target between delta and commit, commit now compares staged and target bytes with a
  bounded-memory streaming read. Identical content is synced and may be acknowledged;
  different content moves the staged arrival under `.ingest_staging/.conflicts`, leaves
  both local versions and the card source intact, and logs the conflict. Later chunk commits
  and top-level staging cleanup ignore that evidence. This full comparison runs only on the
  unusual collision/retry path.
- **Commit could acknowledge bytes before they were durable.** A rename made publication
  atomic but did not flush the staged file before the caller became eligible to erase the
  card copy. Commit now flushes and `fsync`s the file before rename, then syncs the footage
  directory where the platform supports directory handles. Real sync failures propagate
  to the commit boundary and prevent reclaim. A later run also rechecks durability before
  treating a same-size local destination as reclaimable, so a prior failed directory sync
  cannot be bypassed by restart/resume.
- **Scanner fingerprint provenance had a read race.** The scanner stored the earlier
  `scandir` stat as provenance even if a file changed while its fingerprint was read. It
  now stats again after the read; a mismatch clears a new row's inconsistent fingerprint
  or withholds provenance on an existing row and leaves it in `SETTLING` for a stable scan.

Remote discovery already rejects duplicate bare filenames across source directories because
the transfer/staging contract is flat. Reclaim now additionally groups the `RemoteFile`
objects themselves by directory, avoiding a global name lookup that could associate an item
with the wrong directory if a non-discovery caller supplied a collision.

The conditional reclaim remains one control-channel round trip per source directory, so
the stronger identity check does not add per-recording ADB latency. The scanner adds one
post-fingerprint stat only on files already escalated to content sampling; unchanged
library files retain the one-stat fast path.

## Validation

` .venv\Scripts\python.exe -m pytest tests/test_ingest_adb.py tests/test_ingest_transport.py tests/test_file_stability.py -q `

Result: **278 passed** across the focused files. The warnings are existing Alembic and FastAPI
deprecations. Regression coverage includes exact remote identity deletion, unsafe source
rejection, progressive reclaim compatibility, durable commit behaviour on Windows, and a
file mutation during fingerprinting, a failed remote directory change, and storage-sync
failures during both first publication and later same-size resume.

A local synthetic timing check used eight 1 MiB files, repeated three times. Plain rename
had a median of 2.339 ms per eight-file batch; durable commit had a median of 62.165 ms.
The measured durability cost was about 7.5 ms per file on this Windows workspace. This is
a tiny cache/filesystem benchmark rather than a production NFS or device throughput claim.

## Limits

This was intentionally local. No real card filesystem, Android/BusyBox shell, sudden power
loss, network interruption, mounted NFS share, or production database was exercised.
Size plus mtime protects current-transfer reclaim from ordinary recorder replacement but is
not cryptographic source identity; the footage protocol has no remote content digest or
receipt contract. Pre-existing same-size local names are consequently retained on the card.
They are also skipped rather than acquired because delta cannot distinguish a true duplicate
from a recorder-reused same-size name. Fully acquiring reused same-size names requires an
identity-versioning design, such as a remote digest/receipt or persisted remote generation;
that protocol and migration are outside this narrow safety correction.
Directory `fsync`
is unavailable through ordinary directory handles on Windows, while file data is flushed
before publication. OBD bundle receipts use their separate hash/readback path and were
reviewed only at the integration boundary because their implementation was outside scope.
