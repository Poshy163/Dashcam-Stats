import assert from 'node:assert/strict'
import { after, test } from 'node:test'
import { mkdtemp, rm } from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { build } from 'esbuild'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { act, create } from 'react-test-renderer'

const directory = await mkdtemp(fileURLToPath(new URL('../node_modules/.movement-test-', import.meta.url)))
after(() => rm(directory, { recursive: true, force: true }))
await build({
  stdin: {
    contents: `export { MovementManifestEvidence } from '@/components/MovementManifestCard'; export { MaintenanceWakeEvidence, MaintenanceWakeCard } from '@/components/MaintenanceWakeCard'; export { QueryClient, QueryClientProvider } from '@tanstack/react-query';`,
    resolveDir: fileURLToPath(new URL('..', import.meta.url)), loader: 'tsx',
  },
  outfile: path.join(directory, 'evidence.mjs'), bundle: true, format: 'esm', platform: 'node', jsx: 'automatic',
  external: ['react', 'react/*'], alias: { '@': fileURLToPath(new URL('../src', import.meta.url)) },
})
const { MovementManifestEvidence, MaintenanceWakeEvidence, MaintenanceWakeCard, QueryClient, QueryClientProvider } = await import(pathToFileURL(path.join(directory, 'evidence.mjs')).href)
const render = (component, props) => renderToStaticMarkup(React.createElement(component, props))
const counts = { movingObserved: 7, sampledStationary: 19, accOffCandidate: 2, unknown: 3 }
const report = {
  state: 'ready', reportOnly: true, transferFiltering: false, reportStatus: 'ready',
  cardIdentityVerified: true,
  inventory: {
    clipCount: 31, totalBytes: 3100, matchedClips: 28, matchedBytes: 2800,
    unmatchedClips: 3, identityMismatches: 0, classCounts: counts, classBytes: counts,
  },
}

test('current movement counts remain advisory and explicitly retain stationary candidates', () => {
  const html = render(MovementManifestEvidence, { data: report })
  assert.match(html, /Report only/)
  assert.match(html, /movement labels alone never exclude footage/)
  assert.match(html, /No movement in sampled readings/)
  assert.match(html, />19</)
  assert.match(html, /28 of 31 inventoried clips matched/)
  assert.match(html, /Sampled stillness does not rule out an event/)
})

test('stale, invalid and refresh-failed reports never render historical classification totals as current', () => {
  for (const props of [
    { data: { ...report, state: 'stale' } },
    { data: { ...report, state: 'stale', requiresLiveVerification: true } },
    { data: { ...report, state: 'invalid' } },
    { data: { ...report, state: 'invalid', reportStatus: 'directory_required' } },
    { data: report, error: true },
  ]) {
    const html = render(MovementManifestEvidence, props)
    assert.doesNotMatch(html, />19</)
    assert.match(html, /normal backup continues|Normal backup continues/)
  }
})

test('directory setup and truncated reports explain incomplete coverage without claiming saved footage', () => {
  const setup = render(MovementManifestEvidence, { data: { ...report, state: 'ready', reportStatus: 'directory_required' } })
  assert.match(setup, /select the memory card’s DCIM folder with read-only access/)
  assert.doesNotMatch(setup, />19</)
  const partial = render(MovementManifestEvidence, { data: { ...report, truncated: true } })
  assert.match(partial, /Other clips remain eligible for backup/)
  assert.doesNotMatch(partial, /saved|deleted/)
})

const exclusions = {
  policyVersion: 'parked_visual_quiet_v1', status: 'ready', policyEnabled: true,
  stale: false, currentCount: 2, currentBytes: 1000, locallyIndexedCount: 0,
  historicalCount: 8, historicalBytes: 8000, countsAsBackedUp: false, filesDeleted: false,
}

test('quiet transfer decisions explain card retention and processing without treating movement labels as proof', () => {
  const html = render(MovementManifestEvidence, { data: { ...report, intentionalExclusions: exclusions } })
  assert.match(html, /Quiet-clip filtering enabled/)
  assert.match(html, /2 clips .* excluded in the latest transfer plan/)
  assert.match(html, /Both camera views must pass/)
  assert.match(html, /stay on the memory card and are not counted as backed up or sent for processing/)
  assert.match(html, /8 unique clips/)
})

test('stale or failed exclusions never claim that the historical plan is current', () => {
  for (const props of [
    { data: { ...report, intentionalExclusions: { ...exclusions, stale: true } } },
    { data: { ...report, intentionalExclusions: { ...exclusions, status: 'invalid' } } },
    { data: { ...report, intentionalExclusions: exclusions }, error: true },
  ]) {
    assert.doesNotMatch(render(MovementManifestEvidence, props), /2 clips .* excluded in the latest transfer plan/)
  }
})

test('disabled exclusions and later local copies are explained separately', () => {
  const disabled = render(MovementManifestEvidence, { data: { ...report, intentionalExclusions: { ...exclusions, policyEnabled: false } } })
  assert.match(disabled, /Skipping is disabled/)
  assert.doesNotMatch(disabled, /excluded in the latest transfer plan/)
  const local = render(MovementManifestEvidence, { data: { ...report, intentionalExclusions: { ...exclusions, locallyIndexedCount: 1 } } })
  assert.match(local, /1 of these clips also have a local copy/)
})

test('an awake flag alone never renders as a verified maintenance lease', () => {
  const active = { active: true, verified: true, state: 'active', remainingS: 300, evidenceValidForS: 12 }
  assert.match(render(MaintenanceWakeEvidence, { data: active }), /Verified wake hold/)
  for (const props of [
    { data: { ...active, verified: false } },
    { data: { ...active, remainingS: 0 } },
    { data: { ...active, evidenceValidForS: 0 } },
    { data: { ...active, evidenceValidForS: null } },
    { data: { ...active, state: 'failed' } },
    { data: active, error: true },
  ]) assert.doesNotMatch(render(MaintenanceWakeEvidence, props), /Verified wake hold/)
})

test('cached maintenance proof expires in the UI even without another successful response', () => {
  const data = { active: true, verified: true, state: 'active', remainingS: 300, evidenceValidForS: 12 }
  assert.match(render(MaintenanceWakeEvidence, { data, checkedAtMs: 100_000, nowMs: 105_000 }), /Verified wake hold/)
  for (const nowMs of [99_000, 112_000, 120_000, 500_000]) {
    assert.doesNotMatch(render(MaintenanceWakeEvidence, { data, checkedAtMs: 100_000, nowMs }), /Verified wake hold/)
  }
})

test('ending a cancelled native countdown explains the required real ignition transition', () => {
  const html = render(MaintenanceWakeEvidence, { data: { active: false, verified: false, state: 'expired', requiresIgnitionCycle: true } })
  assert.match(html, /fresh accessory-power ON → OFF cycle/)
  assert.match(html, /Normal backup sleep-window checks apply/)
})

test('pending radio recovery keeps a usable retry action and blocks another hold', async t => {
  const query = new QueryClient({ defaultOptions: { queries: { staleTime: Infinity, retry: false, gcTime: Infinity } } })
  query.setQueryData(['maintenance-wake'], { active: false, verified: false, state: 'restoring', remainingS: 0, evidenceValidForS: 0 })
  let root
  t.after(async () => { await act(async () => root?.unmount()); query.clear() })
  await act(async () => { root = create(React.createElement(QueryClientProvider, { client: query }, React.createElement(MaintenanceWakeCard))) })
  assert.match(JSON.stringify(root.toJSON()), /Ending wake hold and restoring radios/)
  const buttons = root.root.findAllByType('button')
  assert.equal(buttons.find(item => item.children.includes('Retry radio restoration')).props.disabled, false)
  assert.equal(buttons.find(item => item.children.includes('Start verified wake hold')).props.disabled, true)
})

async function settle() {
  for (let index = 0; index < 4; index++) await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
}

test('retry uses the duration-only maintenance request and requires new server verification', async t => {
  const cached = { active: false, verified: false, state: 'failed', remainingS: 0, evidenceValidForS: 0,
    countdownCancelled: true, requiresIgnitionCycle: true, reason: 'Fresh maintenance evidence was lost.' }
  const fresh = { ...cached, active: true, verified: true, state: 'active', remainingS: 840,
    evidenceValidForS: 30, reason: null }
  const query = new QueryClient({ defaultOptions: { queries: { staleTime: Infinity, retry: false, gcTime: Infinity } } })
  query.setQueryData(['maintenance-wake'], cached)
  const oldFetch = globalThis.fetch, oldWindow = globalThis.window
  const requests = []
  let finishRequest, attempts = 0, current = cached, root
  globalThis.window = { setInterval, clearInterval }
  globalThis.fetch = async (url, init) => {
    requests.push({ url, method: init.method, body: init.body })
    assert.equal(url, '/api/ingest/maintenance')
    if (init.method === 'POST') {
      attempts++
      await new Promise(resolve => { finishRequest = resolve })
      if (attempts === 1) return new Response(JSON.stringify({ detail: 'Fresh parked-state verification failed.' }), { status: 409 })
      current = fresh
    }
    return new Response(JSON.stringify(current), { status: 200 })
  }
  t.after(async () => { await act(async () => root?.unmount()); query.clear(); globalThis.fetch = oldFetch
    if (oldWindow === undefined) delete globalThis.window; else globalThis.window = oldWindow })
  await act(async () => { root = create(React.createElement(QueryClientProvider, { client: query }, React.createElement(MaintenanceWakeCard))) })
  const button = label => root.root.findAllByType('button').find(item => item.children.includes(label))
  assert.match(JSON.stringify(root.toJSON()), /must not have restarted/)
  assert.doesNotMatch(JSON.stringify(root.toJSON()), /Verified wake hold ·/)
  await act(async () => button('Retry verified wake hold').props.onClick())
  assert.equal(button('Verifying parked state…').props.disabled, true)
  assert.doesNotMatch(JSON.stringify(root.toJSON()), /Verified wake hold ·/)
  await act(async () => finishRequest())
  await settle()
  assert.match(JSON.stringify(root.toJSON()), /Fresh parked-state verification failed/)
  assert.doesNotMatch(JSON.stringify(root.toJSON()), /Verified wake hold ·/)
  await act(async () => button('Retry verified wake hold').props.onClick())
  await act(async () => finishRequest())
  await settle()
  assert.match(JSON.stringify(root.toJSON()), /Verified wake hold ·/)
  const posts = requests.filter(item => item.method === 'POST')
  assert.equal(posts.length, 2)
  for (const request of posts) assert.deepEqual(JSON.parse(request.body), { duration_s: 900 })
  assert.equal(requests.some(item => item.method === 'DELETE'), false, 'retry never deletes the cancellation barrier')
})
