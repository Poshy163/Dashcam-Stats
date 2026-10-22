import type { DashcamTemperatureObservation, DashcamTemperaturePoint } from './types'

export const QUIET_ANALYSIS_LIMIT_C = 85

export function temperatureValue(observation: DashcamTemperatureObservation | null | undefined): number | null {
  const value = observation?.socC
  return observation?.quality === 'valid' && value != null && Number.isFinite(value) && value >= -40 && value <= 200
    ? value : null
}

/** Nulls, capture gaps and restarted/changed capture segments never imply continuity. */
export function temperatureSegments(points: DashcamTemperaturePoint[], gapThresholdS: number, pointsOnly = false): number[][] {
  const segments: number[][] = []
  let run: number[] = []
  const flush = () => { if (run.length) segments.push(run); run = [] }
  const gapMs = Number.isFinite(gapThresholdS) && gapThresholdS > 0 ? gapThresholdS * 1_000 : 90_000
  for (let index = 0; index < points.length; index++) {
    const point = points[index]!
    const at = Date.parse(point.occurredAt)
    if (temperatureValue(point) == null || !Number.isFinite(at)) { flush(); continue }
    const previous = run.length ? points[run[run.length - 1]!] : undefined
    const elapsed = previous ? at - Date.parse(previous.occurredAt) : 0
    // The server assigns segments on the full source stream before reducing buckets.
    // A proven continuous segment may have widely separated retained extrema; applying
    // the raw sampling cadence to those summaries would invent gaps in captured data.
    const provenContinuous = typeof point.segmentId === 'string' && point.segmentId.trim().length > 0 && point.segmentId === previous?.segmentId
    if (pointsOnly || (previous && (elapsed <= 0 || (!provenContinuous && elapsed > gapMs) || point.segmentId !== previous.segmentId || point.sessionId !== previous.sessionId))) flush()
    run.push(index)
    if (pointsOnly) flush()
  }
  flush()
  return segments
}

export function temperatureHistoryIsOld(latest: DashcamTemperatureObservation | null, nowMs: number, gapThresholdS: number): boolean {
  if (!latest) return true
  const age = nowMs - Date.parse(latest.occurredAt)
  return !Number.isFinite(age) || age < -30_000 || age > Math.max(90, gapThresholdS) * 1_000
}
