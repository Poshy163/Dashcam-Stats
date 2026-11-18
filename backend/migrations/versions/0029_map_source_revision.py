"""Transactional revision for cached map results.

Revision ID: 0029
Revises: 0028
"""

from __future__ import annotations

from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def _visible(row: str) -> str:
    # Mirrors visible_revision(Recording.telemetry_revision). Frozen here so old
    # migrations produce identical triggers when reconstructing a backup schema.
    return f"""({row}.ignored IS 0
        AND ({row}.state = 'COMPLETED' OR ({row}.state = 'DELETED' AND {row}.processed_at IS NOT NULL))
        AND {row}.telemetry_revision IS NOT 'invalidated')"""


def _published_point(row: str) -> str:
    # Analysis stages insert telemetry before the recording is published. Its later
    # visibility transition covers those writes together, sparing the cache per-fix churn.
    return f"""({row}.has_fix IS 1 AND {row}.lat IS NOT NULL AND {row}.lon IS NOT NULL
        AND EXISTS (SELECT 1 FROM recordings r WHERE r.id = {row}.recording_id AND {_visible("r")}))"""


def _changed(columns: str) -> str:
    return "(" + " OR ".join(f"OLD.{name} IS NOT NEW.{name}" for name in columns.split(", ")) + ")"


_POINT_COLUMNS = "id, recording_id, journey_id, has_fix, lat, lon, captured_at, t_offset_s, gps_quality, speed_kmh, breaks_segment"
_RECORDING_COLUMNS = "id, state, ignored, processed_at, telemetry_revision, camera_id"

# An update that only changes processed_at for an already published recording does
# not change any map input. Corrections to coordinates rotate the token themselves.
_RECORDING_CHANGED = f"""(
    {_visible("OLD")} IS NOT {_visible("NEW")}
    OR ({_visible("NEW")} AND {_changed("id, camera_id")})
)"""
_HAS_POINTS = """EXISTS (
    SELECT 1 FROM telemetry_points t WHERE t.recording_id = {row}.id
    AND t.has_fix IS 1 AND t.lat IS NOT NULL AND t.lon IS NOT NULL
)"""


def _blocked(row: str) -> str:
    return f"coalesce(json_extract({row}.motion_json, '$.status') IN ('unknown', 'unconfirmed'), 0)"


# Each entry names its SQLite event and optional WHEN expression. Updates only
# inspect map columns; queue progress, scan metadata and unrelated totals stay cheap.
_TRIGGERS = {
    "point_insert": ("AFTER INSERT ON telemetry_points", _published_point("NEW")),
    "point_delete": ("AFTER DELETE ON telemetry_points", _published_point("OLD")),
    "point_update": (
        f"AFTER UPDATE OF {_POINT_COLUMNS} ON telemetry_points",
        f"{_changed(_POINT_COLUMNS)} AND ({_published_point('OLD')} OR {_published_point('NEW')})",
    ),
    "recording_insert": (
        "AFTER INSERT ON recordings",
        f"{_visible('NEW')} AND {_HAS_POINTS.format(row='NEW')}",
    ),
    # BEFORE is important: the foreign-key cascade can remove every point before AFTER fires.
    "recording_delete": (
        "BEFORE DELETE ON recordings",
        f"{_visible('OLD')} AND {_HAS_POINTS.format(row='OLD')}",
    ),
    "recording_update": (
        f"AFTER UPDATE OF {_RECORDING_COLUMNS} ON recordings",
        f"{_RECORDING_CHANGED} AND ({_HAS_POINTS.format(row='NEW')} OR "
        f"(OLD.id IS NOT NEW.id AND {_HAS_POINTS.format(row='OLD')}))",
    ),
    # Routes group by point.journey_id, including NULL. Deletes may clear that id.
    "journey_insert": ("AFTER INSERT ON journeys", _blocked("NEW")),
    "journey_delete": ("AFTER DELETE ON journeys", ""),
    "journey_update": (
        "AFTER UPDATE OF id, motion_json ON journeys",
        f"OLD.id IS NOT NEW.id OR {_blocked('OLD')} IS NOT {_blocked('NEW')}",
    ),
    "camera_insert": ("AFTER INSERT ON cameras", ""),
    "camera_delete": ("AFTER DELETE ON cameras", ""),
    "camera_update": ("AFTER UPDATE OF id, role, key ON cameras", _changed("id, role, key")),
}


def upgrade() -> None:
    if op.get_context().dialect.name != "sqlite":
        return
    op.execute("""CREATE TABLE map_source_state (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        revision BLOB NOT NULL CHECK (typeof(revision) = 'blob' AND length(revision) = 16)
    )""")
    op.execute("INSERT INTO map_source_state (id, revision) VALUES (1, randomblob(16))")
    for name, (event, condition) in _TRIGGERS.items():
        when = f"WHEN {condition}" if condition else ""
        op.execute(f"""CREATE TRIGGER map_source_{name} {event}
            {when} BEGIN
                UPDATE map_source_state SET revision = randomblob(16) WHERE id = 1;
            END""")


def downgrade() -> None:
    if op.get_context().dialect.name != "sqlite":
        return
    for name in _TRIGGERS:
        op.execute(f"DROP TRIGGER map_source_{name}")
    op.execute("DROP TABLE map_source_state")
