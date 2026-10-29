import assert from 'node:assert/strict'
import { after, test } from 'node:test'
import { mkdtemp, rm } from 'node:fs/promises'
import { fileURLToPath, pathToFileURL } from 'node:url'
import path from 'node:path'
import { build } from 'esbuild'
import React from 'react'
import { act, create } from 'react-test-renderer'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const directory = await mkdtemp(fileURLToPath(new URL('../node_modules/.trace-test-', import.meta.url)))
after(() => rm(directory, { recursive: true, force: true }))
await build({
  stdin: {
    contents: "export { default as Card } from '@/components/CarPlayTraceCard'; export { api } from '@/lib/api';",
    resolveDir: fileURLToPath(new URL('..', import.meta.url)), loader: 'tsx',
  },
  outfile: path.join(directory, 'components.mjs'), bundle: true, format: 'esm', platform: 'node', jsx: 'automatic',
  external: ['react', 'react/*', '@tanstack/react-query'],
  alias: { '@': fileURLToPath(new URL('../src', import.meta.url)) },
})
const { Card, api } = await import(pathToFileURL(path.join(directory, 'components.mjs')).href)

async function mount(initial = {}, failStart = false) {
  let state = { requested: false, expiresAt: null, remainingS: 0, passiveEnabled: true, unitOnline: true, ...initial }
  const calls = { start: 0, stop: 0 }
  api.ingest.carplayTraceStatus = async () => state
  api.ingest.startCarplayTrace = async () => {
    calls.start++
    if (failStart) throw new Error('Camera diagnostic request could not be saved')
    state = { ...state, requested: true, expiresAt: '2099-01-01T00:15:00Z', remainingS: 900 }
    return state
  }
  api.ingest.stopCarplayTrace = async () => {
    calls.stop++
    state = { ...state, requested: false, expiresAt: null, remainingS: 0 }
    return state
  }
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  let root
  await act(async () => {
    root = create(React.createElement(QueryClientProvider, { client }, React.createElement(Card)))
  })
  const settle = async () => {
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 15)) })
  }
  await settle()
  return { root, calls, settle, async close() { await act(async () => root.unmount()); client.clear() } }
}

const button = (root, label) => root.root.findAllByType('button').find(b => b.children.join('').includes(label))

test('viewing diagnostics leaves heavy tracing off and needs an explicit click', async () => {
  const page = await mount()
  try {
    assert.equal(page.calls.start, 0)
    assert.equal(page.calls.stop, 0)
    assert.equal(button(page.root, 'Enable for 15 minutes').props.disabled, false)
    assert.equal(button(page.root, 'Stop heavy tracing').props.disabled, true)
    assert.match(JSON.stringify(page.root.toJSON()), /Heavy tracing is not requested/)
    await act(async () => button(page.root, 'Enable for 15 minutes').props.onClick())
    await page.settle()
    assert.equal(page.calls.start, 1)
    assert.match(JSON.stringify(page.root.toJSON()), /Requested until/)
    assert.equal(button(page.root, 'Enable for 15 minutes').props.disabled, true)
    assert.equal(button(page.root, 'Stop heavy tracing').props.disabled, false)
  } finally { await page.close() }
})

test('stop clears the request without changing passive diagnostics', async () => {
  const page = await mount({ requested: true, expiresAt: '2099-01-01T00:15:00Z', remainingS: 900 })
  try {
    await act(async () => button(page.root, 'Stop heavy tracing').props.onClick())
    await page.settle()
    assert.equal(page.calls.stop, 1)
    assert.equal(page.calls.start, 0)
    assert.match(JSON.stringify(page.root.toJSON()), /Heavy tracing is not requested/)
    assert.equal(button(page.root, 'Enable for 15 minutes').props.disabled, false)
  } finally { await page.close() }
})

test('disabled passive logging prevents requesting a trace', async () => {
  const page = await mount({ passiveEnabled: false })
  try {
    assert.equal(button(page.root, 'Enable for 15 minutes').props.disabled, true)
    assert.match(JSON.stringify(page.root.toJSON()), /Enable passive CarPlay timing and unit logs/)
    assert.equal(page.calls.start, 0)
  } finally { await page.close() }
})

test('an existing request can still be stopped after passive logging is disabled', async () => {
  const page = await mount({ passiveEnabled: false, requested: true, expiresAt: '2099-01-01T00:15:00Z', remainingS: 900 })
  try {
    assert.equal(button(page.root, 'Enable for 15 minutes').props.disabled, true)
    assert.equal(button(page.root, 'Stop heavy tracing').props.disabled, false)
    await act(async () => button(page.root, 'Stop heavy tracing').props.onClick())
    await page.settle()
    assert.equal(page.calls.stop, 1)
    assert.match(JSON.stringify(page.root.toJSON()), /Heavy tracing is not requested/)
  } finally { await page.close() }
})

test('failed enable does not claim that tracing started', async () => {
  const page = await mount({}, true)
  try {
    await act(async () => button(page.root, 'Enable for 15 minutes').props.onClick())
    await page.settle()
    assert.match(JSON.stringify(page.root.toJSON()), /Camera diagnostic request could not be saved/)
    assert.match(JSON.stringify(page.root.toJSON()), /Heavy tracing is not requested/)
    assert.equal(button(page.root, 'Stop heavy tracing').props.disabled, true)
  } finally { await page.close() }
})
