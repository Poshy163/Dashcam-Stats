import assert from 'node:assert/strict'
import { after, test } from 'node:test'
import { mkdtemp, rm } from 'node:fs/promises'
import { fileURLToPath, pathToFileURL } from 'node:url'
import path from 'node:path'
import { build } from 'esbuild'
import React from 'react'
import { act, create } from 'react-test-renderer'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

const directory = await mkdtemp(fileURLToPath(new URL('../node_modules/.dashboard-status-test-', import.meta.url)))
after(() => rm(directory, { recursive: true, force: true }))
await build({
  stdin: {
    contents: `export { default as Dashboard } from '@/pages/Dashboard';`,
    resolveDir: fileURLToPath(new URL('..', import.meta.url)),
    loader: 'tsx',
  },
  outfile: path.join(directory, 'dashboard.mjs'), bundle: true, format: 'esm', platform: 'node', jsx: 'automatic',
  external: ['react', 'react/*', 'react-router-dom', '@tanstack/react-query'],
  alias: { '@': fileURLToPath(new URL('../src', import.meta.url)) },
})
const { Dashboard } = await import(pathToFileURL(path.join(directory, 'dashboard.mjs')).href)

const hardware = {
  cpu: { model: 'Test CPU', cores: 4 },
  gpu: { name: 'Test GPU' },
  inference: { backend: { available: true, engine: 'OpenVINO', device: 'GPU', accelerated: true } },
  policy: { decode: 'vaapi', decodeReason: null, gpuInferenceDisabled: null },
  notes: [],
}
const cpuFallback = {
  ...hardware,
  inference: { backend: { available: true, engine: 'OpenVINO', device: 'CPU', accelerated: false } },
  policy: { decode: 'software', decodeReason: null, gpuInferenceDisabled: 'CL_OUT_OF_RESOURCES' },
}

async function mountDashboard(t, { processing = {}, runtime = hardware, features = [], ingest = {}, fail } = {}) {
  const status = {
    totals: { recordings: 10, journeys: 2, footageBytes: 1000, durationS: 600, plates: 4 },
    processing: { completed: 10, pending: 0, processing: 0, failed: 0, invalid: 0, settling: 0, recordingsToday: 0, throughputPerHour: null, ...processing },
    storage: { usedBytes: 1000, limitBytes: 10000, cleanupEnabled: false, deletionEnabled: false, idleDeleteEnabled: false, parkedDeleteEnabled: false },
    latestJourney: null, hardware, features,
  }
  const oldFetch = globalThis.fetch
  globalThis.fetch = async url => {
    const { pathname } = new URL(url, 'http://localhost')
    if (pathname === fail) return new Response(JSON.stringify({ detail: 'The test service is unavailable' }), { status: 503 })
    const data = {
      '/api/status': status,
      '/api/ingest/status': { state: 'offline', unitOnline: false, lastError: null, backlogKnown: true, backlogFiles: 0, lastSuccessTs: null, ...ingest },
      '/api/system/hardware': runtime,
      '/api/jobs': { items: [], page: 1, pages: 0, total: 0 },
    }[pathname]
    assert.notEqual(data, undefined, `Unexpected dashboard request: ${pathname}`)
    return new Response(JSON.stringify(data))
  }
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } })
  let root
  t.after(async () => {
    if (root) await act(async () => root.unmount())
    client.clear()
    globalThis.fetch = oldFetch
  })
  await act(async () => { root = create(React.createElement(QueryClientProvider, { client }, React.createElement(MemoryRouter, null, React.createElement(Dashboard)))) })
  for (let n = 0; n < 4; n++) await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
  const banner = root.root.findByProps({ 'aria-label': 'System status' })
  return {
    root, banner,
    heading: banner.findByType('h2').children.join(''),
    summary: banner.findByType('p').children.join(''),
    action: banner.findByType('a'),
  }
}

test('idle CPU fallback alert explains the warning and opens Advanced diagnostics', async t => {
  const view = await mountDashboard(t, { runtime: cpuFallback })
  assert.equal(view.heading, 'System needs attention')
  assert.match(view.summary, /GPU inference is disabled; using CPU/)
  assert.doesNotMatch(view.summary, /0 recordings queued/)
  assert.equal(view.action.props.href, '/settings?category=advanced')
  assert.equal(view.action.children.join(''), 'View diagnostics')
})

test('idle backup failure explains the error and links directly to Backup', async t => {
  const view = await mountDashboard(t, { ingest: { state: 'error', lastError: 'The transfer failed' } })
  assert.equal(view.heading, 'System needs attention')
  assert.match(view.summary, /last backup attempt reported an error/)
  assert.equal(view.action.props.href, '/backup')
  assert.equal(view.action.children.join(''), 'View backup')
})

test('failed recordings take priority over hardware and backup warnings and open the failed-recording filter', async t => {
  const view = await mountDashboard(t, { processing: { failed: 1, invalid: 2 }, runtime: cpuFallback, ingest: { state: 'error' } })
  assert.equal(view.summary, '1 recording failed analysis.')
  assert.equal(view.action.props.href, '/recordings?state=failed')
  assert.equal(view.action.children.join(''), 'Review failed recordings')
  assert.ok(view.root.root.findAllByType('a').some(link => link.props.href === '/recordings?state=failed' && JSON.stringify(link.children).includes('failed recordings')))
  assert.equal(view.root.root.findAllByType('a').some(link => link.props.href === '/queue?state=failed'), false)
})

test('unusable recordings open the matching recording filter', async t => {
  const view = await mountDashboard(t, { processing: { invalid: 2 } })
  assert.equal(view.summary, '2 unusable recordings need review.')
  assert.equal(view.action.props.href, '/recordings?state=invalid')
})

test('blocked features name the unavailable feature and reason before opening Settings', async t => {
  const view = await mountDashboard(t, { features: [{ key: 'plates', label: 'Plate recognition', blockedReason: 'Required models are missing.' }] })
  assert.equal(view.summary, 'Plate recognition is unavailable. Required models are missing.')
  assert.equal(view.action.props.href, '/settings')
  assert.equal(view.action.children.join(''), 'Review settings')
})

test('polling failures name the unavailable check and offer the relevant destination', async t => {
  for (const [fail, message, href] of [
    ['/api/system/hardware', 'Could not check the processing runtime.', '/settings?category=advanced'],
    ['/api/ingest/status', 'Could not check backup status.', '/backup'],
    ['/api/jobs', 'Recent processing activity could not be loaded.', '/queue'],
  ]) {
    await t.test(fail, async t => {
      const view = await mountDashboard(t, { fail })
      assert.equal(view.heading, 'System needs attention')
      assert.equal(view.summary, message)
      assert.equal(view.action.props.href, href)
    })
  }
})

test('active or waiting processing keeps queue progress and its queue action', async t => {
  for (const [processing, summary] of [
    [{ processing: 1, pending: 3 }, '1 stream running · 3 clips waiting'],
    [{ processing: 2, pending: 3 }, '2 streams running · 3 clips waiting'],
    [{ pending: 1 }, '1 recording queued'],
  ]) {
    await t.test(summary, async t => {
      const view = await mountDashboard(t, { processing, runtime: cpuFallback })
      assert.equal(view.heading, 'System needs attention')
      assert.equal(view.summary, summary)
      assert.equal(view.action.props.href, '/queue')
      assert.equal(view.action.children.join(''), 'View queue')
    })
  }
})

test('healthy idle processing preserves the operational message', async t => {
  const view = await mountDashboard(t)
  assert.equal(view.heading, 'All systems operational')
  assert.equal(view.summary, '0 recordings queued')
  assert.equal(view.action.props.href, '/queue')
})
