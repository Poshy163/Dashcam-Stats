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

const directory = await mkdtemp(fileURLToPath(new URL('../node_modules/.backup-test-', import.meta.url)))
after(() => rm(directory, { recursive: true, force: true }))
await build({
  stdin: {
    contents: `export { sleepCountdown, useSleepCountdown } from '@/lib/useSleepCountdown'; export { default as Backup } from '@/pages/Backup'; export { DashcamStatusBanner } from '@/pages/Dashboard';`,
    resolveDir: fileURLToPath(new URL('..', import.meta.url)), loader: 'tsx',
  },
  outfile: path.join(directory, 'components.mjs'), bundle: true, format: 'esm', platform: 'node', jsx: 'automatic',
  external: ['react', 'react/*', 'react-router-dom', '@tanstack/react-query'],
  alias: { '@': fileURLToPath(new URL('../src', import.meta.url)) },
})
const { sleepCountdown, useSleepCountdown, Backup, DashcamStatusBanner } = await import(pathToFileURL(path.join(directory, 'components.mjs')).href)

const observedAt = Date.parse('2026-10-01T03:36:57Z')
function status(overrides = {}) {
  return {
    state: 'idle', phase: 'idle', unitOnline: true,
    filesTotal: 0, filesDone: 0, bytesTotal: 0, bytesDone: 0, throughputMbs: 0, speedMbsRecent: 0,
    etaSeconds: null, currentFile: null, backlogFiles: 0, backlogBytes: 0, backlogKnown: false,
    activeSkipped: 0, wifiFrequencyMhz: 5200, wifiBandHold: false, wifiBandHoldReason: null,
    unitUptimeS: 127, arrivalHold: false, arrivalHoldReason: null, ignitionHold: false, ignitionHoldReason: null,
    sleepWindowSeconds: 1200, sleepCountdownRemainingS: 120, ignitionState: 'off', ignitionOffAt: null,
    unitObservedAt: new Date(observedAt).toISOString(), unitObservationFresh: true,
    unitObservationAgeS: 0, unitObservationTtlS: 30, sleepCountdownSource: 'estimated', sleepCountdownReason: null,
    sleepWindowPrediction: null, recorderHealth: null, recorderHealthOk: null, recorderHealthAt: null,
    startedAt: null, lastSuccessTs: null, lastError: null, ...overrides,
  }
}

test('unknown, failed, offline and stale observations cannot display a countdown', () => {
  for (const overrides of [
    { unitOnline: false }, { ignitionState: 'unknown' }, { unitObservationFresh: false },
    { sleepCountdownSource: 'unknown' }, { unitObservationAgeS: null },
    { sleepCountdownRemainingS: null }, { sleepCountdownRemainingS: NaN },
    { unitObservationTtlS: undefined },
  ]) {
    assert.equal(sleepCountdown(status(overrides), observedAt, observedAt).remainingS, null, JSON.stringify(overrides))
  }
  assert.equal(sleepCountdown(status(), observedAt, observedAt, true).remainingS, null)
  assert.equal(sleepCountdown(status({ unitObservationAgeS: 20 }), observedAt, observedAt + 10_001).state, 'stale')
  assert.equal(sleepCountdown(status(), observedAt, observedAt + 30_001).state, 'stale')
})

test('known ignition-on and reboot-with-ACC-off do not invent the configured window as remaining time', () => {
  assert.equal(sleepCountdown(status({ ignitionState: 'on', sleepCountdownSource: 'not_running', sleepCountdownRemainingS: null }), observedAt, observedAt).state, 'not_running')
  const reboot = sleepCountdown(status({ sleepCountdownSource: 'unknown', sleepCountdownRemainingS: null, sleepCountdownReason: 'The dashcam was already parked when first observed.' }), observedAt, observedAt)
  assert.equal(reboot.state, 'unknown')
  assert.equal(reboot.remainingS, null)
  assert.match(reboot.hint, /already parked/)
})

test('an elapsed estimate says the unit is still connected rather than claiming it slept', () => {
  const result = sleepCountdown(status({ sleepCountdownRemainingS: 4 }), observedAt, observedAt + 5_000)
  assert.equal(result.remainingS, 0)
  assert.match(result.hint, /Estimated window elapsed.*still connected/)
})

test('the shared hook measures delayed callbacks, refreshes identical values, and expires cached observations', async t => {
  const originalNow = Date.now
  const originalSet = globalThis.setInterval
  const originalClear = globalThis.clearInterval
  let now = observedAt
  Date.now = () => now
  const intervals = new Map()
  let next = 0
  globalThis.setInterval = (callback, ms) => { const id = ++next; intervals.set(id, { callback, ms }); return id }
  globalThis.clearInterval = id => intervals.delete(id)
  let root
  t.after(async () => {
    if (root) await act(async () => root.unmount())
    Date.now = originalNow; globalThis.setInterval = originalSet; globalThis.clearInterval = originalClear
  })
  function Readout({ data, receivedAt }) {
    return React.createElement('output', null, JSON.stringify(useSleepCountdown(data, receivedAt)))
  }
  const data = status()
  await act(async () => { root = create(React.createElement(Readout, { data, receivedAt: observedAt })) })
  const result = () => JSON.parse(root.root.findByType('output').children.join(''))
  now += 20_000
  await act(async () => { for (const timer of intervals.values()) timer.callback() })
  assert.equal(result().remainingS, 100, 'one delayed callback accounts for all twenty elapsed seconds')

  await act(async () => root.update(React.createElement(Readout, { data, receivedAt: now })))
  assert.equal(result().remainingS, 120, 'a new response resets the reference even if its value did not change')
  now += 30_001
  await act(async () => { for (const timer of intervals.values()) timer.callback() })
  assert.equal(result().state, 'stale')
  assert.equal(result().remainingS, null)
  await act(async () => root.unmount())
  assert.equal(intervals.size, 0)
  await act(async () => { root = create(React.createElement(Readout, { data, receivedAt: observedAt })) })
  assert.equal(result().state, 'stale', 'mounting a cached query cannot restart its countdown')
})

async function mountBackup(t, data) {
  const client = new QueryClient({ defaultOptions: { queries: { enabled: false, retry: false, gcTime: Infinity } } })
  client.setQueryData(['ingest-status'], data)
  let root
  await act(async () => { root = create(React.createElement(QueryClientProvider, { client }, React.createElement(MemoryRouter, null, React.createElement(Backup)))) })
  t.after(async () => { await act(async () => root.unmount()); client.clear() })
  return root
}

test('a startup hold with unknown inventory is Waiting, keeps manual pull available, and shows unknown sleep', async t => {
  const root = await mountBackup(t, status({ arrivalHold: true, arrivalHoldReason: 'Waiting for minimum uptime', sleepCountdownSource: 'unknown', sleepCountdownRemainingS: null }))
  assert.equal(root.root.findByProps({ label: 'Status' }).props.value, 'Waiting after startup')
  assert.equal(root.root.findByProps({ label: 'Estimated sleep' }).props.value, 'Unknown')
  assert.doesNotMatch(JSON.stringify(root.toJSON()), /Up to date|Awake window ended/)
  assert.equal(root.root.findAllByType('button').find(button => button.children.includes('Pull now')).props.disabled, false)
})

test('idle becomes Up to date only after known empty inventory; a hold still wins', async t => {
  for (const [overrides, label] of [
    [{}, 'Waiting for backup'],
    [{ state: 'ok' }, 'Backup completed'],
    [{ backlogKnown: true }, 'Up to date'],
    [{ backlogKnown: true, ignitionHold: true, ignitionState: 'unknown' }, 'Waiting for ignition status'],
    [{ backlogKnown: true, wifiBandHold: true }, 'Waiting for 5 GHz WiFi'],
  ]) {
    const root = await mountBackup(t, status(overrides))
    assert.equal(root.root.findByProps({ label: 'Status' }).props.value, label)
  }
})

test('dashboard hold reasons outrank optimistic predictions and stale data never shows a timer', async t => {
  let root
  const data = status({
    ignitionHold: true, ignitionHoldReason: 'Waiting for a confirmed ignition-off reading.',
    sleepWindowPrediction: { willPass: true, headroomS: 90, estimatedDurationS: 30, summary: 'Will complete with headroom' },
  })
  const render = (overrides = {}) => React.createElement(MemoryRouter, null, React.createElement(DashcamStatusBanner, { status: data, receivedAt: Date.now(), ...overrides }))
  await act(async () => { root = create(render()) })
  t.after(async () => act(async () => root.unmount()))
  assert.match(JSON.stringify(root.toJSON()), /Waiting for a confirmed ignition-off reading/)
  assert.doesNotMatch(JSON.stringify(root.toJSON()), /Will complete with headroom/)
  assert.match(JSON.stringify(root.toJSON()), /Estimated sleep:/)
  await act(async () => root.update(render({ requestFailed: true })))
  assert.match(JSON.stringify(root.toJSON()), /Sleep time unknown/)
  assert.doesNotMatch(JSON.stringify(root.toJSON()), /Estimated sleep:/)
})
