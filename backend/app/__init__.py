"""Dashcam Analyser backend.

Deliberately empty of imports: every subpackage (``app.core``, ``app.db``, ``app.api``,
``app.pipeline``) pulls in the others, so importing anything here would create cycles that
only show up under a particular import order. Use the subpackages directly.
"""

from __future__ import annotations

__all__: list[str] = []
