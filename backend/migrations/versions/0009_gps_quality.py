"""Record how much each stored position is trusted, and discard the ones that were not.

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-14

Two jobs, and the second is the reason this exists rather than being left to reprocessing.

**The columns.** ``has_fix`` was the only thing SQL could ask about a position, and it
conflates four different states: a coordinate read cleanly from the overlay, one
interpolated across a dropout, one we refused, and a moment when the camera had no
satellite lock at all. Every consumer that needed the distinction had to load
``quality_json`` and pick it apart in Python, so the heat map — which aggregates in SQL and
cannot — simply did not make it, and drew interpolated fills at the same weight as real
observations.

**The repair.** On this library 92,312 stored fixes include several thousand that no check
in the old pipeline could see. The two guards that existed left a gap between them:
consecutive steps were allowed up to 400 km/h, which at 1 Hz licenses a 111 m jump every
second, while the journey-level check had a 5 km floor on its radius. Nothing at all
examined a coordinate between roughly 100 m and 5 km out of place, which is precisely the
range a single misread digit produces — ``E:138.6510`` losing three digits to a pixel gap
reads as ``E:138.6``, a legal longitude 4.7 km west of the drive at 0.94 confidence.

Those positions are already in the database. The source fixes stop new ones arriving but
cannot touch what is stored, and reprocessing 995 recordings is hours of decoding to
correct something determinable here from the coordinates alone. So this pass re-judges
every recording's positions with :mod:`app.osd.track_quality` and clears the ones its own
neighbours contradict.

Only the position is cleared. The timestamp, speed and raw text are kept, because none of
them was ever in doubt — the speed printed alongside a mangled coordinate is perfectly
good, and dropping it would trade a wrong number for a missing one.

Interpolated positions are re-judged but not re-derived: rebuilding a fill needs the video,
which is what the telemetry stage is for. Recordings reprocessed after this migration get
the conservative interpolation as well; this pass only guarantees that nothing already
stored is left somewhere the vehicle could not have been.

The downgrade drops the columns. It cannot restore the cleared coordinates — they were
wrong, and there is nothing to put back — so it does not pretend to.
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


#: Rows updated per executemany batch. Large enough that the round trips do not dominate,
#: small enough that a library with a million points does not build one enormous statement.
_BATCH = 1_000


def _quality_from_json(raw: str | None, has_fix: bool) -> tuple[str, str | None]:
    """Best available reading of an existing row's provenance.

    Rows written before ``quality_json`` existed carry nothing, and their positions are
    still perfectly good — they simply predate the bookkeeping. Those become ``valid`` on
    the strength of ``has_fix`` rather than being condemned for a missing field.
    """
    status = source = None
    if raw:
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            status = parsed.get("gps_status")
            source = parsed.get("gps_source")

    if has_fix:
        return ("interpolated" if source in {"interpolated", "paired_camera"} else "valid", None)
    if status == "no_fix":
        return "no_fix", "no_fix"
    if status == "rejected":
        return "rejected", "invalid_lat_lon"
    return "rejected", "coordinate_parse_failure"


def _repair() -> int:
    # Imported inside the function rather than at module scope: Alembic imports every
    # migration to build its revision graph, and a module-level app import would make that
    # depend on the application package being importable at that moment.
    from app.osd.reasons import GpsQuality
    from app.osd.track_quality import Fix, classify

    bind = op.get_bind()
    recordings = [
        row[0] for row in bind.execute(sa.text("SELECT id FROM recordings ORDER BY id")).all()
    ]

    cleared = 0
    updates: list[dict[str, object]] = []
    marks: list[dict[str, object]] = []

    def flush() -> None:
        if updates:
            bind.execute(
                sa.text(
                    "UPDATE telemetry_points SET has_fix = 0, lat = NULL, lon = NULL, "
                    "heading_deg = NULL, gps_quality = 'rejected', gps_reason = :reason "
                    "WHERE id = :id"
                ),
                updates,
            )
            updates.clear()
        if marks:
            bind.execute(
                sa.text(
                    "UPDATE telemetry_points SET gps_quality = :quality, "
                    "gps_reason = :reason, breaks_segment = :breaks WHERE id = :id"
                ),
                marks,
            )
            marks.clear()

    for recording_id in recordings:
        rows = bind.execute(
            sa.text(
                "SELECT id, t_offset_s, lat, lon, has_fix, quality_json FROM telemetry_points "
                "WHERE recording_id = :rid ORDER BY t_offset_s"
            ),
            {"rid": recording_id},
        ).all()
        if not rows:
            continue

        # Only positions can be judged geometrically; the rest are labelled from what the
        # extractor recorded at the time.
        located = [r for r in rows if r.has_fix and r.lat is not None and r.lon is not None]
        for row in rows:
            if row.has_fix and row.lat is not None and row.lon is not None:
                continue
            quality, reason = _quality_from_json(row.quality_json, bool(row.has_fix))
            marks.append(
                {"id": row.id, "quality": quality, "reason": reason, "breaks": False}
            )

        if located:
            verdicts = classify(
                [
                    Fix(
                        t_s=float(row.t_offset_s or 0.0),
                        lat=float(row.lat),
                        lon=float(row.lon),
                        synthetic=_quality_from_json(row.quality_json, True)[0] == "interpolated",
                    )
                    for row in located
                ]
            )
            for row, verdict in zip(located, verdicts, strict=True):
                if verdict.quality is GpsQuality.REJECTED:
                    updates.append({"id": row.id, "reason": str(verdict.reasons[0])})
                    cleared += 1
                else:
                    marks.append(
                        {
                            "id": row.id,
                            "quality": str(verdict.quality),
                            "reason": None,
                            "breaks": bool(verdict.breaks_segment),
                        }
                    )

        if len(updates) + len(marks) >= _BATCH:
            flush()
    flush()
    return cleared


def upgrade() -> None:
    with op.batch_alter_table("telemetry_points") as batch:
        batch.add_column(sa.Column("gps_quality", sa.String(length=16), nullable=True))
        batch.add_column(sa.Column("gps_reason", sa.String(length=32), nullable=True))
        batch.add_column(
            sa.Column(
                "breaks_segment", sa.Boolean(), nullable=False, server_default=sa.text("0")
            )
        )
    op.create_index(
        "ix_telemetry_quality", "telemetry_points", ["gps_quality", "journey_id"], unique=False
    )
    _repair()


def downgrade() -> None:
    op.drop_index("ix_telemetry_quality", table_name="telemetry_points")
    with op.batch_alter_table("telemetry_points") as batch:
        batch.drop_column("breaks_segment")
        batch.drop_column("gps_reason")
        batch.drop_column("gps_quality")
