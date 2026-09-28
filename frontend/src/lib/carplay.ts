import type { CarPlayDiagnosticContext, CarPlayGpsContext, CarPlayLocationProvider, CarPlayTimingEvent, CarPlayTimingMinute } from './types'

export type TimingPeriod = {
  id: string
  sessionId: string | null
  start: number
  end: number
}

/** A gap separates observations; it does not establish an ignition or trip boundary. */
export function timingPeriods(minutes: Pick<CarPlayTimingMinute, 'bucketStart' | 'sessionId'>[]): TimingPeriod[] {
  const captures = new Map<string | null, number[]>()
  for (const minute of minutes) {
    const session = minute.sessionId ?? null
    const times = captures.get(session) ?? []
    times.push(Math.floor(Date.parse(minute.bucketStart) / 60_000) * 60_000)
    captures.set(session, times)
  }
  const periods: TimingPeriod[] = []
  for (const [sessionId, times] of captures) {
    let period: TimingPeriod | undefined
    for (const time of [...new Set(times)].filter(Number.isFinite).sort((a, b) => a - b)) {
      if (!period || time - period.end > 90_000) {
        period = { id: `${sessionId ?? 'legacy'}:${time}`, sessionId, start: time, end: time }
        periods.push(period)
      } else {
        period.end = time
      }
    }
  }
  return periods.sort((a, b) => a.start - b.start)
}

export function inTimingPeriod(time: string, sessionId: string | null | undefined, period: TimingPeriod): boolean {
  const at = Date.parse(time)
  return (sessionId ?? null) === period.sessionId && at >= period.start && at < period.end + 60_000
}

/** Never connect unknown values, missing minutes or a restarted sampler on a chart. */
export function timingSegments(minutes: CarPlayTimingMinute[], values: (number | null)[]): number[][] {
  const segments: number[][] = []
  let current: number[] = []
  for (let i = 0; i < minutes.length; i++) {
    const minute = minutes[i]!
    const previous = minutes[i - 1]
    const value = values[i]
    if (value == null || !Number.isFinite(value) || (previous && (
      Date.parse(minute.bucketStart) - Date.parse(previous.bucketStart) > 90_000 ||
      (minute.sessionId ?? null) !== (previous.sessionId ?? null)
    ))) {
      if (current.length) segments.push(current)
      current = []
    }
    if (value != null && Number.isFinite(value)) current.push(i)
  }
  if (current.length) segments.push(current)
  return segments
}

export function meanKnown(values: (number | null | undefined)[]): number | null {
  const known = values.filter((value): value is number => value != null && Number.isFinite(value))
  return known.length ? known.reduce((total, value) => total + value, 0) / known.length : null
}

export function maxKnown(values: (number | null | undefined)[]): number | null {
  const known = values.filter((value): value is number => value != null && Number.isFinite(value))
  return known.length ? Math.max(...known) : null
}

export function radioObservation(ap: number | null | undefined, sta: number | null | undefined): string {
  if (ap == null || sta == null) return 'Both radio frequencies were not reported.'
  return ap === sta
    ? 'Hotspot and Wi-Fi reported the same frequency.'
    : 'Hotspot and Wi-Fi reported different frequencies; this alone does not prove radio contention.'
}

/** Missing or failed captures cannot establish a fresh fix or an enabled provider. */
export function gpsCaptureAvailable(event: CarPlayGpsContext): boolean {
  return event.gpsCaptureStatus === 'ok'
}

export function gpsCaptureLabel(event: CarPlayGpsContext): string {
  switch (event.gpsCaptureStatus) {
    case 'ok': return 'Captured'
    case 'dump_error': return 'Read failed'
    case 'unsupported': return 'Unsupported'
    case 'limit': return 'Capture limit reached'
    case 'parser_error': return 'Could not read measurements'
    case 'tool_unavailable': return 'Capture unavailable'
    default: return 'Unknown'
  }
}

export function diagnosticFlag(value: boolean | number | null | undefined): boolean | null {
  if (value === true || value === 1) return true
  if (value === false || value === 0) return false
  return null
}

function nonnegative(value: number | null | undefined): number | null {
  return value != null && Number.isFinite(value) && value >= 0 ? value : null
}

export function gpsProviderObservation(event: CarPlayGpsContext, provider: CarPlayLocationProvider) {
  const available = gpsCaptureAvailable(event)
  return {
    present: available ? diagnosticFlag(event[`loc${provider}Present`]) : null,
    enabled: available ? diagnosticFlag(event[`loc${provider}Enabled`]) : null,
    ageMs: available ? nonnegative(event[`loc${provider}AgeMs`]) : null,
    accuracyM: available ? nonnegative(event[`loc${provider}HaccM`]) : null,
    satellites: available ? nonnegative(event[`loc${provider}Satellites`]) : null,
    zlinkListener: available ? diagnosticFlag(event[`loc${provider}ZlinkListener`]) : null,
  }
}

/** A missing count or zero reports must not turn a placeholder mean into a measurement. */
export function gpsFirstFixSummary(event: CarPlayGpsContext): string {
  if (!gpsCaptureAvailable(event)) return 'Unknown'
  const count = nonnegative(event.gnssTtffReports)
  if (count == null || !Number.isInteger(count)) return 'Unknown'
  if (count === 0) return 'No first-fix reports'
  const mean = nonnegative(event.gnssTtffMeanS)
  return `${count} ${count === 1 ? 'report' : 'reports'} · mean ${mean == null ? 'unknown' : `${mean.toFixed(1)} s`}`
}

/** Keep startup visible by default, with every observation reachable in bounded pages. */
export function gpsObservationPage(events: CarPlayTimingEvent[], requestedPage: number | 'latest' = 0) {
  const captures = events.filter((event) => event.kind === 'gps_context')
    .sort((a, b) => Date.parse(a.occurredAt) - Date.parse(b.occurredAt))
  const pageSize = 12
  const total = captures.length
  const pageCount = Math.ceil(total / pageSize)
  const lastPage = Math.max(0, pageCount - 1)
  const requested = requestedPage === 'latest' ? lastPage : Number.isFinite(requestedPage) ? Math.trunc(requestedPage) : 0
  const page = Math.max(0, Math.min(requested, lastPage))
  const start = page * pageSize
  return { page, pageCount, total, start, end: Math.min(start + pageSize, total), captures: captures.slice(start, start + pageSize) }
}

/** Newer samplers identify the actual peer path; missing data is never filled from legacy sockets. */
export function peerTransportObservation(events: CarPlayDiagnosticContext[]) {
  const modern = events.filter((event) => (event.diagnosticSchema ?? 0) >= 6)
  const current = modern.length > 0
  const selected = current ? modern : events
  return {
    current,
    captures: selected.length,
    receiveQueue: maxKnown(selected.map((event) => current ? event.wirePeerRxQueueBytes : event.peerRxQueueBytes)),
    rtt: maxKnown(selected.map((event) => current ? event.wirePeerTcpRttMaxMs : event.peerRecentRttMaxMs)),
  }
}
