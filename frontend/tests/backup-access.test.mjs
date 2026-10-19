import assert from 'node:assert/strict'
import { after, test } from 'node:test'
import { mkdtemp, rm } from 'node:fs/promises'
import { fileURLToPath, pathToFileURL } from 'node:url'
import path from 'node:path'
import { build } from 'esbuild'
import React from 'react'
import { act, create } from 'react-test-renderer'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, useLocation } from 'react-router-dom'

const directory = await mkdtemp(fileURLToPath(new URL('../node_modules/.backup-access-test-', import.meta.url)))
after(() => rm(directory, { recursive: true, force: true }))
await build({
  entryPoints: [fileURLToPath(new URL('../src/pages/Backup.tsx', import.meta.url))],
  outfile: path.join(directory, 'backup.mjs'), bundle: true, format: 'esm', platform: 'node', jsx: 'automatic',
  external: ['react', 'react/*', 'react-router-dom', '@tanstack/react-query'],
  alias: { '@': fileURLToPath(new URL('../src', import.meta.url)) },
})
const { default: Backup } = await import(pathToFileURL(path.join(directory, 'backup.mjs')).href)

const idle = {
  state: 'idle', phase: 'idle', unit_online: false, files_total: 0, files_done: 0,
  bytes_total: 0, bytes_done: 0, throughput_mbs: 0, speed_mbs_recent: 0,
  backlog_known: false, backlog_files: 0, backlog_bytes: 0,
}
const bundle = (id, state = 'stored') => ({
  id, drive_id: `retained-drive-${id}`, drive_started_at: '2026-10-01T04:00:00Z',
  sample_count: 20, state, metadata_trusted: true,
  last_error: state === 'failed' ? 'Retained archive needs validation' : null,
})
const paginated = (items, page = 1, pages = 1, total = items.length) => ({ items, page, pages, total, page_size: 10 })
const button = (root, label) => root.root.findAllByType('button').find(node => node.children.join('') === label)
const text = root => JSON.stringify(root.toJSON())
async function flush() {
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
}

async function mount(t, { entry = '/backup', getStatus = () => idle, getBundles = () => paginated([bundle(11)]), eventStream } = {}) {
  const requests = []
  const oldFetch = globalThis.fetch
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity }, mutations: { retry: false } } })
  globalThis.fetch = async (url, init = {}) => {
    const request = new URL(url, 'http://localhost')
    requests.push({ url: request, method: init.method ?? 'GET' })
    let body
    switch (request.pathname) {
      case '/api/ingest/status': body = await getStatus(); break
      case '/api/ingest/radio-status': body = { quieting_enabled: false, transition: null }; break
      case '/api/ingest/history': body = paginated([{ id: 90, started_at: '2026-10-01T04:00:00Z', state: 'ok', files_transferred: 2, bytes_transferred: 100 }]); break
      case '/api/obd/status': body = { stored_drive_count: 11, failed_count: 1, copy_throughput_mbs: 0, event_stream: eventStream }; break
      case '/api/obd/bundles': body = getBundles(Number(request.searchParams.get('page'))); break
      case '/api/obd/events': body = paginated([]); break
      case '/api/obd/bundles/1/validate': body = { valid: true, bundle: bundle(1) }; break
      default: throw new Error(`Unexpected request ${request.pathname}`)
    }
    return new Response(JSON.stringify(body), { status: 200 })
  }
  function Location() {
    return React.createElement('output', { id: 'location' }, useLocation().search)
  }
  let root
  await act(async () => {
    root = create(React.createElement(QueryClientProvider, { client }, React.createElement(MemoryRouter, { initialEntries: [entry] }, React.createElement(Backup), React.createElement(Location))))
  })
  t.after(async () => { await act(async () => root.unmount()); client.clear(); globalThis.fetch = oldFetch })
  await flush()
  return { root, client, requests }
}

test('stored-bundle pagination reaches and validates older failures while preserving other URL parameters', async t => {
  const { root, requests } = await mount(t, {
    entry: '/backup?debug=1',
    getBundles: page => paginated([bundle(page === 1 ? 11 : 1, page === 1 ? 'stored' : 'failed')], page, 2, 11),
  })
  assert.match(text(root), /retained-drive-11/)
  assert.equal(button(root, 'Previous').props.disabled, true)
  await act(async () => button(root, 'Next').props.onClick())
  await flush()
  assert.match(text(root), /retained-drive-1/)
  assert.match(text(root), /Retained archive needs validation/)
  assert.equal(root.root.findByProps({ id: 'location' }).children.join(''), '?debug=1&bundle_page=2')
  assert.ok(requests.some(({ url }) => url.pathname === '/api/obd/bundles' && url.searchParams.get('page') === '2'))
  await act(async () => button(root, 'Validate').props.onClick())
  await flush()
  assert.ok(requests.some(({ url, method }) => url.pathname === '/api/obd/bundles/1/validate' && method === 'POST'))
  assert.match(text(root), /OBD bundle validated/)
  await act(async () => button(root, 'Previous').props.onClick())
  await flush()
  assert.equal(root.root.findByProps({ id: 'location' }).children.join(''), '?debug=1')
  assert.match(text(root), /retained-drive-11/)
})

test('invalid bundle bookmarks load the first page and empty old pages retain a way back', async t => {
  for (const value of ['bad', '0', '-1', '1.5', '1000000001']) {
    await t.test(`invalid page ${value}`, async t => {
      const { requests } = await mount(t, { entry: `/backup?bundle_page=${value}` })
      const calls = requests.filter(({ url }) => url.pathname === '/api/obd/bundles')
      assert.equal(calls[0].url.searchParams.get('page'), '1', value)
    })
  }
  const { root } = await mount(t, { entry: '/backup?bundle_page=9', getBundles: page => paginated([], page, 1, 0) })
  assert.match(text(root), /No stored OBD bundles on this page/)
  assert.equal(button(root, 'Previous').props.disabled, false)
  await act(async () => button(root, 'Previous').props.onClick())
  await flush()
  assert.equal(root.root.findByProps({ id: 'location' }).children.join(''), '')
})

test('failed background status refresh retains independent sections and marks cached progress stale until retry succeeds', async t => {
  let fail = false
  const running = { ...idle, state: 'running', phase: 'transferring', speed_mbs_recent: 6 }
  const { root, client } = await mount(t, { getStatus: () => { if (fail) throw new Error('Status temporarily unavailable'); return running } })
  assert.equal(root.root.findByProps({ label: 'Speed' }).props.value, '6.0 MB/s')
  fail = true
  await act(async () => { await client.refetchQueries({ queryKey: ['ingest-status'] }) })
  await flush()
  assert.match(text(root), /Could not refresh backup status/)
  assert.match(text(root), /Showing last known backup values/)
  assert.match(text(root), /retained-drive-11/)
  assert.match(text(root), /Head-unit activity/)
  assert.match(text(root), /Recent transfers/)
  assert.ok(button(root, 'Recover stored bundles'))
  assert.equal(root.root.findByProps({ label: 'Status' }).props.value, 'Status unavailable')
  assert.equal(root.root.findByProps({ label: 'Speed' }).props.value, '—')
  assert.equal(root.root.findAllByProps({ role: 'progressbar' }).length, 0)
  assert.ok(button(root, 'Cancel'), 'cancellation remains reachable for a previously running transfer')
  fail = false
  await act(async () => button(root, 'Try again').props.onClick())
  await flush()
  assert.doesNotMatch(text(root), /Showing last known backup values/)
  assert.equal(root.root.findByProps({ label: 'Speed' }).props.value, '6.0 MB/s')
})

test('initial status failure keeps stored data reachable and blocks starting a backup from unknown state', async t => {
  const { root } = await mount(t, { getStatus: () => { throw new Error('Status unavailable') } })
  assert.match(text(root), /Could not refresh backup status/)
  assert.match(text(root), /retained-drive-11/)
  assert.match(text(root), /Head-unit activity/)
  assert.equal(button(root, 'Pull now').props.disabled, true)
  assert.equal(root.root.findByProps({ label: 'Last copied' }).props.value, '—')
  assert.doesNotMatch(text(root), /Showing last known backup values/)
})

test('retained activity cleanup is visible without promising an immediate new sync', async t => {
  const { root } = await mount(t, { eventStream: { sync_active: true, sync_phase: 'cleanup_pending', sync_age_s: 180, last_error: 'timeout' } })
  assert.match(text(root), /Activity sync is waiting for cleanup/)
  assert.match(text(root), /Running for 3m/)
  assert.match(text(root), /after the current attempt finishes/)
  assert.match(text(root), /retained-drive-11/)
  assert.doesNotMatch(text(root), /will retry next visit/)
})

test('activity mirror health follows backend phases and disappears after completion', async t => {
  const { root, client } = await mount(t, { eventStream: { sync_active: true, sync_phase: 'reading', sync_age_s: 2 } })
  assert.match(text(root), /Reading the activity snapshot/)
  await act(async () => client.setQueryData(['obd-status'], current => ({ ...current, eventStream: { syncActive: true, syncPhase: 'storing', syncAgeS: 3 } })))
  await flush()
  assert.match(text(root), /Saving the activity snapshot/)
  await act(async () => client.setQueryData(['obd-status'], current => ({ ...current, eventStream: { syncActive: false, syncPhase: 'idle', lastError: null } })))
  await flush()
  assert.doesNotMatch(text(root), /Reading the activity snapshot|Saving the activity snapshot|waiting for cleanup/)
})
