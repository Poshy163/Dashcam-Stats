import assert from 'node:assert/strict'
import { after, test } from 'node:test'
import { mkdtemp, rm } from 'node:fs/promises'
import { fileURLToPath, pathToFileURL } from 'node:url'
import path from 'node:path'
import { build } from 'esbuild'
import React from 'react'
import { act, create } from 'react-test-renderer'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes, useLocation, useNavigate } from 'react-router-dom'

const directory = await mkdtemp(fileURLToPath(new URL('../node_modules/.list-filters-test-', import.meta.url)))
after(() => rm(directory, { recursive: true, force: true }))
await build({
  stdin: {
    contents: `export { default as Logs } from '@/pages/Logs'; export { default as Queue } from '@/pages/Queue'; export { default as Recordings } from '@/pages/Recordings'; export { default as Plates } from '@/pages/Plates'; export { default as Vehicles } from '@/pages/Vehicles'; export { default as TelemetryHealth } from '@/pages/TelemetryHealth'; export { default as ObdDriveDetail } from '@/pages/ObdDriveDetail'; export { listPage, booleanFilter, rowIdFilter } from '@/lib/listFilters';`,
    resolveDir: fileURLToPath(new URL('..', import.meta.url)), loader: 'tsx',
  },
  outfile: path.join(directory, 'lists.mjs'), bundle: true, format: 'esm', platform: 'node', jsx: 'automatic',
  external: ['react', 'react/*', 'react-router-dom', '@tanstack/react-query'],
  alias: { '@': fileURLToPath(new URL('../src', import.meta.url)) },
})
const { Logs, Queue, Recordings, Plates, Vehicles, TelemetryHealth, ObdDriveDetail, listPage, booleanFilter, rowIdFilter } = await import(pathToFileURL(path.join(directory, 'lists.mjs')).href)

async function settle() {
  for (let n = 0; n < 4; n++) await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
}

async function mountList(t, Component, initial, { active = false } = {}) {
  const requests = []
  const oldFetch = globalThis.fetch
  const oldStorage = globalThis.localStorage
  globalThis.localStorage = { getItem: () => null, setItem() {} }
  globalThis.fetch = async url => {
    const request = new URL(url, 'http://localhost')
    requests.push(request)
    if (request.pathname === '/api/unit-logs/tags') return new Response(JSON.stringify([]))
    if (request.pathname === '/api/plates/quality') return new Response(JSON.stringify({ cameras: [] }))
    // Only the outgoing parameter is under test for these large detail response shapes.
    if (request.pathname === '/api/telemetry/quality' || request.pathname.endsWith('/series')) return new Response(JSON.stringify({ detail: 'Fixture response unavailable' }), { status: 503 })
    if (request.pathname === '/api/jobs/stats') return new Response(JSON.stringify({ queued: 0, running: active ? 1 : 0, failed: 0, completedToday: 0, paused: false }))
    const page = Number(request.searchParams.get('page') ?? 1)
    const item = active && request.pathname === '/api/jobs' && request.searchParams.get('state') === 'running'
      ? [{ id: 1, recordingId: 1, recordingFilename: 'Running.mp4', state: 'running', progress: 0.5 }]
      : []
    return new Response(JSON.stringify({ items: item, page, pages: 3, total: 100, pageSize: 24 }))
  }
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } })
  let root, location, navigate
  function Observer() { location = useLocation(); navigate = useNavigate(); return null }
  t.after(async () => {
    if (root) await act(async () => root.unmount())
    client.clear()
    globalThis.fetch = oldFetch
    if (oldStorage === undefined) delete globalThis.localStorage; else globalThis.localStorage = oldStorage
  })
  await act(async () => {
    root = create(React.createElement(QueryClientProvider, { client },
      React.createElement(MemoryRouter, { initialEntries: [initial] },
        React.createElement(Observer),
        React.createElement(Routes, null, React.createElement(Route, {
          path: Component === ObdDriveDetail ? '/obd/:driveId' : '*', element: React.createElement(Component),
        })),
      ),
    ))
  })
  await settle()
  return {
    root, requests,
    get params() { return new URLSearchParams(location.search) },
    async back() { await act(async () => navigate(-1)); await settle() },
    async select(index, value) { await act(async () => root.root.findAllByType('select')[index].props.onChange({ target: { value } })); await settle() },
    async next() { await act(async () => root.root.findAllByType('button').find(button => button.children.includes('Next')).props.onClick()); await settle() },
  }
}

test('list page parsing matches the server page ceiling without accepting malformed numbers', () => {
  for (const value of [null, '', '0', '-1', '1.5', 'NaN', 'Infinity', '1e3', '1000000001', '9007199254740992']) assert.equal(listPage(value), 1, String(value))
  assert.equal(listPage('2'), 2)
  assert.equal(listPage('1000000000'), 1_000_000_000)
})

test('optional boolean and row ID filters do not silently turn malformed bookmarks into false or rounded IDs', () => {
  assert.equal(booleanFilter('true'), true)
  assert.equal(booleanFilter('false'), false)
  for (const value of [null, '', 'yes', 'broken', '0']) assert.equal(booleanFilter(value), undefined)
  assert.equal(rowIdFilter('42'), 42)
  for (const value of [null, '', '-1', '0', '1.5', 'NaN', '9007199254740992']) assert.equal(rowIdFilter(value), undefined)
})

test('all audited list views send a safe page for malformed URLs', async t => {
  for (const [Component, route, endpoint] of [[Logs, '/logs', '/api/logs'], [Logs, '/logs?source=unit', '/api/unit-logs'], [Queue, '/queue', '/api/jobs'], [Recordings, '/recordings', '/api/recordings'], [Plates, '/plates', '/api/plates'], [Vehicles, '/vehicles', '/api/vehicles'], [TelemetryHealth, '/telemetry-health', '/api/telemetry/quality']]) {
    await t.test(route, async t => {
      const view = await mountList(t, Component, `${route}${route.includes('?') ? '&' : '?'}page=Infinity`)
      const request = view.requests.find(request => request.pathname === endpoint && request.searchParams.has('page'))
      assert.equal(request.searchParams.get('page'), '1')
    })
  }
})

test('OBD diagnostic pagination uses the same safe page contract', async t => {
  const view = await mountList(t, ObdDriveDetail, '/obd/example?diagnostic_page=Infinity')
  assert.equal(view.requests.find(request => request.pathname === '/api/obd/drives/example/series').searchParams.get('diagnostic_page'), '1')
})

test('recording URL booleans and lifecycle selections stay consistent with the actual request', async t => {
  const view = await mountList(t, Recordings, '/recordings?has_gps=broken&has_detections=broken&journey_id=NaN&state=unknown')
  const request = view.requests.at(-1)
  for (const key of ['has_gps', 'has_detections', 'journey_id', 'state']) assert.equal(request.searchParams.has(key), false)
  const selects = view.root.root.findAllByType('select')
  assert.equal(selects[1].props.value, '')
  assert.equal(selects[2].props.value, '')
  assert.equal(selects[3].props.value, '')
  await view.select(2, 'false')
  assert.equal(view.requests.at(-1).searchParams.get('has_gps'), 'false')
  assert.equal(view.root.root.findAllByType('select')[2].props.value, 'false')
})

test('unknown vehicle plate/type URL filters do not silently become a no-plate filter', async t => {
  const view = await mountList(t, Vehicles, '/vehicles?has_plate=broken&class_label=unknown&page=2')
  for (const key of ['has_plate', 'class_label']) assert.equal(view.requests.at(-1).searchParams.has(key), false)
  assert.ok(view.root.root.findAllByType('select').every(select => select.props.value === ''))
  await view.select(1, 'no')
  assert.equal(view.requests.at(-1).searchParams.get('has_plate'), 'false')
  assert.equal(view.params.has('page'), false)
})

test('queue empty-state wording respects the selected state and running work', async t => {
  const view = await mountList(t, Queue, '/queue?state=failed', { active: true })
  assert.match(JSON.stringify(view.root.toJSON()), /No jobs match this state/)
  assert.match(JSON.stringify(view.root.toJSON()), /Currently processing/)
  assert.doesNotMatch(JSON.stringify(view.root.toJSON()), /Nothing is queued/)
})

test('queue rejects unknown state filters and changing a valid state resets the page', async t => {
  const view = await mountList(t, Queue, '/queue?state=unknown&page=2')
  assert.equal(view.root.root.findByType('select').props.value, '')
  assert.equal(view.requests.find(request => request.pathname === '/api/jobs' && request.searchParams.has('page')).searchParams.has('state'), false)
  await view.select(0, 'completed')
  assert.equal(view.params.has('page'), false)
  assert.equal(view.params.get('state'), 'completed')
})

test('server logs include Critical filtering and preserve search/level through pagination and browser back', async t => {
  const view = await mountList(t, Logs, '/logs?page=2&level=critical&search=encoder')
  assert.equal(view.root.root.findByType('select').props.value, 'CRITICAL')
  assert.ok(view.root.root.findAllByType('option').some(option => option.props.value === 'CRITICAL'))
  assert.equal(view.requests.at(-1).searchParams.get('level'), 'CRITICAL')
  await view.next()
  assert.equal(view.params.get('page'), '3')
  assert.equal(view.params.get('search'), 'encoder')
  assert.equal(view.requests.at(-1).searchParams.get('level'), 'CRITICAL')
  await view.back()
  assert.equal(view.params.get('page'), '2')
  assert.equal(view.root.root.findAllByType('input').find(input => input.props.type !== 'checkbox').props.value, 'encoder')
})

test('head-unit log filters expose Debug/Verbose and source changes clear incompatible level and tag', async t => {
  const view = await mountList(t, Logs, '/logs?source=unit&page=2&level=DEBUG&tag=Recorder&search=buffer')
  const level = view.root.root.findAllByType('select')[0]
  assert.equal(level.props.value, 'D')
  assert.ok(level.findAllByType('option').some(option => option.props.value === 'D'))
  assert.ok(level.findAllByType('option').some(option => option.props.value === 'V'))
  assert.equal(view.requests.find(request => request.pathname === '/api/unit-logs').searchParams.get('level'), 'D')
  await act(async () => view.root.root.findAllByType('button').find(button => button.children.includes('Server')).props.onClick())
  await settle()
  assert.equal(view.params.has('page'), false)
  assert.equal(view.params.has('level'), false)
  assert.equal(view.params.has('tag'), false)
  assert.equal(view.params.get('search'), 'buffer')
})
