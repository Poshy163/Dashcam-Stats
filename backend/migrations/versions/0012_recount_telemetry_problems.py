"""Recount telemetry problems, so a clean recording stops reporting itself as degraded.

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-26

The extractor used to record a "problem" on every sample it assembled from more than one
candidate frame -- which is every cleanly parsed sample, because ``candidate_fps`` is twice
``sample_fps`` precisely so each stored sample can be built from the better of two
independent reads. It was provenance written into a field that means fault.

``quality_rollup`` counted it, so ``recordings.telemetry_problem_count`` came out equal to
the point count on healthy recordings, and the Telemetry Health page -- whose entire job is
to separate real GPS loss from OCR noise -- classified the whole library as degraded. On the
library this was found on, 1,976 of 2,057 recordings reported "degraded" while holding a
fix on every single sample and no gaps at all. "Healthy" was unreachable.

The extractor no longer writes it. That fixes recordings processed from here on and does
nothing for the ones already in the table, and the stage revision is deliberately *not*
bumped to force those: re-deriving this needs no video. The per-sample strings are still in
``telemetry_points.quality_json``, so the counter can simply be counted again from what is
already stored -- no decode, no OCR, no ffmpeg, on a library where the alternative is
re-reading every clip on the share.

Only ``telemetry_problem_count`` is rewritten, and only ever to the value a rollup over the
stored samples produces. ``quality_json`` is not touched: how many frames were considered is
real provenance and stays where it was recorded. The other four rollups -- gaps, no-fix,
OCR gaps, rejected -- key off ``has_fix`` and ``gps_status`` and were never affected.

There is no downgrade beyond leaving the corrected numbers in place: the previous values
were wrong, and restoring them would mean recomputing the very noise this removes.
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

#: Matched with SQL ``LIKE`` rather than a regex, because SQLite has no ``REGEXP`` unless
#: the application registers one -- and a migration runs before any of that exists.
#: ``_`` is a single-character wildcard in ``LIKE``, so a literal underscore would need
#: escaping; there is none in this string.
_NOISE_LIKE = "selected best fields from % candidate frames"

#: The rule from ``quality_rollup``, in SQL: a sample is a problem when it holds a problem
#: string that is not the noise above, or when its OCR outright failed.
#:
#: Both guards on the way in are load-bearing, and neither is hypothetical -- each was
#: reproduced against SQLite before this migration was allowed near a real library:
#:
#: * ``json_valid`` -- ``json_extract`` raises "malformed JSON" rather than returning NULL
#:   when the column holds an empty string or anything that is not JSON. A NULL column is
#:   fine, but an empty string is not, and the difference is invisible until it happens.
#: * ``json_type(...) = 'array'`` -- ``json_each`` over a scalar raises the same way, so a
#:   ``problems`` key holding a bare string would take the whole statement down. It also
#:   keeps this in step with ``real_problems``, which returns nothing for a non-list.
#:
#: An unreadable sample counts as no problem rather than as a problem: this recount exists
#: to stop the page inventing faults, and it should not invent a different one on the way.
#: A migration that raises leaves the container unable to start, which is a far worse
#: failure than a counter that stays as it was.
_RECOUNT = f"""
UPDATE recordings
   SET telemetry_problem_count = (
       SELECT COUNT(*)
         FROM telemetry_points tp
        WHERE tp.recording_id = recordings.id
          AND json_valid(tp.quality_json)
          AND (
              (
                  json_type(tp.quality_json, '$.problems') = 'array'
                  AND EXISTS (
                      SELECT 1
                        FROM json_each(tp.quality_json, '$.problems') je
                       WHERE je.value NOT LIKE '{_NOISE_LIKE}'
                  )
              )
              OR json_extract(tp.quality_json, '$.ocr_status') IN ('failed', 'rejected')
          )
   )
 WHERE telemetry_problem_count > 0
"""


def _recount_in_python(bind: sa.engine.Connection) -> None:
    """The same recount without JSON1, for a SQLite built without it.

    Streamed a recording at a time rather than loaded whole: the library this was written
    for holds 219,000 telemetry rows, and the point of this migration is to be cheaper than
    reprocessing, not merely different.
    """
    ids = [
        row[0]
        for row in bind.execute(
            sa.text("SELECT id FROM recordings WHERE telemetry_problem_count > 0")
        )
    ]
    for recording_id in ids:
        count = 0
        rows = bind.execute(
            sa.text("SELECT quality_json FROM telemetry_points WHERE recording_id = :rid"),
            {"rid": recording_id},
        )
        for (raw,) in rows:
            if not raw:
                continue
            try:
                quality = json.loads(raw) if isinstance(raw, str) else raw
            except (TypeError, ValueError):
                # Unreadable, so nothing can be asserted about it. Skipped rather than
                # counted, for the same reason the SQL above skips it.
                continue
            if not isinstance(quality, dict):
                continue
            problems = quality.get("problems")
            real = [
                p
                for p in (problems if isinstance(problems, (list, tuple)) else [])
                if not (
                    str(p).startswith("selected best fields from ")
                    and str(p).endswith(" candidate frames")
                )
            ]
            if real or quality.get("ocr_status") in {"failed", "rejected"}:
                count += 1
        bind.execute(
            sa.text(
                "UPDATE recordings SET telemetry_problem_count = :n WHERE id = :rid"
            ),
            {"n": count, "rid": recording_id},
        )


def upgrade() -> None:
    bind = op.get_bind()
    try:
        bind.execute(sa.text(_RECOUNT))
    except sa.exc.OperationalError:
        # No JSON1. Rare on any modern build, but the fallback costs nothing to carry and
        # a migration that dies here would leave the library unable to start.
        _recount_in_python(bind)


def downgrade() -> None:
    # Nothing to undo. The counter is derived, and the values this replaced were wrong.
    pass
