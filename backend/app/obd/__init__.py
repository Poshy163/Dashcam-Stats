"""Dashcam-hosted, read-only OBD-II logging primitives.

The Android companion owns the live Bluetooth connection.  These pure-Python modules are
the executable bundle and protocol specification used by the backup server and its tests.
"""

from .bundle import BundleExporter, BundleValidationError, inspect_bundle
from .elm import Elm327Session, ElmCommandTimeout, ElmSessionTainted
from .storage import ObdStore

__all__ = [
    "BundleExporter",
    "BundleValidationError",
    "Elm327Session",
    "ElmCommandTimeout",
    "ElmSessionTainted",
    "ObdStore",
    "inspect_bundle",
]
