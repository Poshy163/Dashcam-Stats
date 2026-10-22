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
  assert.match(html, /85 °C quiet-analysis limit/)
  assert.match(html, /not a manufacturer safety rating/)
  assert.match(html, /role="img"/)
  assert.match(html, /degrees Celsius/)
  assert.match(html, /<polyline/)
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
