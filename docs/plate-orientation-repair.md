# Plate orientation repair

`plates-v4` compares both orientations of each plate crop. It uses the OCR score and
regional evidence, penalises format substitutions, and abstains when different readings
have comparable evidence. The selected orientation stays attached to the reading and is
used for both its plate and vehicle previews. Bounding boxes remain in source-video
coordinates. There is no permanent orientation decision based on the first eight crops.

The OCR badge is a mean character score, not a calibrated probability that an entire
registration is correct. Independent multi-frame corroboration is not implemented by the
plate-candidate limit: that limit applies to boxes in the track's best saved vehicle image.

`AU-SA` prefers SA formats in close orientation comparisons, while stronger interstate
evidence can still win. Supported SA series include standard S-number-letter plates,
older shared three-letter/three-digit plates, Premium (`AA000A`) and Euro (`SEA00A` and
`SXA00A`). The latter formats follow the official [Premium](https://ezyplates.sa.gov.au/plate-styles/premium-number-plates)
and [Euro](https://ezyplates.sa.gov.au/plate-styles/euro-number-plates) catalogues.
Unrestricted custom words and numeric-only plates still need additional verification;
recognising an arbitrary word is not evidence that it is a vehicle registration.

## Updating historical results

Back up the database before reprocessing. Deploy the image containing `plates-v4`, then
verify `GET /api/plates/quality` returns that revision. Do not run the rebuild against the
old image: it will repeat the old orientation decisions.

With `plates.auto_revalidate` enabled (the default), the scheduler checks historical
results shortly after startup and then every 30 seconds. It queues at most 200 affected
recordings per sweep, below new ingestion priority. A different plate revision or relevant
plate setting triggers revalidation; inconsistent stored evidence is also checked. Existing
queued/running work is preserved, and the target profile is recorded durably so exhausted
failures cannot create an infinite automatic retry loop. Missing, ignored, invalid and
not-yet-detected footage is not queued as a plate-only repair.

The Plates page shows progress separately for each camera and links failed work to the
queue. A paused processing queue remains paused. No operator-triggered bulk reset is
required after an update.

For an explicit spot check, reprocess representative recordings from both cameras with:

```http
POST /api/recordings/{id}/reprocess
Content-Type: application/json

{"stages":["plates"]}
```

Verify the refreshed observations and pictures. To request a manual historical repair
instead of waiting for the automatic batches:

```http
POST /api/reprocess
Content-Type: application/json

{"stages":["plates"],"only_outdated":true}
```

Authenticate through the normal session or API-key header; never put credentials in a
URL or this document. The targeted request preserves the existing queue, reuses stored
vehicle crops where available, and rebuilds plate-dependent summaries. It replaces the
recording's previous observations and refreshes affected plate counts; it does not rename
whole identities based on one image. Old identities with no remaining observations are
handled by the normal orphan cleanup.

`GET /api/plates/quality` reports eligible, current, remaining, failed and excluded
recordings separately for each camera. Excluded files (missing, ignored or invalid) are
not silently counted as repaired. Observation responses also expose the actual OCR
orientation, method, score margin and normalisation substitution count. These values are
null for legacy observations, whose preview orientation did not prove OCR orientation.

Completion requires zero remaining eligible recordings, no unexplained failed repairs,
and inspection of representative refreshed text/crop pairs from both cameras. Revision
coverage verifies that the fix ran; it does not prove perfect OCR. Saved JPEG re-reads can
differ from the original inference because of compression, and low-resolution or blurred
plates still need human review. Record comparison results separately from an accuracy
benchmark unless the test set has independently labelled ground truth.
