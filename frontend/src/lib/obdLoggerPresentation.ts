export const LOGGER_STATUS_TTL_MS = 90_000
const CLOCK_SKEW_MS = 5_000

export function loggerTimestampMillis(value: string | null | undefined): number | null {
  if (typeof value !== 'string' || value.length > 64) return null
  const parts = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.exec(value)
  if (!parts) return null
  const year = Number(parts[1])
  const month = Number(parts[2])
  const day = Number(parts[3])
  const leapYear = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0)
  const daysInMonth = [31, leapYear ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1] ?? 0
  // Date.parse normalizes impossible dates such as February 30 instead of rejecting them.
  if (day < 1 || day > daysInMonth || Number(parts[4]) > 23 ||
      Number(parts[5]) > 59 || Number(parts[6]) > 59) return null
  const timestamp = Date.parse(value)
  return Number.isFinite(timestamp) ? timestamp : null
}

/** A successful server request does not make a cached producer report current. */
export function loggerEvidenceFreshness(
  updatedAtUtc: string | null | undefined,
  unitOnline: boolean | null | undefined,
  nowMs = Date.now(),
): { current: boolean; label: string; hint: string; tone: 'default' | 'warn' | 'ok' } {
  if (unitOnline === false) return {
    current: false, label: 'Dashcam offline', tone: 'default',
    hint: 'The dashcam is offline. Connection, engine and BLE ownership values are historical.',
  }
  if (unitOnline !== true) return {
    current: false, label: 'Connection unconfirmed', tone: 'default',
    hint: 'Dashcam connectivity has not been confirmed. Current connection and ownership states are unknown.',
  }
  const timestamp = loggerTimestampMillis(updatedAtUtc)
  if (timestamp === null || !Number.isFinite(nowMs)) return {
    current: false, label: 'Status time unavailable', tone: 'warn',
    hint: 'The dashcam is online, but the logger report has no valid producer time.',
  }
  const age = nowMs - timestamp
  if (age < -CLOCK_SKEW_MS) return {
    current: false, label: 'Logger clock mismatch', tone: 'warn',
    hint: 'The logger report is dated in the future. Current connection and ownership states cannot be confirmed.',
  }
  if (age > LOGGER_STATUS_TTL_MS) return {
    current: false, label: 'Logger status stale', tone: 'warn',
    hint: 'The dashcam is online, but the logger has not published a fresh report in over 90 seconds.',
  }
  return {
    current: true, label: 'Fresh logger report', tone: 'ok',
    hint: 'The dashcam is online and the logger report is recent.',
  }
}
