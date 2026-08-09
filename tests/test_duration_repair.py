"""A duration that a later fix would now reject must not survive in the database.

The 33-bit PTS wrap clamp was written for two specific files, found not to fire, and
fixed. Those two files then went on carrying 95,376 s and 95,377 s anyway -- 53 hours
between them -- because a probe runs once, when a recording is discovered, and nothing
ever asked them again. The dashboard reported 72.5 hours of footage for a library holding
about 19.5, months after the bug it came from was fixed.

Measured against the live library: 675 recordings with both a duration and a size span
2.86 to 43.0 Mbps. The three impossible rows sit at 0.0008, 0.0009 and 1,879 Mbps.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.db.models import Recording, RecordingState
from app.db.session import session_scope
from app.hardware import ffmpeg as ffmpeg_module
from app.pipeline.repair import repair_durations


@pytest.fixture
async def library(db_session):
    """One wrapped-PTS row, one absurdly-short row, and three that are perfectly fine."""
    rows = {
        # 9.6 MB claiming 26.5 hours: 802 bps.
        "wrapped.ts": (95376.184, 9_568_256),
        # 87.7 MB claiming 0.373 s: 1,879 Mbps.
        "truncated_header.ts": (0.373322, 87_687_168),
        # Genuine, and deliberately at the extremes of the real corpus.
        "busiest_real.ts": (10.680, 57_400_000),
        "quietest_real.ts": (97.035, 37_000_000),
        "short_but_real.ts": (1.099989, 1_048_576),
    }
    made = {}
    for name, (duration, size) in rows.items():
        recording = Recording(
            rel_path=name,
            filename=name,
            size_bytes=size,
            duration_s=duration,
            state=RecordingState.COMPLETED,
            started_at=datetime(2026, 8, 4, 8, 57, tzinfo=UTC),
            ended_at=datetime(2026, 8, 4, 8, 57, tzinfo=UTC) + timedelta(seconds=duration),
        )
        db_session.add(recording)
        made[name] = recording
    await db_session.flush()
    await db_session.commit()
    return {name: r.id for name, r in made.items()}


class TestWhatCountsAsImpossible:
    async def test_only_the_impossible_rows_are_selected(self, db_session, library):
        from app.pipeline.repair import implausible_duration

        flagged = {
            r.filename
            for r in (
                await db_session.execute(select(Recording).where(implausible_duration()))
            ).scalars()
        }
        assert flagged == {"wrapped.ts", "truncated_header.ts"}, (
            "the rule does not separate impossible durations from real ones; a genuine "
            f"recording would be re-probed on every scan. Flagged: {sorted(flagged)}"
        )

    async def test_a_row_with_no_duration_is_left_alone(self, db_session):
        """Unknown is not wrong, and dividing by it would raise."""
        from app.pipeline.repair import implausible_duration

        db_session.add(
            Recording(
                rel_path="unknown.ts",
                filename="unknown.ts",
                size_bytes=1024,
                duration_s=None,
                state=RecordingState.DISCOVERED,
            )
        )
        db_session.add(
            Recording(
                rel_path="empty.ts",
                filename="empty.ts",
                size_bytes=0,
                duration_s=None,
                state=RecordingState.DISCOVERED,
            )
        )
        await db_session.flush()
        flagged = {
            r.filename
            for r in (
                await db_session.execute(select(Recording).where(implausible_duration()))
            ).scalars()
        }
        assert not ({"unknown.ts", "empty.ts"} & flagged)


class TestRepair:
    async def test_a_wrapped_duration_is_corrected_and_the_total_stops_lying(
        self, library, monkeypatch
    ):
        """The whole point: 53 hours of footage that does not exist, removed."""

        async def corrected_probe(path, **kwargs):
            result = ffmpeg_module.ProbeResult(path=str(path))
            result.size_bytes = 9_568_256
            result.duration_s = 9.9  # what the clamp recovers by decoding
            result.bitrate = 7_732_000
            result.fps = 30.0
            result.warnings = ["container duration 95376s is a wrapped PTS"]
            result.pts_wrapped = True
            return result

        monkeypatch.setattr("app.pipeline.repair.probe", corrected_probe)
        monkeypatch.setattr(
            "app.pipeline.repair.resolve_footage_path", lambda rel, **k: f"/footage/{rel}"
        )

        async with session_scope() as session:
            repaired = await repair_durations(session)
        assert repaired >= 1

        async with session_scope() as session:
            row = await session.get(Recording, library["wrapped.ts"])
            assert row.duration_s == pytest.approx(9.9)
            # ended_at is derived from it, and claimed the clip finished 26 hours later.
            assert row.ended_at == row.started_at + timedelta(seconds=9.9)

    async def test_a_genuine_recording_is_never_re_probed(self, library, monkeypatch):
        """A repair that touches good rows is a repair that runs forever."""
        probed = []

        async def recording_probe(path, **kwargs):
            probed.append(str(path))
            result = ffmpeg_module.ProbeResult(path=str(path))
            result.size_bytes = 1
            result.duration_s = 1.0
            return result

        monkeypatch.setattr("app.pipeline.repair.probe", recording_probe)
        monkeypatch.setattr(
            "app.pipeline.repair.resolve_footage_path", lambda rel, **k: f"/footage/{rel}"
        )

        async with session_scope() as session:
            await repair_durations(session)

        for good in ("busiest_real.ts", "quietest_real.ts", "short_but_real.ts"):
            assert not any(good in p for p in probed), f"{good} was needlessly re-probed"

    async def test_a_duration_that_re_probing_cannot_fix_is_not_retried_silently(
        self, library, monkeypatch, capsys
    ):
        """Some files simply have no recoverable duration. Say so rather than loop."""

        async def unchanged_probe(path, **kwargs):
            """Echoes each file's own stored numbers, i.e. probing changes nothing."""
            known = {
                "wrapped.ts": (9.9, 9_568_256),
                "truncated_header.ts": (0.373322, 87_687_168),
            }
            name = str(path).rsplit("/", 1)[-1]
            duration, size = known[name]
            result = ffmpeg_module.ProbeResult(path=str(path))
            result.size_bytes = size
            result.duration_s = duration
            return result

        monkeypatch.setattr("app.pipeline.repair.probe", unchanged_probe)
        monkeypatch.setattr(
            "app.pipeline.repair.resolve_footage_path", lambda rel, **k: f"/footage/{rel}"
        )

        async with session_scope() as session:
            repaired = await repair_durations(session)

        # wrapped.ts is recoverable and is repaired; truncated_header.ts is not, and must
        # be left alone rather than have one impossible number swapped for another.
        assert repaired == 1
        out = capsys.readouterr().out
        assert "does not recover it" in out
        assert "truncated_header.ts" in out

        async with session_scope() as session:
            row = await session.get(Recording, library["truncated_header.ts"])
            assert row.duration_s == pytest.approx(0.373322), (
                "an impossible duration was replaced by another impossible duration, so "
                "the row will be flagged and 'repaired' again on every scan, forever"
            )
