"""Inference devices reported by an individual processing attempt."""

from collections.abc import Iterable


def device_summary(devices: Iterable[str | None]) -> str | None:
    # A detector can fall back partway through a clip. Keep each device once in
    # order of use, including when several stages contain the same fallback.
    distinct = dict.fromkeys(part for label in devices if label for part in label.split(" + "))
    return " + ".join(distinct) or None


def stage_devices(result: dict | None) -> dict[str, str]:
    stages = result.get("stages") if isinstance(result, dict) else None
    devices = {}
    for stage in stages if isinstance(stages, list) else ():
        if not isinstance(stage, dict) or not isinstance(stage.get("stats"), dict):
            continue
        name, stats = stage.get("name"), stage["stats"]
        device = stats.get("device")
        if isinstance(name, str) and isinstance(device, str) and device:
            devices[name] = device
    return devices
