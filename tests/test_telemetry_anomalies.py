"""Historical GPS checks identify candidates for replay from source video."""

import pytest
from sqlalchemy import text

from app.db.models import Recording, TelemetryPoint
from app.pipeline.telemetry_anomalies import suspect_telemetry_recording_ids


def point(offset, *, lat=None, lon=None, speed=None, source="none", raw=None, partial=True):
    quality = {"direct": "valid", "interpolated": "interpolated", "paired_camera": "interpolated"}
    return TelemetryPoint(
        t_offset_s=offset,
        lat=lat,
        lon=lon,
        has_fix=lat is not None and lon is not None,
        speed_kmh=speed,
        gps_quality=quality.get(source, "rejected"),
        quality_json={
            "gps_source": source,
            "ocr_status": "partial" if partial else "valid",
            "problems": ["GPS fields unreadable"] if partial else [],
        },
        raw_text=raw,
    )


async def add_recording(session, name, samples):
    clip = Recording(rel_path=name, filename=name, telemetry=samples)
    session.add(clip)
    await session.flush()
    return clip.id


@pytest.mark.parametrize(
    ("earlier", "later", "later_offset", "expected"),
    [
        ("E:-80.0300 N:?", "E:-80.0305N:unreadable", 16, True),
        ("N:39.9700", "E:?N:39.9703", 16, True),
        ("E:-80.0300", "N:39.9700", 16, False),  # witnesses disagree about which axis
        ("E:-80.0300", "E:-79.9600", 16, False),  # witnesses disagree with one another
        ("E:-80.0300", "E:-80.0305", 22, False),  # travel across a long GPS gap
        ("E:-80.0300", "E:-80.0305", 5, False),  # both on the same side of the fix
        ("E:-80.0001", "E:-80.0005", 16, False),  # legitimate movement
        ("E:-8.0300", "E:-8.0305", 16, False),  # corrupt degree digits in raw text
        ("E:-80.03", "E:-80.0305", 16, False),  # truncated reading
    ],
)
async def test_raw_axis_requires_two_compatible_full_readings_around_a_weak_fix(
    db_session, earlier, later, later_offset, expected
):
    samples = [point(t) for t in range(24)]
    samples[0] = point(0, raw=earlier)
    samples[9] = point(9, lat=40, lon=-80, source="direct")
    # The raw single-axis evidence survives even when the stored location was filled.
    samples[later_offset] = point(later_offset, lat=40, lon=-80, source="interpolated", raw=later)
    rid = await add_recording(db_session, "sparse.ts", samples)
    assert await suspect_telemetry_recording_ids(db_session, [rid]) == (
        {rid} if expected else set()
    )


@pytest.mark.parametrize(
    ("interior_speed", "has_fill", "end_partial", "low_speed_nearby", "expected"),
    [
        (None, True, True, True, True),
        (0, True, True, True, True),
        (42, True, True, True, False),  # real motion along a straight road
        (None, False, True, True, False),  # GPS dropped out
        (None, True, False, True, False),  # corroborated endpoint
        (None, True, True, False, False),  # no stationary evidence
    ],
)
async def test_damaged_interpolated_bridge_needs_evidence_of_low_speed(
    db_session, interior_speed, has_fill, end_partial, low_speed_nearby, expected
):
    samples = [
        point(1, speed=0 if low_speed_nearby else None),
        point(5, lat=40, lon=-80, source="direct"),
        point(
            7,
            lat=40.0003,
            lon=-80,
            speed=interior_speed,
            source="interpolated" if has_fill else "none",
        ),
        point(13, lat=40.0009, lon=-80, source="direct", partial=end_partial),
    ]
    rid = await add_recording(db_session, "bridge.ts", samples)
    assert await suspect_telemetry_recording_ids(db_session, [rid]) == (
        {rid} if expected else set()
    )


async def test_never_uses_a_paired_copy_or_another_recording_as_an_anchor(db_session):
    copied = await add_recording(
        db_session, "copied.ts", [point(9, lat=40, lon=-80, source="paired_camera")]
    )
    halves = []
    for label, offset in (("left", 0), ("right", 16)):
        samples = [point(t) for t in range(24)]
        samples[offset] = point(offset, raw="E:-80.0300")
        samples[9] = point(9, lat=40, lon=-80, source="direct")
        halves.append(await add_recording(db_session, label + ".ts", samples))
    assert await suspect_telemetry_recording_ids(db_session, [copied, *halves]) == set()
    assert await suspect_telemetry_recording_ids(db_session, []) == set()


async def test_a_partly_damaged_historical_record_does_not_block_the_batch(db_session):
    ids = []
    for name in ("invalid-json", "non-object", "legacy-null", "bad-problems"):
        samples = [point(t) for t in range(24)]
        samples[0] = point(0, raw="E:-80.0300")
        samples[9] = point(9, lat=40, lon=-80, source="direct")
        samples[16] = point(16, raw="E:-80.0302")
        ids.append(await add_recording(db_session, name + ".ts", samples))

    for rid, value in zip(
        ids,
        ("{damaged", "42", None, '{"gps_source":"direct","ocr_status":"partial","problems":7}'),
        strict=True,
    ):
        await db_session.execute(
            text(
                "UPDATE telemetry_points SET quality_json = :value "
                "WHERE recording_id = :rid AND t_offset_s = 9"
            ),
            {"rid": rid, "value": value},
        )
    assert await suspect_telemetry_recording_ids(db_session, ids) == set(ids[2:])
