"""Immutable, atomic and independently verifiable OBD drive bundles."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

from .schema import BUNDLE_FORMAT, BUNDLE_MEMBERS, BUNDLE_SUFFIX, SAMPLE_UNITS, SCHEMA_VERSION
from .storage import ObdStore
from .summary import calculate_summary

MAX_MEMBER_BYTES = 256 * 1024 * 1024
MAX_SAMPLE_LINE_BYTES = 64 * 1024
MAX_SAMPLES = 2_000_000
_SAFE_DRIVE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def _completion_status(stop_reason: Any) -> str:
    """Map the persisted recovery marker onto the immutable v1 contract."""
    return "recovered" if stop_reason == "device_restart" else "complete"


class BundleValidationError(ValueError):
    """Bundle contents do not match the immutable manifest."""


@dataclass(frozen=True, slots=True)
class ExportResult:
    path: Path
    size_bytes: int
    sha256: str
    sample_count: int
    diagnostic_count: int


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _hash_stream(handle: BinaryIO) -> str:
    digest = hashlib.sha256()
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


def _write_fsynced(path: Path, payload: bytes) -> None:
    with path.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class BundleExporter:
    """Stream one completed drive into a ZIP_STORED file and rename it into view."""

    def __init__(self, store: ObdStore, ready_dir: Path) -> None:
        self.store = store
        self.ready_dir = ready_dir

    def export(self, drive_id: str, *, created_at: datetime | None = None) -> ExportResult:
        if not _SAFE_DRIVE_ID.fullmatch(drive_id):
            raise ValueError("drive ID cannot be used as a bundle filename")
        self.ready_dir.mkdir(parents=True, exist_ok=True)
        drive = self.store.drive(drive_id)
        if drive["status"] != "complete" or not drive["finish_time_utc"]:
            raise ValueError("only completed drives can be exported")
        sample_count = int(drive["sample_count"])
        if sample_count <= 0:
            raise ValueError("zero-sample drives are retained locally and cannot be exported")
        diagnostics = self.store.diagnostics(drive_id)
        final = self.ready_dir / f"{drive_id}{BUNDLE_SUFFIX}"
        partial = self.ready_dir / f"{drive_id}{BUNDLE_SUFFIX}.partial"

        if final.exists():
            manifest = inspect_bundle(final)
            if manifest["drive_id"] != drive_id:
                raise BundleValidationError("existing bundle belongs to another drive")
            digest = _hash_file(final)
            self.store.mark_exported(drive_id, digest)
            return ExportResult(final, final.stat().st_size, digest, sample_count, len(diagnostics))

        with tempfile.TemporaryDirectory(prefix="obd-export-") as temporary:
            work = Path(temporary)
            sample_path = work / "samples.ndjson.gz"
            with sample_path.open("wb") as raw:
                with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
                    for row in self.store.iter_samples(drive_id):
                        sample = {
                            key: value
                            for key, value in row.items()
                            if value is not None and key not in {"quality_json"}
                        }
                        compressed.write(_json_bytes(sample) + b"\n")
                raw.flush()
                os.fsync(raw.fileno())

            diagnostic_path = work / "diagnostics.json"
            _write_fsynced(
                diagnostic_path,
                _json_bytes(
                    {"schema_version": SCHEMA_VERSION, "drive_id": drive_id, "events": diagnostics}
                ),
            )
            summary = calculate_summary(drive, self.store.iter_samples(drive_id), diagnostics)
            summary_path = work / "summary.json"
            _write_fsynced(summary_path, _json_bytes(summary))

            payload_counts = {
                "samples.ndjson.gz": sample_count,
                "diagnostics.json": len(diagnostics),
                "summary.json": 1,
            }
            files = {
                name: {
                    "size_bytes": (work / name).stat().st_size,
                    "sha256": _hash_file(work / name),
                    "record_count": payload_counts[name],
                }
                for name in payload_counts
            }
            when = created_at or datetime.now(UTC)
            if when.tzinfo is None:
                raise ValueError("bundle creation time must include a timezone")
            manifest = {
                "schema_version": SCHEMA_VERSION,
                "bundle_format": BUNDLE_FORMAT,
                "drive_id": drive_id,
                "vehicle_id": drive["vehicle_id"],
                "adapter_id": drive["adapter_id"],
                "logger_id": drive["logger_id"],
                "logger_version": drive["logger_version"],
                "start_time_utc": drive["start_time_utc"],
                "finish_time_utc": drive["finish_time_utc"],
                "original_timezone": drive["original_timezone"],
                "start_reason": drive["start_reason"],
                "stop_reason": drive["stop_reason"],
                "obd_protocol": drive["obd_protocol"],
                "completion_status": _completion_status(drive["stop_reason"]),
                "clean_end": bool(drive["clean_end"]),
                "error_count": int(drive["error_count"]),
                "sample_count": sample_count,
                "diagnostic_count": len(diagnostics),
                "created_at_utc": when.astimezone(UTC).isoformat().replace("+00:00", "Z"),
                "included_filenames": list(BUNDLE_MEMBERS),
                "units": SAMPLE_UNITS,
                "files": files,
            }
            manifest_path = work / "manifest.json"
            _write_fsynced(manifest_path, _json_bytes(manifest))

            if partial.exists():
                partial.unlink()
            with partial.open("w+b") as raw_bundle:
                with zipfile.ZipFile(raw_bundle, "w", compression=zipfile.ZIP_STORED) as archive:
                    for name in BUNDLE_MEMBERS:
                        archive.write(work / name, arcname=name)
                raw_bundle.flush()
                os.fsync(raw_bundle.fileno())
            inspect_bundle(partial)
            os.replace(partial, final)
            _fsync_directory(self.ready_dir)

        digest = _hash_file(final)
        self.store.mark_exported(drive_id, digest)
        return ExportResult(final, final.stat().st_size, digest, sample_count, len(diagnostics))


def _bounded_gzip_line_count(archive: zipfile.ZipFile) -> int:
    total = 0
    count = 0
    try:
        with (
            archive.open("samples.ndjson.gz") as raw,
            gzip.GzipFile(fileobj=raw, mode="rb") as decompressed,
        ):
            while line := decompressed.readline(MAX_SAMPLE_LINE_BYTES + 1):
                if len(line) > MAX_SAMPLE_LINE_BYTES:
                    raise BundleValidationError("sample line exceeds the safety limit")
                total += len(line)
                count += 1
                if total > MAX_MEMBER_BYTES or count > MAX_SAMPLES:
                    raise BundleValidationError("sample stream exceeds the safety limit")
                try:
                    value = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise BundleValidationError("samples.ndjson.gz contains invalid JSON") from exc
                if not isinstance(value, dict):
                    raise BundleValidationError("each sample must be a JSON object")
    except (OSError, EOFError) as exc:
        raise BundleValidationError("samples.ndjson.gz is corrupt") from exc
    return count


def inspect_bundle(path: Path) -> dict[str, Any]:
    """Validate names, ZIP mode, member hashes, gzip bounds and record counts."""
    try:
        with zipfile.ZipFile(path, "r") as archive:
            infos = archive.infolist()
            names = [item.filename for item in infos]
            if len(names) != len(set(names)) or set(names) != set(BUNDLE_MEMBERS):
                raise BundleValidationError("bundle must contain exactly the four v1 root members")
            if any("/" in name or "\\" in name or ".." in name for name in names):
                raise BundleValidationError("bundle member path is unsafe")
            if any(item.compress_type != zipfile.ZIP_STORED for item in infos):
                raise BundleValidationError("outer bundle must use ZIP_STORED")
            if any(item.file_size > MAX_MEMBER_BYTES for item in infos):
                raise BundleValidationError("bundle member exceeds the safety limit")
            manifest = json.loads(archive.read("manifest.json"))
            if not isinstance(manifest, dict):
                raise BundleValidationError("manifest must be an object")
            if manifest.get("schema_version") != SCHEMA_VERSION:
                raise BundleValidationError("unsupported bundle schema")
            if manifest.get("bundle_format") != BUNDLE_FORMAT:
                raise BundleValidationError("unexpected bundle format")
            if manifest.get("included_filenames") != list(BUNDLE_MEMBERS):
                raise BundleValidationError("manifest member list does not match v1")
            if manifest.get("units") != SAMPLE_UNITS:
                raise BundleValidationError("manifest units do not match v1")
            files = manifest.get("files")
            if not isinstance(files, dict) or set(files) != set(BUNDLE_MEMBERS[1:]):
                raise BundleValidationError("manifest payload map is incomplete")
            for name, expected in files.items():
                if not isinstance(expected, dict):
                    raise BundleValidationError(f"manifest entry for {name} is invalid")
                info = archive.getinfo(name)
                if expected.get("size_bytes") != info.file_size:
                    raise BundleValidationError(f"size mismatch for {name}")
                with archive.open(name) as handle:
                    if expected.get("sha256") != _hash_stream(handle):
                        raise BundleValidationError(f"hash mismatch for {name}")
            sample_count = _bounded_gzip_line_count(archive)
            if files["samples.ndjson.gz"].get("record_count") != sample_count:
                raise BundleValidationError("sample record count mismatch")
            diagnostics = json.loads(archive.read("diagnostics.json"))
            events = diagnostics.get("events") if isinstance(diagnostics, dict) else None
            if not isinstance(events, list):
                raise BundleValidationError("diagnostics must contain an events list")
            if files["diagnostics.json"].get("record_count") != len(events):
                raise BundleValidationError("diagnostic record count mismatch")
            summary = json.loads(archive.read("summary.json"))
            if not isinstance(summary, dict) or summary.get("drive_id") != manifest.get("drive_id"):
                raise BundleValidationError("summary does not match manifest drive")
            return manifest
    except (zipfile.BadZipFile, KeyError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise BundleValidationError("bundle container is corrupt") from exc


def ready_bundles(directory: Path) -> list[Path]:
    """Partial files are invisible; valid final bundles sort by drive start then ID."""
    found: list[tuple[str, str, Path]] = []
    for path in directory.glob(f"*{BUNDLE_SUFFIX}") if directory.is_dir() else ():
        try:
            manifest = inspect_bundle(path)
        except BundleValidationError:
            continue
        found.append((str(manifest["start_time_utc"]), str(manifest["drive_id"]), path))
    return [path for _started, _drive_id, path in sorted(found)]


def publish_status(directory: Path, status: dict[str, Any]) -> Path:
    """Atomically expose a small redacted status file to the ADB backup host."""
    allowed = {
        "state",
        "ownership_enabled",
        "last_drive_id",
        "last_drive_finished_at_utc",
        "pending_bundle_count",
        "last_error",
        "last_error_at_utc",
    }
    if unknown := set(status) - allowed:
        raise ValueError(f"status contains private or unsupported fields: {sorted(unknown)}")
    directory.mkdir(parents=True, exist_ok=True)
    final = directory / "status.json"
    partial = directory / "status.json.partial"
    _write_fsynced(partial, _json_bytes(status))
    os.replace(partial, final)
    _fsync_directory(directory)
    return final
