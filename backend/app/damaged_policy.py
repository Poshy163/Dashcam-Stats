"""Automatic handling for footage the media pipeline positively classifies as damaged.

The default action is deliberately non-destructive: hide the recording from normal
library views while keeping both the source file and everything derived from it.  Deletion
is a separate, explicit setting and still has to pass the same mount/path/writability
guards as retention immediately before the unlink.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.paths import PathTraversalError, forget_tree_measurements, safe_join
from app.core.settings_service import get_settings_service
from app.db.models import Recording, RecordingState
from app.retention.safety import SafetyReport, evaluate_safety

log = get_logger(__name__)

_POLICY_KEY = "damaged_policy"
_UNUSABLE_WARNING = "no usable frame could be decoded"


@dataclass(slots=True)
class DamagedPolicySummary:
    hidden: int = 0
    deleted: int = 0
    restored: int = 0
    blocked: int = 0


def is_damaged(recording: Recording) -> bool:
    """Whether the source is permanently unusable, not merely imperfect.

    Recoverable container warnings are still shown on a recording, but they are not a
    reason to hide it. In particular, dashcam transport streams commonly end partway
    through a 188-byte packet or report a nonsense nominal FPS while decoding normally.
    """
    probe = recording.probe_json if isinstance(recording.probe_json, dict) else {}
    warnings = [str(item).lower() for item in probe.get("warnings") or []]
    return (
        recording.state is RecordingState.INVALID
        or bool(probe.get("source_unusable"))
        or any(_UNUSABLE_WARNING in warning for warning in warnings)
    )


def _probe(recording: Recording) -> dict:
    return dict(recording.probe_json) if isinstance(recording.probe_json, dict) else {}


def _reason(recording: Recording) -> str:
    probe = _probe(recording)
    warnings = [str(item) for item in probe.get("warnings") or []]
    return recording.error_message or (warnings[0] if warnings else "source classified as damaged")


def _remember_original_state(recording: Recording, probe: dict) -> dict:
    existing = probe.get(_POLICY_KEY)
    if isinstance(existing, dict) and existing.get("previous_state"):
        return dict(existing)
    return {
        "previous_state": recording.state.value,
        "reason": _reason(recording),
    }


def _hide(recording: Recording, *, status: str, detail: str | None = None) -> None:
    probe = _probe(recording)
    policy = _remember_original_state(recording, probe)
    policy.update(
        {
            "action": "hide",
            "status": status,
            "detail": detail,
            "applied_at": datetime.now(UTC).isoformat(),
        }
    )
    probe[_POLICY_KEY] = policy
    probe["source_damaged"] = True
    recording.probe_json = probe
    recording.ignored = True


def _restore_policy_hidden(recording: Recording) -> bool:
    probe = _probe(recording)
    policy = probe.get(_POLICY_KEY)
    if not isinstance(policy, dict) or recording.state is RecordingState.DELETED:
        return False

    previous = str(policy.get("previous_state") or RecordingState.INVALID.value)
    try:
        restored = RecordingState(previous)
    except ValueError:
        restored = RecordingState.INVALID if is_damaged(recording) else RecordingState.DISCOVERED

    probe.pop(_POLICY_KEY, None)
    recording.probe_json = probe
    recording.ignored = False
    # New policy-hidden rows retain their lifecycle state. Restore it as well for rows
    # hidden by an earlier implementation that used the IGNORED enum value.
    if recording.state is RecordingState.IGNORED:
        recording.state = restored
    return True


async def apply_damaged_policy(
    session: AsyncSession,
    recording: Recording,
    *,
    action: str | None = None,
    safety: SafetyReport | None = None,
) -> str:
    """Apply the configured policy to one recording and return its outcome."""
    settings = get_settings_service()
    selected = action or str(settings.get_nowait("scanner.damaged_footage_action"))

    if selected == "keep":
        return "restored" if _restore_policy_hidden(recording) else "kept"
    if not is_damaged(recording):
        # Also repairs rows hidden by an older, over-broad policy. A warning may still be
        # useful diagnostics without making a playable recording disappear.
        return "restored" if _restore_policy_hidden(recording) else "kept"
    if recording.file_missing:
        return "kept"
    if selected == "hide":
        probe = _probe(recording)
        existing = probe.get(_POLICY_KEY)
        if recording.ignored and isinstance(existing, dict) and existing.get("status") == "hidden":
            # Reconciliation runs after every scan. Reapplying the same hide inflated the
            # scan summary, logged the same files and triggered a full journey rebuild on
            # every interval even though nothing had changed.
            return "kept"
        _hide(recording, status="hidden")
        log.info("damaged footage hidden", file=recording.filename, reason=_reason(recording))
        return "hidden"

    # Deletion is explicit, but explicit is not enough: the current mount must still pass
    # every retention safety check and be writable at the moment the file is removed.
    safety = safety or await evaluate_safety(session)
    if not safety.ok or not safety.writable:
        detail = safety.blocked_reason or "footage directory is read-only"
        _hide(recording, status="delete_blocked", detail=detail)
        log.warning(
            "damaged footage deletion blocked; hidden instead",
            file=recording.filename,
            reason=detail,
        )
        return "blocked"

    root = await settings.footage_dir()
    try:
        target = safe_join(root, recording.rel_path)
    except PathTraversalError as exc:
        _hide(recording, status="delete_blocked", detail=str(exc))
        log.error(
            "refusing to delete damaged footage outside root",
            file=recording.filename,
            error=str(exc),
        )
        return "blocked"

    if not target.is_file():
        _hide(recording, status="delete_blocked", detail="source file is not a regular file")
        return "blocked"

    try:
        target.unlink()
    except OSError as exc:
        _hide(recording, status="delete_blocked", detail=str(exc))
        log.warning(
            "could not delete damaged footage; hidden instead",
            file=recording.filename,
            error=str(exc),
        )
        return "blocked"

    # The share is not what it was measured to be a moment ago. Retention's own delete
    # path does this too; this is the other place footage leaves the tree.
    forget_tree_measurements()

    probe = _probe(recording)
    policy = _remember_original_state(recording, probe)
    policy.update(
        {
            "action": "delete",
            "status": "deleted",
            "applied_at": datetime.now(UTC).isoformat(),
        }
    )
    probe[_POLICY_KEY] = policy
    probe["source_damaged"] = True
    recording.probe_json = probe
    recording.ignored = True
    recording.file_missing = True
    recording.deleted_at = datetime.now(UTC)
    recording.state = RecordingState.DELETED
    log.warning("damaged footage deleted", file=recording.filename, reason=_reason(recording))
    return "deleted"


async def apply_known_damaged_policy(session: AsyncSession) -> DamagedPolicySummary:
    """Reconcile known damaged files after a scan, including previously hidden ones."""
    action = str(get_settings_service().get_nowait("scanner.damaged_footage_action"))
    if action == "keep":
        stmt = select(Recording).where(
            Recording.file_missing.is_(False),
            Recording.ignored.is_(True),
        )
    else:
        # SQLite's JSON extractor lets a scan touch only the exceptional rows instead of
        # materialising every healthy recording merely to inspect a Python dictionary.
        stmt = select(Recording).where(
            Recording.file_missing.is_(False),
            or_(
                Recording.state.in_([RecordingState.INVALID, RecordingState.IGNORED]),
                func.json_extract(Recording.probe_json, "$.source_damaged") == 1,
            ),
        )

    rows = list((await session.execute(stmt)).scalars())
    summary = DamagedPolicySummary()
    safety = await evaluate_safety(session) if action == "delete" and rows else None
    for recording in rows:
        outcome = await apply_damaged_policy(
            session,
            recording,
            action=action,
            safety=safety,
        )
        if outcome == "hidden":
            summary.hidden += 1
        elif outcome == "deleted":
            summary.deleted += 1
        elif outcome == "restored":
            summary.restored += 1
        elif outcome == "blocked":
            summary.blocked += 1
    return summary
