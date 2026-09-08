import type { CarPlayTimingMinute } from './types'

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
