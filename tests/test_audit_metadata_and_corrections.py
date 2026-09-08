"""API regression coverage for the September footage audit."""

import pytest

from app.db.models import Plate, Recording, RecordingState, StageState
from app.db.session import session_scope


async def test_dashboard_does_not_create_a_writability_probe(client, monkeypatch):
    from app.api.routes import system

    async def forbidden_probe(*args, **kwargs):
        pytest.fail("A status GET must not run write-based retention safety checks")

    monkeypatch.setattr(system, "evaluate_safety", forbidden_probe)
    response = await client.get("/api/status")
    assert response.status_code == 200
    assert response.json()["storage"]["footage_writable"] is None


async def test_runtime_info_identifies_source_revision(client, monkeypatch):
    from app.config import get_config

    revision = "a" * 40
    monkeypatch.setattr(get_config(), "source_revision", revision)
    response = await client.get("/api/system/info")
    assert response.status_code == 200
    assert response.json()["source_revision"] == revision


def test_stage_timings_are_not_deduplicated_across_stages():
    from app.core.logging import DatabaseLogSink

    sink = DatabaseLogSink(session_factory=lambda: None)
    for stage, elapsed in (("metadata", 28.3), ("telemetry", 4.1), ("detection", 8.0)):
        sink.emit_event(
            {
                "logger": "app.pipeline.orchestrator",
                "level": "info",
                "event": "processing stage complete",
                "recording_id": 1,
                "job_id": 2,
                "stage": stage,
                "elapsed_s": elapsed,
            }
        )
    assert [(item.context["stage"], item.context["elapsed_s"]) for item in sink._queue] == [
        ("metadata", 28.3),
        ("telemetry", 4.1),
        ("detection", 8.0),
    ]
    # Real repeat noise still uses the same bounded suppression path.
    sink.emit_event(
        {
            "logger": "app.pipeline.orchestrator",
            "level": "info",
            "event": "processing stage complete",
            "recording_id": 1,
            "job_id": 2,
            "stage": "detection",
            "elapsed_s": 8.0,
        }
    )
    assert len(sink._queue) == 3


@pytest.mark.parametrize("verdict", ["rejected", "no_fix", "interpolated"])
def test_api_quality_uses_final_verdict_without_losing_raw_provenance(verdict):
    from types import SimpleNamespace

    from app.api.visibility import telemetry_quality_view

    original = {"gps_status": "valid", "source": "overlay_ocr"}
    row = SimpleNamespace(
        quality_json=original,
        gps_quality=verdict,
        gps_reason="journey_validation",
        breaks_segment=True,
    )
    result = telemetry_quality_view(row)
    assert result["gps_status"] == verdict
    assert result["gps_quality"] == verdict
    assert result["observed_gps_status"] == "valid"
    assert result["gps_reason"] == "journey_validation"
    assert original == {"gps_status": "valid", "source": "overlay_ocr"}


async def test_stored_media_properties_reach_list_detail_and_export(client):
    properties = {
        "container": "mpegts",
        "video_profile": "High",
        "fps_container": None,
        "bitrate": 8_000_000,
        "pix_fmt": "yuv420p",
        "audio_codec": "aac",
        "audio_sample_rate": 48000,
        "audio_channels": 2,
    }
    async with session_scope() as session:
        row = Recording(
            rel_path="audit.ts",
            filename="audit.ts",
            state=RecordingState.COMPLETED,
            metadata_state=StageState.DONE,
            fps=30.0,
            has_audio=True,
            probe_json={"private_internal_marker": "must not be exposed"},
            **properties,
        )
        session.add(row)
        await session.flush()
        recording_id = row.id
    for path in (
        "/api/recordings",
        f"/api/recordings/{recording_id}",
        f"/api/recordings/{recording_id}/export.json",
    ):
        response = await client.get(path)
        assert response.status_code == 200
        payload = response.json()
        data = payload["items"][0] if "items" in payload else payload.get("recording", payload)
        assert {key: data[key] for key in properties} == properties
        assert data["fps"] == 30.0
        assert "probe_json" not in data
        assert "private_internal_marker" not in response.text


async def test_unprobed_metadata_stays_unknown(client):
    async with session_scope() as session:
        row = Recording(rel_path="unknown.ts", filename="unknown.ts")
        session.add(row)
        await session.flush()
        recording_id = row.id
    response = await client.get(f"/api/recordings/{recording_id}")
    assert response.status_code == 200
    for field in (
        "container",
        "video_profile",
        "fps_container",
        "bitrate",
        "pix_fmt",
        "audio_codec",
        "audio_sample_rate",
        "audio_channels",
    ):
        assert response.json()[field] is None


@pytest.mark.parametrize("action", ["correct", "merge"])
@pytest.mark.parametrize("source_dismissed,target_dismissed", [(True, False), (False, True)])
async def test_merging_plate_identity_preserves_user_dismissal(
    client,
    action,
    source_dismissed,
    target_dismissed,
):
    async with session_scope() as session:
        source = Plate(
            normalised_text="S123ABC", display_text="S123ABC", dismissed=source_dismissed
        )
        target = Plate(
            normalised_text="S456DEF", display_text="S456DEF", dismissed=target_dismissed
        )
        session.add_all([source, target])
        await session.flush()
        source_id, target_id = source.id, target.id
    body = {"text": "S456DEF"} if action == "correct" else {"target_plate_id": target_id}
    response = await client.post(f"/api/plates/{source_id}/{action}", json=body)
    assert response.status_code == 200
    assert response.json()["dismissed"] is True
    async with session_scope() as session:
        assert await session.get(Plate, source_id) is None
        assert (await session.get(Plate, target_id)).dismissed is True
