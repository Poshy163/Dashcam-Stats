import assert from 'node:assert/strict'
import { after, test } from 'node:test'
import { mkdtemp, rm } from 'node:fs/promises'
import { fileURLToPath, pathToFileURL } from 'node:url'
import path from 'node:path'
import { build } from 'esbuild'
import React from 'react'
import { act, create } from 'react-test-renderer'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const directory = await mkdtemp(fileURLToPath(new URL('../node_modules/.map-viewport-test-', import.meta.url)))
after(() => rm(directory, { recursive: true, force: true }))
await build({
  stdin: {
    contents: `export { default as FitMapToData } from '@/components/FitMapToData';
      export { default as HeatmapPage } from '@/pages/Heatmap';
      export { mapBounds } from '@/lib/mapBounds';
      export { api } from '@/lib/api';
      export { invalidateAnalysisQueries, MAP_REVISION_KEY, refreshMapRevision } from '@/lib/queryInvalidation';`,
    resolveDir: fileURLToPath(new URL('..', import.meta.url)), loader: 'tsx',
  },
  outfile: path.join(directory, 'viewport.mjs'), bundle: true, format: 'esm', platform: 'node', jsx: 'automatic',
  external: ['react', 'react/*', '@tanstack/react-query'], alias: { '@': fileURLToPath(new URL('../src', import.meta.url)) },
  plugins: [{
    name: 'map-context',
    setup(build) {
      build.onResolve({ filter: /^react-leaflet$/ }, () => ({ path: 'map-context', namespace: 'test' }))
      build.onResolve({ filter: /^@\/components\/HeatmapCanvas$/ }, () => ({ path: 'canvas', namespace: 'canvas-stub' }))
      build.onLoad({ filter: /.*/, namespace: 'canvas-stub' }, () => ({ contents: 'export default function Canvas() { return null }' }))
      build.onLoad({ filter: /.*/, namespace: 'test' }, () => ({ contents: 'export function useMap() { return globalThis.__viewportTestMap }', loader: 'js' }))
    },
  }],
})
const { FitMapToData, HeatmapPage, mapBounds, api, invalidateAnalysisQueries, MAP_REVISION_KEY, refreshMapRevision } =
  await import(pathToFileURL(path.join(directory, 'viewport.mjs')).href)

test('all cached map filters refresh only when the server map revision changes', async t => {
  const client = new QueryClient()
  t.after(() => client.clear())
  let revision = 'before'
  t.mock.method(globalThis, 'fetch', async url => {
    assert.equal(url, '/api/system/map-revision')
    return { ok: true, json: async () => revision }
  })
  const mapKeys = [['heatmap', 3, 5], ['heatmap', 4, 0], ['map-routes']]
  for (const key of mapKeys) client.setQueryData(key, { sourceRevision: revision })
  client.setQueryData(['recordings'], {})
  client.setQueryData(MAP_REVISION_KEY, revision)
  const refreshRevision = () => client.fetchQuery({
    queryKey: MAP_REVISION_KEY,
    queryFn: ({ signal }) => refreshMapRevision(client, signal),
  })

  await invalidateAnalysisQueries(client)
  await refreshRevision()
  assert.equal(client.getQueryState(['recordings']).isInvalidated, true)
  assert.ok(mapKeys.every(key => !client.getQueryState(key).isInvalidated))

  revision = 'after'
  await refreshRevision()
  assert.ok(mapKeys.every(key => client.getQueryState(key).isInvalidated))
})

test('first Map opens both layers before the revision arrives and repairs a late stale response', async t => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const paths = []
  let releaseRevision, releaseRoutes
  const slowRevision = new Promise(resolve => { releaseRevision = resolve })
  const slowRoutes = new Promise(resolve => { releaseRoutes = resolve })
  const reply = (body, revision) => ({
    ok: true, headers: new Headers(revision ? { 'X-Map-Revision': revision } : {}), json: async () => body,
  })
  const heat = { points: [[-35, 139, 1]], cells: 1, total_points: 1, max_weight: 1 }
  const routes = { lines: [], journeys: 0, segments: 0 }
  t.mock.method(globalThis, 'fetch', async url => {
    paths.push(url)
    if (url === '/api/settings/display') return reply({})
    if (url === '/api/system/map-revision') return reply(await slowRevision)
    if (url.includes('/map/heatmap?')) return reply(heat, 'current')
    if (url === '/api/map/routes') {
      return reply(routes, paths.filter(path => path === url).length === 1 ? await slowRoutes : 'current')
    }
    assert.fail('unrecognised URL: ' + url)
  })
  let root
  t.after(async () => {
    if (root) await act(async () => root.unmount())
    client.clear()
  })
  await act(async () => {
    root = create(React.createElement(QueryClientProvider, { client }, React.createElement(HeatmapPage)))
  })
  assert.ok(paths.includes('/api/system/map-revision'))
  assert.ok(paths.some(path => path.includes('/map/heatmap?')))
  assert.ok(paths.includes('/api/map/routes'))
  assert.equal(client.getQueryData(['heatmap', 3, 5]).sourceRevision, 'current')

  await act(async () => releaseRevision('current'))
  await act(async () => releaseRoutes('old'))
  assert.equal(client.getQueryData(['map-routes']).sourceRevision, 'old')
  // Simulate the next scheduled poll. No further write occurs so the token stays the same.
  await act(async () => client.refetchQueries({ queryKey: MAP_REVISION_KEY }))
  assert.equal(client.getQueryData(['map-routes']).sourceRevision, 'current')
  assert.equal(paths.filter(path => path.includes('/map/heatmap?')).length, 1)
  assert.equal(paths.filter(path => path.endsWith('/map/routes')).length, 2)
})

test('overlay bounds include disjoint routes and weighted cells while ignoring missing fixes', () => {
  assert.deepEqual(mapBounds([[[-35, 139], [-34, 138]], [[-36, 141], [0, 0], [NaN, 145]]]), [[-36, 138], [-34, 141]])
  assert.deepEqual(mapBounds([[[0, 123, 9], [-1, 125, 2]]]), [[-1, 123], [0, 125]])
  assert.deepEqual(mapBounds([[[], [0, 0]]]), [])
})

test('map API converts metadata and filter keys without recopying coordinates', async t => {
  const lines = [[[-35, 138], [-34, 139]]]
  const points = [[-35, 138, 4]]
  const responses = new Map([
    ['/api/map/routes', { lines, simplify_m: 15 }],
    ['/api/map/heatmap?min_speed_kmh=5', { points, max_weight: 4, total_points: 4 }],
  ])
  t.mock.method(globalThis, 'fetch', async url => {
    assert.ok(responses.has(url))
    return { ok: true, headers: new Headers({ 'X-Map-Revision': 'token' }), json: async () => responses.get(url) }
  })
  const routes = await api.map.routes()
  const heat = await api.map.heatmap({ minSpeedKmh: 5 })
  assert.equal(routes.lines, lines)
  assert.equal(routes.simplifyM, 15)
  assert.equal(heat.points, points)
  assert.equal(heat.maxWeight, 4)
  assert.equal(heat.totalPoints, 4)
  assert.equal(heat.sourceRevision, 'token')
  assert.equal(routes.sourceRevision, 'token')
})

test('map refits changed geographic coverage even when the point count stays the same, without undoing panning for identical bounds', async t => {
  const oldMap = globalThis.__viewportTestMap
  const fits = []
  globalThis.__viewportTestMap = { fitBounds: (bounds, options) => fits.push({ bounds, options }) }
  let root
  t.after(async () => {
    if (root) await act(async () => root.unmount())
    if (oldMap === undefined) delete globalThis.__viewportTestMap; else globalThis.__viewportTestMap = oldMap
  })
  await act(async () => { root = create(React.createElement(FitMapToData, { points: [[-35, 138], [-34, 139]] })) })
  assert.deepEqual(fits, [{ bounds: [[-35, 138], [-34, 139]], options: { padding: [32, 32] } }])
  await act(async () => root.update(React.createElement(FitMapToData, { points: [[-37, 144], [-36, 145]] })))
  assert.equal(fits.length, 2)
  assert.deepEqual(fits[1].bounds, [[-37, 144], [-36, 145]])
  await act(async () => root.update(React.createElement(FitMapToData, { points: [[-36, 145], [-37, 144], [-36.5, 144.5]] })))
  assert.equal(fits.length, 2, 'a new response with unchanged coverage must preserve the user pan/zoom')
  await act(async () => root.update(React.createElement(FitMapToData, { points: [] })))
  assert.equal(fits.length, 2, 'empty coverage does not create invalid bounds')
  await act(async () => root.update(React.createElement(FitMapToData, { points: [[NaN, 1], [1, Infinity]] })))
  assert.equal(fits.length, 2)
  await act(async () => root.update(React.createElement(FitMapToData, { points: [[NaN, 1], [-35, 138]] })))
  assert.equal(fits.length, 3)
  assert.deepEqual(fits[2].bounds, [[-35, 138], [-35, 138]])
})
