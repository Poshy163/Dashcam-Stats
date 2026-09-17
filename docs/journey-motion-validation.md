# Journey movement validation

An accurately recognised speed overlay can contain inaccurate sensor data. A parked
camera was observed printing 111 km/h while the surrounding view remained unchanged.
OCR reprocessing cannot fix a number already wrong in the original footage.

Journey summaries now apply `motion-v1` to the existing camera-deduplicated GPS track:

- A speed substantially above its ten-second neighbourhood median is excluded when
  there is sufficient context on both sides. Missing context remains unknown; this is
  not a replacement speed estimate.
- Counting a drive requires a continuous 10–30 second stretch with at least three
  distinct observations, reported speed of at least 5 km/h, and at least 50 metres of
  positional progress. Gaps over ten seconds, route breaks, low speeds and rejected
  speeds interrupt the evidence. The existing configured average/maximum thresholds
  still apply after this check.
- Sparse GPS coverage can also establish travel when adjacent fixes are at least 500
  metres apart across a 10–300 second gap, both endpoint speeds agree with the implied
  speed within 50%, and that speed is at most 200 km/h. Explicit route breaks exclude
  this fallback. This preserves genuine travel across tunnels without accepting small
  receiver jumps or inventing observations inside a dropout.
- Unconfirmed sessions have no validated journey speed/distance or travelled route.
  They remain accessible through their original links and the list's **Include parked /
  unconfirmed** option. Very short trips or poor GPS coverage can remain unconfirmed;
  this label does not assert that the vehicle never moved.
- `motion_json` preserves the observed aggregate values, assessment revision, evidence
  counts and reason. Motion assessment does not alter raw speed readings or footage;
  the builder's existing GPS integrity checks still apply. Raw recording speeds are
  observations, not validated journey statistics.
- This assessment does not authorise deleting recordings. Missing validated speed
  cannot qualify for the existing parked-session deletion rule.

Migration 0023 adds the nullable assessment column; the existing pre-migration database
backup runs before the schema change. The scheduler checks ten historical sessions per
pass, with a separate transaction per session, every thirty seconds when journeys are
enabled. It resumes from the persisted revision after a restart. A failed session stays
pending for retry while the other sessions in its batch continue. Newly processed,
extended, split and merged journeys are assessed by the normal summary builder.
Manual boundaries and journey IDs are preserved.

`GET /api/journeys/motion-quality` reports total, pending, moving, unconfirmed and unknown
counts. The Journeys page displays this progress and refreshes dependent views as checks
finish. The update reuses stored telemetry; it does not queue another video/OCR/plate pass.

This changes the application's acceptance and classification of sensor output. It does
not modify vendor firmware or repair the GPS receiver. The original cause of a device
emitting false speed requires live device diagnostics to distinguish reception,
receiver and firmware behaviour.
