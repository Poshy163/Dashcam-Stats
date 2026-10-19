import type { OBDDriveSeries } from './api'

type FaultEvidence = Pick<OBDDriveSeries, 'drive' | 'samples' | 'diagnostics' | 'diagnosticTotal' | 'diagnosticPage' | 'sampling'>

/** Positive observations survive a later lamp-off reply; missing evidence stays unknown. */
export function obdDiagnosticState(data: FaultEvidence, filtered = false) {
  const lamps: boolean[] = []
  const counts: number[] = []
  const addCount = (value: unknown) => {
    if (typeof value === 'number' && Number.isInteger(value) && value >= 0 && value <= 127) counts.push(value)
  }
  for (const sample of data.samples) {
    if (typeof sample.milOn === 'boolean') lamps.push(sample.milOn)
    addCount(sample.dtcCount)
  }
  let scanComplete = false
  for (const event of data.diagnostics) {
    if (event.kind === 'mil_state' && typeof event.payload.on === 'boolean') lamps.push(event.payload.on)
    if (event.kind === 'readiness') addCount(event.payload.confirmedDtcCount)
    if (event.kind === 'dtc_scan_complete') {
      const modes = event.payload.modes
      scanComplete ||= Array.isArray(modes) && [3, 7, 10].every(mode => modes.includes(mode))
    }
  }
  const lampOn = lamps.includes(true) ? true : lamps.includes(false) ? false : null
  const storedCount = counts.length ? Math.max(...counts) : null
  const warning = lampOn === true || (storedCount !== null && storedCount > 0)
  const codes = [...new Set(data.drive.dtcsObserved)]
  const partialEvidence = filtered || data.sampling?.downsampled === true ||
    (data.diagnosticPage ?? 1) > 1 || (data.diagnosticTotal ?? data.diagnostics.length) > data.diagnostics.length
  const codeStatus = codes.length ? 'Codes recorded' : scanComplete ? 'No codes returned' :
    partialEvidence ? 'No codes recorded in summary' : 'Codes not retrieved'
  return { lampOn, storedCount, warning, codes, scanComplete, partialEvidence, codeStatus }
}
