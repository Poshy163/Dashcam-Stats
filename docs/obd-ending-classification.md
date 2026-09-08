# OBD end-of-drive classification

The server's lifecycle projection distinguishes a backup handoff from an unexpected
recording interruption. The drive list and detail page use the same labels and explanations.

- **Saved for backup** (`saved_for_backup`): the logger finalised the drive with
  `ingestion_requested`. This reports an orderly recording handoff, not proof that the
  engine stopped or that every expected vehicle signal was captured.
- **Likely engine shutdown** (`shutdown_detected`): a `connection_lost` ending has recent
  recorded shutdown evidence. A running engine above 300 RPM at at least 13 V must be
  followed within 30 seconds by a stationary sample at 0–300 RPM and 10–13 V (exclusive
  upper voltage bound). That sample must be within 30 seconds of the producer's ending.
  Later running RPM, motion, charging voltage or a non-increasing sample clock invalidates
  the inference. This remains an inference: it does not assert a confirmed ignition-off.
- Other unclean endings remain **Interrupted** or **Recovered**. A drive with no vehicle
  data still takes precedence over any end-of-drive classification.

Projection version 3 is applied on import, server startup and explicit reprocessing, so
existing history is corrected when the updated server starts. No database schema migration
or companion APK installation is required. Original manifests, producer completion status,
stop reasons, clean-end flags, diagnostics, sample rows and immutable bundles are retained.
The inferred status and its source sample timestamp are recorded in derived lifecycle
evidence. Effective end times retain the existing last-sample rule for unclean recordings.

The change affects presentation and server interpretation. It does not alter Android's
engine-stop debounce, ingestion protocol, radio control or recorder policy.

Regression coverage includes recent shutdown, stale/missing/contradictory signals, resumed
activity, unexpected failure reasons, historical startup repair, repeated reprocessing and
preservation of raw sample content and bundle bytes.
