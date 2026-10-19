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

const directory = await mkdtemp(fileURLToPath(new URL('../node_modules/.detail-state-test-', import.meta.url)))
after(() => rm(directory, { recursive: true, force: true }))
await build({
  stdin: {
    contents: `export { default as PlateDetail } from '@/pages/PlateDetail'; export { default as JourneyDetail } from '@/pages/JourneyDetail'; export { default as RecordingViewer } from '@/pages/RecordingViewer'; export { default as RouteMap } from '@/components/RouteMap';`,
    resolveDir: fileURLToPath(new URL('..', import.meta.url)), loader: 'tsx',
  },
  outfile: path.join(directory, 'details.mjs'), bundle: true, format: 'esm', platform: 'node', jsx: 'automatic',
  external: ['react', 'react/*', 'react-router-dom', '@tanstack/react-query'],
  alias: { '@': fileURLToPath(new URL('../src', import.meta.url)) }, loader: { '.css': 'empty' },
  plugins: [{
    name: 'leaflet-context',
    setup(build) {
      build.onResolve({ filter: /^(leaflet|react-leaflet)$/ }, args => ({ path: args.path, namespace: 'test' }))
      build.onLoad({ filter: /.*/, namespace: 'test' }, args => ({
        contents: args.path === 'leaflet'
          ? 'export default { divIcon: value => value }'
          : `import React from 'react'; export function useMap() { return globalThis.__detailTestMap }; export const MapContainer = ({ children }) => React.createElement('div', null, children); export const Marker = () => null; export const Popup = () => null; export const Polyline = () => null; export const TileLayer = () => null;`,
        loader: 'js',
      }))
    },
  }],
})
const { PlateDetail, JourneyDetail, RecordingViewer, RouteMap } = await import(pathToFileURL(path.join(directory, 'details.mjs')).href)

async function settle() {
  for (let n = 0; n < 4; n++) await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
}

const plate = id => ({ id, display_text: `PLATE${id}`, best_confidence: 0.95, notes: `Saved notes for plate ${id}`, observation_count: 60, journey_count: 2, flagged: false, dismissed: false })
const sighting = text => ({ id: 44, recording_id: 11, normalised_text: text, raw_text: text, captured_at: '2026-10-19T01:00:00Z', ocr_confidence: 0.9, detection_confidence: 0.9, t_offset_s: 0, lat: null, lon: null })

async function mountDetail(t, { Component = PlateDetail, route = '/plates/:id', initial = '/plates/1', patchFails = false, correctionId = 2, correctionResponse, mergeResponse, observationsFor = () => [] } = {}) {
  const requests = []
  const oldFetch = globalThis.fetch
  globalThis.fetch = async (url, init) => {
    const request = new URL(url, 'http://localhost')
    requests.push({ url: request, init })
    if (request.pathname === '/api/settings') return new Response('[]')
    const match = request.pathname.match(/^\/api\/plates\/(\d+)(?:\/(observations|correct|merge))?$/)
    assert.ok(match, `Unexpected request: ${url}`)
    if (match[2] === 'observations') return new Response(JSON.stringify({ items: await observationsFor(Number(match[1])), page: Number(request.searchParams.get('page') ?? 1), pages: 3, total: 60, page_size: 25 }))
    if (match[2] === 'correct') return correctionResponse ?? new Response(JSON.stringify(plate(correctionId)))
    if (match[2] === 'merge') return mergeResponse ?? new Response(JSON.stringify(plate(correctionId)))
    if (init?.method === 'PATCH' && patchFails) return new Response(JSON.stringify({ detail: 'Synthetic notes save failure' }), { status: 503 })
    return new Response(JSON.stringify(plate(Number(match[1]))))
  }
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } })
  let root, navigate, location
  function Observer() { navigate = useNavigate(); location = useLocation(); return null }
  t.after(async () => {
    if (root) await act(async () => root.unmount())
    client.clear()
    globalThis.fetch = oldFetch
  })
  await act(async () => {
    root = create(React.createElement(QueryClientProvider, { client }, React.createElement(MemoryRouter, { initialEntries: [initial] },
      React.createElement(Observer), React.createElement(Routes, null, React.createElement(Route, { path: route, element: React.createElement(Component) })),
    )))
  })
  await settle()
  return {
    root, requests,
    get pathname() { return location.pathname },
    input: label => root.root.findByProps({ 'aria-label': label }),
    button: label => root.root.findAllByType('button').find(button => button.children.join('') === label),
    async go(url) { await act(async () => navigate(url)); await settle() },
    async notes(value) { await act(async () => root.root.findByType('textarea').props.onChange({ target: { value } })) },
    async change(label, value) { await act(async () => this.input(label).props.onChange({ target: { value } })) },
    async click(label) { await act(async () => this.button(label).props.onClick()); await settle() },
  }
}

test('switching plates clears the previous editor, mutation state and page before the new sightings request', async t => {
  const view = await mountDetail(t, { patchFails: true })
  await view.notes('Draft intended only for plate one')
  await view.change('Correct plate text', 'FIRST')
  await view.change('Target plate ID for merge', '99')
  await view.click('Save notes')
  assert.match(JSON.stringify(view.root.toJSON()), /Synthetic notes save failure/)
  await view.click('Next')
  await view.go('/plates/2')
  assert.equal(view.root.root.findByType('textarea').props.value, 'Saved notes for plate 2')
  assert.equal(view.input('Correct plate text').props.value, '')
  assert.equal(view.input('Target plate ID for merge').props.value, '')
  assert.equal(view.button('Save notes'), undefined)
  assert.doesNotMatch(JSON.stringify(view.root.toJSON()), /Synthetic notes save failure/)
  const sightings = view.requests.filter(request => request.url.pathname === '/api/plates/2/observations')
  assert.equal(sightings.length, 1)
  assert.equal(sightings[0].url.searchParams.get('page'), '1')
  assert.equal(view.requests.filter(request => request.url.pathname === '/api/plates/2' && request.init?.method === 'PATCH').length, 0)
})

test('drafts and sightings page survive navigation within the same plate identity', async t => {
  const view = await mountDetail(t)
  await view.notes('Keep my current draft')
  await view.change('Correct plate text', 'DRAFT')
  await view.change('Target plate ID for merge', '3')
  await view.click('Next')
  await view.go('/plates/1?review=1')
  assert.equal(view.root.root.findByType('textarea').props.value, 'Keep my current draft')
  assert.equal(view.input('Correct plate text').props.value, 'DRAFT')
  assert.equal(view.input('Target plate ID for merge').props.value, '3')
  assert.match(view.root.root.findByProps({ 'aria-label': 'Pagination' }).findAllByType('div')[0].children.join(''), /Page 2 of 3/)
  await view.go('/plates/01')
  assert.equal(view.root.root.findByType('textarea').props.value, 'Keep my current draft', 'canonical IDs name the same entity')
})

test('correcting into an existing plate opens its clean editor and first sightings page', async t => {
  const view = await mountDetail(t)
  await view.notes('Source plate draft')
  await view.click('Next')
  await view.change('Correct plate text', 'EXISTING')
  await view.click('Apply correction')
  assert.equal(view.pathname, '/plates/2')
  assert.equal(view.root.root.findByType('textarea').props.value, 'Saved notes for plate 2')
  assert.equal(view.input('Correct plate text').props.value, '')
  assert.equal(view.requests.find(request => request.url.pathname === '/api/plates/2/observations').url.searchParams.get('page'), '1')
})

for (const operation of ['correction', 'merge']) {
  test(`a ${operation} completing after navigation preserves the new plate and its draft`, async t => {
    let complete
    const response = new Promise(resolve => { complete = resolve })
    const view = await mountDetail(t, operation === 'correction' ? { correctionResponse: response } : { mergeResponse: response })
    await view.change(operation === 'correction' ? 'Correct plate text' : 'Target plate ID for merge', operation === 'correction' ? 'EXISTING' : '2')
    await view.click(operation === 'correction' ? 'Apply correction' : 'Merge into plate')
    await view.go('/plates/3')
    await view.notes('Keep this plate-three draft')
    await act(async () => complete(new Response(JSON.stringify(plate(2)))))
    await settle()
    assert.equal(view.pathname, '/plates/3')
    assert.equal(view.root.root.findByType('textarea').props.value, 'Keep this plate-three draft')
  })
}

test('a late correction refreshes the opened target sightings without discarding its draft', async t => {
  let complete, merged = false
  const correctionResponse = new Promise(resolve => { complete = resolve })
  const view = await mountDetail(t, {
    correctionResponse,
    observationsFor: id => merged && id === 2 ? [sighting('MERGED44')] : [],
  })
  await view.change('Correct plate text', 'EXISTING')
  await view.click('Apply correction')
  await view.go('/plates/2')
  await view.notes('Keep target notes')
  assert.doesNotMatch(JSON.stringify(view.root.toJSON()), /MERGED44/)
  merged = true
  await act(async () => complete(new Response(JSON.stringify(plate(2)))))
  await settle()
  assert.match(JSON.stringify(view.root.toJSON()), /MERGED44/)
  assert.equal(view.root.root.findByType('textarea').props.value, 'Keep target notes')
})

for (const operation of ['correction', 'merge']) test(`an older pending target read cannot hide sightings added by a ${operation}`, async t => {
  let complete, releaseOldRead, targetReads = 0
  const correctionResponse = new Promise(resolve => { complete = resolve })
  const oldTargetRead = new Promise(resolve => { releaseOldRead = resolve })
  const fresh = [sighting('FRESH44')]
  const view = await mountDetail(t, {
    ...(operation === 'correction' ? { correctionResponse } : { mergeResponse: correctionResponse }),
    observationsFor: id => id !== 2 ? [] : ++targetReads === 1 ? oldTargetRead : fresh,
  })
  await view.change(operation === 'correction' ? 'Correct plate text' : 'Target plate ID for merge', operation === 'correction' ? 'EXISTING' : '2')
  await view.click(operation === 'correction' ? 'Apply correction' : 'Merge into plate')
  await view.go('/plates/2')
  await view.notes('Keep target draft')
  await act(async () => complete(new Response(JSON.stringify(plate(2)))))
  await settle()
  await act(async () => releaseOldRead([]))
  await settle()
  assert.match(JSON.stringify(view.root.toJSON()), /FRESH44/)
  assert.equal(view.root.root.findByType('textarea').props.value, 'Keep target draft')
})

test('invalid detail row IDs show a recoverable error without issuing API requests', async t => {
  for (const [Component, collection, title] of [[PlateDetail, 'plates', 'plate'], [JourneyDetail, 'journeys', 'journey'], [RecordingViewer, 'recordings', 'recording']]) {
    await t.test(collection, async t => {
      for (const id of ['not-a-number', '0', '-1', '1.5', '9007199254740992']) {
        await t.test(id, async t => {
          const view = await mountDetail(t, { Component, route: `/${collection}/:id`, initial: `/${collection}/${id}` })
          assert.ok(view.root.root.findAllByProps({ role: 'alert' }).length > 0)
          assert.match(JSON.stringify(view.root.toJSON()), new RegExp(`Invalid ${title} ID`))
          assert.equal(view.root.root.findByType('a').props.href, `/${collection}`)
          assert.deepEqual(view.requests, [])
        })
      }
    })
  }
})

test('route maps preserve panning for unchanged coverage and retain a single-point zoom of sixteen', async t => {
  const oldMap = globalThis.__detailTestMap
  const changes = []
  globalThis.__detailTestMap = { setView: (point, zoom) => changes.push({ point, zoom }), fitBounds: (bounds, options) => changes.push({ bounds, options }) }
  let root
  t.after(async () => {
    if (root) await act(async () => root.unmount())
    if (oldMap === undefined) delete globalThis.__detailTestMap; else globalThis.__detailTestMap = oldMap
  })
  const one = label => [{ lat: -34.8, lon: 138.6, label }]
  await act(async () => { root = create(React.createElement(RouteMap, { markers: one('First label') })) })
  assert.deepEqual(changes, [{ point: [-34.8, 138.6], zoom: 16 }])
  await act(async () => root.update(React.createElement(RouteMap, { markers: one('Updated label, same location') })))
  assert.equal(changes.length, 1)
  const markers = [...one('First'), { lat: -35, lon: 139, label: 'Second' }]
  await act(async () => root.update(React.createElement(RouteMap, { markers })))
  assert.deepEqual(changes[1], { bounds: [[-35, 138.6], [-34.8, 139]], options: { padding: [24, 24] } })
  await act(async () => root.update(React.createElement(RouteMap, { markers: [...markers].reverse().map(marker => ({ ...marker })) })))
  assert.equal(changes.length, 2)
  await act(async () => root.update(React.createElement(RouteMap, { markers: [{ lat: -33, lon: 137, label: 'New location' }] })))
  assert.deepEqual(changes[2], { point: [-33, 137], zoom: 16 })
})
