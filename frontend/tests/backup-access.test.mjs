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

async function mount(t, {
  entry = '/backup', getStatus = () => idle, getBundles = () => paginated([bundle(11)]), eventStream,
  getRadioStatus = () => ({ quieting_enabled: false, transition: null }),
  getObdStatus = () => ({ stored_drive_count: 11, failed_count: 1, copy_throughput_mbs: 0, event_stream: eventStream }),
  onCancel = () => ({ cancelled: true }),
} = {}) {
  const requests = []
  const oldFetch = globalThis.fetch
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity }, mutations: { retry: false } } })
  globalThis.fetch = async (url, init = {}) => {
    const request = new URL(url, 'http://localhost')
    requests.push({ url: request, method: init.method ?? 'GET' })
    let body
    switch (request.pathname) {
      case '/api/ingest/status': body = await getStatus(); break
      case '/api/ingest/radio-status': body = await getRadioStatus(); break
      case '/api/ingest/cancel': body = await onCancel(); break
      case '/api/ingest/history': body = paginated([{ id: 90, started_at: '2026-10-01T04:00:00Z', state: 'ok', files_transferred: 2, bytes_transferred: 100 }]); break
      case '/api/obd/status': body = await getObdStatus(); break
      case '/api/system/info': body = { version: 'main', source_revision: 'abcdef0123456789' }; break
      case '/api/unit-logs/temperature': body = { source: 'retained_carplay_timing', sensor: 'soc-thmzone', hours: 24, range_start: '2026-10-01T00:00:00Z', range_end: '2026-10-02T00:00:00Z', bucket_s: 300, gap_threshold_s: 90, latest: null, minimum_c: null, maximum_c: null, unique_observation_count: 0, source_row_count: 0, input_truncated: false, output_truncated: false, truncated: false, points: [] }; break
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

test('an acknowledged stop stays disabled through shutdown, then prevents a new pull until recovery settles', async t => {
  let current = { ...idle, state: 'running', phase: 'transferring' }
  let transition = {
    phase: 'restoring_radios', active: true, recovery_required: true,
    bluetooth: { baseline: 'on', disable_attempted: true, restore_verified: false },
    hotspot: { baseline: 'on', disable_attempted: true, restore_verified: false },
    obd_logger: { quiesce_attempted: true, resume_attempted: true, resume_verified: false },
  }
  const { root, client, requests } = await mount(t, {
    getStatus: () => current,
    getRadioStatus: () => ({ quieting_enabled: true, transition }),
    onCancel: () => { current = { ...current, cancellation_requested: true }; return { cancelled: true } },
  })
  await act(async () => button(root, 'Cancel').props.onClick())
  await flush()
  assert.equal(button(root, 'Stopping…').props.disabled, true)
  assert.equal(root.root.findByProps({ label: 'Status' }).props.value, 'Stopping backup')
  assert.match(text(root), /stop request is acknowledged/)
  assert.equal(requests.filter(({ url }) => url.pathname === '/api/ingest/cancel').length, 1)

  current = { ...current, state: 'cancelled', phase: 'idle', cancellation_requested: false }
  await act(async () => { await client.refetchQueries({ queryKey: ['ingest-status'] }) })
  await flush()
  assert.match(text(root), /Backup stopped/)
  assert.match(text(root), /Logger resumption is not confirmed yet/)
  assert.equal(button(root, 'Pull now').props.disabled, true)

  transition = {
    ...transition, active: false, recovery_required: false, phase: 'complete',
    bluetooth: { ...transition.bluetooth, restore_verified: true },
    hotspot: { ...transition.hotspot, restore_verified: true },
    obd_logger: { ...transition.obd_logger, resume_verified: true },
  }
  await act(async () => { await client.refetchQueries({ queryKey: ['ingest-radio-status'] }) })
  await flush()
  assert.equal(button(root, 'Pull now').props.disabled, false)
  assert.match(text(root), /logger was verified resumed/)
})

test('a reloaded page reads an existing stop request and a failed request can be retried', async t => {
  await t.test('server stop acknowledgement survives a fresh page', async t => {
    const { root } = await mount(t, { getStatus: () => ({ ...idle, state: 'running', cancellation_requested: true }) })
    assert.equal(button(root, 'Stopping…').props.disabled, true)
    assert.match(text(root), /stop request is acknowledged/)
  })
  await t.test('request failure is visible and does not claim acknowledgement', async t => {
    const { root } = await mount(t, {
      getStatus: () => ({ ...idle, state: 'running' }),
      onCancel: () => { throw new Error('Stop request failed') },
    })
    await act(async () => button(root, 'Cancel').props.onClick())
    await flush()
    assert.match(text(root), /Could not cancel the backup/)
    assert.equal(button(root, 'Cancel').props.disabled, false)
    assert.doesNotMatch(text(root), /stop request is acknowledged/)
  })
})

test('an offline dashcam cannot leave the summary claiming a live ECU connection', async t => {
  const { root, requests } = await mount(t, {
    getObdStatus: () => ({ logger: { state: 'ecu_online', ecu_connected: true, updated_at_utc: new Date().toISOString() } }),
  })
  const tile = root.root.findByProps({ label: 'Logger state' })
  assert.equal(tile.props.value, 'Dashcam offline')
  assert.equal(tile.props.tone, 'default')
  assert.ok(requests.some(({ url }) => url.pathname === '/api/obd/events' && url.searchParams.get('kind') === 'ingest.webhook'))
  assert.match(text(root), /abcdef012345/)
})

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
