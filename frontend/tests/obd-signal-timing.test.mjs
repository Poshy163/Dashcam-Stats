import assert from 'node:assert/strict'
import { after, test } from 'node:test'
import { mkdtemp, rm } from 'node:fs/promises'
import { fileURLToPath, pathToFileURL } from 'node:url'
import path from 'node:path'
import { build } from 'esbuild'
import React from 'react'
import { act, create } from 'react-test-renderer'
import { renderToStaticMarkup } from 'react-dom/server'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'

const directory = await mkdtemp(fileURLToPath(new URL('../node_modules/.obd-timing-test-', import.meta.url)))
after(() => rm(directory, { recursive: true, force: true }))
await build({
  stdin: { contents: `export * from '@/lib/obdSignalTiming'; export { default as ObdDriveDetail, TimeChart } from '@/pages/ObdDriveDetail'; export { ObdLifecycleReason } from '@/components/ObdLifecycle'; export { formatDateTimeMillis, utcInputValue } from '@/lib/format';`, resolveDir: fileURLToPath(new URL('..', import.meta.url)), loader: 'tsx' },
  outfile: path.join(directory, 'timing.mjs'), bundle: true, format: 'esm', platform: 'node', jsx: 'automatic',
  external: ['react', 'react/*', 'react-router-dom', '@tanstack/react-query'], alias: { '@': fileURLToPath(new URL('../src', import.meta.url)) },
})
const { signalObservation, rowsContinuous, buildSignalTimings, signalSegments, deriveDrivingStats,
  ObdDriveDetail, TimeChart, ObdLifecycleReason, formatDateTimeMillis, utcInputValue } = await import(pathToFileURL(path.join(directory, 'timing.mjs')).href)
const base = Date.parse('2026-10-22T00:00:00Z')
const iso = seconds => new Date(base + seconds * 1000).toISOString()
const row = (seconds, sequence = 0, overrides = {}) => ({
  t: iso(seconds), sequence, ecuDataStatus: 'live', quality: { transport: 'ok', parser: 'ok' }, provenance: {},
  vehicleSpeedKmh: 30, engineRpm: 1500, massAirFlowGS: 4, estimatedFuelRateLH: 1.3,
  estimatedFuelConsumptionL100km: 4.3, adapterVoltageV: 14, coolantTemperatureC: null, ...overrides,
})
const ages = (speed = 2000, rpm = 1000, maf = 0) => ({ transport: 'ok', parser: 'ok', pidResponseAgeMs: { '0D': speed, '0C': rpm, '10': maf } })
const timelines = (rows, version = 8, extra = {}) => buildSignalTimings(rows, { pollPlanVersion: version, nominalCycleS: version === 8 ? 3 : 5, startedAt: iso(0), ...extra })

test('speed RPM and MAF keep independent reply completion times and fuel uses MAF time', () => {
  const sample = row(10, 1, { quality: ages(2200, 1300, 300) })
  assert.equal(signalObservation(sample, 'vehicle_speed', 8).at, base + 7800)
  assert.equal(signalObservation(sample, 'engine_rpm', 8).at, base + 8700)
  assert.equal(signalObservation(sample, 'mass_air_flow', 8).at, base + 9700)
  assert.equal(signalObservation(sample, 'estimated_fuel_rate', 8).at, base + 9700)
  assert.equal(signalObservation(sample, 'estimated_fuel_consumption', 8).at, base + 9700)
  assert.deepEqual(signalObservation(sample, 'adapter_voltage', 8), { at: base + 10_000, basis: 'row_recorded' })
  assert.equal(signalObservation(sample, 'obd_standard', 8).at, null, 'carried static values are not fresh replies')
})

test('only an absent legacy age map can fall back to row time', () => {
  assert.deepEqual(signalObservation(row(5), 'vehicle_speed', 7), { at: base + 5000, basis: 'legacy_row' })
  assert.equal(signalObservation(row(5), 'vehicle_speed', 8).at, null)
  for (const map of [null, [], {}, { '0d': 100 }, { '0D': -1 }, { '0D': 60001 }, { '0D': 1.5 }, { '0D': '100' }]) {
    for (const version of [7, 8]) assert.equal(signalObservation(row(5, 0, { quality: { pidResponseAgeMs: map } }), 'vehicle_speed', version).at, null)
  }
  assert.equal(signalObservation(row(70, 0, { quality: ages(60000) }), 'vehicle_speed', 8).at, base + 10_000)
  assert.equal(signalObservation(row(5, 0, { quality: ages(), vehicleSpeedKmh: null }), 'vehicle_speed', 8).at, null)
  assert.equal(signalObservation(row(5, 0, { quality: { ...ages(), missingPids: [13] } }), 'vehicle_speed', 8).at, null)
  assert.equal(signalObservation(row(5, 0, { quality: { ...ages(), transport: 'failed_after_partial' } }), 'vehicle_speed', 8).at, null)
  assert.equal(signalObservation(row(5, 0, { quality: ages(), ecuDataStatus: 'last_known' }), 'vehicle_speed', 8).at, null)
})

test('new consumption timing stays unknown if either sequential source has no valid reply time', () => {
  const sample = row(10, 0, { quality: { transport: 'ok', pidResponseAgeMs: { '10': 200 } } })
  assert.equal(signalObservation(sample, 'estimated_fuel_rate', 8).at, base + 9800)
  assert.equal(signalObservation(sample, 'estimated_fuel_consumption', 8).at, null)
})

test('per-signal filtering uses reply times and rejects nonmonotonic completions instead of row-time substitution', () => {
  const skewed = timelines([row(10, 0, { quality: ages(2200, 1300, 300) })], 8, { start: iso(8.5), end: iso(10) })
  assert.equal(skewed.vehicle_speed.timesIso[0], null)
  assert.equal(skewed.engine_rpm.timesIso[0], iso(8.7))
  const samples = [row(3, 0, { quality: ages(0) }), row(6, 1, { quality: ages(4000) }), row(9, 2, { quality: ages(0) })]
  const timing = timelines(samples)
  assert.deepEqual(timing.vehicle_speed.elapsedS, [3, null, 9])
  assert.deepEqual(signalSegments(samples.map(sample => sample.vehicleSpeedKmh), timing.vehicle_speed, 3, false), [[0], [2]])
})

test('trial and baseline cadence remain distinct and row/sequence/transport gaps cannot be hidden by reply ages', () => {
  assert.equal(rowsContinuous(row(0, 0), row(5, 1), 3), false)
  assert.equal(rowsContinuous(row(0, 0), row(5, 1), 5), true)
  assert.equal(rowsContinuous(row(0, 0), row(3, 2), 3), false)
  assert.equal(rowsContinuous(row(0, undefined, { sequence: undefined, quality: ages() }), row(3, undefined, { sequence: undefined, quality: ages() }), 3), false)
  assert.equal(rowsContinuous(row(0, 0), row(3, 1, { quality: { transport: 'failed_after_partial' } }), 3), false)
  const samples = [row(3, 0, { quality: ages(0) }), row(8, 1, { quality: ages(2000) })]
  const timing = timelines(samples)
  assert.deepEqual(timing.vehicle_speed.elapsedS, [3, 6])
  assert.deepEqual(signalSegments([30, 30], timing.vehicle_speed, 3, false), [[0], [1]], 'regular reply timestamps cannot erase a row transport gap')
})

test('acceleration and time bands use each signal clock with legacy valid-row behavior retained', () => {
  const samples = [row(3, 0, { vehicleSpeedKmh: 0, engineRpm: 900, quality: ages(2000, 1000) }),
    row(6, 1, { vehicleSpeedKmh: 12, engineRpm: 1200, quality: ages(1000, 1000) }),
    row(9, 2, { vehicleSpeedKmh: 24, engineRpm: 1800, quality: ages(2000, 1000) })]
  const result = deriveDrivingStats(samples, timelines(samples), { vehicle_speed: 3, engine_rpm: 3 }, 3)
  assert.equal(result.maxAccelKmhS, 6)
  assert.equal(result.speedBands.reduce((total, item) => total + item.seconds, 0), 6)
  assert.equal(result.rpmBands.reduce((total, item) => total + item.seconds, 0), 6)
  assert.equal(result.movingAverageKmh, 16)
  const legacy = [row(0, 0, { vehicleSpeedKmh: 0 }), row(5, 1, { vehicleSpeedKmh: 10 }), row(10, 2, { vehicleSpeedKmh: 20 })]
  const old = deriveDrivingStats(legacy, timelines(legacy, 7), { vehicle_speed: 5, engine_rpm: 5 }, 5)
  assert.equal(old.maxAccelKmhS, 2)
  assert.equal(old.movingAverageKmh, 15)
  assert.equal(old.speedBands.reduce((total, item) => total + item.seconds, 0), 10)
  for (const altered of [samples.map((sample, index) => index === 1 ? { ...sample, sequence: 8 } : sample),
    samples.map((sample, index) => index === 1 ? { ...sample, vehicleSpeedKmh: null } : sample)]) {
    assert.equal(deriveDrivingStats(altered, timelines(altered), { vehicle_speed: 3 }, 3).maxAccelKmhS, null)
  }
  const sampled = deriveDrivingStats(samples, timelines(samples), { vehicle_speed: 3 }, 3, true)
  assert.equal(sampled.maxAccelKmhS, null)
  assert.equal(sampled.movingAverageKmh, null)
  assert.equal(sampled.speedBands.reduce((total, item) => total + item.seconds, 0), 0)
})

test('sparse optional signals follow their own cadence while invalid timing and sampled output break lines', () => {
  const legacy = [row(0, 0), row(5, 1, { massAirFlowGS: null }), row(10, 2, { massAirFlowGS: null }), row(15, 3)]
  const timing = timelines(legacy, 7)
  assert.deepEqual(signalSegments(legacy.map(item => item.massAirFlowGS), timing.mass_air_flow, 15, false), [[0, 3]])
  assert.deepEqual(signalSegments(legacy.map(item => item.massAirFlowGS), timing.mass_air_flow, 15, true), [[0], [3]])
})

test('chart keyboard, tooltip markers and semantic cells disclose individual response times', async t => {
  const samples = [row(3, 0, { quality: ages(1000, 200) }), row(6, 1, { quality: ages(1000, 200) }), row(9, 2, { quality: ages(1000, 200) })]
  const timing = timelines(samples)
  let root
  await act(async () => { root = create(React.createElement(TimeChart, { title: 'Trial signals', unit: 'units', elapsedS: [3, 6, 9], timesIso: samples.map(item => item.t),
    cadences: { vehicle_speed: 3, engine_rpm: 3 }, downsampled: false, signalTimings: timing,
    series: [{ label: 'Speed', signal: 'vehicle_speed', values: [10, 20, 30], colorClass: 'text-accent' }, { label: 'RPM', signal: 'engine_rpm', values: [1000, 1100, 1200], colorClass: 'text-cyan' }],
  })) })
  t.after(async () => act(async () => root.unmount()))
  await act(async () => root.root.findByType('input').props.onChange({ target: { value: '1' } }))
  const selected = root.root.findByType('input').props['aria-valuetext']
  assert.ok(selected.includes(formatDateTimeMillis(iso(5))))
  assert.ok(selected.includes(formatDateTimeMillis(iso(5.8))))
  const markers = root.root.findAllByType('circle').filter(node => node.props.r === 3)
  assert.equal(markers.length, 2)
  assert.notEqual(markers[0].props.cx, markers[1].props.cx)
  await act(async () => root.root.findByType('details').props.onToggle({ currentTarget: { open: true } }))
  assert.match(JSON.stringify(root.toJSON()), /Reply completed/)
  assert.match(JSON.stringify(root.toJSON()), /Row saved/)
})

test('fallback reason names the trial stop and separate baseline drive', () => {
  const html = renderToStaticMarkup(React.createElement(ObdLifecycleReason, { status: 'interrupted', reason: 'poll_trial_fallback' }))
  assert.match(html, /3-second trial stopped/)
  assert.match(html, /5-second baseline selected for the next connection/)
})

test('a reply inside a time window remains selectable when its row was saved after the end', async t => {
  const samples = [row(8, 0, { quality: ages(1000, 500) }), row(11, 1, { quality: ages(1200, 300) })]
  const timing = timelines(samples, 8, { start: iso(7), end: iso(10) })
  let root
  await act(async () => { root = create(React.createElement(TimeChart, {
    title: 'Speed', unit: 'km/h', elapsedS: [8, 11], timesIso: samples.map(item => item.t),
    cadences: { vehicle_speed: 3 }, downsampled: false, signalTimings: timing, windowBounds: { startS: 7, endS: 10 },
    series: [{ label: 'Speed', signal: 'vehicle_speed', values: [30, 40], colorClass: 'text-accent' }],
  }), { createNodeMock: element => element.type === 'svg' ? { getBoundingClientRect: () => ({ width: 720, left: 0 }) } : null }) })
  t.after(async () => act(async () => root.unmount()))
  const svg = root.root.findByType('svg')
  await act(async () => svg.props.onPointerMove({ clientX: 44 + (9.8 - 7) / 3 * (720 - 44 - 10) }))
  const slider = root.root.findByType('input')
  assert.equal(slider.props.value, 1)
  assert.ok(slider.props['aria-valuetext'].includes(formatDateTimeMillis(iso(9.8))))
  const marker = root.root.findAllByType('circle').find(item => item.props.r === 3)
  assert.ok(marker.props.cx >= 44 && marker.props.cx <= 710)
})

test('drive time-window inputs accept seconds and preserve second-precision bookmarks after submission', async t => {
  const start = iso(3), end = iso(10)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity, gcTime: Infinity } } })
  const data = { drive: { id: 'timing-window', vehicleId: 'fixture', startedAt: iso(0), finishedAt: iso(15),
    lifecycleStatus: 'complete', summarySource: 'derived', connectionLossCount: 0, validationWarnings: [],
    sampleCount: 0, gapCount: 0, errorCount: 0, dtcsObserved: [], gapAnalysis: { pollPlanVersion: 8,
      nominalCycleS: 3, signals: [], transport: { totalGapDurationS: 0 } } },
    samples: [], signalMetadata: [], diagnostics: [], diagnosticTotal: 0, diagnosticPage: 1, diagnosticPages: 0,
  }
  client.setQueryData(['obd-drive', 'timing-window', start, end, 1], data)
  client.setQueryData(['obd-drive', 'timing-window', start, iso(12), 1], data)
  client.setQueryData(['obd-app-events', 'timing-window'], { pages: [{ items: [], total: 0, page: 1, pages: 1 }], pageParams: [1] })
  function Location() { return React.createElement('output', { 'data-location': true }, useLocation().search) }
  let root
  const oldFormData = globalThis.FormData
  globalThis.FormData = class { constructor(form) { this.fields = form } get(key) { return this.fields[key] } }
  t.after(async () => { await act(async () => root?.unmount()); client.clear(); globalThis.FormData = oldFormData })
  await act(async () => { root = create(React.createElement(QueryClientProvider, { client }, React.createElement(MemoryRouter,
    { initialEntries: [`/obd/drives/timing-window?start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}`] },
    React.createElement(Location), React.createElement(Routes, null, React.createElement(Route, { path: '/obd/drives/:driveId', element: React.createElement(ObdDriveDetail) })))) ) })
  const fields = () => root.root.findAllByProps({ type: 'datetime-local' })
  assert.equal(fields().length, 2)
  for (const input of fields()) assert.equal(input.props.step, '1', 'native controls must permit non-minute boundaries')
  assert.equal(fields().find(input => input.props.name === 'end').props.defaultValue, '2026-10-22T00:00:10')
  assert.equal(utcInputValue(end), '2026-10-22T00:00', 'other callers retain minute precision')
  await act(async () => root.root.findByType('form').props.onSubmit({ preventDefault() {}, currentTarget: {
    start: '2026-10-22T00:00:03', end: '2026-10-22T00:00:12',
  } }))
  const next = new URLSearchParams(root.root.findByProps({ 'data-location': true }).children.join(''))
  assert.equal(next.get('start'), start)
  assert.equal(next.get('end'), iso(12))
  assert.equal(fields().find(input => input.props.name === 'end').props.defaultValue, '2026-10-22T00:00:12')
})
