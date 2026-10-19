export type JourneyDatePreset = 'today' | 'yesterday' | 'last7'

/** Work with calendar days, so DST changes do not shorten or lengthen a preset. */
export function journeyDatePreset(preset: JourneyDatePreset, timeZone: string, now = new Date()) {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone, year: 'numeric', month: '2-digit', day: '2-digit',
  }).formatToParts(now)
  const part = (type: string) => parts.find((value) => value.type === type)!.value
  const today = `${part('year')}-${part('month')}-${part('day')}`
  const shift = (days: number) => {
    const date = new Date(`${today}T12:00:00Z`)
    date.setUTCDate(date.getUTCDate() + days)
    return date.toISOString().slice(0, 10)
  }
  if (preset === 'yesterday') return { dateFrom: shift(-1), dateTo: shift(-1) }
  return { dateFrom: preset === 'last7' ? shift(-6) : today, dateTo: today }
}

export function journeyDateRangeError(dateFrom: string, dateTo: string): string | null {
  for (const value of [dateFrom, dateTo]) {
    if (!value) continue
    const date = new Date(`${value}T00:00:00Z`)
    if (!/^\d{4}-\d{2}-\d{2}$/.test(value) || !Number.isFinite(date.getTime()) || date.toISOString().slice(0, 10) !== value) {
      return 'Choose valid dates in YYYY-MM-DD format.'
    }
  }
  return dateFrom && dateTo && dateFrom > dateTo ? 'From date must be on or before To date.' : null
}
