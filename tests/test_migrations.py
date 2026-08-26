"""Database migrations and first-boot seeding.

A migration that does not match the models is only discovered when a user's container
fails to start, so the schema Alembic produces is compared against the schema SQLAlchemy
declares rather than assumed to agree.
"""

from __future__ import annotations

import json

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

    async def test_retired_detection_model_is_rewritten(self, migrated):
        """A stored model name from the retired registry must not survive the upgrade.

        Settings are seeded idempotently, so an upgraded deployment keeps whatever is in
        the row. Left alone, that is a name the registry no longer knows, and detection
        switches itself off with only a log line to show for it — the failure mode this
        migration exists to prevent. Asserting the rewrite happens is the only way to know
        it does: a migration that matches nothing passes just as quietly as one that works.
        """
        import asyncio
        from datetime import UTC, datetime

        from alembic import command
        from sqlalchemy import text

        from app.ai.models import REGISTRY
        from app.db.models import AppSetting
        from app.db.session import alembic_config

        key = "processing.detection_model"
        async with session_scope() as session:
            # Settings persist only once chosen, so this row exists exactly for the users
            # the migration is for: those who picked a model from the retired registry.
            # Written as JSON, the way the application stores a string setting.
            await session.execute(
                text(
                    "INSERT INTO app_settings (key, value, updated_at) "
                    "VALUES (:k, :v, :t) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
                ),
                {"k": key, "v": '"yolov8n"', "t": datetime.now(UTC)},
            )
            await session.commit()

        await dispose_engine()
        config = alembic_config()
        # Step back over the migration and forward again, so the real upgrade() runs
        # against a genuinely stale row rather than a re-creation of what it does.
        await asyncio.to_thread(command.downgrade, config, "0002")
        await asyncio.to_thread(command.upgrade, config, "head")

        async with session_scope() as session:
            stored = (
                await session.execute(select(AppSetting.value).where(AppSetting.key == key))
            ).scalar_one()
        assert stored == "rfdetr-nano"
        assert stored in REGISTRY

    async def test_every_detection_choice_exists_in_the_registry(self, migrated):
        """The settings dropdown must not offer a model that cannot be fetched.

        These two lists live in different files and drifted apart once already, which is
        what left every deployment pointing at weights that 404'd.
        """
        from app.ai.models import REGISTRY
        from app.core.settings_schema import SETTINGS

        definition = next(s for s in SETTINGS if s.key == "processing.detection_model")
        offered = {value for value, _label in (definition.choices or ())}
        assert offered, "detection model setting offers no choices"
        assert offered <= set(REGISTRY), (
            f"offered but unavailable: {sorted(offered - set(REGISTRY))}"
        )
        assert definition.default in REGISTRY

    async def test_the_fingerprint_backfill_spares_a_settling_row(self, migrated):
        """0011's rescue, asserted against the real chain rather than assumed.

        The backfill stamps every fingerprinted row with the stat it currently carries, so
        an existing library is not re-read on the next scan. The one exception is a row in
        ``settling`` — the state this whole change set exists to un-strand — which must be
        left null so the scanner reads its bytes again.

        It is asserted because a migration that matches nothing passes exactly as quietly
        as one that works, and this predicate is easy to get wrong in a way nothing else
        catches: ``Enum`` persists the member *name*, so ``state <> 'settling'`` (the
        member's *value*) excludes nothing at all.
        """
        import asyncio
        from datetime import UTC, datetime

        from alembic import command
        from sqlalchemy import text

        from app.db.session import alembic_config

        await dispose_engine()
        config = alembic_config()
        await asyncio.to_thread(command.downgrade, config, "0010")

        async with session_scope() as session:
            for name, state in (("stranded.ts", "SETTLING"), ("settled.ts", "COMPLETED")):
                await session.execute(
                    text(
                        "INSERT INTO recordings (rel_path, filename, size_bytes, mtime_ns, "
                        "fingerprint, state, error_count, metadata_state, telemetry_state, "
                        "detection_state, plate_state, has_gps, ignored, protected, "
                        "file_missing, time_from_osd, has_audio, vehicle_count, plate_count, "
                        "telemetry_point_count, gps_point_count, gps_gap_count, "
                        "gps_longest_gap_s, gps_recovered_count, gps_no_fix_count, "
                        "gps_ocr_gap_count, gps_rejected_count, telemetry_problem_count, "
                        "first_seen_at) "
                        "VALUES (:p, :p, 100, 123456789, 'abc', :s, 0, 'PENDING', 'PENDING', "
                        "'PENDING', 'PENDING', 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0.0, 0, 0, "
                        "0, 0, 0, :t)"
                    ),
                    {"p": name, "s": state, "t": datetime.now(UTC)},
                )
            await session.commit()

        await dispose_engine()
        await asyncio.to_thread(command.upgrade, config, "head")

        async with session_scope() as session:
            rows = dict(
                (
                    await session.execute(
                        text(
                            "SELECT filename, fingerprint_mtime_ns FROM recordings "
                            "WHERE filename IN ('stranded.ts', 'settled.ts')"
                        )
                    )
                ).all()
            )

        assert rows["settled.ts"] == 123456789, (
            "an ordinary fingerprinted row was not stamped, so the whole library will be "
            "re-fingerprinted on the next scan"
        )
        assert rows["stranded.ts"] is None, (
            "a settling row was stamped with a stat it never fingerprinted, so it stays "
            "stranded -- which is the bug this migration exists to fix"
        )

    async def test_the_timezone_is_seeded_from_the_environment_once(self, migrated, monkeypatch):
        """`TZ` decides how every filename timestamp is read, so it must be pinned.

        Left as a schema *default* it would be recomputed on every process start, and the
        day somebody edited the compose file the meaning of an already-analysed library
        would change underneath it — every journey boundary and date filter, with no
        migration and no warning. Seeding the row once makes the README's own description
        true: `TZ` supplies it on first boot, and the setting owns it afterwards.
        """
        import importlib
        from datetime import UTC, datetime

        from sqlalchemy import delete

        import app.core.settings_schema as schema
        from app.db.models import AppSetting, Recording
        from app.db.seed import seed_timezone

        key = "general.timezone"
        async with session_scope() as session:
            await session.execute(delete(AppSetting).where(AppSetting.key == key))

        monkeypatch.setenv("TZ", "Europe/Berlin")
        importlib.reload(schema)
        try:
            async with session_scope() as session:
                assert await seed_timezone(session) is True

            async with session_scope() as session:
                stored = (
                    await session.execute(select(AppSetting.value).where(AppSetting.key == key))
                ).scalar_one()
            assert stored == "Europe/Berlin"

            # And a library that has already been indexed keeps the zone it was read
            # with, whatever the environment now says. `TZ` did nothing before this
            # existed, so every recording in such a database was interpreted through the
            # old hardcoded default; writing the environment's answer over that would
            # shift every timestamp in the library on an upgrade nobody asked for.
            async with session_scope() as session:
                await session.execute(delete(AppSetting).where(AppSetting.key == key))
                session.add(
                    Recording(
                        rel_path="already-indexed.ts",
                        filename="already-indexed.ts",
                        first_seen_at=datetime.now(UTC),
                    )
                )

            monkeypatch.setenv("TZ", "America/New_York")
            importlib.reload(schema)
            async with session_scope() as session:
                assert await seed_timezone(session) is True

            async with session_scope() as session:
                stored = (
                    await session.execute(select(AppSetting.value).where(AppSetting.key == key))
                ).scalar_one()
            assert stored == schema.HISTORICAL_TIMEZONE, (
                "an already-indexed library was retroactively reinterpreted"
            )

            # And once a row exists, a later TZ change never reaches it.
            monkeypatch.setenv("TZ", "Asia/Tokyo")
            importlib.reload(schema)
            async with session_scope() as session:
                assert await seed_timezone(session) is False

            async with session_scope() as session:
                stored = (
                    await session.execute(select(AppSetting.value).where(AppSetting.key == key))
                ).scalar_one()
            assert stored == schema.HISTORICAL_TIMEZONE
        finally:
            monkeypatch.delenv("TZ", raising=False)
            importlib.reload(schema)

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


class TestTheProblemRecount:
    """0012, against the real chain: a clean recording must stop calling itself degraded.

    The extractor used to record a "problem" on every sample assembled from more than one
    candidate frame, which is every cleanly parsed sample. `quality_rollup` counted it, so
    `telemetry_problem_count` equalled the point count on healthy recordings and the
    Telemetry Health page put an entire library into "degraded" -- 1,976 of 2,057 on the
    library this was found on, every one of them holding a fix on every sample.

    The extractor no longer writes it, which does nothing for rows already stored. This
    migration counts them again from the per-sample strings still in `quality_json`, so no
    video is read. Asserted here because a recount that matches nothing passes as quietly
    as one that works.
    """

    async def test_the_noise_is_recounted_and_real_faults_are_kept(self, migrated):
        import asyncio
        from datetime import UTC, datetime

        from alembic import command
        from sqlalchemy import text

        from app.db.session import alembic_config

        noise = "selected best fields from 2 candidate frames"
        # (filename, per-sample quality, stale stored count, what the recount must produce)
        cases = [
            ("clean.ts", [{"problems": [noise]}] * 4, 4, 0),
            (
                "mixed.ts",
                [{"problems": [noise]}, {"problems": [noise, "overlay unreadable"]}],
                2,
                1,
            ),
            ("faulty.ts", [{"problems": ["overlay unreadable"]}] * 3, 3, 3),
            ("ocr.ts", [{"problems": [], "ocr_status": "failed"}], 1, 1),
            ("untouched.ts", [], 0, 0),
        ]

        await dispose_engine()
        config = alembic_config()
        await asyncio.to_thread(command.downgrade, config, "0011")

        async with session_scope() as session:
            for name, samples, stale, _ in cases:
                await session.execute(
                    text(
                        "INSERT INTO recordings (rel_path, filename, size_bytes, mtime_ns, "
                        "state, error_count, metadata_state, telemetry_state, "
                        "detection_state, plate_state, has_gps, ignored, protected, "
                        "file_missing, time_from_osd, has_audio, vehicle_count, plate_count, "
                        "telemetry_point_count, gps_point_count, gps_gap_count, "
                        "gps_longest_gap_s, gps_recovered_count, gps_no_fix_count, "
                        "gps_ocr_gap_count, gps_rejected_count, telemetry_problem_count, "
                        "first_seen_at) "
                        "VALUES (:p, :p, 100, 1, 'COMPLETED', 0, 'DONE', 'DONE', "
                        "'DONE', 'DONE', 1, 0, 0, 0, 0, 0, 0, 0, :n, :n, 0, 0.0, 0, 0, "
                        "0, 0, :stale, :t)"
                    ),
                    {"p": name, "n": len(samples), "stale": stale, "t": datetime.now(UTC)},
                )
                rid = (
                    await session.execute(
                        text("SELECT id FROM recordings WHERE rel_path = :p"), {"p": name}
                    )
                ).scalar_one()
                for offset, quality in enumerate(samples):
                    await session.execute(
                        text(
                            "INSERT INTO telemetry_points (recording_id, t_offset_s, has_fix, "
                            "quality_json) VALUES (:rid, :o, 1, :q)"
                        ),
                        {"rid": rid, "o": float(offset), "q": json.dumps(quality)},
                    )
            await session.commit()

        await dispose_engine()
        await asyncio.to_thread(command.upgrade, config, "head")

        async with session_scope() as session:
            counted = dict(
                (
                    await session.execute(
                        text("SELECT rel_path, telemetry_problem_count FROM recordings")
                    )
                ).all()
            )

        for name, _samples, stale, expected in cases:
            assert counted[name] == expected, (
                f"{name}: stored {stale}, recount produced {counted[name]}, expected {expected}"
            )

    async def test_a_recount_agrees_with_a_rollup_over_the_same_rows(self, migrated):
        """The migration and `quality_rollup` must not drift; they answer one question."""
        from app.pipeline.telemetry_quality import quality_rollup

        noise = "selected best fields from 2 candidate frames"
        rows = [
            {"t_offset_s": 0.0, "has_fix": True, "quality_json": {"problems": [noise]}},
            {"t_offset_s": 1.0, "has_fix": True, "quality_json": {"problems": [noise, "bad"]}},
            {"t_offset_s": 2.0, "has_fix": True, "quality_json": {"ocr_status": "failed"}},
        ]
        _gaps, _longest, problems, *_rest = quality_rollup(rows)
        assert problems == 2, "the rollup still counts the candidate-count noise"

    async def test_the_recount_survives_the_json_a_real_library_actually_holds(self, migrated):
        """A migration that raises leaves the container unable to start.

        `json_extract` does not return NULL for a column that is not JSON -- it raises
        "malformed JSON" -- and `json_each` raises the same way over a scalar. A NULL
        column is fine; an empty string is not, and the difference is invisible until a
        real library hits it. Every shape here was reproduced against SQLite first.
        """
        import asyncio
        from datetime import UTC, datetime

        from alembic import command
        from sqlalchemy import text

        from app.db.session import alembic_config

        noise = "selected best fields from 2 candidate frames"
        # (filename, raw quality_json text per sample, expected recount)
        cases = [
            ("null.ts", [None], 0),
            ("empty.ts", [""], 0),
            ("garbage.ts", ["not json at all"], 0),
            ("nokey.ts", ["{}"], 0),
            ("nullkey.ts", ['{"problems": null}'], 0),
            ("scalar.ts", ['{"problems": "not a list"}'], 0),
            ("noise.ts", [json.dumps({"problems": [noise]})], 0),
            ("real.ts", [json.dumps({"problems": ["overlay unreadable"]})], 1),
            ("ocr.ts", ['{"problems": [], "ocr_status": "failed"}'], 1),
            ("mixed.ts", [json.dumps({"problems": [noise, "overlay unreadable"]}), None], 1),
        ]

        await dispose_engine()
        config = alembic_config()
        await asyncio.to_thread(command.downgrade, config, "0011")

        async with session_scope() as session:
            for name, samples, _ in cases:
                await session.execute(
                    text(
                        "INSERT INTO recordings (rel_path, filename, size_bytes, mtime_ns, "
                        "state, error_count, metadata_state, telemetry_state, "
                        "detection_state, plate_state, has_gps, ignored, protected, "
                        "file_missing, time_from_osd, has_audio, vehicle_count, plate_count, "
                        "telemetry_point_count, gps_point_count, gps_gap_count, "
                        "gps_longest_gap_s, gps_recovered_count, gps_no_fix_count, "
                        "gps_ocr_gap_count, gps_rejected_count, telemetry_problem_count, "
                        "first_seen_at) "
                        "VALUES (:p, :p, 100, 1, 'COMPLETED', 0, 'DONE', 'DONE', "
                        "'DONE', 'DONE', 1, 0, 0, 0, 0, 0, 0, 0, :n, :n, 0, 0.0, 0, 0, "
                        "0, 0, :n, :t)"
                    ),
                    {"p": name, "n": len(samples), "t": datetime.now(UTC)},
                )
                rid = (
                    await session.execute(
                        text("SELECT id FROM recordings WHERE rel_path = :p"), {"p": name}
                    )
                ).scalar_one()
                for offset, raw in enumerate(samples):
                    await session.execute(
                        text(
                            "INSERT INTO telemetry_points (recording_id, t_offset_s, has_fix, "
                            "quality_json) VALUES (:rid, :o, 1, :q)"
                        ),
                        {"rid": rid, "o": float(offset), "q": raw},
                    )
            await session.commit()

        await dispose_engine()
        # The assertion is partly that this does not raise at all.
        await asyncio.to_thread(command.upgrade, config, "head")

        async with session_scope() as session:
            counted = dict(
                (
                    await session.execute(
                        text("SELECT rel_path, telemetry_problem_count FROM recordings")
                    )
                ).all()
            )

        for name, _samples, expected in cases:
            assert counted[name] == expected, f"{name}: got {counted[name]}, expected {expected}"


class TestTheJourneyRollupRecount:
    """0013, against the real chain.

    Two derived values on `journeys` were measured against rows their own page excludes:
    a distance walked straight through the breaks the route layer declines to draw, and a
    member count taken without the `ignored` test `get_journey` has always applied. Both
    are re-derived from stored rows; neither needs a frame decoded.
    """

    async def test_a_hidden_member_stops_being_counted(self, migrated):
        import asyncio
        from datetime import UTC, datetime, timedelta

        from alembic import command
        from sqlalchemy import text

        from app.db.session import alembic_config

        await dispose_engine()
        config = alembic_config()
        await asyncio.to_thread(command.downgrade, config, "0012")

        base = datetime(2026, 8, 4, 17, 43, tzinfo=UTC)
        async with session_scope() as session:
            await session.execute(
                text(
                    "INSERT INTO journeys (started_at, ended_at, duration_s, recording_count, "
                    "has_gps, distance_m, manual, vehicle_count, unique_plate_count, created_at, updated_at) "
                    "VALUES (:s, :e, 120.0, 3, 1, 5000.0, 0, 0, 0, :s, :s)"
                ),
                {"s": base, "e": base + timedelta(minutes=2)},
            )
            jid = (await session.execute(text("SELECT id FROM journeys"))).scalar_one()
            # one visible, one hidden, one invalidated — the count must end up 1
            for name, ignored, rev in (
                ("visible.ts", 0, None),
                ("hidden.ts", 1, None),
                ("reanalysing.ts", 0, "invalidated"),
            ):
                await session.execute(
                    text(
                        "INSERT INTO recordings (rel_path, filename, size_bytes, mtime_ns, "
                        "state, error_count, metadata_state, telemetry_state, detection_state, "
                        "plate_state, telemetry_revision, has_gps, ignored, protected, "
                        "file_missing, time_from_osd, has_audio, vehicle_count, plate_count, "
                        "telemetry_point_count, gps_point_count, gps_gap_count, "
                        "gps_longest_gap_s, gps_recovered_count, gps_no_fix_count, "
                        "gps_ocr_gap_count, gps_rejected_count, telemetry_problem_count, "
                        "journey_id, first_seen_at) "
                        "VALUES (:p, :p, 1, 1, 'COMPLETED', 0, 'DONE', 'DONE', 'DONE', 'DONE', "
                        ":rev, 1, :ig, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0.0, 0, 0, 0, 0, 0, :j, :t)"
                    ),
                    {"p": name, "ig": ignored, "rev": rev, "j": jid, "t": base},
                )
            await session.commit()

        await dispose_engine()
        await asyncio.to_thread(command.upgrade, config, "head")

        async with session_scope() as session:
            count = (
                await session.execute(text("SELECT recording_count FROM journeys"))
            ).scalar_one()
        assert count == 1, f"hidden and reanalysing members were counted; got {count}"

    async def test_a_journey_holding_a_break_is_handed_back_for_remeasurement(self, migrated):
        """Cleared, not recomputed here: the breaks-aware walk is Python.

        `repair_stale` already looks for exactly this shape — `has_gps` with no distance is
        a state no correctly refreshed journey can be in — so clearing it is what routes the
        journey back through `refresh` without duplicating the walk in SQL.
        """
        import asyncio
        from datetime import UTC, datetime, timedelta

        from alembic import command
        from sqlalchemy import text

        from app.db.session import alembic_config

        await dispose_engine()
        config = alembic_config()
        await asyncio.to_thread(command.downgrade, config, "0012")

        base = datetime(2026, 8, 4, 17, 43, tzinfo=UTC)
        async with session_scope() as session:
            for breaks in (1, 0):
                await session.execute(
                    text(
                        "INSERT INTO journeys (started_at, ended_at, duration_s, "
                        "recording_count, has_gps, distance_m, manual, vehicle_count, "
                        "unique_plate_count, created_at, updated_at) "
                        "VALUES (:s, :e, 120.0, 1, 1, 5000.0, 0, 0, 0, :s, :s)"
                    ),
                    {"s": base, "e": base + timedelta(minutes=2)},
                )
                jid = (await session.execute(text("SELECT MAX(id) FROM journeys"))).scalar_one()
                await session.execute(
                    text(
                        "INSERT INTO recordings (rel_path, filename, size_bytes, mtime_ns, "
                        "state, error_count, metadata_state, telemetry_state, detection_state, "
                        "plate_state, has_gps, ignored, protected, file_missing, time_from_osd, "
                        "has_audio, vehicle_count, plate_count, telemetry_point_count, "
                        "gps_point_count, gps_gap_count, gps_longest_gap_s, gps_recovered_count, "
                        "gps_no_fix_count, gps_ocr_gap_count, gps_rejected_count, "
                        "telemetry_problem_count, journey_id, first_seen_at) "
                        "VALUES (:p, :p, 1, 1, 'COMPLETED', 0, 'DONE', 'DONE', 'DONE', 'DONE', "
                        "1, 0, 0, 0, 0, 0, 0, 0, 1, 1, 0, 0.0, 0, 0, 0, 0, 0, :j, :t)"
                    ),
                    {"p": f"j{jid}.ts", "j": jid, "t": base},
                )
                rid = (
                    await session.execute(
                        text("SELECT id FROM recordings WHERE rel_path = :p"),
                        {"p": f"j{jid}.ts"},
                    )
                ).scalar_one()
                await session.execute(
                    text(
                        "INSERT INTO telemetry_points (recording_id, t_offset_s, has_fix, "
                        "lat, lon, breaks_segment) "
                        "VALUES (:r, 0.0, 1, -34.8, 138.6, :b)"
                    ),
                    {"r": rid, "b": breaks},
                )
            await session.commit()

        await dispose_engine()
        await asyncio.to_thread(command.upgrade, config, "head")

        async with session_scope() as session:
            rows = (
                await session.execute(text("SELECT id, distance_m FROM journeys ORDER BY id"))
            ).all()
        with_break, without = rows
        assert with_break[1] is None, "the journey holding a break kept its stale distance"
        assert without[1] == 5000.0, "a journey with no break was needlessly cleared"
