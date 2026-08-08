"""Stage sequencing for one recording.

A corrupt recording must never stall the queue, so each stage is isolated: a failure is
recorded against that stage and the recording, and the run stops for *this* recording only.
Permanent failures (an empty file, a stream with no video) are distinguished from transient
ones so retries are not spent on work that can never succeed.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models import Recording, RecordingState, StageState
from app.pipeline.stages import STAGE_ORDER, STAGES, StageError, StageResult

log = get_logger(__name__)

ProgressCallback = Callable[[str, float], None]

#: What the reprocess options in the UI expand to.
REPROCESS_PRESETS: dict[str, tuple[str, ...]] = {
    "metadata": ("metadata", "summarise"),
    "telemetry": ("telemetry", "summarise"),
    "detection": ("detection", "summarise"),
    "plates": ("plates", "summarise"),
    "everything": STAGE_ORDER,
}

_STAGE_FIELDS = {
    "metadata": "metadata_state",
    "telemetry": "telemetry_state",
    "detection": "detection_state",
    "plates": "plate_state",
}


@dataclass(slots=True)
class RunReport:
    recording_id: int
    stages: list[StageResult] = field(default_factory=list)
    ok: bool = True
    error: str | None = None
    permanent: bool = False
    elapsed_s: float = 0.0
    realtime_factor: float | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "recording_id": self.recording_id,
            "ok": self.ok,
            "error": self.error,
            "permanent": self.permanent,
            "elapsed_s": round(self.elapsed_s, 2),
            "realtime_factor": self.realtime_factor,
            "stages": [
                {"name": s.name, "ok": s.ok, "detail": s.detail, "stats": s.stats}
                for s in self.stages
            ],
        }


def expand_stages(requested: list[str] | None) -> tuple[str, ...]:
    """Turn a request into a concrete ordered stage list."""
    if not requested:
        return STAGE_ORDER

    wanted: set[str] = set()
    for item in requested:
        key = item.strip().lower()
        if key in REPROCESS_PRESETS:
            wanted.update(REPROCESS_PRESETS[key])
        elif key in STAGES:
            wanted.add(key)

    if not wanted:
        return STAGE_ORDER
    # Summarising is cheap and keeps rollups honest after any change.
    wanted.add("summarise")
    return tuple(name for name in STAGE_ORDER if name in wanted)


def pending_stages(recording: Recording) -> tuple[str, ...]:
    """Stages this recording still needs, for a job that did not name any."""
    needed = [
        name
        for name, attr in _STAGE_FIELDS.items()
        if getattr(recording, attr) in (StageState.PENDING, StageState.FAILED)
    ]
    if not needed:
        return ()
    # Metadata underpins everything else; if it is missing, run the lot.
    if "metadata" in needed:
        return STAGE_ORDER
    needed.append("summarise")
    return tuple(name for name in STAGE_ORDER if name in needed)


async def run_stages(
    session: AsyncSession,
    recording: Recording,
    stages: list[str] | None = None,
    *,
    progress: ProgressCallback | None = None,
) -> RunReport:
    """Execute the requested stages against one recording."""
    selected = expand_stages(stages)
    report = RunReport(recording_id=recording.id)
    started = time.monotonic()

    recording.state = RecordingState.PROCESSING
    await session.flush()

    total = len(selected)
    for index, name in enumerate(selected):
        stage = STAGES[name]
        attr = _STAGE_FIELDS.get(name)

        # Both loop variables are bound as defaults. Closing over them by reference would
        # make every stage report the *last* stage's name and index once the loop moved on.
        def stage_progress(
            _stage: str, fraction: float, _i: int = index, _name: str = name
        ) -> None:
            if progress:
                progress(_name, (_i + min(1.0, max(0.0, fraction))) / total)

        if attr:
            setattr(recording, attr, StageState.RUNNING)
        if progress:
            progress(name, index / total)

        try:
            result = await stage(session, recording, progress=stage_progress)
            report.stages.append(result)
            await session.flush()
        except StageError as exc:
            if attr:
                setattr(recording, attr, StageState.FAILED)
            report.ok = False
            report.error = str(exc)
            report.permanent = exc.permanent
            recording.state = RecordingState.FAILED
            recording.error_message = str(exc)
            recording.error_count += 1
            log.warning(
                "stage failed",
                recording=recording.filename,
                stage=name,
                error=str(exc),
                permanent=exc.permanent,
            )
            break
        except Exception as exc:
            if attr:
                setattr(recording, attr, StageState.FAILED)
            report.ok = False
            report.error = f"{type(exc).__name__}: {exc}"
            recording.state = RecordingState.FAILED
            recording.error_message = report.error
            recording.error_count += 1
            log.exception("stage raised", recording=recording.filename, stage=name)
            break

    report.elapsed_s = time.monotonic() - started
    if report.ok and recording.duration_s and report.elapsed_s > 0:
        # Video seconds processed per wall-clock second — what the queue page reports.
        report.realtime_factor = round(recording.duration_s / report.elapsed_s, 2)

    if progress:
        progress("done" if report.ok else "failed", 1.0)
    return report
