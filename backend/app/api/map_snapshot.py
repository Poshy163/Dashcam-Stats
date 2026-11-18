"""Two replaceable files under the data volume, one for each default map layer."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from app.config import get_config

Layer = TypeVar("Layer", bound=BaseModel)
_MAX_BYTES = 16 * 1024 * 1024
# Bump when a map computation changes the meaning of an existing set of parameters.
_FORMAT = 1


def _header(key: tuple, revision: bytes) -> bytes:
    parameters = json.dumps([_FORMAT, sqlite3.sqlite_version, *key], separators=(",", ":")).encode()
    return hashlib.sha256(revision + parameters).digest()


def load(key: tuple, revision: bytes, model: type[Layer]) -> Layer | None:
    path = get_config().cache_dir / f"map-{key[0]}.snapshot"
    try:
        with path.open("rb") as source:
            if source.read(32) != _header(key, revision):
                return None
            digest = source.read(32)
            payload = source.read(_MAX_BYTES + 1)
        if len(payload) > _MAX_BYTES or hashlib.sha256(payload).digest() != digest:
            return None
        return model.model_validate_json(payload)
    except (OSError, ValidationError):
        return None


def save(key: tuple, revision: bytes, value: BaseModel) -> None:
    path = get_config().cache_dir / f"map-{key[0]}.snapshot"
    payload = value.model_dump_json().encode()
    if len(payload) > _MAX_BYTES:
        return
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name, delete=False) as dest:
            temporary = Path(dest.name)
            dest.write(_header(key, revision))
            dest.write(hashlib.sha256(payload).digest())
            dest.write(payload)
        os.replace(temporary, path)
    except OSError:
        # Space and write permissions in the cache directory do not gate map reads.
        pass
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
