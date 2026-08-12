"""Pulling footage off the head unit.

Named ``ingest`` rather than ``backup`` to keep it distinct from the database backup in
``app.api.routes.system``: this moves recordings from the car onto the NAS, and the
directory it writes into is the same one the scanner and pipeline already read, so a
transferred file is analysed with no further glue.
"""

from __future__ import annotations

from app.ingest.models import DeltaPlan, RemoteFile, RunResult, RunState, UnitInfo, UnitState
from app.ingest.poller import get_poller
from app.ingest.puller import STAGING_DIRNAME, commit, delta, probe_unit, run_pull
from app.ingest.status import get_status

__all__ = [
    "STAGING_DIRNAME",
    "DeltaPlan",
    "RemoteFile",
    "RunResult",
    "RunState",
    "UnitInfo",
    "UnitState",
    "commit",
    "delta",
    "get_poller",
    "get_status",
    "probe_unit",
    "run_pull",
]
