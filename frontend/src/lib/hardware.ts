import type { HardwareInfo } from './types'

/** Describe the selected runtime without mistaking an installed GPU for its use. */
export function hardwareSummary(hardware: HardwareInfo) {
  const runtime = hardware.inference.backend
  const inference = runtime
    ? runtime.available
      ? [runtime.engine ?? runtime.using, runtime.device ?? 'Device not reported'].filter(Boolean).join(' · ')
      : 'Unavailable'
    : 'Device not reported'
  const decoder = hardware.policy?.decode
  const decode = decoder === 'software' ? 'Software' : decoder ? decoder.toUpperCase() : 'Not reported'
  const notes = [...hardware.notes]
  if (hardware.policy?.gpuInferenceDisabled) {
    const fallback = runtime?.available && runtime.device
      ? `using ${runtime.device}`
      : 'the fallback device has not been reported'
    notes.push(`GPU inference is disabled; ${fallback}. See Advanced diagnostics for details.`)
  }
  if (runtime?.available === false) notes.push('No inference runtime is available.')
  return { inference, decode, decodeReason: hardware.policy?.decodeReason, notes }
}
