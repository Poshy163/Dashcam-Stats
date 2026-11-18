"""Version route inputs per journey, including the unassigned group.

Revision ID: 0031
Revises: 0030
"""

from __future__ import annotations

from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def _visible(r: str) -> str:
    return f"""({r}.ignored IS 0 AND {r}.telemetry_revision IS NOT 'invalidated'
        AND ({r}.state = 'COMPLETED' OR ({r}.state = 'DELETED' AND {r}.processed_at IS NOT NULL)))"""


def _front(r: str) -> str:
    # The route query includes recordings with no matching camera.
    return f"""({_visible(r)} AND NOT EXISTS (
        SELECT 1 FROM cameras c WHERE c.id = {r}.camera_id AND c.role IS NOT 'FRONT'))"""


def _drawable(t: str) -> str:
    return f"""({t}.has_fix IS 1 AND {t}.lat IS NOT NULL AND {t}.lon IS NOT NULL
        AND ({t}.gps_quality IS NULL OR {t}.gps_quality IN ('valid', 'interpolated')))"""


def _included(t: str) -> str:
    return f"""({_drawable(t)} AND EXISTS (
        SELECT 1 FROM recordings r WHERE r.id = {t}.recording_id AND {_front("r")}))"""


def _blocked(j: str) -> str:
    return f"coalesce(json_extract({j}.motion_json, '$.status') IN ('unknown', 'unconfirmed'), 0)"


def _mark(ids: str, has_unassigned: str) -> str:
    return f"""
        UPDATE journeys SET route_revision = randomblob(16) WHERE id IN ({ids});
        UPDATE map_source_state SET unassigned_route_revision = randomblob(16)
        WHERE id = 1 AND ({has_unassigned});"""


def _mark_point(row: str) -> str:
    return _mark(f"{row}.journey_id", f"{row}.journey_id IS NULL")


def _mark_recordings(ids: str) -> str:
    # Points can be reassigned before recording.journey_id is updated. Use the
    # same grouping column as the reader and restrict both queries by recording.
    points = f"FROM telemetry_points t WHERE t.recording_id IN ({ids}) AND {_drawable('t')}"
    return _mark(
        f"SELECT t.journey_id {points}",
        f"EXISTS (SELECT 1 {points} AND t.journey_id IS NULL)",
    )


def _camera_recordings(camera_id: str) -> str:
    return f"SELECT r.id FROM recordings r WHERE r.camera_id IN ({camera_id}) AND {_visible('r')}"


_POINT_COLUMNS = "id, recording_id, journey_id, captured_at, t_offset_s, lat, lon, has_fix, gps_quality, breaks_segment"
_POINT_CHANGED = " OR ".join(f"OLD.{c} IS NOT NEW.{c}" for c in _POINT_COLUMNS.split(", "))
_RECORDING_COLUMNS = "id, camera_id, state, processed_at, ignored, telemetry_revision"

_TRIGGERS = {
    "point_insert": ("AFTER INSERT ON telemetry_points", _included("NEW"), _mark_point("NEW")),
    "point_delete": ("AFTER DELETE ON telemetry_points", _included("OLD"), _mark_point("OLD")),
    "point_update": (
        f"AFTER UPDATE OF {_POINT_COLUMNS} ON telemetry_points",
        f"OLD.journey_id IS NEW.journey_id AND ({_POINT_CHANGED}) "
        f"AND ({_included('OLD')} OR {_included('NEW')})",
        _mark_point("NEW"),
    ),
    "point_leave": (
        "AFTER UPDATE OF journey_id ON telemetry_points",
        f"OLD.journey_id IS NOT NEW.journey_id AND {_included('OLD')}",
        _mark_point("OLD"),
    ),
    "point_join": (
        "AFTER UPDATE OF journey_id ON telemetry_points",
        f"OLD.journey_id IS NOT NEW.journey_id AND {_included('NEW')}",
        _mark_point("NEW"),
    ),
    "recording_insert": ("AFTER INSERT ON recordings", _front("NEW"), _mark_recordings("NEW.id")),
    # The parent must identify its groups before the foreign-key cascade clears the points.
    "recording_delete": ("BEFORE DELETE ON recordings", _front("OLD"), _mark_recordings("OLD.id")),
    "recording_update": (
        f"AFTER UPDATE OF {_RECORDING_COLUMNS} ON recordings",
        f"{_front('OLD')} IS NOT {_front('NEW')} OR (OLD.id IS NOT NEW.id AND "
        f"({_front('OLD')} OR {_front('NEW')}))",
        _mark_recordings("OLD.id, NEW.id"),
    ),
    "journey_insert": (
        "AFTER INSERT ON journeys",
        "",
        "UPDATE journeys SET route_revision = randomblob(16) WHERE id = NEW.id;",
    ),
    "journey_update": (
        "AFTER UPDATE OF id, motion_json ON journeys",
        f"OLD.id IS NOT NEW.id OR {_blocked('OLD')} IS NOT {_blocked('NEW')}",
        "UPDATE journeys SET route_revision = randomblob(16) WHERE id = NEW.id;",
    ),
    # An absent camera is included, so inserting/deleting a non-front camera can
    # change a LEFT JOIN in a database with deferred foreign keys.
    "camera_insert": (
        "AFTER INSERT ON cameras",
        "NEW.role IS NOT 'FRONT'",
        _mark_recordings(_camera_recordings("NEW.id")),
    ),
    "camera_delete": (
        "AFTER DELETE ON cameras",
        "OLD.role IS NOT 'FRONT'",
        _mark_recordings(_camera_recordings("OLD.id")),
    ),
    "camera_update": (
        "AFTER UPDATE OF id, role ON cameras",
        "(OLD.role IS 'FRONT') IS NOT (NEW.role IS 'FRONT') OR OLD.id IS NOT NEW.id",
        _mark_recordings(_camera_recordings("OLD.id, NEW.id")),
    ),
}


def upgrade() -> None:
    if op.get_context().dialect.name != "sqlite":
        return
    # The SQLite driver starts the enclosing Alembic transaction for DML, but can
    # autocommit DDL issued first. Start it without changing a token so an interrupted
    # upgrade rolls back both columns, triggers and the version stamp together.
    op.execute("UPDATE map_source_state SET revision = revision WHERE 0")
    # Keep the NULL group separate: every signed integer, including zero, can
    # legitimately be a journey id. Empty journeys cost one small manifest entry.
    op.execute("ALTER TABLE journeys ADD COLUMN route_revision BLOB")
    op.execute("ALTER TABLE map_source_state ADD COLUMN unassigned_route_revision BLOB")
    op.execute("UPDATE journeys SET route_revision = randomblob(16)")
    op.execute("UPDATE map_source_state SET unassigned_route_revision = randomblob(16)")
    for name, (event, condition, changes) in _TRIGGERS.items():
        when = f"WHEN {condition}" if condition else ""
        op.execute(f"CREATE TRIGGER map_source_route_{name} {event} {when} BEGIN {changes} END")


def downgrade() -> None:
    if op.get_context().dialect.name != "sqlite":
        return
    op.execute("UPDATE map_source_state SET revision = revision WHERE 0")
    for name in _TRIGGERS:
        op.execute(f"DROP TRIGGER map_source_route_{name}")
    op.execute("ALTER TABLE map_source_state DROP COLUMN unassigned_route_revision")
    op.execute("ALTER TABLE journeys DROP COLUMN route_revision")
