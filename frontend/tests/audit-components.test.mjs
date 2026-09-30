import assert from 'node:assert/strict'
import { after, test } from 'node:test'
import { mkdtemp, rm } from 'node:fs/promises'
import { fileURLToPath, pathToFileURL } from 'node:url'
import path from 'node:path'
import { build } from 'esbuild'
import React, { Suspense } from 'react'
import { act, create } from 'react-test-renderer'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, useLocation } from 'react-router-dom'

const directory = await mkdtemp(fileURLToPath(new URL('../node_modules/.audit-component-test-', import.meta.url)))
after(() => rm(directory, { recursive: true, force: true }))
await build({
  stdin: {
    contents: `export { default as RouteBoundary } from '@/components/RouteBoundary'; export { lazyRoute } from '@/lib/lazyRoute'; export { default as JourneyPlayer } from '@/components/JourneyPlayer'; export { TimeChart } from '@/pages/ObdDriveDetail'; export { useDisplayTimeZone } from '@/lib/useDisplayTimeZone'; export { formatDateTime, setDisplayTimeZone } from '@/lib/format'; export { Pagination } from '@/components/ui'; export { default as Settings } from '@/pages/Settings'; export { default as TelemetryHealth } from '@/pages/TelemetryHealth';`,
    resolveDir: fileURLToPath(new URL('..', import.meta.url)),
    loader: 'tsx',
  },
  outfile: path.join(directory, 'components.mjs'), bundle: true, format: 'esm', platform: 'node', jsx: 'automatic',
  external: ['react', 'react/*', 'react-router-dom', '@tanstack/react-query'],
  alias: { '@': fileURLToPath(new URL('../src', import.meta.url)) },
})
const { RouteBoundary, lazyRoute, JourneyPlayer, TimeChart, useDisplayTimeZone, formatDateTime, setDisplayTimeZone, Pagination, Settings, TelemetryHealth } = await import(pathToFileURL(path.join(directory, 'components.mjs')).href)

const deferred = () => {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

test('one chunk reload survives a new Suspense mount and is restored only after successful content commits', async t => {
  const previousWindow = globalThis.window
  const previousStorage = globalThis.sessionStorage
  const previousError = console.error
  const stored = new Map()
  let reloads = 0
  globalThis.window = { location: { reload: () => reloads++ } }
  globalThis.sessionStorage = { getItem: key => stored.get(key) ?? null, setItem: (key, value) => stored.set(key, value), removeItem: key => stored.delete(key) }
  console.error = () => {} // React reports the intentionally rejected lazy imports.
  const roots = []
  t.after(async () => {
    for (const root of roots) await act(async () => root.unmount())
    console.error = previousError
    if (previousWindow === undefined) delete globalThis.window; else globalThis.window = previousWindow
    if (previousStorage === undefined) delete globalThis.sessionStorage; else globalThis.sessionStorage = previousStorage
  })
  async function mountLazy() {
    const load = deferred()
    const Page = lazyRoute(() => load.promise)
    let root
    await act(async () => { root = create(React.createElement(RouteBoundary, null, React.createElement(Suspense, { fallback: 'Loading route' }, React.createElement(Page)))) })
    roots.push(root)
    return { load, root }
  }
  const first = await mountLazy()
  await act(async () => first.load.reject(new Error('Failed to fetch dynamically imported module')))
  assert.equal(reloads, 1)
  assert.equal(stored.size, 1)
  await act(async () => first.root.unmount())

  const afterReload = await mountLazy()
  assert.equal(afterReload.root.toJSON(), 'Loading route')
  assert.equal(stored.size, 1, 'a suspended route is not successful content')
  await act(async () => afterReload.load.reject(new Error('Failed to fetch dynamically imported module')))
  assert.equal(reloads, 1, 'the second failed download must not automatically reload again')
  assert.match(JSON.stringify(afterReload.root.toJSON()), /could not reload/)

  const recovered = await mountLazy()
  await act(async () => recovered.load.resolve({ default: () => React.createElement('p', null, 'Ready') }))
  assert.equal(stored.size, 0)
  const later = await mountLazy()
  await act(async () => later.load.reject(new Error('Failed to fetch dynamically imported module')))
  assert.equal(reloads, 2, 'a genuinely successful route permits recovery from a later deployment')
})

test('journey camera switch preserves capture time and playback, but pauses explicitly across a missing span', async t => {
  const base = Date.parse('2026-09-30T00:00:00Z')
  const recording = (id, role, start, duration, missing = false) => ({ id, filename: `${role}-${id}.mp4`, camera: { role }, startedAt: new Date(base + start * 1000).toISOString(), durationS: duration, fileMissing: missing })
  const journey = { recordings: [recording(1, 'front', 0, 180), recording(2, 'rear', 20, 25), recording(3, 'rear', 45, 35), recording(4, 'rear', 80, 60, true), recording(5, 'rear', 140, 60)] }
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } })
  const requests = []
  const oldFetch = globalThis.fetch
  globalThis.fetch = async (url) => { requests.push(new URL(url, 'http://localhost')); return new Response(JSON.stringify({ samples: [] }), { status: 200 }) }
  let node, root
  await act(async () => {
    root = create(React.createElement(QueryClientProvider, { client }, React.createElement(JourneyPlayer, { journey, driveId: 'audit' })), {
      createNodeMock: (element) => {
        if (element.type !== 'video') return null
        node = { currentTime: 0, duration: 180, paused: true, plays: 0, play() { this.paused = false; this.plays++; return Promise.resolve() }, load() {} }
        return node
      },
    })
  })
  t.after(async () => { await act(async () => root.unmount()); client.clear(); globalThis.fetch = oldFetch })
  const camera = name => root.root.findAllByType('button').find(button => button.children.includes(name))
  node.currentTime = 50
  node.paused = false
  await act(async () => camera('Rear').props.onClick())
  assert.match(root.root.findByType('video').props.src, /\/stream\/3$/)
  await act(async () => root.root.findByType('video').props.onLoadedMetadata({ currentTarget: node }))
  assert.equal(node.currentTime, 5)
  assert.equal(node.plays, 1)
  assert.equal(root.root.findByType('input').props.value, 30)

  await act(async () => camera('Front').props.onClick())
  await act(async () => root.root.findByType('video').props.onLoadedMetadata({ currentTarget: node }))
  assert.equal(node.currentTime, 50)
  node.currentTime = 110
  node.paused = false
  await act(async () => camera('Rear').props.onClick())
  assert.match(root.root.findByType('video').props.src, /\/stream\/5$/)
  await act(async () => root.root.findByType('video').props.onLoadedMetadata({ currentTarget: node }))
  assert.equal(node.currentTime, 0)
  assert.equal(node.plays, 0)
  assert.match(JSON.stringify(root.toJSON()), /Paused at the nearest available moment/)
  assert.ok(requests.length > 0)
  for (const request of requests) {
    assert.equal(request.searchParams.get('signals'), 'vehicle_speed,engine_rpm,engine_load')
    assert.ok(Date.parse(request.searchParams.get('end')) - Date.parse(request.searchParams.get('start')) <= 190_000)
    assert.equal(request.searchParams.has('max_points'), false, 'playback uses exact samples in a bounded time window')
  }
})

test('OBD chart supports keyboard sample selection and a paginated semantic table', async t => {
  const base = Date.parse('2026-09-30T00:00:00Z')
  const values = Array.from({ length: 31 }, (_, index) => index === 1 ? null : index * 2)
  let root
  const props = { title: 'Speed', unit: 'km/h', elapsedS: values.map((_, i) => i * 5), timesIso: values.map((_, i) => new Date(base + i * 5000).toISOString()), series: [{ label: 'Speed', values, colorClass: 'text-cyan', signal: 'vehicle_speed' }], cadences: { vehicle_speed: 5 }, downsampled: false }
  await act(async () => { root = create(React.createElement(TimeChart, props)) })
  t.after(async () => act(async () => root.unmount()))
  let slider = root.root.findByType('input')
  assert.equal(slider.props.type, 'range')
  assert.equal(root.root.findByType('label').props.htmlFor, slider.props.id)
  await act(async () => slider.props.onChange({ target: { value: '2' } }))
  slider = root.root.findByType('input')
  assert.match(slider.props['aria-valuetext'], /Speed: 4\.0 km\/h/)
  await act(async () => root.root.findByType('details').props.onToggle({ currentTarget: { open: true } }))
  assert.equal(root.root.findByType('tbody').findAllByType('tr').length, 25)
  assert.match(JSON.stringify(root.root.findByType('tbody').toJSON?.() ?? root.toJSON()), /Unavailable/)
  await act(async () => root.root.findAllByType('button').find(button => button.children.includes('Next')).props.onClick())
  assert.equal(root.root.findByType('tbody').findAllByType('tr').length, 6)
  await act(async () => root.update(React.createElement(TimeChart, { ...props, downsampled: true })))
  assert.equal(root.root.findAllByType('polyline').length, 0, 'sampled points must not invent continuity across omitted rows')
  assert.equal(root.root.findAllByType('circle').filter(circle => circle.props.r === 2.5).length, 30)
  await act(async () => root.update(React.createElement(TimeChart, { ...props, downsampled: true, series: [{ ...props.series[0], values: values.map((value, index) => index === 0 ? value : null) }] })))
  assert.match(JSON.stringify(root.toJSON()), /last shown/)
  assert.doesNotMatch(JSON.stringify(root.toJSON()), /stale/, 'omitted samples cannot establish a stale signal')
})

test('already-loaded child dates rerender when the camera timezone arrives or changes', async t => {
  const timestamp = '2026-09-30T15:00:00Z'
  function DateLabel() { return React.createElement('output', null, formatDateTime(timestamp)) }
  function Application({ zone }) { useDisplayTimeZone(zone); return React.createElement(DateLabel) }
  let root
  await act(async () => { root = create(React.createElement(Application, { zone: 'UTC' })) })
  t.after(async () => { await act(async () => root.unmount()); setDisplayTimeZone(undefined) })
  const before = root.root.findByType('output').children.join('')
  await act(async () => root.update(React.createElement(Application, { zone: 'Australia/Adelaide' })))
  const after = root.root.findByType('output').children.join('')
  assert.notEqual(after, before)
  assert.equal(after, formatDateTime(timestamp))
  await act(async () => root.update(React.createElement(Application, { zone: 'UTC' })))
  assert.equal(root.root.findByType('output').children.join(''), before)
})

test('pagination still escapes a page that vanished when the result count shrank', async t => {
  let selected, root
  await act(async () => { root = create(React.createElement(Pagination, { page: 4, pages: 1, total: 30, onChange: page => { selected = page } })) })
  t.after(async () => act(async () => root.unmount()))
  const buttons = root.root.findAllByType('button')
  assert.equal(buttons.find(button => button.children.includes('Next')).props.disabled, true)
  await act(async () => buttons.find(button => button.children.includes('Previous')).props.onClick())
  assert.equal(selected, 1)
})

async function settle() {
  for (let n = 0; n < 4; n++) await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
}

test('settings reset cannot race a save, and edits made while saving survive success', async t => {
  const storedSetting = width => [{ key: 'general', label: 'General', settings: [{ key: 'thumbnail.width', label: 'Width', type: 'int', value: width, is_default: false, choices: [], read_only: false }] }]
  const pendingSave = deferred()
  const requests = []
  const oldFetch = globalThis.fetch
  globalThis.fetch = async (url, init) => {
    requests.push({ url, init })
    if (init.method === 'PUT') return pendingSave.promise
    return new Response(JSON.stringify(storedSetting(320)))
  }
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } })
  let root
  await act(async () => { root = create(React.createElement(QueryClientProvider, { client }, React.createElement(MemoryRouter, null, React.createElement(Settings)))) })
  t.after(async () => { await act(async () => root.unmount()); client.clear(); globalThis.fetch = oldFetch })
  await settle()
  const width = () => root.root.findByProps({ id: 'setting-thumbnail-width' })
  await act(async () => width().props.onChange({ target: { value: '640' } }))
  await act(async () => root.root.findAllByType('button').find(button => button.children.includes('Save changes')).props.onClick())
  await settle()
  assert.equal(root.root.findAllByType('button').find(button => button.children.includes('reset to default')).props.disabled, true)
  assert.equal(width().props.disabled, false, 'typing remains available during the save')
  await act(async () => width().props.onChange({ target: { value: '800' } }))
  await act(async () => pendingSave.resolve(new Response(JSON.stringify(storedSetting(640)))))
  await settle()
  assert.equal(width().props.value, 800)
  assert.match(JSON.stringify(root.toJSON()), /unsaved change/)
  assert.equal(JSON.parse(requests.find(request => request.init.method === 'PUT').init.body).values['thumbnail.width'], 640)
})

test('telemetry polling returns from an empty removed page without claiming the library is healthy', async t => {
  const coverage = { totalPoints: 0, acceptedPoints: 0, fullCoverageRecordings: 0, gapRecordings: 0, warningOnlyRecordings: 0, problemSamples: 0, warningRecordings: 0, noFixPoints: 0, ocrUnreadablePoints: 0, rejectedPoints: 0, recordings: 0 }
  const issue = { recordingId: 1, filename: 'Issue remains.mp4', status: 'degraded', fixes: 0, points: 0, gaps: 0, longestGapS: 0, recovered: 0, problems: 0, reasons: ['no_samples'], realGpsLoss: 0, ocrUnreadable: 0, rejected: 0, startedAt: null }
  const summary = { recordings: 30, healthy: 0, degraded: 30, noFix: 0, pending: 0, outdatedRecordings: 0, emptyRecordings: 30, totalGaps: 0, pairedRecoveries: 0, gpsCoverage: coverage, issueTotal: 30, issuePages: 1 }
  const oldFetch = globalThis.fetch
  globalThis.fetch = async url => {
    const query = new URL(url, 'http://localhost').searchParams
    const page = Number(query.get('page') ?? 1)
    const filtered = query.has('reason')
    return new Response(JSON.stringify({ ...summary, issuePage: page, issueTotal: filtered ? 0 : 30, issues: page > 1 || filtered ? [] : [issue] }))
  }
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } })
  let root, location
  function Observer() { location = useLocation(); return null }
  await act(async () => { root = create(React.createElement(QueryClientProvider, { client }, React.createElement(MemoryRouter, { initialEntries: ['/telemetry-health?page=2'] }, React.createElement(Observer), React.createElement(TelemetryHealth)))) })
  t.after(async () => { await act(async () => root.unmount()); client.clear(); globalThis.fetch = oldFetch })
  await settle()
  await settle()
  assert.equal(location.search, '')
  assert.match(JSON.stringify(root.toJSON()), /Issue remains.mp4/)
  assert.doesNotMatch(JSON.stringify(root.toJSON()), /Telemetry looks healthy/)
  await act(async () => root.root.findByType('select').props.onChange({ target: { value: 'gps_no_fix' } }))
  await settle()
  assert.match(JSON.stringify(root.toJSON()), /No issues match these filters/)
  assert.doesNotMatch(JSON.stringify(root.toJSON()), /Telemetry looks healthy/)
})
