import assert from 'node:assert/strict'
import { after, test } from 'node:test'
import { mkdtemp, rm } from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { build } from 'esbuild'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

const directory = await mkdtemp(fileURLToPath(new URL('../node_modules/.carplay-outcome-', import.meta.url)))
after(() => rm(directory, { recursive: true, force: true }))
const outfile = path.join(directory, 'outcome.mjs')
await build({
  entryPoints: [fileURLToPath(new URL('../src/components/ObdCarPlayOutcome.tsx', import.meta.url))],
  outfile, bundle: true, format: 'esm', platform: 'node', jsx: 'automatic',
  external: ['react', 'react/*', '@tanstack/react-query'],
  alias: { '@': fileURLToPath(new URL('../src', import.meta.url)) },
})
const { ObdCarPlayOutcome } = await import(pathToFileURL(outfile))
const mark = { at: '2026-11-27T00:00:00Z', afterIgnitionS: 0 }
const empty = {
  status: 'unknown', ignitionAt: mark.at, observedUntil: mark.at,
  hotspot: null, savedPhoneLease: null, phoneHandshake: null, videoSession: null, helper: null,
}
function render(overrides) {
  const client = new QueryClient()
  client.setQueryData(['obd-carplay-outcome', 'drive'], { ...empty, ...overrides })
  return renderToStaticMarkup(React.createElement(QueryClientProvider, { client },
    React.createElement(ObdCarPlayOutcome, { driveId: 'drive' })))
}

test('a requested reconnect alone leaves the outcome unknown', () => {
  const html = render({ helper: { ...mark, action: 'waiting_cycle', afterIgnitionS: 109 } })
  assert.match(html, /CarPlay outcome unknown/)
  assert.match(html, /Disconnect and reconnect requested at \+109 s/)
  assert.match(html, /Unobserved/)
})

test('a confirmed handoff labels its estimated decoder time separately from a displayed frame', () => {
  const html = render({ status: 'carplay_video', videoSession: { ...mark, afterIgnitionS: 49.4 } })
  assert.match(html, /Saved phone handshake and ZLink video confirmed/)
  assert.match(html, /ZLink video decoder start \(estimated\)/)
  assert.match(html, /\+49.4 s/)
  assert.match(html, /first frame shown on screen was not measured/)
})

test('an ambiguous ignition has no attributed timeline', () => {
  const html = render({ ignitionAt: null })
  assert.match(html, /could not link this recording to one ignition start/)
  assert.doesNotMatch(html, /Hotspot active/)
})
