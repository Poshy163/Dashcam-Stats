"""What the adapter's voltage reading can and cannot say about the battery.

The ELM327 reports the voltage on pin 16 of the OBD port, which is the battery terminal
through a few metres of loom. That single number is two completely different measurements
depending on whether the engine is turning, and conflating them is the whole trap here:

* **Engine running.** The alternator is holding the bus at 13.5-14.5 V and the battery is
  clamped to whatever the regulator decides. The reading says a great deal about the
  *charging system* and precisely nothing about state of charge -- a battery at 40 % and a
  battery at 100 % both read ~14 V while being charged.
* **Engine off.** Now the reading is the battery's own open-circuit voltage, which does map
  to state of charge, via a curve that is well established for flooded lead-acid.

So this module refuses to produce a state of charge from a running engine, and reports the
charging system separately. Both are useful; they are not the same field.

**Why voltage is the discriminator rather than RPM.** RPM is a polled PID and is absent from
two thirds of the samples in a tiered poll plan -- and it is missing entirely from the
samples *after* shutdown, which are exactly the ones worth having. An alternator that is
turning never lets the bus sag below :data:`CHARGING_FLOOR_V`, so the voltage classifies
itself. RPM, when present, is used only to corroborate.

**Two honesties the output carries rather than hides.** The adapter quantises to 0.1 V, and
in the band that matters 0.1 V is about seven points of charge -- so a point estimate alone
would imply a precision that does not exist, and every result carries an uncertainty. And
voltage measured seconds after the engine stops is inflated by surface charge, which decays
over tens of minutes; that cannot be corrected for without knowing the rest history, so it
is reported as reduced confidence and named in the summary instead of being quietly fudged.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from itertools import pairwise

#: At or above this, something is driving the bus: the alternator is turning, or an external
#: charger is connected. Below it, the battery is on its own. A healthy alternator regulates
#: to 13.5-14.5 V, so 13.0 V sits clear of the regulation band while staying above the
#: open-circuit voltage of even a fully charged battery (~12.75 V).
CHARGING_FLOOR_V = 13.0

#: The regulation band a healthy charging system holds while the engine runs. Below is an
#: alternator or belt that is not keeping up; above is a regulator boiling the electrolyte.
CHARGING_HEALTHY_MIN_V = 13.2
CHARGING_HEALTHY_MAX_V = 14.8

#: Open-circuit volts to percent for a 12 V flooded lead-acid battery at roughly 20 °C, as
#: published in essentially every battery manufacturer's service data. Interpolated linearly
#: between points, which is accurate to a point or two across this range.
#:
#: Temperature is deliberately not compensated. The correction is about -0.01 V/°C on a cold
#: battery, and the coolant and intake sensors read the engine rather than the battery, so
#: applying it would trade a known small error for an unknown one.
_SOC_CURVE: tuple[tuple[float, float], ...] = (
    (11.36, 0.0),
    (11.51, 10.0),
    (11.66, 20.0),
    (11.81, 30.0),
    (11.96, 40.0),
    (12.10, 50.0),
    (12.24, 60.0),
    (12.37, 70.0),
    (12.50, 80.0),
    (12.62, 90.0),
    (12.73, 100.0),
)

#: The adapter's quantisation. Every reading is a multiple of this.
_ADAPTER_RESOLUTION_V = 0.1

#: How long the battery must have been off charge before surface charge has decayed enough
#: to call the reading a settled open-circuit voltage. Real settling takes hours; this is
#: the threshold past which the estimate stops being called provisional.
_SETTLED_REST_S = 1800.0

#: A drop this far below the resting level is the starter motor, not a measurement to feed
#: into a charge curve.
_CRANK_SAG_V = 1.0


@dataclass(frozen=True, slots=True)
class VoltageSample:
    """The three fields of a sample this module reads."""

    at: datetime
    volts: float
    rpm: float | None = None


def state_of_charge_pct(volts: float) -> float:
    """Interpolate :data:`_SOC_CURVE`, clamped to 0-100 outside it."""
    if volts <= _SOC_CURVE[0][0]:
        return 0.0
    if volts >= _SOC_CURVE[-1][0]:
        return 100.0
    for (v_low, pct_low), (v_high, pct_high) in pairwise(_SOC_CURVE):
        if v_low <= volts <= v_high:
            span = v_high - v_low
            return pct_low + (pct_high - pct_low) * ((volts - v_low) / span)
    return 100.0


def _soc_uncertainty_pct(volts: float) -> float:
    """How much of the charge scale one adapter step covers at *volts*.

    The curve is steepest in the middle, so the same 0.1 V is worth ~7 points at 12.1 V and
    rather less near the ends. Deriving it from the curve keeps the two consistent.
    """
    half = _ADAPTER_RESOLUTION_V / 2
    spread = state_of_charge_pct(volts + half) - state_of_charge_pct(volts - half)
    return round(max(spread, 1.0), 1)


def _classify(samples: Sequence[VoltageSample]) -> tuple[list[VoltageSample], list[VoltageSample]]:
    charging = [s for s in samples if s.volts >= CHARGING_FLOOR_V]
    resting = [s for s in samples if s.volts < CHARGING_FLOOR_V]
    return charging, resting


def _resting_runs(samples: Sequence[VoltageSample]) -> list[list[VoltageSample]]:
    """Split into the *contiguous* stretches where nothing was charging the battery.

    Contiguity is the point. A drive can hold two of these -- one before the engine was
    cranked and one after it stopped -- and treating them as a single pool would put the
    whole drive between the first and last resting reading, reporting a quarter of an hour
    of rest for a battery that has had twenty seconds of it.
    """
    runs: list[list[VoltageSample]] = []
    current: list[VoltageSample] = []
    for sample in samples:
        if sample.volts < CHARGING_FLOOR_V:
            current.append(sample)
        elif current:
            runs.append(current)
            current = []
    if current:
        runs.append(current)
    return runs


def _cranking(resting: Sequence[VoltageSample]) -> float | None:
    """The lowest reading that looks like a starter draw rather than a state of charge."""
    if len(resting) < 2:
        return None
    lowest = min(s.volts for s in resting)
    typical = sorted(s.volts for s in resting)[len(resting) // 2]
    return lowest if typical - lowest >= _CRANK_SAG_V else None


def _charging_verdict(charging: Sequence[VoltageSample]) -> dict[str, object] | None:
    if not charging:
        return None
    volts = [s.volts for s in charging]
    peak = max(volts)
    typical = sorted(volts)[len(volts) // 2]
    if typical < CHARGING_HEALTHY_MIN_V:
        state = "low"
        summary = (
            f"The charging voltage held around {typical:.1f} V while the engine ran, below the "
            f"{CHARGING_HEALTHY_MIN_V:.1f} V a healthy alternator maintains. Worth checking the "
            "belt, the alternator and the battery terminals."
        )
    elif peak > CHARGING_HEALTHY_MAX_V:
        state = "high"
        summary = (
            f"The charging voltage reached {peak:.1f} V, above the "
            f"{CHARGING_HEALTHY_MAX_V:.1f} V ceiling for a healthy regulator. Sustained "
            "overcharging shortens battery life."
        )
    else:
        state = "healthy"
        summary = (
            f"The alternator held {typical:.1f} V while the engine ran, inside the "
            f"{CHARGING_HEALTHY_MIN_V:.1f}-{CHARGING_HEALTHY_MAX_V:.1f} V band a healthy "
            "charging system maintains."
        )
    return {
        "state": state,
        "summary": summary,
        "typical_v": round(typical, 2),
        "min_v": round(min(volts), 2),
        "max_v": round(peak, 2),
        "sample_count": len(volts),
    }


def estimate(samples: Iterable[VoltageSample]) -> dict[str, object] | None:
    """Everything the voltage trace of one drive supports saying about the battery.

    ``None`` when no sample carried a voltage at all. Otherwise a dict whose
    ``state_of_charge`` is itself ``None`` whenever the engine never stopped inside the
    drive -- which is the common case for an interrupted drive, and is a genuine "cannot
    know from this" rather than a failure.
    """
    ordered = sorted((s for s in samples if s.volts is not None), key=lambda s: s.at)
    if not ordered:
        return None

    charging, resting = _classify(ordered)
    crank_v = _cranking(resting)

    # Cranking readings are the starter motor's draw, not a charge level, so they are kept
    # out of the runs entirely rather than merely ignored at the end.
    runs = [
        [s for s in run if crank_v is None or s.volts > crank_v] for run in _resting_runs(ordered)
    ]
    trailing = next((run for run in reversed(runs) if run), None)

    result: dict[str, object] = {
        "observed_min_v": round(min(s.volts for s in ordered), 2),
        "observed_max_v": round(max(s.volts for s in ordered), 2),
        "last_v": round(ordered[-1].volts, 2),
        "charging": _charging_verdict(charging),
        "cranking_dip_v": round(crank_v, 2) if crank_v is not None else None,
        "state_of_charge": None,
    }

    if trailing is None:
        result["state_of_charge_unavailable_reason"] = (
            "The engine was running for every reading in this drive, so the voltage only "
            "shows what the alternator was doing. State of charge needs a reading taken "
            "with the engine off."
        )
        return result

    # The last reading of the run, not an average of it. Surface charge decays downwards
    # throughout, so every earlier reading is more inflated than the one after it and
    # averaging would deliberately reintroduce the bias the caveat is there to warn about.
    usable = trailing
    volts = usable[-1].volts
    rest_s = (usable[-1].at - usable[0].at).total_seconds()
    settled = rest_s >= _SETTLED_REST_S
    pct = state_of_charge_pct(volts)
    engine_off_confirmed = any(s.rpm == 0 for s in usable)

    if settled:
        confidence = "settled"
        caveat = ""
    elif len(usable) >= 2:
        confidence = "provisional"
        caveat = (
            " Taken shortly after the engine stopped, so surface charge may be flattering it "
            "by a few points."
        )
    else:
        confidence = "single_reading"
        caveat = " Based on one reading taken just after shutdown, so treat it as indicative."

    result["state_of_charge"] = {
        "percent": round(pct),
        "uncertainty_pct": _soc_uncertainty_pct(volts),
        "resting_v": round(volts, 2),
        "confidence": confidence,
        "rest_duration_s": round(rest_s, 1),
        "engine_off_confirmed": engine_off_confirmed,
        "sample_count": len(usable),
        "measured_at": usable[-1].at.isoformat(),
        "summary": (
            f"About {round(pct)} % charged, from a resting {volts:.2f} V after the engine "
            f"stopped.{caveat}"
        ),
    }
    return result


def samples_from_rows(rows: Iterable[object]) -> list[VoltageSample]:
    """Adapt ``OBDSample`` rows, skipping any that carry no voltage."""
    out: list[VoltageSample] = []
    for row in rows:
        volts = getattr(row, "adapter_voltage_v", None)
        at = getattr(row, "captured_at", None)
        if volts is None or at is None:
            continue
        out.append(VoltageSample(at=at, volts=float(volts), rpm=getattr(row, "engine_rpm", None)))
    return out
