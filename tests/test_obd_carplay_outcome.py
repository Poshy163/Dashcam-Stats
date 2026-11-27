import hashlib
import os
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from test_carplay_peer_enrollment import CLASSIC, LOG, METRICS, NETWORK, NOW, OTHER, WIFI, iap, line
from test_obd_ignition_coverage import DRIVE, event, store_samples
from test_obd_server_import import BASE, _sample

from app.db.models import OBDCarPlayObservation, UnitLogEntry
from app.db.session import session_scope
from app.ingest import carplay_outcome as collector

URL = f"/api/obd/drives/{DRIVE}/carplay-outcome"


def unitline(at, tag, message):
    return UnitLogEntry(
        occurred_at=BASE + timedelta(seconds=at),
        pid=10,
        tid=10,
        level="E",
        tag=tag,
        message=message,
        line_hash=hashlib.sha256(f"{at}{tag}{message}".encode()).hexdigest(),
    )


def observation(at, kind, duration=None, source="camera"):
    return OBDCarPlayObservation(
        source_id_hash=source,
        occurred_at=BASE + timedelta(seconds=at),
        kind=kind,
        duration_ms=duration,
    )


async def test_real_wake_shape_uses_pre_obd_evidence_and_does_not_claim_first_display(
    db_session, client, tmp_path
):
    await store_samples(tmp_path, [_sample(DRIVE, i) for i in range(18)])
    async with session_scope() as s:
        s.add_all(
            [
                event(1, -24, "acc_on"),
                event(2, -21, "acc_on"),
                event(3, 0, "engine_detected", anchor=True),
                event(4, 75, "acc_off"),
                event(5, 80, "acc_on"),
                observation(-100, "early_retry"),
                observation(-19, "hotspot"),
                observation(-13.113, "lease"),
                observation(21.079, "iap"),
                observation(24.568, "phone_handoff"),
                observation(25.418, "video_output", 35_000),
                observation(77, "waiting_cycle"),
                observation(-15, "early_retry", source="other"),
            ]
        )
    result = (await client.get(URL)).json()
    assert result["status"] == "carplay_video"
    assert result["saved_phone_lease"]["after_ignition_s"] == 10.887
    assert result["phone_handshake"]["after_ignition_s"] == 45.079
    assert result["video_session"]["after_ignition_s"] == 49.418
    assert result["video_session"]["output_confirmed"] is True
    assert result["helper"] is None and result["hotspot"]["after_ignition_s"] == 5
    assert not any("frame" in key for key in result)


async def test_historical_partial_evidence_and_source_collision(db_session, client, tmp_path):
    await store_samples(tmp_path, [_sample(DRIVE, i) for i in range(10)])
    async with session_scope() as s:
        s.add_all(
            [
                event(1, -10, "acc_on"),
                event(2, 0, "engine_detected", anchor=True),
                unitline(-6, "zj", "updateStates:CP"),
                unitline(12, "zj", "获取到图片数据"),
                unitline(-2, "WifiStartupGuard", "zlink_phone_waiting_connect_requested"),
                unitline(
                    20,
                    "zj",
                    "[zjBtService]device:02:00:00:00:01:02   uuid:00000000-deca-fade-deca-deafdecacafe",
                ),
            ]
        )
    reply = await client.get(URL)
    result = reply.json()
    assert result["status"] == "unknown" and result["video_session"] is None
    assert result["helper"] is None
    assert "02:00" not in reply.text and "uuid" not in reply.text
    async with session_scope() as s:
        s.add(event(1, 12, "acc_on", source="other-camera"))
        s.add(observation(-3, "iap"))
        s.add(observation(-2, "waiting_connect"))
    result = (await client.get(URL)).json()
    assert result["status"] == "phone_handshake" and result["helper"]["action"] == "waiting_connect"
    assert result["ignition_at"] is not None
    assert (await client.get("/api/obd/drives/no_such_drive/carplay-outcome")).status_code == 404


async def test_sequence_gap_and_multiple_recording_anchors(db_session, client, tmp_path):
    await store_samples(tmp_path, [_sample(DRIVE, i) for i in range(10)])
    async with session_scope() as s:
        s.add_all(
            [
                event(1, -10, "acc_on"),
                event(4, 0, "engine_detected", anchor=True),
                observation(-3, "lease"),
            ]
        )
    result = (await client.get(URL)).json()
    assert result["ignition_at"] is None and result["saved_phone_lease"] is None
    async with session_scope() as s:
        s.add(event(5, 0, "engine_detected", source="alternate", anchor=True))
    assert (await client.get(URL)).json()["ignition_at"] is None


def test_actual_parked_shell_with_two_phones_redacts_before_transport(tmp_path):
    shell = shutil.which("sh") or (
        str(Path(os.environ["PROGRAMFILES"]) / "Git/bin/sh.exe") if os.name == "nt" else None
    )
    if not shell or not Path(shell).is_file():
        pytest.skip("POSIX shell unavailable")
    fixtures = {
        "network": NETWORK,
        "metrics": METRICS.replace(
            "lifetimeMs=", "latency.n=2400, android.media.mediacodec.lifetimeMs="
        )
        + METRICS.replace("com.zjinnova.zlink,", "other.app,"),
        "logs": LOG,
    }
    for name, data in fixtures.items():
        (tmp_path / name).write_text(data, encoding="utf-8")
    binding = tmp_path / "carplay_peer"
    binding.write_text(f"{WIFI} {CLASSIC}\n", newline="\n")
    # Functions are inherited by the shell interpreting the real collector asset.
    setup = f"""
settings() {{ echo 0; }}
getprop() {{ case "$1" in persist.sys.timezone) echo Australia/Adelaide;; *) echo {WIFI};; esac; }}
date() {{ echo {int(NOW.timestamp())}; }}
dumpsys() {{ case "$3" in network_stack) cat network;; media.metrics) cat metrics;; esac; }}
pidof() {{ echo 2537; }}
timeout() {{ shift; "$@"; }}
logcat() {{ cat logs; }}
"""

    def run(commands):
        return subprocess.run(
            [shell, "-s"],
            cwd=tmp_path,
            input=commands
            + collector._SCRIPT.replace("root=/data/local/tmp/dashcam_wifi_startup", "root=."),
            text=True,
            capture_output=True,
            check=True,
        ).stdout

    raw = run(setup)
    assert WIFI not in raw and "iPhone" not in raw and "uuid:" not in raw and "192.0" not in raw
    parsed = collector.parse_snapshot(raw, NOW)
    assert {row["kind"] for row in parsed} == {"lease", "video_output", "iap", "phone_handoff"}
    assert len(parsed) == 4
    decoder = next(row for row in parsed if row["kind"] == "video_output")
    assert decoder["occurred_at"] == datetime.fromisoformat("2026-11-27T10:53:12.829+10:30")
    assert decoder["duration_ms"] == 505029
    (tmp_path / "metrics").write_text(METRICS)
    assert any(r["kind"] == "decoder" for r in collector.parse_snapshot(run(setup), NOW))
    binding.write_text(f"{OTHER} {CLASSIC}\n", newline="\n")
    assert {r["kind"] for r in collector.parse_snapshot(run(setup), NOW)} == {"decoder"}
    binding.write_text(f"{WIFI} {CLASSIC}\n", newline="\n")
    (tmp_path / "logs").write_text(line(-4.339, iap(OTHER)))
    assert {r["kind"] for r in collector.parse_snapshot(run(setup), NOW)} == {
        "decoder",
        "lease",
        "other_iap",
    }
    assert not run(setup + "settings() { echo 1; }\n")


async def test_other_phone_and_decoder_spanning_next_ignition(db_session, client, tmp_path):
    await store_samples(tmp_path, [_sample(DRIVE, i) for i in range(12)])
    async with session_scope() as s:
        s.add_all(
            [
                event(1, -22, "acc_on"),
                event(2, 0, "engine_detected", anchor=True),
                event(3, 40, "acc_off"),
                event(4, 45, "acc_on"),
                observation(-10, "lease"),
                observation(-4, "iap"),
                observation(-3, "phone_handoff"),
                observation(-2, "other_iap"),
                observation(-1, "video_output", 30_000),
            ]
        )
    assert (await client.get(URL)).json()["status"] == "zlink_video"
    async with session_scope() as s:
        record = await s.scalar(
            select(OBDCarPlayObservation).where(OBDCarPlayObservation.kind == "video_output")
        )
        record.duration_ms = 50_000
    result = (await client.get(URL)).json()
    assert result["video_session"]["output_confirmed"] is False
    assert result["status"] == "decoder_observed"


def test_parser_rejects_truncation_clock_fold_unknown_zone_and_stale_rows():
    during_fold = datetime.fromisoformat("2026-04-05T11:00:00+09:30")
    header = f"clock {int(during_fold.timestamp())} Australia/Adelaide\n"
    assert (
        collector.parse_snapshot(header + "lease 2026-04-05T02:30:00.123\ndone", during_fold) == []
    )
    assert collector.parse_snapshot(header + "lease 2026-04-05T08:33:00.123", during_fold) is None
    assert collector.parse_snapshot(header + "done", NOW) is None
    assert (
        collector.parse_snapshot(header + "lease 2026-03-01T08:33:00.123\ndone", during_fold) == []
    )
    assert (
        collector.parse_snapshot(
            header.replace("Australia/Adelaide", "Invalid/Timezone")
            + "lease 2026-04-05T08:30:00.123\ndone",
            during_fold,
        )
        == []
    )


async def test_parked_read_once_and_idempotent_across_restart(db_session, monkeypatch):
    await collector.shutdown()
    now = datetime.now(UTC).replace(microsecond=0)

    async def read(address, command, timeout):
        nonlocal calls
        calls += 1
        return (
            f"clock {int(now.timestamp())} UTC\nhotspot {int(now.timestamp())}.000\n"
            "network_complete\nmetrics_complete\nlog_complete\ndone"
        )

    calls = 0
    monkeypatch.setattr(collector.adb, "shell", read)
    monkeypatch.setattr(
        collector, "get_settings_service", lambda: SimpleNamespace(get_nowait=lambda _: True)
    )
    snap = SimpleNamespace(source_id_hash="saved", events=[event(1, -20, "acc_on")])
    collector.on_event_snapshot("unit", snap)
    assert collector._task is None
    snap.events.append(event(2, 0, "acc_off"))
    for _ in range(2):
        collector.on_event_snapshot("unit", snap)
        await collector._task
    assert calls == 1
    await collector.shutdown()
    collector.on_event_snapshot("unit", snap)
    await collector._task
    async with session_scope() as s:
        assert await s.scalar(select(func.count()).select_from(OBDCarPlayObservation)) == 1
    assert calls == 2
    await collector.shutdown()


async def test_late_request_with_original_lease_needs_subsequent_saved_phone_handoff(
    db_session, client, tmp_path
):
    await store_samples(tmp_path, [_sample(DRIVE, i) for i in range(40)])
    async with session_scope() as s:
        s.add_all(
            [
                event(1, -5, "acc_on"),
                event(2, 0, "engine_detected", anchor=True),
                observation(5, "lease"),
                observation(101, "iap"),
                observation(104, "phone_handoff"),
                observation(105, "video_output", 20_000),
            ]
        )
    assert (await client.get(URL)).json()["status"] == "zlink_video"
    async with session_scope() as s:
        s.add(observation(95, "waiting_connect"))
    result = (await client.get(URL)).json()
    assert result["status"] == "carplay_video"
    assert result["helper"] == {
        "action": "waiting_connect",
        "at": result["helper"]["at"],
        "after_ignition_s": 100,
    }
    async with session_scope() as s:
        s.add(observation(60, "other_iap"))
    result = (await client.get(URL)).json()
    assert result["status"] == "zlink_video" and result["helper"]["action"] == "waiting_connect"


def test_carplay_schema_replays_without_data_loss_and_rejects_a_collision(app_config):
    import sqlite3
    from contextlib import closing

    from alembic import command

    from app.db.session import alembic_config

    cfg = alembic_config()
    command.upgrade(cfg, "0035")
    with closing(sqlite3.connect(app_config.db_path)) as conn, conn:
        conn.execute(
            "INSERT INTO obd_carplay_observations VALUES ('camera','2026-11-27','iap',null)"
        )
    command.stamp(cfg, "0034")
    command.upgrade(cfg, "0035")
    with closing(sqlite3.connect(app_config.db_path)) as conn:
        assert conn.execute("SELECT kind FROM obd_carplay_observations").fetchall() == [("iap",)]
    command.downgrade(cfg, "0034")
    command.stamp(cfg, "0035")
    command.downgrade(cfg, "0034")
    with closing(sqlite3.connect(app_config.db_path)) as conn, conn:
        conn.execute("CREATE TABLE obd_carplay_observations (unrelated INTEGER)")
    with pytest.raises(RuntimeError, match="incompatible schema"):
        command.upgrade(cfg, "0035")
