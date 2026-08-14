"""Stable revision ids for persisted stage output.

Bump only the stage whose durable output changed. This turns upgrades into targeted
work instead of making users guess whether an entire library needs decoding again.
"""

CURRENT_REVISIONS: dict[str, str] = {
    "metadata": "metadata-v2",
    # v5: coordinates are only accepted at the overlay's own printed precision, positions
    # are judged against the samples either side of them rather than only the one before,
    # interpolation runs after validation instead of over the top of it, and every sample
    # records how much it is trusted and why. Recordings processed by v4 hold positions
    # that none of those rules had ever seen, so their telemetry is rebuilt.
    "telemetry": "telemetry-v5",
    "detection": "detection-v3",
    "plates": "plates-v3",
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
