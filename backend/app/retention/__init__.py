"""Storage retention: planning, safety guards and (optional) deletion."""

from __future__ import annotations

from app.retention.planner import (
    RetentionCandidate,
    RetentionPlan,
    current_usage,
    execute,
    plan,
)
from app.retention.safety import SafetyCheck, SafetyReport, can_delete, evaluate_safety

__all__ = [
    "RetentionCandidate",
    "RetentionPlan",
    "SafetyCheck",
    "SafetyReport",
    "can_delete",
    "current_usage",
    "evaluate_safety",
    "execute",
    "plan",
]
