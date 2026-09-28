"""Recount resolved candidate warnings without rewriting telemetry or provenance.

Revision ID: 0024
Revises: 0023

Per-field OCR selection already rescued good fields from failed sibling frames, but the
warning union still classified them as faulty. Recount existing cached problem totals
from the same conservative evidence used by new rollups. No video is decoded, and no
quality_json, coordinates, GPS counters, processing revision or raw OCR is rewritten.
Malformed/incomplete recordings retain their previous counter rather than claiming that
missing evidence is healthy. Work is streamed in recording-ID batches and point rows.
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

from app.osd.problems import stored_problems

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def recount(bind: sa.engine.Connection, *, apply: bool = True) -> dict[str, int]:
    """Bounded, idempotent recount; apply=False supports an offline preview."""
    result = {"examined": 0, "changed": 0, "skipped": 0, "problems_removed": 0}
    last_id = 0
    while True:
        batch = bind.execute(
            sa.text(
                "SELECT id, telemetry_problem_count, telemetry_point_count FROM recordings "
                "WHERE id > :last AND telemetry_problem_count > 0 ORDER BY id LIMIT 128"
            ),
            {"last": last_id},
        ).all()
        if not batch:
            break
        for recording_id, previous, expected in batch:
            last_id = recording_id
            result["examined"] += 1
            count = seen = 0
            valid = True
            for raw, has_fix, speed in bind.execute(
                sa.text(
                    "SELECT quality_json, has_fix, speed_kmh FROM telemetry_points "
                    "WHERE recording_id = :rid"
                ),
                {"rid": recording_id},
            ):
                seen += 1
                try:
                    quality = json.loads(raw) if isinstance(raw, str) else raw
                except (TypeError, ValueError):
                    valid = False
                    continue
                if (
                    not isinstance(quality, dict)
                    or not isinstance(quality.get("problems"), list)
                    or quality.get("ocr_status")
                    not in ("valid", "partial", "failed", "rejected", "low_confidence")
                ):
                    valid = False
                    continue
                if stored_problems(quality, has_fix=bool(has_fix), speed_kmh=speed) or quality.get(
                    "ocr_status"
                ) in ("failed", "rejected"):
                    count += 1
            if not valid or not seen or seen != expected:
                result["skipped"] += 1
                continue
            if count != previous:
                result["changed"] += 1
                result["problems_removed"] += previous - count
                if apply:
                    bind.execute(
                        sa.text(
                            "UPDATE recordings SET telemetry_problem_count = :n WHERE id = :rid"
                        ),
                        {"n": count, "rid": recording_id},
                    )
    return result


def upgrade() -> None:
    recount(op.get_bind())


def downgrade() -> None:
    # Corrected derived counts remain; provenance needed to recalculate was preserved.
    pass
