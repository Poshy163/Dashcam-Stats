import type { DashcamTemperatureHistory, DashcamTemperatureObservation, DashcamTemperaturePoint } from './types'

// Versioned reference only: historical captures do not identify the running companion.
export const QUIET_ANALYSIS_START_C = 95
export const QUIET_ANALYSIS_HARD_STOP_C = 100

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

function recentTimestamp(value: string | null | undefined, nowMs: number): boolean {
  const age = typeof value === 'string' ? nowMs - Date.parse(value) : NaN
  return Number.isFinite(age) && age >= -30_000 && age <= 90_000
}

/** Never turn a cached running flag into proof that the camera is still capturing. */
export function temperatureCapturePresentation(data: DashcamTemperatureHistory, nowMs: number, refreshFailed = false): {
  label: string
  detail: string
  warning: boolean
  recognised: boolean
} {
  const collector = data.collector
  if (!collector) return {
    label: 'On-device capture state not reported',
    detail: 'Older companions use timing diagnostics. Saved readings may arrive later; their presence does not prove continuous capture.',
    warning: false,
    recognised: false,
  }
  if (collector.schemaVersion !== 1 || collector.source !== 'companion_temperature_v1' || collector.sensor !== 'soc-thmzone'
    || collector.intervalMs !== 30_000 || !Number.isInteger(collector.retainedReadings) || collector.retainedReadings < 0 || collector.retainedReadings > 8_192
    || !['starting', 'running', 'sensor_unavailable', 'storage_unavailable', 'clock_unavailable'].includes(collector.state)
    || !(collector.error === null || ['sensor_unavailable', 'journal_unavailable', 'mirror_unavailable', 'clock_unavailable'].includes(collector.error))) return {
    label: 'On-device capture report not recognised',
    detail: 'Current capture is unknown. The graph still shows the retained observations available to the server.',
    warning: true,
    recognised: false,
  }
  if (refreshFailed || !recentTimestamp(collector.checkedAtUtc, nowMs)) return {
    label: 'Capture report is stale · current capture unknown',
    detail: 'The last saved report cannot confirm whether the camera is capturing now. New readings may still be waiting on the camera.',
    warning: true,
    recognised: true,
  }
  if (collector.state === 'clock_unavailable' || collector.error === 'clock_unavailable') return {
    label: 'Camera time unavailable in the last report',
    detail: 'New readings could not be given a reliable capture time and are excluded from this graph. Earlier timestamped observations remain visible.',
    warning: true,
    recognised: true,
  }
  if (collector.state === 'sensor_unavailable' || collector.error === 'sensor_unavailable') return {
    label: 'Sensor unavailable in the last report',
    detail: 'No temperature is inferred for unavailable readings. Earlier captured temperatures remain in the graph.',
    warning: true,
    recognised: true,
  }
  if (collector.state === 'storage_unavailable' || collector.error !== null) return {
    label: 'Temperature storage unavailable in the last report',
    detail: 'Some captured readings may not have been saved or made available for import. Earlier history remains visible.',
    warning: true,
    recognised: true,
  }
  if (collector.state === 'starting') return {
    label: 'On-device collector starting in the last report',
    detail: 'A fresh captured sample is needed before capture progress can be confirmed.',
    warning: false,
    recognised: true,
  }
  if (!recentTimestamp(collector.lastCapturedAtUtc, nowMs)) return {
    label: 'No recent captured sample · current capture unknown',
    detail: 'The collector reported running, but its latest captured timestamp is missing, old or invalid.',
    warning: true,
    recognised: true,
  }
  return {
    label: 'On-device capture recently confirmed',
    detail: 'The latest imported report contains a recent captured sample. This is captured evidence, not a live temperature reading.',
    warning: false,
    recognised: true,
  }
}
