import type { OBDSeriesSample } from './api'

const SIGNALS: Record<string, { field: keyof OBDSeriesSample; pid: string | null }> = {
  vehicle_speed: { field: 'vehicleSpeedKmh', pid: '0D' }, engine_rpm: { field: 'engineRpm', pid: '0C' },
  mass_air_flow: { field: 'massAirFlowGS', pid: '10' }, engine_load: { field: 'engineLoadPct', pid: '04' },
  coolant_temperature: { field: 'coolantTemperatureC', pid: '05' }, intake_air_temperature: { field: 'intakeAirTemperatureC', pid: '0F' },
  throttle_position: { field: 'throttlePositionPct', pid: '11' }, timing_advance: { field: 'timingAdvanceDeg', pid: '0E' },
  short_term_fuel_trim_bank_1: { field: 'shortTermFuelTrimPct', pid: '06' }, long_term_fuel_trim_bank_1: { field: 'longTermFuelTrimPct', pid: '07' },
  oxygen_sensor_1_voltage: { field: 'oxygenSensor1VoltageV', pid: '14' }, oxygen_sensor_2_voltage: { field: 'oxygenSensor2VoltageV', pid: '15' },
  adapter_voltage: { field: 'adapterVoltageV', pid: null },
  estimated_fuel_rate: { field: 'estimatedFuelRateLH', pid: '10' }, estimated_fuel_consumption: { field: 'estimatedFuelConsumptionL100km', pid: '10' },
}

export type SignalTimeBasis = 'reply_completion' | 'legacy_row' | 'row_recorded' | 'unknown'
export interface SignalTimeline {
  timesIso: (string | null)[]
  elapsedS: (number | null)[]
  basis: SignalTimeBasis[]
  breakBefore: boolean[]
}

const transportSafe = (sample: OBDSeriesSample) => sample.ecuDataStatus !== 'last_known' &&
  (sample.quality?.transport == null || sample.quality.transport === 'ok')

/** A reply age belongs to its PID, never to the whole serialized row. */
export function signalObservation(sample: OBDSeriesSample, signal: string, pollPlanVersion = 7): { at: number | null; basis: SignalTimeBasis } {
  const spec = SIGNALS[signal]
  const value = spec ? sample[spec.field] : null
  const rowAt = Date.parse(sample.t)
  if (!spec || value == null || typeof value !== 'number' || !Number.isFinite(value) || !Number.isFinite(rowAt) || !transportSafe(sample)) return { at: null, basis: 'unknown' }
  if (spec.pid == null) return { at: rowAt, basis: 'row_recorded' }
  if (sample.quality?.missingPids?.includes(Number.parseInt(spec.pid, 16))) return { at: null, basis: 'unknown' }
  const quality = sample.quality
  if (quality != null && (typeof quality !== 'object' || Array.isArray(quality))) return { at: null, basis: 'unknown' }
  const explicit = quality != null && Object.hasOwn(quality, 'pidResponseAgeMs')
  if (!explicit) return pollPlanVersion <= 7 ? { at: rowAt, basis: 'legacy_row' } : { at: null, basis: 'unknown' }
  const ages = quality.pidResponseAgeMs
  if (ages == null || typeof ages !== 'object' || Array.isArray(ages)) return { at: null, basis: 'unknown' }
  const age = Object.hasOwn(ages, spec.pid) ? ages[spec.pid] : undefined
  if (typeof age !== 'number' || !Number.isInteger(age) || age < 0 || age > 60_000) return { at: null, basis: 'unknown' }
  if (signal === 'estimated_fuel_consumption' && signalObservation(sample, 'vehicle_speed', pollPlanVersion).at == null) return { at: null, basis: 'unknown' }
  return { at: rowAt - age, basis: 'reply_completion' }
}

/** Sequence and row cadence stay separate from a signal's actual completion cadence. */
export function rowsContinuous(previous: OBDSeriesSample, current: OBDSeriesSample, nominalCycleS: number): boolean {
  const dt = (Date.parse(current.t) - Date.parse(previous.t)) / 1_000
  const legacy = [previous, current].every((sample) => sample.quality == null || !Object.hasOwn(sample.quality, 'pidResponseAgeMs'))
  const sequencesAbsent = legacy && previous.sequence == null && current.sequence == null
  const consecutive = Number.isSafeInteger(previous.sequence) && previous.sequence >= 0 && Number.isSafeInteger(current.sequence) && current.sequence === previous.sequence + 1
  return Number.isFinite(nominalCycleS) && nominalCycleS > 0 && transportSafe(previous) && transportSafe(current) && (sequencesAbsent || consecutive) && dt > 0 && dt <= nominalCycleS * 1.5
}

export function buildSignalTimings(samples: OBDSeriesSample[], options: {
  pollPlanVersion: number; nominalCycleS: number; startedAt: string; start?: string; end?: string
}): Record<string, SignalTimeline> {
  const origin = Date.parse(options.startedAt)
  const windowStart = options.start ? Date.parse(options.start) : -Infinity
  const windowEnd = options.end ? Date.parse(options.end) : Infinity
  return Object.fromEntries(Object.keys(SIGNALS).map((signal) => {
    let lastKnown = -Infinity
    const timeline: SignalTimeline = { timesIso: [], elapsedS: [], basis: [], breakBefore: [] }
    for (const [index, sample] of samples.entries()) {
      const observation = signalObservation(sample, signal, options.pollPlanVersion)
      const field = SIGNALS[signal]!.field
      const invalidMeasurement = sample[field] != null && observation.at == null
      const at = observation.at
      const monotonic = at != null && at > lastKnown
      if (at != null && monotonic) lastKnown = at
      const known = monotonic && at != null && at >= windowStart && at <= windowEnd && Number.isFinite(origin)
      timeline.timesIso.push(known ? new Date(at).toISOString() : null)
      timeline.elapsedS.push(known ? (at - origin) / 1_000 : null)
      timeline.basis.push(known ? observation.basis : 'unknown')
      timeline.breakBefore.push(index === 0 || !rowsContinuous(samples[index - 1]!, sample, options.nominalCycleS) || invalidMeasurement || (at != null && !monotonic))
    }
    return [signal, timeline]
  }))
}

/** Null scheduled slots do not invent observations; invalid timing explicitly breaks a run. */
export function signalSegments(values: (number | null)[], timeline: SignalTimeline, cadenceS: number, downsampled: boolean): number[][] {
  const result: number[][] = []
  let run: number[] = []
  const flush = () => { if (run.length) result.push(run); run = [] }
  values.forEach((value, index) => {
    if (timeline.breakBefore[index]) flush()
    const at = timeline.elapsedS[index]
    if (value == null || !Number.isFinite(value) || at == null || !Number.isFinite(at)) { if (value != null) flush(); return }
    const previous = run.length ? timeline.elapsedS[run[run.length - 1]!] : null
    const isolated = downsampled || !Number.isFinite(cadenceS) || cadenceS <= 0
    if (isolated || (previous != null && (at <= previous || at - previous > cadenceS * 1.5))) flush()
    run.push(index)
    if (isolated) flush()
  })
  flush()
  return result
}

export interface DrivingStats {
  movingAverageKmh: number | null; stoppedShare: number | null; maxAccelKmhS: number | null; maxBrakeKmhS: number | null; warmupS: number | null
  speedBands: { label: string; seconds: number }[]; rpmBands: { label: string; seconds: number }[]
}

export function deriveDrivingStats(samples: OBDSeriesSample[], timings: Record<string, SignalTimeline>, cadences: Record<string, number>, nominalCycleS: number, downsampled = false): DrivingStats {
  const speedEdges = [{ label: 'Stopped', match: (v: number) => v < 1 }, { label: '1–30 km/h', match: (v: number) => v >= 1 && v < 30 },
    { label: '30–50 km/h', match: (v: number) => v >= 30 && v < 50 }, { label: '50–60 km/h', match: (v: number) => v >= 50 && v < 60 }, { label: '60+ km/h', match: (v: number) => v >= 60 }]
  const rpmEdges = [{ label: 'Under 1,000', match: (v: number) => v < 1_000 }, { label: '1,000–1,500', match: (v: number) => v >= 1_000 && v < 1_500 },
    { label: '1,500–2,000', match: (v: number) => v >= 1_500 && v < 2_000 }, { label: '2,000–2,500', match: (v: number) => v >= 2_000 && v < 2_500 }, { label: '2,500+', match: (v: number) => v >= 2_500 }]
  const speedBands = speedEdges.map((band) => ({ label: band.label, seconds: 0 }))
  const rpmBands = rpmEdges.map((band) => ({ label: band.label, seconds: 0 }))
  let movingWeighted = 0; let movingTime = 0; let stoppedTime = 0; let speedTime = 0
  let maxAccelKmhS: number | null = null; let maxBrakeKmhS: number | null = null; let warmupS: number | null = null
  if (downsampled) return { movingAverageKmh: null, stoppedShare: null, maxAccelKmhS, maxBrakeKmhS, warmupS, speedBands, rpmBands }
  const interval = (signal: string, index: number): number | null => {
    const timeline = timings[signal]
    const current = timeline?.elapsedS[index]; const previous = timeline?.elapsedS[index - 1]
    if (index === 0 || timeline?.breakBefore[index] || current == null || previous == null || !rowsContinuous(samples[index - 1]!, samples[index]!, nominalCycleS)) return null
    const dt = current - previous
    return dt > 0 && dt <= (cadences[signal] ?? nominalCycleS) * 1.5 ? dt : null
  }
  samples.forEach((sample, index) => {
    const speedDt = interval('vehicle_speed', index)
    const speed = sample.vehicleSpeedKmh; const previousSpeed = samples[index - 1]?.vehicleSpeedKmh
    if (speed != null && previousSpeed != null && speedDt != null) {
      speedTime += speedDt
      const band = speedEdges.findIndex((edge) => edge.match(speed))
      if (band >= 0) speedBands[band]!.seconds += speedDt
      if (speed < 1) stoppedTime += speedDt
      else { movingWeighted += speed * speedDt; movingTime += speedDt }
      const accel = (speed - previousSpeed) / speedDt
      if (accel >= 0) maxAccelKmhS = maxAccelKmhS == null ? accel : Math.max(maxAccelKmhS, accel)
      else maxBrakeKmhS = maxBrakeKmhS == null ? -accel : Math.max(maxBrakeKmhS, -accel)
    }
    const rpmDt = interval('engine_rpm', index)
    if (sample.engineRpm != null && samples[index - 1]?.engineRpm != null && rpmDt != null) {
      const band = rpmEdges.findIndex((edge) => edge.match(sample.engineRpm!))
      if (band >= 0) rpmBands[band]!.seconds += rpmDt
    }
    const coolantAt = timings.coolant_temperature?.elapsedS[index]
    if (warmupS == null && sample.coolantTemperatureC != null && sample.coolantTemperatureC >= 80 &&
      samples[0]?.coolantTemperatureC != null && samples[0].coolantTemperatureC < 80 && coolantAt != null) warmupS = coolantAt
  })
  return { movingAverageKmh: movingTime ? movingWeighted / movingTime : null, stoppedShare: speedTime ? stoppedTime / speedTime : null,
    maxAccelKmhS, maxBrakeKmhS, warmupS, speedBands, rpmBands }
}
