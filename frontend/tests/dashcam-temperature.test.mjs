import assert from 'node:assert/strict'
import { after, test } from 'node:test'
import { mkdtemp, rm } from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { build } from 'esbuild'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { act, create } from 'react-test-renderer'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const directory = await mkdtemp(fileURLToPath(new URL('../node_modules/.temperature-test-', import.meta.url)))
after(() => rm(directory, { recursive: true, force: true }))
await build({
  stdin: {
    contents: `export { DashcamTemperatureCard, DashcamTemperatureEvidence } from '@/components/DashcamTemperatureCard'; export { temperatureValue, temperatureSegments, temperatureHistoryIsOld } from '@/lib/dashcamTemperature'; export { setDisplayTimeZone, formatDateTime, formatShortDateTime } from '@/lib/format';`,
    resolveDir: fileURLToPath(new URL('..', import.meta.url)), loader: 'tsx',
  },
  outfile: path.join(directory, 'temperature.mjs'), bundle: true, format: 'esm', platform: 'node', jsx: 'automatic',
  external: ['react', 'react/*', '@tanstack/react-query'], alias: { '@': fileURLToPath(new URL('../src', import.meta.url)) },
})
const { DashcamTemperatureCard, DashcamTemperatureEvidence, temperatureValue, temperatureSegments,
  temperatureHistoryIsOld, setDisplayTimeZone, formatDateTime, formatShortDateTime } = await import(pathToFileURL(path.join(directory, 'temperature.mjs')).href)

const base = Date.parse('2026-10-22T00:00:00Z')
const point = (seconds, socC, overrides = {}) => ({ occurredAt: new Date(base + seconds * 1000).toISOString(), socC, sessionId: 'first', quality: socC == null ? 'unavailable' : 'valid', segmentId: 'one', samples: 1, kind: 'last', ...overrides })
const data = (overrides = {}) => ({
  source: 'retained_carplay_timing', sensor: 'soc-thmzone', hours: 24,
  rangeStart: new Date(base).toISOString(), rangeEnd: new Date(base + 24 * 3600_000).toISOString(),
  bucketS: 300, gapThresholdS: 90, latest: point(60, 107.02), minimumC: 80, maximumC: 175,
  uniqueObservationCount: 3, sourceRowCount: 6, inputTruncated: false, outputTruncated: false, truncated: false,
  points: [point(0, 80), point(30, 175, { kind: 'max' }), point(60, 107.02)], ...overrides,
})
const markup = (value, extra = {}) => renderToStaticMarkup(React.createElement(DashcamTemperatureEvidence, { data: value, nowMs: base + 60_000, ...extra }))
const collector = (overrides = {}) => ({
  schemaVersion: 1, source: 'companion_temperature_v1', sensor: 'soc-thmzone', state: 'running',
  lastCapturedAtUtc: point(60, 70).occurredAt, lastCapturedElapsedMs: 60_000,
  checkedAtUtc: point(60, 70).occurredAt, intervalMs: 30_000, retainedReadings: 12, error: null,
  ...overrides,
})
const text = root => JSON.stringify(root.toJSON())
async function flush() { await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) }) }

test('camera temperature keeps broad valid peaks and never replaces unknown values with zero', () => {
  for (const value of [-40, 0, 107.02, 175, 200]) assert.equal(temperatureValue(point(0, value)), value)
  for (const value of [null, NaN, Infinity, -41, 201]) assert.equal(temperatureValue(point(0, value)), null)
  assert.equal(temperatureValue(point(0, 107, { quality: 'invalid' })), null)
  assert.equal(temperatureValue(point(0, 107, { quality: 'unavailable' })), null)
})

test('null, invalid, gap, session and segment boundaries leave disconnected observations', () => {
  const points = [point(0, 80), point(30, 90), point(60, null), point(90, 100), point(300, 105, { segmentId: 'after-raw-gap' }),
    point(330, 110, { sessionId: 'second' }), point(360, 115, { sessionId: 'second', segmentId: 'two' }),
    point(390, 120, { sessionId: 'second', segmentId: 'two' })]
  assert.deepEqual(temperatureSegments(points, 90), [[0, 1], [3], [4], [5], [6, 7]])
  assert.deepEqual(temperatureSegments(points, 90, true), [[0], [1], [3], [4], [5], [6], [7]])
  assert.deepEqual(temperatureSegments([point(0, 80), point(0, 81), point(30, 82)], 90), [[0], [1, 2]])
})

test('raw-stream segment proof preserves bucket-summary continuity while missing proof uses the raw gap rule', () => {
  // These distant bucket extrema share a segment assigned after checking all original
  // observations at90s-or-less cadence, rather than just the two retained summary points.
  const summaries = [point(0, 80, { segmentId: 'continuous-raw-capture' }), point(1_800, 110, { segmentId: 'continuous-raw-capture' })]
  assert.deepEqual(temperatureSegments(summaries, 90), [[0, 1]])
  assert.deepEqual(temperatureSegments([summaries[0], { ...summaries[1], segmentId: 'after-raw-gap' }], 90), [[0], [1]])
  assert.deepEqual(temperatureSegments([summaries[0], { ...summaries[1], sessionId: 'new-session' }], 90), [[0], [1]])
  assert.deepEqual(temperatureSegments([point(0, 80, { segmentId: undefined }), point(30, 85, { segmentId: undefined }), point(300, 90, { segmentId: undefined })], 90), [[0, 1], [2]])
})

test('SVG preserves a 175 degree peak with units and an explicit quiet-analysis reference', () => {
  const html = markup(data())
  assert.match(html, /175\.0 °C/)
  assert.match(html, /180 °C/)
  assert.match(html, /Companion 0\.3\.14 policy reference/)
  assert.match(html, /start\/restart below 95 °C/)
  assert.match(html, /stop running analysis at 100 °C/)
  assert.match(html, /Earlier companions used 85 °C/)
  assert.match(html, /do not identify the policy used for historical captures/)
  assert.match(html, /not a manufacturer safety rating/)
  assert.match(html, /role="img"/)
  assert.match(html, /degrees Celsius/)
  assert.match(html, /<polyline/)
})

test('both versioned thermal references remain distinct and visible below the observed range', () => {
  const html = markup(data({ points: [point(0, 70)], latest: point(0, 70), minimumC: 70, maximumC: 70 }))
  const start = html.match(/<line[^>]+aria-label="0\.3\.14 analysis start or restart reference: 95 degrees Celsius"/)
  const stop = html.match(/<line[^>]+aria-label="0\.3\.14 analysis hard stop reference: 100 degrees Celsius"/)
  assert.ok(start)
  assert.ok(stop)
  const y = line => Number(line[0].match(/y1="([^"]+)"/)[1])
  assert.ok(y(stop) < y(start))
  assert.ok(y(stop) > 16 && y(start) < 192)
  assert.match(start[0], /stroke-dasharray="5 4"/)
  assert.match(stop[0], /stroke-dasharray="2 3"/)
  assert.doesNotMatch(html, /85 °C quiet-analysis limit/)
})

test('historical and failed refreshes keep observations visible without claiming current temperature', () => {
  assert.equal(temperatureHistoryIsOld(point(60, 107), base + 180_000, 90), true)
  assert.equal(temperatureHistoryIsOld(point(60, 107), base + 90_000, 90), false)
  assert.equal(temperatureHistoryIsOld(point(60, 107), base, 90), true)
  const html = markup(data(), { nowMs: base + 180_000, error: true })
  assert.match(html, /107\.0 °C/)
  assert.match(html, /175\.0 °C/)
  assert.match(html, /Temperature refresh failed/)
  assert.match(html, /Historical reading · current temperature unknown/)
  assert.match(html, /<svg/)
})

test('legacy history remains usable without implying boot recovery or a running collector', () => {
  const html = markup(data())
  assert.match(html, /History source: legacy timing diagnostics/)
  assert.match(html, /On-device capture state not reported/)
  assert.match(html, /does not prove continuous capture/)
  assert.doesNotMatch(html, /On-device capture recently confirmed|Capture starts with the logger after reboot/)
  assert.match(html, /107\.0 °C/)
})

test('new history distinguishes on-camera capture, server check and mixed provenance', () => {
  const html = markup(data({ source: 'retained_device_temperature', companionObservationCount: 3, legacyObservationCount: 2, collector: collector() }))
  assert.match(html, /companion temperature capture \(3 observations\) and legacy timing diagnostics \(2 observations\)/)
  assert.match(html, /On-device capture recently confirmed/)
  assert.match(html, /Last captured on camera/)
  assert.match(html, /Server last checked capture/)
  assert.match(html, /Last reported retained on camera/)
  assert.match(html, /12 observations/)
  assert.match(html, /every 30 seconds while its logger is enabled and the camera is awake, including during backup/)
  assert.match(html, /does not prevent sleep/)
  assert.match(html, /not a live temperature reading/)
})

test('stale checks and stalled capture timestamps cannot keep a running label current', () => {
  const stale = markup(data({ collector: collector() }), { nowMs: base + 180_000 })
  assert.match(stale, /Capture report is stale · current capture unknown/)
  assert.doesNotMatch(stale, /On-device capture recently confirmed/)
  const stalled = markup(data({ collector: collector({ checkedAtUtc: point(180, 70).occurredAt }) }), { nowMs: base + 180_000 })
  assert.match(stalled, /No recent captured sample · current capture unknown/)
  assert.match(stalled, /collector reported running, but its latest captured timestamp is missing, old or invalid/)
  assert.doesNotMatch(stalled, /On-device capture recently confirmed/)
  const failed = markup(data({ collector: collector() }), { error: true })
  assert.match(failed, /Temperature refresh failed/)
  assert.match(failed, /Capture report is stale · current capture unknown/)
})

test('capture resource failures preserve prior observations and never invent temperatures', () => {
  const unavailable = markup(data({ collector: collector({ state: 'sensor_unavailable', error: 'sensor_unavailable' }), latest: point(60, null) }))
  assert.match(unavailable, /Sensor unavailable in the last report/)
  assert.match(unavailable, /Last captured<\/div><div[^>]*>Unavailable<\/div>/)
  assert.match(unavailable, /175\.0 °C/)
  assert.doesNotMatch(unavailable, /On-device capture recently confirmed|>0\.0 °C/)
  for (const error of ['journal_unavailable', 'mirror_unavailable']) {
    const storage = markup(data({ collector: collector({ state: 'storage_unavailable', error }) }))
    assert.match(storage, /Temperature storage unavailable in the last report/)
    assert.match(storage, /Some captured readings may not have been saved or made available for import/)
    assert.match(storage, /107\.0 °C/)
  }
  const starting = markup(data({ collector: collector({ state: 'starting', lastCapturedAtUtc: null }) }))
  assert.match(starting, /On-device collector starting in the last report/)
  assert.doesNotMatch(starting, /On-device capture recently confirmed/)
})

test('unknown metadata, invalid counts and future evidence do not become current capture claims', () => {
  for (const override of [{ state: 'private exception text' }, { source: 'private exception text' }, { error: 'private exception text' }, { schemaVersion: 2 }, { retainedReadings: 8193 }]) {
    const html = markup(data({ source: 'retained_device_temperature', companionObservationCount: NaN, legacyObservationCount: -1, collector: collector(override) }))
    assert.match(html, /On-device capture report not recognised/)
    assert.doesNotMatch(html, /private exception text|NaN observations|-1 observations|On-device capture recently confirmed/)
  }
  const futureCheck = markup(data({ collector: collector({ checkedAtUtc: point(121, 70).occurredAt }) }))
  assert.match(futureCheck, /Capture report is stale · current capture unknown/)
  const futureCapture = markup(data({ collector: collector({ lastCapturedAtUtc: point(121, 70).occurredAt }) }))
  assert.match(futureCapture, /No recent captured sample · current capture unknown/)
})

test('an unavailable camera clock is distinct from missing capture and does not fabricate graph timestamps', () => {
  const html = markup(data({ source: 'retained_device_temperature', collector: collector({ state: 'clock_unavailable', error: 'clock_unavailable', lastCapturedAtUtc: null, lastCapturedElapsedMs: null }) }))
  assert.match(html, /Camera time unavailable in the last report/)
  assert.match(html, /could not be given a reliable capture time and are excluded from this graph/)
  assert.match(html, /Earlier timestamped observations remain visible/)
  assert.match(html, /107\.0 °C/)
  assert.doesNotMatch(html, /On-device capture recently confirmed|On-device capture report not recognised|1970/)
})

test('the semantic observations table identifies each known capture source without exposing unknown values', async t => {
  let root
  await act(async () => { root = create(React.createElement(DashcamTemperatureEvidence, {
    data: data({ points: [point(0, 70, { source: 'companion_temperature_v1' }), point(30, 71, { source: 'carplay_timing' }), point(60, 72, { source: 'private source text' })] }), nowMs: base + 60_000,
  })) })
  t.after(async () => act(async () => root.unmount()))
  await act(async () => root.root.findByType('details').props.onToggle({ currentTarget: { open: true } }))
  assert.match(text(root), /Capture source/)
  const rows = root.root.findByType('tbody').findAllByType('tr')
  assert.equal(rows[0].findAllByType('td').at(-1).children.join(''), 'Companion temperature capture')
  assert.equal(rows[1].findAllByType('td').at(-1).children.join(''), 'Legacy timing diagnostics')
  assert.equal(rows[2].findAllByType('td').at(-1).children.join(''), 'Not reported')
  assert.doesNotMatch(text(root), /private source text/)
})

test('latest unavailable capture is not silently replaced by an earlier valid reading', () => {
  const html = markup(data({ latest: point(90, null) }))
  assert.match(html, /Last captured<\/div><div[^>]*>Unavailable<\/div>/)
  assert.match(html, /107\.0 °C/)
  assert.match(html, /175\.0 °C/)
})

test('empty history stays unknown and truncated output cannot invent connected lines', () => {
  const empty = markup(data({ latest: null, minimumC: null, maximumC: null, uniqueObservationCount: 0, sourceRowCount: 0, points: [] }))
  assert.match(empty, /No temperature history in this range/)
  assert.doesNotMatch(empty, /0\.0 °C/)
  const truncated = markup(data({ truncated: true, inputTruncated: true, outputTruncated: true }))
  assert.match(truncated, /History is incomplete/)
  assert.match(truncated, /Statistics describe the available observations/)
  assert.doesNotMatch(truncated, /<polyline/)
  assert.equal((truncated.match(/<circle/g) ?? []).length, 3)
  const isolated = markup(data({ points: [point(0, 107)] }))
  assert.equal((isolated.match(/<circle/g) ?? []).length, 1)
})

test('captured timestamps follow the configured camera timezone', t => {
  t.after(() => setDisplayTimeZone(undefined))
  setDisplayTimeZone('UTC')
  const utc = markup(data())
  setDisplayTimeZone('Australia/Adelaide')
  const local = markup(data())
  assert.notEqual(utc, local)
  assert.ok(local.includes(formatDateTime(point(60, 107).occurredAt)))
  const week = markup(data({ hours: 168, rangeEnd: new Date(base + 168 * 3600_000).toISOString() }))
  const chart = week.match(/<svg[\s\S]*?<\/svg>/)?.[0]
  assert.ok(chart.includes(formatShortDateTime(new Date(base + 84 * 3600_000).toISOString())), 'week chart includes the date and local time at its middle tick')
})

test('keyboard sample exploration and the semantic table expose unavailable rows in bounded pages', async t => {
  const points = Array.from({ length: 31 }, (_, index) => point(index * 30, index === 1 ? null : 70 + index))
  let root
  await act(async () => { root = create(React.createElement(DashcamTemperatureEvidence, { data: data({ points }), nowMs: base + 60_000 })) })
  t.after(async () => act(async () => root.unmount()))
  const slider = root.root.findByType('input')
  assert.equal(slider.props.type, 'range')
  assert.equal(root.root.findByType('label').props.htmlFor, slider.props.id)
  await act(async () => slider.props.onChange({ target: { value: '1' } }))
  assert.match(root.root.findByType('input').props['aria-valuetext'], /Unavailable/)
  await act(async () => root.root.findByType('details').props.onToggle({ currentTarget: { open: true } }))
  assert.equal(root.root.findByType('tbody').findAllByType('tr').length, 25)
  assert.match(text(root), /Unavailable/)
  await act(async () => root.root.findAllByType('button').find(node => node.children.includes('Next')).props.onClick())
  assert.equal(root.root.findByType('tbody').findAllByType('tr').length, 6)
})

test('narrow charts keep readable axes, map pointer selection correctly and resize with observer cleanup', async t => {
  const originalObserver = globalThis.ResizeObserver
  const observers = []
  globalThis.ResizeObserver = class {
    constructor(callback) { this.callback = callback; observers.push(this) }
    observe(target) { this.target = target }
    disconnect() { this.disconnected = true }
  }
  let width = 282
  const container = { getBoundingClientRect: () => ({ width }) }
  const svg = { getBoundingClientRect: () => ({ width, left: 10 }) }
  const points = [point(0, 80), point(60, 90), point(120, 107)]
  let root
  await act(async () => { root = create(React.createElement(DashcamTemperatureEvidence, {
    data: data({ points, rangeEnd: point(120, 107).occurredAt }), nowMs: base + 120_000,
  }), { createNodeMock: element => element.type === 'div' ? container : element.type === 'svg' ? svg : null }) })
  t.after(async () => { if (root) await act(async () => root.unmount()); globalThis.ResizeObserver = originalObserver })
  const chart = () => root.root.findByType('svg')
  const ticks = () => chart().findAllByType('text').filter(node => node.props.y === 213)
  assert.equal(chart().props.viewBox, '0 0 282 220')
  assert.equal(chart().props.height, 220)
  assert.equal(ticks().length, 2)
  assert.ok(chart().findAllByType('text').every(node => node.props.fontSize === 11))
  await act(async () => chart().props.onPointerMove({ clientX: 10 + 45 + (width - 45 - 18) / 2 }))
  assert.match(root.root.findByType('input').props['aria-valuetext'], /90\.0 °C/)
  width = 700
  await act(async () => observers[0].callback([{ target: container, contentRect: { width } }]))
  assert.equal(chart().props.viewBox, '0 0 700 220')
  assert.equal(ticks().length, 3)
  await act(async () => chart().props.onPointerMove({ clientX: 10 + 45 + width - 45 - 18 }))
  assert.match(root.root.findByType('input').props['aria-valuetext'], /107\.0 °C/)
  await act(async () => root.unmount())
  root = null
  assert.equal(observers.length, 1)
  assert.equal(observers[0].disconnected, true)
})

test('chart measurement falls back to window resize and removes the listener without ResizeObserver', async t => {
  const originalObserver = globalThis.ResizeObserver
  const originalWindow = globalThis.window
  globalThis.ResizeObserver = undefined
  const listeners = new Map()
  globalThis.window = { addEventListener: (name, callback) => listeners.set(name, callback), removeEventListener: (name, callback) => { if (listeners.get(name) === callback) listeners.delete(name) } }
  let width = 320
  const container = { getBoundingClientRect: () => ({ width }) }
  let root
  await act(async () => { root = create(React.createElement(DashcamTemperatureEvidence, { data: data() }), {
    createNodeMock: element => element.type === 'div' ? container : null,
  }) })
  t.after(async () => { if (root) await act(async () => root.unmount()); globalThis.ResizeObserver = originalObserver; globalThis.window = originalWindow })
  assert.equal(root.root.findByType('svg').props.viewBox, '0 0 320 220')
  assert.ok(listeners.has('resize'))
  width = 600
  await act(async () => listeners.get('resize')())
  assert.equal(root.root.findByType('svg').props.viewBox, '0 0 600 220')
  await act(async () => root.unmount())
  root = null
  assert.equal(listeners.size, 0)
})

test('range changes use read-only history API and refresh errors retain cached chart data', async t => {
  const previousFetch = globalThis.fetch
  const previousNow = Date.now
  let now = base + 60_000
  Date.now = () => now
  const requests = []
  let fail = false
  const payload = hours => ({
    source: 'retained_carplay_timing', sensor: 'soc-thmzone', hours,
    range_start: new Date(base).toISOString(), range_end: new Date(base + hours * 3600_000).toISOString(),
    bucket_s: 300, gap_threshold_s: 90,
    latest: { occurred_at: point(60, 107).occurredAt, soc_c: 107.02, session_id: 'first', quality: 'valid' },
    minimum_c: 80, maximum_c: 175, unique_observation_count: 3, source_row_count: 6,
    input_truncated: false, output_truncated: false, truncated: false,
    points: data().points.map(item => ({ occurred_at: item.occurredAt, soc_c: item.socC, session_id: item.sessionId, quality: item.quality, segment_id: item.segmentId, samples: item.samples, kind: item.kind })),
  })
  globalThis.fetch = async (url, init = {}) => {
    const request = new URL(url, 'http://localhost')
    assert.equal(request.pathname, '/api/unit-logs/temperature')
    assert.equal(init.method ?? 'GET', 'GET')
    requests.push(Number(request.searchParams.get('hours')))
    if (fail) throw new Error('Network unavailable')
    return new Response(JSON.stringify(payload(requests.at(-1))), { status: 200 })
  }
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } })
  let root
  await act(async () => { root = create(React.createElement(QueryClientProvider, { client }, React.createElement(DashcamTemperatureCard))) })
  t.after(async () => { await act(async () => root.unmount()); client.clear(); globalThis.fetch = previousFetch; Date.now = previousNow })
  await flush()
  assert.match(text(root), /175\.0 °C/)
  assert.match(text(root), /not continuous monitoring/)
  assert.match(text(root), /separate from engine coolant/)
  assert.match(text(root), /Captured observation, not a live reading/)
  now += 120_000
  await act(async () => { await client.refetchQueries({ queryKey: ['dashcam-temperature', 24] }) })
  await flush()
  assert.match(text(root), /Historical reading · current temperature unknown/, 'identical successful responses must still age the unchanged source observation')
  for (const [label, hours] of [['1 h', 1], ['6 h', 6], ['7 d', 168]]) {
    await act(async () => root.root.findAllByType('button').find(node => node.children.includes(label)).props.onClick())
    await flush()
    assert.equal(requests.at(-1), hours)
    assert.equal(root.root.findAllByType('button').find(node => node.children.includes(label)).props['aria-pressed'], true)
  }
  fail = true
  await act(async () => { await client.refetchQueries({ queryKey: ['dashcam-temperature', 168] }) })
  await flush()
  assert.match(text(root), /Temperature refresh failed/)
  assert.match(text(root), /175\.0 °C/)
  assert.equal(root.root.findAllByType('svg').length, 1)
})

test('history API maps companion capture and import evidence without extra device requests', async t => {
  const previousFetch = globalThis.fetch
  const previousNow = Date.now
  Date.now = () => base + 60_000
  const requests = []
  globalThis.fetch = async (url, init = {}) => {
    const request = new URL(url, 'http://localhost')
    requests.push(request.pathname)
    assert.equal(init.method ?? 'GET', 'GET')
    assert.equal(request.pathname, '/api/unit-logs/temperature')
    return new Response(JSON.stringify({
      source: 'retained_device_temperature', sensor: 'soc-thmzone', hours: 24,
      range_start: point(0, 70).occurredAt, range_end: point(3600, 70).occurredAt,
      bucket_s: 300, gap_threshold_s: 90, latest: { occurred_at: point(60, 70).occurredAt, soc_c: 70, quality: 'valid', session_id: 'first', source: 'companion_temperature_v1' },
      minimum_c: 70, maximum_c: 70, unique_observation_count: 1, source_row_count: 1,
      input_truncated: false, output_truncated: false, truncated: false, points: [],
      companion_observation_count: 1, legacy_observation_count: 0,
      collector: { schema_version: 1, source: 'companion_temperature_v1', sensor: 'soc-thmzone', state: 'running', last_captured_at_utc: point(60, 70).occurredAt,
        last_captured_elapsed_ms: 60_000, checked_at_utc: point(60, 70).occurredAt, interval_ms: 30_000, retained_readings: 12, error: null },
    }), { status: 200 })
  }
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } })
  let root
  await act(async () => { root = create(React.createElement(QueryClientProvider, { client }, React.createElement(DashcamTemperatureCard))) })
  t.after(async () => { await act(async () => root.unmount()); client.clear(); globalThis.fetch = previousFetch; Date.now = previousNow })
  await flush()
  assert.match(text(root), /On-device capture recently confirmed/)
  assert.match(text(root), /companion temperature capture/)
  assert.match(text(root), /Server last checked capture/)
  assert.match(text(root), /Last captured on camera/)
  assert.match(text(root), /12/)
  assert.deepEqual(requests, ['/api/unit-logs/temperature'])
})
