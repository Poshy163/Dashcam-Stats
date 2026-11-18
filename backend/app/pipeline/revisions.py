"""Stable revision ids for persisted stage output.

Bump only the stage whose durable output changed. This turns upgrades into targeted
work instead of making users guess whether an entire library needs decoding again.
"""

CURRENT_REVISIONS: dict[str, str] = {
    "metadata": "metadata-v2",
    # v6 checks damaged OCR reads that previously drew a moving route through a
    # stationary sequence. Historical review selects recordings using their stored
    # evidence so installing the update does not require decoding every video.
    "telemetry": "telemetry-v6",
    "detection": "detection-v3",
    # v5 reads distinct source views and counts temporal agreement. Re-reading v4's one
    # saved vehicle image misses clear plates and can repeat a clipped side-view guess.
    "plates": "plates-v5",
}

INVALIDATED_REVISION = "invalidated"

REVISION_FIELDS: dict[str, str] = {
    "metadata": "metadata_revision",
    "telemetry": "telemetry_revision",
    "detection": "detection_revision",
    "plates": "plate_revision",
}


def outdated_stages(recording: object) -> list[str]:
    return [
        name
        for name, revision in CURRENT_REVISIONS.items()
        if getattr(recording, REVISION_FIELDS[name], None) != revision
    ]
