"""Track cells affected by edits to the default moving heatmap.

Revision ID: 0033
Revises: 0032
"""

from alembic import op

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def _visible(r: str) -> str:
    return f"""({r}.ignored IS 0 AND {r}.telemetry_revision IS NOT 'invalidated'
        AND ({r}.state = 'COMPLETED' OR ({r}.state = 'DELETED' AND {r}.processed_at IS NOT NULL)))"""


def _candidate(t: str) -> str:
    return f"""({t}.has_fix IS 1 AND {t}.lat IS NOT NULL AND {t}.lon IS NOT NULL
        AND ({t}.speed_kmh IS NULL OR {t}.speed_kmh >= 5)
        AND ({t}.gps_quality IS NULL OR {t}.gps_quality IN ('valid', 'interpolated')))"""


def _blocked(j: str) -> str:
    return f"coalesce(json_extract({j}.motion_json, '$.status') IN ('unconfirmed','unknown'), 0)"


def _mark_point(t: str) -> str:
    # During processing a recording is hidden. Its eventual publication marks
    # its cells in one statement. A correction can affect both the old and new cell.
    return f"""INSERT INTO map_heat_dirty (lat, lon)
        SELECT round({t}.lat, 3), round({t}.lon, 3) WHERE {_candidate(t)}
        AND EXISTS (SELECT 1 FROM recordings r WHERE r.id = {t}.recording_id AND {_visible('r')})
        ON CONFLICT DO NOTHING;"""


def _mark_group(condition: str) -> str:
    return f"""INSERT INTO map_heat_dirty (lat, lon)
        SELECT DISTINCT round(t.lat, 3), round(t.lon, 3) FROM telemetry_points t
        WHERE {condition} AND {_candidate('t')} ON CONFLICT DO NOTHING;"""


_POINT_COLUMNS = "id, recording_id, journey_id, has_fix, lat, lon, captured_at, gps_quality, speed_kmh"
_CHANGED = " OR ".join(f"OLD.{c} IS NOT NEW.{c}" for c in _POINT_COLUMNS.split(", "))
_TRIGGERS = {
    "point_insert": ("AFTER INSERT ON telemetry_points", "", _mark_point("NEW")),
    "point_delete": ("AFTER DELETE ON telemetry_points", "", _mark_point("OLD")),
    "point_update": (
        f"AFTER UPDATE OF {_POINT_COLUMNS} ON telemetry_points",
        _CHANGED,
        _mark_point("OLD") + _mark_point("NEW"),
    ),
    "recording_insert": (
        "AFTER INSERT ON recordings",
        _visible("NEW"),
        _mark_group("t.recording_id = NEW.id"),
    ),
    # Record the cells before a cascading deletion removes their source rows.
    "recording_delete": (
        "BEFORE DELETE ON recordings",
        _visible("OLD"),
        _mark_group("t.recording_id = OLD.id"),
    ),
    "recording_update": (
        "AFTER UPDATE OF id, ignored, telemetry_revision, state, processed_at ON recordings",
        f"OLD.id IS NOT NEW.id OR {_visible('OLD')} IS NOT {_visible('NEW')}",
        _mark_group("t.recording_id IN (OLD.id, NEW.id)"),
    ),
    "journey_insert": (
        "AFTER INSERT ON journeys",
        _blocked("NEW"),
        _mark_group("t.journey_id = NEW.id"),
    ),
    "journey_delete": (
        "BEFORE DELETE ON journeys",
        _blocked("OLD"),
        _mark_group("t.journey_id = OLD.id"),
    ),
    "journey_update": (
        "AFTER UPDATE OF id, motion_json ON journeys",
        f"OLD.id IS NOT NEW.id OR {_blocked('OLD')} IS NOT {_blocked('NEW')}",
        _mark_group("t.journey_id IN (OLD.id, NEW.id)"),
    ),
}


def upgrade() -> None:
    if op.get_context().dialect.name != "sqlite":
        return
    op.execute("UPDATE map_source_state SET revision = revision WHERE 0")
    op.execute("ALTER TABLE map_source_state ADD COLUMN heat_cells_ready INTEGER NOT NULL DEFAULT 0")
    op.execute("""CREATE TABLE map_heat_cells (
        lat REAL NOT NULL, lon REAL NOT NULL, weight INTEGER NOT NULL,
        speed_sum REAL, speed_count INTEGER NOT NULL, PRIMARY KEY (lat, lon)
    ) WITHOUT ROWID""")
    # No affinity on dirty keys: native ROUND returns a number, and SQLite can
    # seek the matching expression index directly when comparing it to these keys.
    op.execute("""CREATE TABLE map_heat_dirty (
        lat NOT NULL, lon NOT NULL, PRIMARY KEY (lat, lon)
    ) WITHOUT ROWID""")
    for name, (event, condition, changes) in _TRIGGERS.items():
        when = f"WHEN {condition}" if condition else ""
        op.execute(f"CREATE TRIGGER map_source_heat_{name} {event} {when} BEGIN {changes} END")


def downgrade() -> None:
    if op.get_context().dialect.name != "sqlite":
        return
    op.execute("UPDATE map_source_state SET revision = revision WHERE 0")
    for name in _TRIGGERS:
        op.execute(f"DROP TRIGGER map_source_heat_{name}")
    op.execute("DROP TABLE map_heat_dirty")
    op.execute("DROP TABLE map_heat_cells")
    op.execute("ALTER TABLE map_source_state DROP COLUMN heat_cells_ready")
