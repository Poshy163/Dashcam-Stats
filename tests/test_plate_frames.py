import numpy as np
import pytest
from sqlalchemy import select

from app.ai.plate_frames import VehicleView, sampling_rate, select_vehicle_views, temporal_vote
from app.ai.plates import PlateReading
from app.core.settings_service import get_settings_service
from app.db.models import Detection, Plate, PlateObservation, Recording, TrackedObject
from app.pipeline import stages


def reading(text, offset, confidence=0.98):
    return PlateReading(
        text,
        confidence,
        0.95,
        (0.2, 0.5, 0.4, 0.6),
        crop=np.ones((20, 80, 3), dtype=np.uint8),
        offset_s=offset,
    )


def vote(readings):
    return temporal_vote(readings, region="AU-SA", min_confidence=0.3, store_unmatched=False)


def test_large_clipped_side_view_does_not_displace_clear_front_views():
    views = [VehicleView(t, (0.3, 0.4, 0.6, 0.8), 0.9) for t in [0, 0.25, 1, 2]]
    views.append(VehicleView(31.25, (0, 0.3, 0.6, 0.95), 0.95))
    assert [v.offset_s for v in select_vehicle_views(views, 3)] == [0, 1, 2]


def test_saved_clock_and_invalid_views():
    assert sampling_rate([VehicleView(31.25, (0, 0, 1, 1), 1)]) == 4
    assert select_vehicle_views([VehicleView(float("nan"), (0, 0, 1, 1), 1)], 5) == []


def test_nearby_sparse_frame_cannot_displace_saved_comparison_view():
    baseline = VehicleView(31.25, (0, 0.3, 0.6, 0.95), 0.95)
    views = [VehicleView(t, (0.3, 0.4, 0.6, 0.8), 0.9) for t in [0, 1, 2, 31]] + [baseline]
    selected = select_vehicle_views(views, 4, baseline=baseline)
    assert [v.offset_s for v in selected] == [0, 1, 2, 31.25]


def test_independent_exact_reads_outvote_a_wrong_single_frame():
    result = vote([reading("S123ABC", t) for t in [0, 1, 2]] + [reading("0971MM", 31, 0.61)])
    assert result.text == "S123ABC"
    assert result.vote_count == 3
    assert result.supporting_offsets == [0, 1, 2]
    assert result.best.raw_text == result.text


def test_boxes_or_orientations_in_one_frame_cannot_count_as_confirmation():
    result = vote([reading("S123ABC", 0), reading("S123ABC", 0), reading("S123ABC", 0.1)])
    assert result.vote_count == 1
    assert result.supporting_offsets == [0]


def test_replacing_a_duplicate_does_not_compare_numpy_images_for_equality():
    result = vote([reading("S123ABC", 0), reading("S123ABC", 1), reading("S123ABC", 1, 0.99)])
    assert result.vote_count == 2
    assert result.supporting_offsets == [0, 1]
    assert result.best.ocr_confidence == 0.99


def test_generic_single_guess_is_rejected_and_distinct_frames_can_confirm_it():
    assert vote([reading("0971MM", 0, 0.999)]) is None
    assert vote([reading("0971MM", 0), reading("0971MM", 1)]).vote_count == 2


def test_conflicting_high_scores_abstain_without_synthesizing_characters():
    assert (
        vote([reading("S123ABC", 0), reading("S123AXC", 1, 0.999), reading("S123AXC", 2, 0.999)])
        is None
    )
    assert vote([reading("S123ABC", 0), reading("S456DEF", 1)]) is None


def test_normalisation_groups_reads_but_preserves_the_actual_preview_text():
    result = vote([reading("5123ABC", 0, 0.96), reading("S123ABC", 1, 0.99)])
    assert result.text == "S123ABC"
    assert result.vote_count == 2
    assert result.best.raw_text == "S123ABC"


async def setup_track(db_session):
    rec = Recording(rel_path="multi.ts", filename="multi.ts", size_bytes=100, width=100, height=100)
    db_session.add(rec)
    await db_session.flush()
    track = TrackedObject(
        recording_id=rec.id,
        track_key=1,
        class_label="car",
        frame_count=131,
        confidence_max=0.95,
        confidence_avg=0.9,
        first_seen_offset_s=0,
        last_seen_offset_s=32.5,
        duration_s=32.5,
        best_frame_offset_s=31.25,
        best_bbox=[0, 0.3, 0.6, 0.95],
        crop_path="vehicles/saved.jpg",
    )
    db_session.add(track)
    await db_session.flush()
    for t in [0, 1, 2]:
        db_session.add(
            Detection(
                recording_id=rec.id,
                tracked_object_id=track.id,
                class_label="car",
                t_offset_s=t,
                x=0.3,
                y=0.4,
                w=0.3,
                h=0.4,
                confidence=0.9,
            )
        )
    await db_session.commit()
    await get_settings_service().set("plates.max_ocr_per_track", 4)
    return rec, track


def fake_models(monkeypatch):
    class Detector:
        async def detect(self, image, **kwargs):
            return [((0.2, 0.5, 0.8, 0.7), 0.95)]

    class OCR:
        async def read(self, image):
            return ("S123ABC", 0.99) if image.mean() > 50 else ("0971MM", 0.61)

    async def models():
        return Detector(), OCR()

    monkeypatch.setattr(stages, "_shared_plate_models", models)
    monkeypatch.setattr(stages, "resolve_footage_path", lambda *a: "multi.ts")


async def test_source_frames_replace_bad_saved_view_with_matching_time_and_coordinates(
    db_session, monkeypatch
):
    rec, track = await setup_track(db_session)
    fake_models(monkeypatch)
    calls = []

    async def frames(path, **kwargs):
        calls.append(kwargs)
        for t in [0, 1, 2]:
            yield t, np.full((100, 100, 3), 100, dtype=np.uint8)
        yield 31.25, np.zeros((100, 100, 3), dtype=np.uint8)

    monkeypatch.setattr(stages, "iter_frames", frames)
    result = await stages.stage_plates(db_session, rec)
    assert result.ok
    assert len(calls) == 1
    assert calls[0]["preserve_final_frame"] is True
    assert result.stats["vehicle_frames_checked"] == 4
    obs = (await db_session.execute(select(PlateObservation))).scalar_one()
    assert obs.t_offset_s == 0
    assert obs.t_offset_s != track.best_frame_offset_s
    assert obs.vote_count == 3
    assert obs.normalised_text == "S123ABC"
    assert obs.bbox["box"] == pytest.approx([0.36, 0.6, 0.54, 0.68])
    assert obs.bbox["supporting_frame_offsets_s"] == [0, 1, 2]
    assert obs.bbox["confirmation"] == "multi_frame"
    assert obs.bbox["frames_checked"] == 4


async def test_source_decode_uses_frame_clock_and_closes_at_last_selected_view(
    db_session, monkeypatch
):
    rec, _ = await setup_track(db_session)
    fake_models(monkeypatch)
    closed = False

    async def frames(path, **kwargs):
        nonlocal closed
        try:
            # A container/output timestamp limit must not truncate a read whose
            # stored detection offsets were assigned by the sampled frame counter.
            if kwargs.get("duration") is not None:
                yield 0, np.full((100, 100, 3), 100, dtype=np.uint8)
                return
            for t in [0, 1, 2, 31.25]:
                yield t, np.full((100, 100, 3), 100, dtype=np.uint8)
            pytest.fail("plate revalidation decoded beyond its last selected view")
        finally:
            closed = True

    monkeypatch.setattr(stages, "iter_frames", frames)
    result = await stages.stage_plates(db_session, rec)
    assert result.ok
    assert result.stats["vehicle_frames_checked"] == 4
    assert closed


async def test_incomplete_decode_does_not_stamp_success_or_replace_observations(
    db_session, monkeypatch
):
    rec, _ = await setup_track(db_session)
    fake_models(monkeypatch)
    plate = Plate(normalised_text="ABC123", display_text="ABC123")
    db_session.add(plate)
    await db_session.flush()
    old = PlateObservation(
        recording_id=rec.id,
        plate_id=plate.id,
        t_offset_s=1,
        raw_text="ABC123",
        normalised_text="ABC123",
        ocr_confidence=0.99,
        detection_confidence=0.95,
    )
    db_session.add(old)
    await db_session.commit()

    async def frames(path, **kwargs):
        yield 0, np.full((100, 100, 3), 100, dtype=np.uint8)

    warnings = []
    monkeypatch.setattr(stages.log, "warning", lambda message, **data: warnings.append(data))
    monkeypatch.setattr(stages, "iter_frames", frames)
    with pytest.raises(stages.StageError, match=r"could not read 3.*unread_tail=3"):
        await stages.stage_plates(db_session, rec)
    assert "plate_validation_key" not in (rec.probe_json or {})
    assert (await db_session.execute(select(PlateObservation))).scalar_one().id == old.id
    assert warnings[-1]["decoded_frames"] == 1
    assert warnings[-1]["last_decoded_offset_s"] == 0
    assert warnings[-1]["last_requested_offset_s"] == 31.25
    assert warnings[-1]["sample_fps"] == 4
    assert [sample["offset_s"] for sample in warnings[-1]["missing_samples"]] == [1, 2, 31.25]
    assert warnings[-1]["missing_reasons"] == {"unread_tail": 3}


@pytest.mark.parametrize("reason", ["outside_tolerance", "empty_crop"])
async def test_incomplete_frame_diagnostics_distinguish_bad_crops_from_clock_gaps(
    db_session, monkeypatch, reason
):
    rec, _ = await setup_track(db_session)
    fake_models(monkeypatch)
    warnings = []
    monkeypatch.setattr(stages.log, "warning", lambda message, **data: warnings.append(data))
    if reason == "empty_crop":
        monkeypatch.setattr(stages, "crop_with_margin", lambda *args: None)

    async def frames(path, **kwargs):
        for t in [0, 1, 2, 31.25]:
            yield (
                t + (0.25 if reason == "outside_tolerance" else 0),
                np.full((100, 100, 3), 100, dtype=np.uint8),
            )

    monkeypatch.setattr(stages, "iter_frames", frames)
    with pytest.raises(stages.StageError, match=rf"could not read 4.*{reason}=4"):
        await stages.stage_plates(db_session, rec)
    assert warnings[-1]["missing_reasons"] == {reason: 4}
    assert [sample["offset_s"] for sample in warnings[-1]["missing_samples"]] == [0, 1, 2, 31.25]
    assert "plate_validation_key" not in (rec.probe_json or {})
