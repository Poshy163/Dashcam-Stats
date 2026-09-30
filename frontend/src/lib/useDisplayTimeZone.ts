import { useLayoutEffect, useReducer } from 'react'
import { setDisplayTimeZone } from './format'

/** Re-render formatted dates before paint when asynchronously loaded settings change. */
export function useDisplayTimeZone(zone: string | undefined) {
  const [, refreshDates] = useReducer((version: number) => version + 1, 0)
  useLayoutEffect(() => {
    setDisplayTimeZone(zone)
    refreshDates()
  }, [zone])
}
