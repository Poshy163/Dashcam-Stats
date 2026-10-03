import { useEffect, useReducer } from 'react'

import type { IngestStatus } from './api'

type SleepState = 'offline' | 'unknown' | 'stale' | 'not_running' | 'estimated' | 'elapsed'

export interface SleepCountdown {
  state: SleepState
  remainingS: number | null
  hint: string
}

/** Refresh parked estimates before their evidence expires, without extending its TTL. */
export function sleepStatusRefetchInterval(
  status: IngestStatus | undefined,
  runningInterval: number,
  idleInterval: number,
): number {
  const normal = status?.state === 'running' ? runningInterval : idleInterval
  if (!status?.unitOnline || status.ignitionState === 'on') return normal
  let interval = Math.min(normal, 5_000)
  const runtimeBudget = typeof status.unitObservationTtlS === 'number' &&
    typeof status.unitObservationAgeS === 'number'
    ? status.unitObservationTtlS - status.unitObservationAgeS : undefined
  for (const budget of [runtimeBudget, status.sleepCountdownValidForS]) {
    if (typeof budget === 'number' && Number.isFinite(budget)) {
      interval = Math.min(interval, Math.max(1_000, budget * 500))
    }
  }
  return interval
}

/** The server estimates the timer; a fresh API response alone is not a fresh unit read. */
export function sleepCountdown(
  status: IngestStatus | undefined,
  receivedAt: number,
  now: number,
  requestFailed = false,
): SleepCountdown {
  if (!status?.unitOnline) {
    return { state: 'offline', remainingS: null, hint: 'Dashcam is not connected' }
  }
  const elapsedS = Math.max(0, now - receivedAt) / 1000
  const age = status.unitObservationAgeS
  const ttl = status.unitObservationTtlS
  const observationFresh = receivedAt > 0 && status.unitObservationFresh === true &&
    typeof age === 'number' && Number.isFinite(age) && age >= 0 &&
    typeof ttl === 'number' && Number.isFinite(ttl) && ttl > 0 && age + elapsedS <= ttl
  if (requestFailed || !observationFresh) {
    return {
      state: 'stale', remainingS: null,
      hint: requestFailed ? 'Could not refresh the sleep estimate' : 'Waiting for a fresh reading from the dashcam',
    }
  }
  if (status.ignitionState === 'on' && status.sleepCountdownSource === 'not_running') {
    return { state: 'not_running', remainingS: null, hint: 'The sleep window starts after ignition off' }
  }
  if (status.ignitionState !== 'off' || status.sleepCountdownSource !== 'estimated' ||
      status.sleepCountdownRemainingS === null || !Number.isFinite(status.sleepCountdownRemainingS)) {
    return { state: 'unknown', remainingS: null, hint: status.sleepCountdownReason || 'The current sleep time is not known' }
  }
  const validForS = status.sleepCountdownValidForS
  // Unit evidence can expire sooner than the live power observation. Older servers
  // without this field expose only server-observed estimates and keep the TTL above.
  if ((validForS !== undefined || status.sleepCountdownEvidenceSource === 'unit') &&
      (typeof validForS !== 'number' || !Number.isFinite(validForS) || validForS <= elapsedS)) {
    return { state: 'stale', remainingS: null, hint: 'Waiting for fresh sleep timing from the dashcam' }
  }
  const remainingS = Math.max(0, status.sleepCountdownRemainingS - elapsedS)
  return {
    state: remainingS > 0 ? 'estimated' : 'elapsed', remainingS,
    hint: remainingS > 0
      ? status.sleepCountdownReason || 'Estimated from ignition off and the reported sleep window; the unit’s timer can differ'
      : 'Estimated window elapsed; the dashcam is still connected',
  }
}

export function useSleepCountdown(status: IngestStatus | undefined, receivedAt: number, requestFailed = false) {
  const [, redraw] = useReducer((version: number) => version + 1, 0)
  useEffect(() => {
    if (!status?.unitOnline) return
    // Redraws measure actual elapsed time, including browser suspension or throttling.
    const timer = setInterval(redraw, 1000)
    return () => clearInterval(timer)
  }, [status?.unitOnline])
  return sleepCountdown(status, receivedAt, Date.now(), requestFailed)
}
