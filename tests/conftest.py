"""Shared test fixtures.

Video fixtures are *generated*, not committed. ``backend/scripts/make_fixtures.py``
synthesises clips that match the real dashcam format — MPEG-TS, H.264 Baseline, 1920x1080,
front/rear split, and a 1 Hz burned-in telemetry overlay — so the tests exercise the real
code paths without publishing anyone's actual driving footage.

Generation needs ffmpeg. Tests that genuinely need video are skipped when it is absent;
everything else (settings, retention safety, journey grouping, migrations, the API) runs
regardless, so a developer without ffmpeg still gets a useful suite.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND = REPO_ROOT / "backend"
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"

if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


# --------------------------------------------------------------------------------------
# Media fixtures
# --------------------------------------------------------------------------------------


def _ffmpeg() -> str | None:
    return shutil.which("ffmpeg")


@pytest.fixture(scope="session")
def ffmpeg_path() -> str:
    path = _ffmpeg()
    if not path:
        pytest.skip("ffmpeg not available; skipping tests that need real video")
    return path


@pytest.fixture(scope="session")
def fixture_dir(ffmpeg_path: str) -> Path:
    """Directory of synthetic dashcam clips, generated once per session if missing.

    Regeneration is keyed on the expected filenames rather than a timestamp, so a normal
    run costs nothing after the first.
    """
    expected = {
        "20260804174353_camera_0.ts",  # front, GPS, with audio
        "20260804174354_camera_1.ts",  # rear, GPS, no audio, 1s filename offset
        "20260804174359_camera_0.ts",  # contiguous next segment (same journey)
        "20260805150358_camera_0.ts",  # no GPS fix: E:00.0000 N:00.0000
        "20260805201500_camera_0.ts",  # hours later (separate journey)
        "20260807214113_camera_0.ts",  # zero bytes
        "20260807214114_camera_0.ts",  # not a transport stream
        "20260807214200_camera_0.ts",  # truncated mid-GOP
    }
    missing = {n for n in expected if not (FIXTURE_DIR / n).exists()}
    if missing:
        subprocess.run(
            [
                sys.executable,
                str(BACKEND / "scripts" / "make_fixtures.py"),
                "--out",
                str(FIXTURE_DIR),
                "--ffmpeg",
                ffmpeg_path,
            ],
            check=True,
            capture_output=True,
        )
    return FIXTURE_DIR


@pytest.fixture
def front_clip(fixture_dir: Path) -> Path:
    """A front-camera segment with a moving GPS fix and rising speed."""
    return fixture_dir / "20260804174353_camera_0.ts"


@pytest.fixture
def rear_clip(fixture_dir: Path) -> Path:
    """The rear counterpart, whose filename is deliberately 1 s off the front."""
    return fixture_dir / "20260804174354_camera_1.ts"


@pytest.fixture
def nofix_clip(fixture_dir: Path) -> Path:
    """Parked, no satellite fix — the ``E:00.0000 N:00.0000`` case."""
    return fixture_dir / "20260805150358_camera_0.ts"


@pytest.fixture
def damaged_clips(fixture_dir: Path) -> dict[str, Path]:
    """The failure modes that genuinely occur in the real corpus."""
    return {
        "zero_byte": fixture_dir / "20260807214113_camera_0.ts",
        "garbage": fixture_dir / "20260807214114_camera_0.ts",
        "truncated": fixture_dir / "20260807214200_camera_0.ts",
    }


# --------------------------------------------------------------------------------------
# Application fixtures
# --------------------------------------------------------------------------------------


@pytest.fixture
def temp_dirs(tmp_path: Path) -> tuple[Path, Path]:
    """An isolated (data_dir, footage_dir) pair.

    Both are real directories on the test's own tmpfs so that retention's mount and
    writability probes have something genuine to inspect.
    """
    data = tmp_path / "data"
    footage = tmp_path / "dashcam"
    data.mkdir()
    footage.mkdir()
    return data, footage


@pytest.fixture
def app_config(temp_dirs: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch):
    """Point the app at the temporary directories and clear the config cache.

    ``get_config`` is ``lru_cache``d, so the cache must be cleared on the way in *and*
    on the way out; otherwise one test's temp path leaks into the next one's config.
    """
    from app.config import get_config

    data, footage = temp_dirs
    monkeypatch.setenv("DASHCAM_DATA_DIR", str(data))
    monkeypatch.setenv("DASHCAM_FOOTAGE_DIR", str(footage))
    monkeypatch.delenv("DASHCAM_DATABASE_URL", raising=False)
    get_config.cache_clear()

    cfg = get_config()
    cfg.ensure_dirs()
    yield cfg

    get_config.cache_clear()


@pytest.fixture
async def db_session(app_config) -> AsyncIterator:
    """A migrated, seeded database with the settings service live.

    The settings service is initialised here rather than in its own fixture because
    almost everything under test reads configuration through it, and a half-initialised
    service fails in a confusing way (`RuntimeError: settings service is not initialised`)
    far from the code that actually needed it.
    """
    from app.core.settings_service import (
        init_settings_service,
        set_settings_service,
        shutdown_settings_service,
    )
    from app.db.session import dispose_engine, get_session_factory, init_db, session_scope

    await init_db()
    await init_settings_service(get_session_factory())

    try:
        async with session_scope() as session:
            yield session
    finally:
        await shutdown_settings_service()
        # Reset the process-wide singleton so the next test does not inherit this
        # database's settings cache.
        set_settings_service(None)
        await dispose_engine()


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "needs_ffmpeg: requires a real ffmpeg binary")
    config.addinivalue_line("markers", "slow: decodes video and takes a few seconds")
    # Keep tests from ever reaching a real share, whatever the developer's environment says.
    os.environ.pop("DASHCAM_SMB_URL", None)

    # Set here rather than by overriding pytest-asyncio's `event_loop_policy` fixture,
    # which that plugin deprecates. Windows' default proactor loop does not host aiosqlite
    # cleanly; the selector policy matches how the Linux container behaves anyway.
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
