"""Recovery works for large libraries without buffering or cross-request files."""

import asyncio
import hashlib
import sqlite3
import subprocess
import threading

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.db import backup


@pytest.fixture
def snapshot(db_session):
    return backup.create_backup()


async def chunks(path, block=65536):
    with path.open("rb") as source:
        while data := source.read(block):
            yield data


async def test_valid_chunked_restore_is_atomic_and_reports_policy(client, snapshot, app_config):
    response = await client.post("/api/system/database/restore", content=chunks(snapshot))
    assert response.status_code == 200, response.text
    assert response.json()["size_bytes"] == snapshot.stat().st_size
    assert response.json()["migration_revision"] == "0024"
    pending = backup.backup_dir() / "restore.pending.db"
    assert pending.read_bytes() == snapshot.read_bytes()
    assert not list(backup.backup_dir().glob("restore-upload-*"))
    info = (await client.get("/api/system/database")).json()
    assert info["restore_max_bytes"] == 16 * 1024**3
    assert app_config.db_path.is_file()


@pytest.mark.parametrize("declared", [False, True])
async def test_oversized_restore_keeps_previous_pending(client, snapshot, app_config, declared):
    pending = backup.backup_dir() / "restore.pending.db"
    pending.write_bytes(b"previous validated publication")
    app_config.restore_max_bytes = 1024
    headers = {"Content-Length": str(snapshot.stat().st_size)} if declared else {}
    response = await client.post(
        "/api/system/database/restore", content=chunks(snapshot), headers=headers
    )
    assert response.status_code == 413
    assert pending.read_bytes() == b"previous validated publication"
    assert not list(backup.backup_dir().glob("restore-upload-*"))


@pytest.mark.parametrize(
    "corruption",
    [
        "garbage",
        "unknown_revision",
        "missing_column",
        "missing_unique",
        "missing_index",
        "extra_unique",
        "foreign_key",
    ],
)
async def test_invalid_restore_never_replaces_previous(client, snapshot, corruption):
    pending = backup.backup_dir() / "restore.pending.db"
    pending.write_bytes(b"previous publication")
    if corruption == "garbage":
        snapshot.write_bytes(b"not a database")
    else:
        with sqlite3.connect(snapshot) as connection:
            if corruption == "unknown_revision":
                connection.execute("UPDATE alembic_version SET version_num='future-9999'")
            elif corruption == "missing_column":
                connection.execute("ALTER TABLE recordings DROP COLUMN event_notes")
            elif corruption == "missing_unique":
                connection.execute("DROP INDEX ix_recordings_rel_path")
            elif corruption == "missing_index":
                connection.execute("DROP INDEX ix_recordings_started_at")
            elif corruption == "extra_unique":
                connection.execute("CREATE UNIQUE INDEX unexpected_unique ON recordings(filename)")
            elif corruption == "foreign_key":
                sql = connection.execute(
                    "SELECT sql FROM sqlite_master WHERE name='recordings'"
                ).fetchone()[0]
                assert "REFERENCES cameras (id)" in sql
                connection.execute("PRAGMA writable_schema=ON")
                connection.execute(
                    "UPDATE sqlite_master SET sql=? WHERE name='recordings'",
                    (sql.replace("REFERENCES cameras (id)", "REFERENCES plates (id)"),),
                )
        connection.close()
    response = await client.post("/api/system/database/restore", content=chunks(snapshot))
    assert response.status_code == 422, response.text
    assert pending.read_bytes() == b"previous publication"
    assert not list(backup.backup_dir().glob("restore-upload-*"))


async def test_concurrent_uploads_publish_complete_independent_databases(
    client, snapshot, tmp_path
):
    second = tmp_path / "second.db"
    second.write_bytes(snapshot.read_bytes())
    for path, marker in ((snapshot, "first"), (second, "second")):
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE audit_marker(value TEXT)")
            connection.execute("INSERT INTO audit_marker VALUES (?)", (marker,))
        connection.close()
    expected = {hashlib.sha256(path.read_bytes()).digest() for path in (snapshot, second)}
    reached = 0
    ready = asyncio.Event()

    async def interleaved(path):
        nonlocal reached
        async for data in chunks(path, 8192):
            yield data
            if reached < 2:
                reached += 1
                if reached == 2:
                    ready.set()
                await ready.wait()

    responses = await asyncio.gather(
        *[
            client.post("/api/system/database/restore", content=interleaved(path))
            for path in (snapshot, second)
        ]
    )
    assert [response.status_code for response in responses] == [200, 200]
    pending = backup.backup_dir() / "restore.pending.db"
    assert hashlib.sha256(pending.read_bytes()).digest() in expected
    assert not list(backup.backup_dir().glob("restore-upload-*"))


async def test_disconnect_removes_partial_upload(db_session):
    from app.api.routes.system import upload_database_restore

    messages = iter(
        [
            {"type": "http.request", "body": b"partial sqlite", "more_body": True},
            {"type": "http.disconnect"},
        ]
    )

    async def receive():
        return next(messages)

    request = Request({"type": "http", "method": "POST", "headers": []}, receive)
    with pytest.raises(HTTPException) as error:
        await upload_database_restore(request)
    assert error.value.status_code == 400
    assert not list(backup.backup_dir().glob("restore*"))


@pytest.mark.parametrize("operation", ["__init__", "validate", "publish"])
async def test_cancellation_waits_for_io_and_cleans_upload(
    client, snapshot, monkeypatch, operation
):
    entered, release = threading.Event(), threading.Event()
    original = getattr(backup.RestoreUpload, operation)

    def delayed(self, *args):
        if operation == "__init__":
            result = original(self, *args)
        entered.set()
        assert release.wait(10)
        if operation != "__init__":
            result = original(self, *args)
        return result

    monkeypatch.setattr(backup.RestoreUpload, operation, delayed)
    task = asyncio.create_task(
        client.post("/api/system/database/restore", content=chunks(snapshot))
    )
    assert await asyncio.to_thread(entered.wait, 10)
    task.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not list(backup.backup_dir().glob("restore-upload-*"))
    assert not (backup.backup_dir() / "restore.pending.db").exists()


async def test_low_disk_space_returns_507_and_keeps_prior_restore(client, snapshot, monkeypatch):
    pending = backup.backup_dir() / "restore.pending.db"
    pending.write_bytes(b"previous publication")
    usage = backup.shutil.disk_usage(backup.backup_dir())
    monkeypatch.setattr(backup.shutil, "disk_usage", lambda _: usage._replace(free=1))
    response = await client.post("/api/system/database/restore", content=chunks(snapshot))
    assert response.status_code == 507
    assert pending.read_bytes() == b"previous publication"
    assert not list(backup.backup_dir().glob("restore-upload-*"))


async def test_backup_download_low_disk_returns_507_without_partial_files(
    client,
    db_session,
    monkeypatch,
):
    before = set(backup.backup_dir().iterdir())
    usage = backup.shutil.disk_usage(backup.backup_dir())
    monkeypatch.setattr(backup.shutil, "disk_usage", lambda _: usage._replace(free=1))
    response = await client.get("/api/system/database/backup")
    assert response.status_code == 507
    assert (
        response.json()["detail"]
        == "The backup could not be stored; check free disk space and permissions"
    )
    assert set(backup.backup_dir().iterdir()) == before


async def test_truncated_upload_preserves_prior_restore(client, snapshot):
    pending = backup.backup_dir() / "restore.pending.db"
    pending.write_bytes(b"previous publication")
    response = await client.post(
        "/api/system/database/restore",
        content=chunks(snapshot),
        headers={"Content-Length": str(snapshot.stat().st_size + 1)},
    )
    assert response.status_code == 422
    assert pending.read_bytes() == b"previous publication"
    assert not list(backup.backup_dir().glob("restore-upload-*"))


@pytest.mark.parametrize("failure", ["validation", "publication", "disk"])
def test_failed_backup_leaves_no_partial_snapshot(db_session, monkeypatch, failure):
    before = set(backup.backup_dir().iterdir())
    if failure == "validation":

        def invalid(_path):
            raise ValueError("injected validation failure")

        monkeypatch.setattr(backup, "validate_database", invalid)
        expected = ValueError
    elif failure == "publication":

        def fail_replace(*_args):
            raise OSError("injected publication failure")

        monkeypatch.setattr(backup.os, "replace", fail_replace)
        expected = OSError
    else:
        usage = backup.shutil.disk_usage(backup.backup_dir())
        monkeypatch.setattr(backup.shutil, "disk_usage", lambda _: usage._replace(free=1))
        expected = backup.RestoreStorageError
    with pytest.raises(expected):
        backup.create_backup()
    assert set(backup.backup_dir().iterdir()) == before


async def test_failed_restore_replacement_preserves_current_and_pending(
    db_session,
    snapshot,
    app_config,
    monkeypatch,
):
    from app.db.session import dispose_engine

    backup.stage_restore(snapshot.read_bytes())
    await dispose_engine()
    original = backup.os.replace
    pending = backup.backup_dir() / "restore.pending.db"

    def fail_replace(source, target):
        if target == app_config.db_path:
            raise OSError("injected replacement failure")
        return original(source, target)

    monkeypatch.setattr(backup.os, "replace", fail_replace)
    with pytest.raises(OSError):
        backup.apply_pending_restore()
    assert pending.is_file()
    assert backup.validate_database(app_config.db_path, compatible=True) == "0024"
    assert len(list(backup.backup_dir().glob("pre-restore-*.db"))) == 1


async def test_schema_checker_failure_is_controlled_and_clean(client, snapshot, monkeypatch):
    backup._expected_schema.cache_clear()
    monkeypatch.setattr(
        backup.subprocess,
        "run",
        lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired("checker", 120)),
    )
    response = await client.post("/api/system/database/restore", content=chunks(snapshot))
    assert response.status_code == 503
    assert "checker" not in response.text
    assert not list(backup.backup_dir().glob("restore*"))


@pytest.mark.parametrize("revision", ["0001", "0013", "0022"])
async def test_supported_legacy_schema_can_restore_and_migrate(
    db_session, app_config, tmp_path, revision
):
    from alembic import command
    from sqlalchemy import create_engine

    from app.db.session import alembic_config, dispose_engine, upgrade_to_head

    path = tmp_path / "legacy.db"
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as connection:
        config = alembic_config()
        config.attributes["connection"] = connection
        command.upgrade(config, revision)
        connection.exec_driver_sql("CREATE TABLE audit_marker(value TEXT)")
        connection.exec_driver_sql("INSERT INTO audit_marker VALUES ('preserved')")
    engine.dispose()
    await asyncio.to_thread(backup.stage_restore, path.read_bytes())
    await dispose_engine()
    assert await asyncio.to_thread(backup.apply_pending_restore)
    await asyncio.to_thread(upgrade_to_head)
    with sqlite3.connect(app_config.db_path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("0024",)
        assert connection.execute("SELECT value FROM audit_marker").fetchone() == ("preserved",)


@pytest.mark.slow
async def test_restore_larger_than_512_mib_streams_without_request_body(
    client, snapshot, monkeypatch
):
    async def forbidden_body(_self):
        raise AssertionError("restore must not buffer the request body")

    monkeypatch.setattr(Request, "body", forbidden_body)
    target_size = 513 * 1024**2
    zeroes = bytes(1024**2)

    async def large_stream():
        async for data in chunks(snapshot):
            yield data
        remaining = target_size - snapshot.stat().st_size
        while remaining:
            take = min(remaining, len(zeroes))
            yield zeroes[:take]
            remaining -= take

    response = await client.post("/api/system/database/restore", content=large_stream())
    assert response.status_code == 200, response.text
    assert response.json()["size_bytes"] == target_size
    assert (backup.backup_dir() / "restore.pending.db").stat().st_size == target_size
    assert not list(backup.backup_dir().glob("restore-upload-*"))
