"""Stable revision ids for persisted stage output.

Bump only the stage whose durable output changed. This turns upgrades into targeted
work instead of making users guess whether an entire library needs decoding again.
"""

CURRENT_REVISIONS: dict[str, str] = {
    "metadata": "metadata-v2",
    "telemetry": "telemetry-v4",
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
