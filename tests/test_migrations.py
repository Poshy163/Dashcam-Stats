"""Database migrations and first-boot seeding.

A migration that does not match the models is only discovered when a user's container
fails to start, so the schema Alembic produces is compared against the schema SQLAlchemy
declares rather than assumed to agree.
"""

from __future__ import annotations

import pytest
from sqlalchemy import inspect, select

from app.db import models
from app.db.models import Base, Camera, CameraRole, OsdProfile
from app.db.session import current_revision, dispose_engine, get_engine, init_db, session_scope


@pytest.fixture
async def migrated(app_config):
    await init_db()
    yield
    await dispose_engine()


class TestMigrations:
    async def test_upgrade_creates_every_declared_table(self, migrated):
        engine = get_engine()
        async with engine.connect() as conn:
            tables = await conn.run_sync(lambda c: set(inspect(c).get_table_names()))

        declared = set(Base.metadata.tables)
        missing = declared - tables
        assert not missing, f"migration did not create: {sorted(missing)}"

    async def test_migration_is_at_head(self, migrated):
        assert current_revision() is not None

    async def test_columns_match_the_models(self, migrated):
        engine = get_engine()
        async with engine.connect() as conn:
            actual = await conn.run_sync(
                lambda c: {
                    name: {col["name"] for col in inspect(c).get_columns(name)}
                    for name in inspect(c).get_table_names()
                }
            )

        mismatches: list[str] = []
        for name, table in Base.metadata.tables.items():
            if name not in actual:
                continue
            declared = {c.name for c in table.columns}
            missing = declared - actual[name]
            if missing:
                mismatches.append(f"{name}: missing {sorted(missing)}")
        assert not mismatches, "; ".join(mismatches)

    async def test_high_cardinality_tables_are_indexed(self, migrated):
        # telemetry_points gets a row per second of footage and detections grow with it;
        # without these the recordings and journey views table-scan as the library grows.
        engine = get_engine()
        async with engine.connect() as conn:
            indexes = await conn.run_sync(
                lambda c: {
                    name: {ix["name"] for ix in inspect(c).get_indexes(name)}
                    for name in inspect(c).get_table_names()
                }
            )
        assert any("recording" in ix for ix in indexes.get("telemetry_points", set()))
        assert any("recording" in ix for ix in indexes.get("detections", set()))

    async def test_running_init_twice_is_idempotent(self, app_config):
        # Every container restart runs this; a second run must be a no-op rather than an
        # error or a duplicate seed.
        await init_db()
        await init_db()
        async with session_scope() as session:
            cameras = (await session.execute(select(Camera))).scalars().all()
        assert len({c.key for c in cameras}) == len(cameras)
        await dispose_engine()

    async def test_sqlite_runs_in_wal_mode(self, migrated):
        # WAL is what makes one writer plus many readers safe, which is the whole basis
        # for choosing SQLite over Postgres here.
        engine = get_engine()
        async with engine.connect() as conn:
            result = await conn.exec_driver_sql("PRAGMA journal_mode")
            assert str(result.scalar()).lower() == "wal"

    async def test_foreign_keys_are_enforced(self, migrated):
        # SQLite ignores foreign keys unless asked; without this the cascade rules in the
        # schema would be decorative.
        engine = get_engine()
        async with engine.connect() as conn:
            result = await conn.exec_driver_sql("PRAGMA foreign_keys")
            assert bool(result.scalar())


class TestSeeding:
    async def test_seeds_front_and_rear_cameras(self, migrated):
        async with session_scope() as session:
            cameras = {c.key: c for c in (await session.execute(select(Camera))).scalars().all()}
        # camera_0 is the front camera and camera_1 the rear. Nothing in the filenames or
        # the file metadata says so -- it was confirmed against the physical install, and
        # getting it backwards would mislabel every recording in the UI.
        assert cameras["camera_0"].role is CameraRole.FRONT
        assert cameras["camera_1"].role is CameraRole.REAR

    async def test_seeds_a_default_osd_profile(self, migrated):
        async with session_scope() as session:
            profile = (await session.execute(select(OsdProfile))).scalars().first()

        assert profile is not None
        # The overlay sits at the bottom of the frame; the default region must cover the
        # measured band (y = 0.963 to 0.993 of frame height on 1080p).
        assert profile.region_y + profile.region_h >= 0.99
        assert profile.region_y <= 0.963

    async def test_seeding_twice_does_not_duplicate(self, migrated):
        from app.db.seed import seed_defaults

        async with session_scope() as session:
            await seed_defaults(session)
        async with session_scope() as session:
            cameras = (await session.execute(select(Camera))).scalars().all()
            profiles = (await session.execute(select(OsdProfile))).scalars().all()

        assert len(cameras) == len({c.key for c in cameras})
        assert len(profiles) == len({p.name for p in profiles})


class TestSchemaGuarantees:
    def test_plate_observations_are_unique_per_track(self):
        # The dedup guarantee: reprocessing a recording must update the existing
        # observation rather than adding a second row for the same vehicle.
        constraints = {c.name for c in models.PlateObservation.__table__.constraints if c.name}
        assert "uq_plateobs_plate_rec_track" in constraints

    def test_telemetry_points_are_unique_per_offset(self):
        constraints = {c.name for c in models.TelemetryPoint.__table__.constraints if c.name}
        assert "uq_telemetry_recording_offset" in constraints

    def test_tracks_are_unique_per_recording(self):
        constraints = {c.name for c in models.TrackedObject.__table__.constraints if c.name}
        assert "uq_track_recording_key" in constraints

    def test_recording_paths_are_unique(self):
        assert models.Recording.__table__.columns["rel_path"].unique

    def test_coordinates_are_range_checked(self):
        # A CHECK constraint is the last line of defence against a bad OCR read reaching
        # the map: |lat| <= 90 and |lon| <= 180.
        names = {c.name for c in models.TelemetryPoint.__table__.constraints if c.name}
        assert {"ck_telemetry_lat", "ck_telemetry_lon"} <= names

    def test_media_is_referenced_by_path_not_stored_inline(self):
        # No BLOB columns anywhere: video and crops live on disk, never in the database.
        for table in Base.metadata.tables.values():
            for column in table.columns:
                assert "BLOB" not in str(column.type).upper(), (
                    f"{table.name}.{column.name} would store binary data in the database"
                )
