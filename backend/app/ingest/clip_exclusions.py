"""Durable evidence for intentional transfer omissions, independent of media state.

This ledger never authorizes a skip: every transfer needs a fresh device decision.
Rows record observed plans rather than claiming a clip was never uploaded. The scanner
only indexes actual local files, and API projections also report any later indexed copy.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from bisect import bisect_left
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ClipTransferEvaluation, ClipTransferExclusion, Recording
from app.db.session import session_scope

POLICY_VERSION = "parked_visual_quiet_v1"
REASON = "parked_visual_quiet"
MAX_DECISIONS = 512
MAX_EVALUATION_AGE_S = 180
STATUSES = {
    "disabled",
    "unsupported",
    "missing",
    "invalid",
    "stale",
    "card_mismatch",
    "ready",
    "persistence_failed",
}


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("exclusion timestamp needs a timezone")
    return parsed.astimezone(UTC)


def _count(value: int) -> int:
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        raise ValueError("invalid exclusion inventory count")
    return value


async def record_evaluation(
    *,
    manifest: dict | None,
    excluded: list[dict],
    status: str,
    considered_count: int,
    considered_bytes: int,
    policy_enabled: bool,
) -> dict[str, Any]:
    """Commit one bounded plan before its caller may remove clips from a transfer plan.

    Exceptions/cancellation propagate so the caller includes footage normally. A late
    database commit can retain an observed decision; it never creates media or proves
    that transfer was omitted, and it cannot authorize a future inventory's skip.
    """
    from app.ingest.parked_filter import MAX_AGE_SECONDS, eligible_skip, validate_policy_manifest

    if status not in STATUSES or type(policy_enabled) is not bool:
        raise ValueError("invalid exclusion evaluation status")
    _count(considered_count)
    _count(considered_bytes)
    if not isinstance(excluded, list) or len(excluded) > MAX_DECISIONS:
        raise ValueError("too many exclusion decisions")
    doc = validate_policy_manifest(_canonical(manifest)) if manifest is not None else None
    now = datetime.now(UTC)
    if excluded and (
        status != "ready"
        or not policy_enabled
        or doc is None
        or not doc["enabled"]
        or doc["status"] != "ready"
        or doc["truncated"]
    ):
        raise ValueError("exclusions require a current enabled policy evaluation")
    if (
        excluded
        and not -30 <= (now - _time(doc["generated_at_utc"])).total_seconds() <= MAX_AGE_SECONDS
    ):
        raise ValueError("exclusions require a fresh policy evaluation")
    present = {_canonical(entry) for entry in doc["decisions"]} if doc else set()
    rows: dict[str, dict] = {}
    generation = str(uuid.uuid4())
    for decision in excluded:
        if _canonical(decision) not in present or not eligible_skip(decision):
            raise ValueError("exclusion proof is absent or unsafe")
        clip = decision["clip"]
        identity = {
            "source_id": doc["source_id"],
            "card_id": doc["card_id"],
            **{
                key: clip[key]
                for key in (
                    "clip_id",
                    "relative_path",
                    "size_bytes",
                    "mtime_ms",
                    "fingerprint",
                )
            },
        }
        digest = hashlib.sha256(_canonical(identity).encode()).hexdigest()
        if digest in rows:
            raise ValueError("duplicate exclusion identity")
        rows[digest] = {
            "identity_sha256": digest,
            "source_id": doc["source_id"],
            "card_id": doc["card_id"],
            "clip_id": clip["clip_id"],
            "relative_path": clip["relative_path"],
            "filename": clip["relative_path"].rsplit("/", 1)[-1],
            "camera_key": clip["camera_key"],
            "size_bytes": clip["size_bytes"],
            "mtime_ms": clip["mtime_ms"],
            "fingerprint_json": clip["fingerprint"],
            "started_at": _time(clip["start_utc"]),
            "ended_at": _time(clip["end_utc"]),
            "policy_version": POLICY_VERSION,
            "reason": REASON,
            "proof_json": {"guard": decision["guard"], "visual": decision["visual"]},
            "last_observed_at": now,
            "latest_generation": generation,
        }
    excluded_bytes = sum(row["size_bytes"] for row in rows.values())
    if len(rows) > considered_count or excluded_bytes > considered_bytes:
        raise ValueError("exclusions exceed the evaluated inventory")
    evaluation_values = {
        "generation": generation,
        "evaluated_at": now,
        "policy_enabled": policy_enabled,
        "policy_version": POLICY_VERSION,
        "status": status,
        "source_id": doc["source_id"] if doc else None,
        "card_id": doc["card_id"] if doc else None,
        "manifest_generated_at": _time(doc["generated_at_utc"]) if doc else None,
        "manifest_sha256": hashlib.sha256(_canonical(doc).encode()).hexdigest() if doc else None,
        "considered_count": considered_count,
        "considered_bytes": considered_bytes,
        "excluded_count": len(rows),
        "excluded_bytes": excluded_bytes,
    }
    async with session_scope() as session:
        existing = (
            {
                row.identity_sha256: row
                for row in (
                    await session.scalars(
                        select(ClipTransferExclusion).where(
                            ClipTransferExclusion.identity_sha256.in_(rows)
                        )
                    )
                ).all()
            }
            if rows
            else {}
        )
        for digest, values in rows.items():
            row = existing.get(digest)
            if row is None:
                session.add(
                    ClipTransferExclusion(
                        **values,
                        first_observed_at=now,
                        observation_count=1,
                    )
                )
            else:
                for key, value in values.items():
                    setattr(row, key, value)
                row.observation_count += 1
        evaluation = await session.get(ClipTransferEvaluation, 1)
        if evaluation is None:
            session.add(ClipTransferEvaluation(id=1, **evaluation_values))
        else:
            for key, value in evaluation_values.items():
                setattr(evaluation, key, value)
    # Only successful exit from session_scope has committed this generation.
    return {"generation": generation, "excluded_count": len(rows), "excluded_bytes": excluded_bytes}


async def exclusion_summary(session: AsyncSession) -> dict[str, Any]:
    evaluation = await session.get(ClipTransferEvaluation, 1)
    historical_count, historical_bytes = (
        await session.execute(
            select(
                func.count(ClipTransferExclusion.id),
                func.coalesce(func.sum(ClipTransferExclusion.size_bytes), 0),
            )
        )
    ).one()
    result: dict[str, Any] = {
        "policy_version": POLICY_VERSION,
        "status": "not_evaluated",
        "policy_enabled": False,
        "evaluated_at": None,
        "manifest_generated_at": None,
        "stale": True,
        "current_count": 0,
        "current_bytes": 0,
        "locally_indexed_count": 0,
        "local_match_basis": "filename_and_size",
        "historical_count": historical_count,
        "historical_bytes": historical_bytes,
        "reason_counts": {},
        "disposition": "observed_exclusion_decision",
        "files_deleted": False,
        "counts_as_backed_up": False,
    }
    if evaluation is None:
        return result
    current = list(
        (
            await session.scalars(
                select(ClipTransferExclusion).where(
                    ClipTransferExclusion.latest_generation == evaluation.generation,
                )
            )
        ).all()
    )
    names = {row.filename for row in current}
    indexed = (
        set(
            (
                await session.execute(
                    select(Recording.filename, Recording.size_bytes).where(
                        Recording.filename.in_(names),
                    )
                )
            ).all()
        )
        if names
        else set()
    )
    age = (datetime.now(UTC) - evaluation.evaluated_at).total_seconds()
    result.update(
        status=evaluation.status,
        policy_enabled=evaluation.policy_enabled,
        evaluated_at=evaluation.evaluated_at.isoformat(),
        manifest_generated_at=(
            evaluation.manifest_generated_at.isoformat()
            if evaluation.manifest_generated_at
            else None
        ),
        stale=age < 0 or age > MAX_EVALUATION_AGE_S,
        current_count=evaluation.excluded_count,
        current_bytes=evaluation.excluded_bytes,
        locally_indexed_count=sum((row.filename, row.size_bytes) in indexed for row in current),
        local_match_basis="filename_and_size",
        reason_counts={REASON: evaluation.excluded_count} if evaluation.excluded_count else {},
    )
    return result


async def exclusion_page(
    session: AsyncSession,
    *,
    offset: int,
    limit: int,
    current_only: bool = False,
) -> tuple[list[dict[str, Any]], int]:
    stmt = select(ClipTransferExclusion)
    evaluation = await session.get(ClipTransferEvaluation, 1)
    if current_only:
        stmt = stmt.where(
            ClipTransferExclusion.latest_generation == (evaluation.generation if evaluation else "")
        )
    total = await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = list(
        (
            await session.scalars(
                stmt.order_by(
                    ClipTransferExclusion.last_observed_at.desc(),
                    ClipTransferExclusion.id.desc(),
                )
                .offset(offset)
                .limit(limit)
            )
        ).all()
    )
    names = {row.filename for row in rows}
    recordings = (
        list(
            (
                await session.scalars(
                    select(Recording).where(
                        Recording.filename.in_(names),
                    )
                )
            ).all()
        )
        if names
        else []
    )
    items = []
    for row in rows:
        items.append(
            {
                "id": row.id,
                "identity_sha256": row.identity_sha256,
                "source_id": row.source_id,
                "card_id": row.card_id,
                "clip_id": row.clip_id,
                "relative_path": row.relative_path,
                "camera_key": row.camera_key,
                "size_bytes": row.size_bytes,
                "mtime_ms": row.mtime_ms,
                "fingerprint": row.fingerprint_json,
                "start_utc": row.started_at.isoformat(),
                "end_utc": row.ended_at.isoformat(),
                "policy_version": row.policy_version,
                "reason": row.reason,
                "proof": row.proof_json,
                "first_observed_at": row.first_observed_at.isoformat(),
                "last_observed_at": row.last_observed_at.isoformat(),
                "observation_count": row.observation_count,
                "in_latest_evaluation": bool(
                    evaluation and row.latest_generation == evaluation.generation
                ),
                "disposition": "observed_exclusion_decision",
                "counts_as_backed_up": False,
                "local_recordings": [
                    {
                        "id": recording.id,
                        "state": recording.state.value,
                        "size_matches": recording.size_bytes == row.size_bytes,
                    }
                    for recording in recordings
                    if recording.filename == row.filename
                ],
            }
        )
    return items, total


async def intentional_intervals(
    session: AsyncSession,
    *,
    start: datetime,
    end: datetime,
) -> list[tuple[datetime, datetime]]:
    """Only evidence overlapping a real uncovered interval can form a journey boundary."""
    if end <= start:
        return []
    return list(
        (
            await session.execute(
                select(
                    ClipTransferExclusion.started_at,
                    ClipTransferExclusion.ended_at,
                )
                .where(
                    ClipTransferExclusion.reason == REASON,
                    ClipTransferExclusion.policy_version == POLICY_VERSION,
                    ClipTransferExclusion.started_at < end,
                    ClipTransferExclusion.ended_at > start,
                )
                .order_by(ClipTransferExclusion.started_at)
            )
        ).all()
    )


@dataclass(frozen=True)
class IntentionalGapBarriers:
    """Merged intervals make the per-recording boundary check logarithmic."""

    starts: tuple[datetime, ...] = ()
    ends: tuple[datetime, ...] = ()

    @classmethod
    def from_intervals(
        cls,
        intervals: Iterable[tuple[datetime, datetime]],
    ) -> IntentionalGapBarriers:
        merged: list[tuple[datetime, datetime]] = []
        for start, end in sorted(intervals):
            if start >= end:
                continue
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
            else:
                merged.append((start, end))
        return cls(tuple(start for start, _ in merged), tuple(end for _, end in merged))

    def blocks(self, available_end: datetime, available_start: datetime) -> bool:
        if available_start <= available_end:
            return False
        index = bisect_left(self.starts, available_start) - 1
        return index >= 0 and self.ends[index] > available_end
