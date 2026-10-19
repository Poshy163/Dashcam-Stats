import assert from 'node:assert/strict'
import { after, test } from 'node:test'
import { mkdtemp, rm } from 'node:fs/promises'
import { fileURLToPath, pathToFileURL } from 'node:url'
import path from 'node:path'
import { build } from 'esbuild'
import React from 'react'
import { act, create } from 'react-test-renderer'

const directory = await mkdtemp(fileURLToPath(new URL('../node_modules/.map-viewport-test-', import.meta.url)))
after(() => rm(directory, { recursive: true, force: true }))
await build({
  stdin: {
    contents: `export { default as FitMapToData } from '@/components/FitMapToData';`,
    resolveDir: fileURLToPath(new URL('..', import.meta.url)), loader: 'tsx',
  },
  outfile: path.join(directory, 'viewport.mjs'), bundle: true, format: 'esm', platform: 'node', jsx: 'automatic',
  external: ['react', 'react/*'], alias: { '@': fileURLToPath(new URL('../src', import.meta.url)) },
  plugins: [{
    name: 'map-context',
    setup(build) {
      build.onResolve({ filter: /^react-leaflet$/ }, () => ({ path: 'map-context', namespace: 'test' }))
      build.onLoad({ filter: /.*/, namespace: 'test' }, () => ({ contents: 'export function useMap() { return globalThis.__viewportTestMap }', loader: 'js' }))
    },
  }],
})
const { FitMapToData } = await import(pathToFileURL(path.join(directory, 'viewport.mjs')).href)

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
