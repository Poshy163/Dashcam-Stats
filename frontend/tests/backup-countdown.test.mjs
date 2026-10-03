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
    contents: `export { sleepCountdown, sleepStatusRefetchInterval, useSleepCountdown } from '@/lib/useSleepCountdown'; export { default as Backup } from '@/pages/Backup'; export { DashcamStatusBanner } from '@/pages/Dashboard';`,
    resolveDir: fileURLToPath(new URL('..', import.meta.url)), loader: 'tsx',
  },
  outfile: path.join(directory, 'components.mjs'), bundle: true, format: 'esm', platform: 'node', jsx: 'automatic',
  external: ['react', 'react/*', 'react-router-dom', '@tanstack/react-query'],
  alias: { '@': fileURLToPath(new URL('../src', import.meta.url)) },
})
const { sleepCountdown, sleepStatusRefetchInterval, useSleepCountdown, Backup, DashcamStatusBanner } = await import(pathToFileURL(path.join(directory, 'components.mjs')).href)

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
    sleepCountdownValidForS: 30,
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

test('a changed sleep policy never replaces or caps the active cycle estimate', () => {
  for (const policy of [300, 1200, null]) {
    const result = sleepCountdown(status({ sleepWindowSeconds: policy, sleepCountdownRemainingS: 720 }), observedAt, observedAt + 5_000)
    assert.equal(result.remainingS, 715)
    assert.equal(result.state, 'estimated')
  }
})

test('an elapsed estimate says the unit is still connected rather than claiming it slept', () => {
  const result = sleepCountdown(status({ sleepCountdownRemainingS: 4 }), observedAt, observedAt + 5_000)
  assert.equal(result.state, 'elapsed')
  assert.equal(result.remainingS, 0)
  assert.match(result.hint, /Estimated window elapsed.*still connected/)
  assert.equal(sleepCountdown(status({ sleepCountdownRemainingS: 0 }), observedAt, observedAt).state, 'elapsed')
  assert.equal(sleepCountdown(status({ sleepCountdownRemainingS: 0 }), observedAt, observedAt + 31_000).state, 'stale', 'elapsed estimates still require fresh evidence that the unit is connected')
})

test('parked polling respects the shorter evidence budget and stays bounded when readings expire', () => {
  for (const [runningInterval, idleInterval] of [[1_500, 15_000], [2_000, 10_000]]) {
    const interval = data => sleepStatusRefetchInterval(data, runningInterval, idleInterval)
    assert.equal(interval(status({ sleepCountdownValidForS: 8.23 })), 4_115, 'refresh before the live snapshot expires instead of waiting ten or fifteen seconds')
    assert.equal(interval(status({ unitObservationAgeS: 28 })), 1_000, 'runtime freshness can be shorter than the evidence TTL')
    assert.equal(interval(status({ sleepCountdownValidForS: 0 })), 1_000)
    assert.equal(interval(status({ sleepCountdownValidForS: 0.1 })), 1_000, 'near-expiry values cannot create a polling loop')
    assert.equal(interval(status({ sleepCountdownSource: 'unknown', sleepCountdownValidForS: null })), 5_000, 'parked uncertainty is rechecked promptly')
    assert.equal(interval(status({ state: 'running' })), runningInterval)
    assert.equal(interval(status({ unitOnline: false })), idleInterval)
    assert.equal(interval(status({ ignitionState: 'on' })), idleInterval)
    assert.equal(interval(undefined), idleInterval)
  }
})

test('head-unit evidence explains an estimate without overriding freshness or expiry', () => {
  const reason = 'Estimated from ignition-off timing recorded by the dashcam; its timer can differ.'
  const data = status({ sleepCountdownEvidenceSource: 'unit', sleepCountdownReason: reason })
  assert.equal(sleepCountdown(data, observedAt, observedAt + 5_000).hint, reason)
  assert.equal(sleepCountdown(data, observedAt, observedAt + 5_000).remainingS, 115)
  const stale = sleepCountdown(data, observedAt, observedAt + 30_001)
  assert.equal(stale.remainingS, null)
  assert.match(stale.hint, /fresh reading/)
  const expired = sleepCountdown({ ...data, sleepCountdownRemainingS: 4 }, observedAt, observedAt + 5_000)
  assert.match(expired.hint, /Estimated window elapsed.*still connected/)
})

test('unit evidence expires before the power observation even while the cached API response stays available', () => {
  const data = status({ sleepCountdownEvidenceSource: 'unit', sleepCountdownValidForS: 1 })
  assert.equal(sleepCountdown(data, observedAt, observedAt + 500).remainingS, 119.5)
  const expired = sleepCountdown(data, observedAt, observedAt + 1_000)
  assert.equal(expired.state, 'stale')
  assert.equal(expired.remainingS, null)
  assert.match(expired.hint, /fresh sleep timing/)
  for (const validity of [0, -1, null, undefined, NaN, Infinity]) {
    assert.equal(sleepCountdown({ ...data, sleepCountdownValidForS: validity }, observedAt, observedAt).remainingS, null)
  }
  assert.equal(sleepCountdown(status({ sleepCountdownValidForS: undefined }), observedAt, observedAt + 5_000).remainingS, 115, 'older server observations still use their runtime TTL')
  assert.equal(sleepCountdown(status({ sleepCountdownValidForS: 100 }), observedAt, observedAt + 30_001).remainingS, null, 'a longer evidence budget never extends the runtime TTL')
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

async function mountBackup(t, data, radio) {
  const client = new QueryClient({ defaultOptions: { queries: { enabled: false, retry: false, gcTime: Infinity } } })
  client.setQueryData(['ingest-status'], data)
  if (radio) client.setQueryData(['ingest-radio-status'], radio)
  let root
  await act(async () => { root = create(React.createElement(QueryClientProvider, { client }, React.createElement(MemoryRouter, null, React.createElement(Backup)))) })
  t.after(async () => { await act(async () => root.unmount()); client.clear() })
  return root
}

test('Wi-Fi reports observed band and frequency without claiming transfer speed', async t => {
  for (const [frequency, band] of [[5560, '5 GHz'], [2412, '2.4 GHz']]) {
    const root = await mountBackup(t, status({ wifiFrequencyMhz: frequency }))
    const tile = root.root.findByProps({ label: 'Wi-Fi' })
    assert.equal(tile.props.value, band)
    assert.equal(tile.props.hint, `${frequency} MHz • Observed frequency`)
    assert.doesNotMatch(JSON.stringify(root.toJSON()), /Fast link|Slow link/)
  }
})

test('Backup displays the on-unit estimate reason and preserves the one-minute quieting warning', async t => {
  const reason = 'Estimated from ignition-off timing recorded by the dashcam; its timer can differ.'
  const root = await mountBackup(t, status({
    state: 'running', phase: 'transferring', sleepCountdownRemainingS: 60,
    sleepCountdownEvidenceSource: 'unit', sleepCountdownReason: reason,
    radioQuietingHold: true,
    radioQuietingHoldReason: 'At most one minute remains in the estimated sleep window.',
  }), { quietingEnabled: true, transition: null })
  assert.equal(root.root.findByProps({ label: 'Estimated sleep' }).props.hint, reason)
  assert.equal(root.root.findByProps({ label: 'Status' }).props.value, 'Copying')
  const text = JSON.stringify(root.toJSON())
  assert.match(text, /At most one minute remains/)
  assert.match(text, /Radios left unchanged/)
  assert.match(text, /Backup can continue/)
  assert.ok(root.root.findAllByType('span').some(span => span.props.title?.startsWith(reason)), 'active countdown tooltip uses the same evidence explanation')
})

test('Backup and Dashboard label a fresh elapsed estimate without a zero timer or obsolete fit prediction', async t => {
  const data = status({
    state: 'running', phase: 'transferring', sleepCountdownRemainingS: 0,
    sleepWindowPrediction: { willPass: true, headroomS: 90, estimatedDurationS: 30, summary: 'Will complete with headroom' },
  })
  const backup = await mountBackup(t, data)
  assert.equal(backup.root.findByProps({ label: 'Estimated sleep' }).props.value, 'Awaiting sleep')
  assert.match(backup.root.findByProps({ label: 'Estimated sleep' }).props.hint, /Estimated window elapsed.*still connected/)
  let dashboard
  await act(async () => {
    dashboard = create(React.createElement(MemoryRouter, null, React.createElement(DashcamStatusBanner, { status: data, receivedAt: Date.now() })))
  })
  t.after(async () => act(async () => dashboard.unmount()))
  for (const view of [backup, dashboard]) {
    const text = JSON.stringify(view.toJSON())
    assert.match(text, /Awaiting sleep/)
    assert.doesNotMatch(text, /Estimated sleep in|Estimated sleep:|Likely fits|Will complete with headroom|Sleep time unknown/)
  }
  await act(async () => dashboard.update(React.createElement(MemoryRouter, null, React.createElement(DashcamStatusBanner, {
    status: data, receivedAt: Date.now(), requestFailed: true,
  }))))
  assert.match(JSON.stringify(dashboard.toJSON()), /Sleep time unknown/)
  assert.doesNotMatch(JSON.stringify(dashboard.toJSON()), /Awaiting sleep/)
})

test('a positive fraction of a second does not round into a zero-second timer', async t => {
  const root = await mountBackup(t, status({ sleepCountdownRemainingS: 0.49 }))
  assert.equal(root.root.findByProps({ label: 'Estimated sleep' }).props.value, '1s')
})

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

test('skipped quieting explains unchanged radios while backup stays Copying', async t => {
  const reason = 'The remaining sleep window is not known.'
  const root = await mountBackup(t, status({ state: 'running', phase: 'transferring', radioQuietingHold: true, radioQuietingHoldReason: reason }), { quietingEnabled: true, transition: null })
  assert.equal(root.root.findByProps({ label: 'Status' }).props.value, 'Copying')
  const text = JSON.stringify(root.toJSON())
  assert.match(text, /Radios left unchanged/)
  assert.match(text, /The remaining sleep window is not known/)
  assert.match(text, /Backup can continue without switching Bluetooth or the hotspot off/)
  assert.doesNotMatch(text, /Radio quieting is ready|Waiting for backup/)
})

test('recovery and active transitions outrank skipped-quieting notices; finished and offline runs hide them', async t => {
  const radio = { baseline: 'on', disableAttempted: true, disableVerified: true, restoreAttempted: false, restoreVerified: false }
  const transition = {
    phase: 'ingesting', active: true, recoveryRequired: false, bluetooth: radio, hotspot: radio,
    createdAt: new Date().toISOString(), updatedAt: new Date().toISOString(), restoreEvidenceSource: null,
    unitReportedAt: null, unitSleepReportedAt: null,
  }
  for (const [overrides, evidence, expected] of [
    [{}, { ...transition, active: false, recoveryRequired: true }, /may still be off/],
    [{}, transition, /Backup radio window active/],
    [{ state: 'ok' }, null, /Radio quieting is ready/],
    [{ unitOnline: false }, null, /Radio quieting is ready/],
  ]) {
    const root = await mountBackup(t, status({ state: 'running', radioQuietingHold: true, radioQuietingHoldReason: 'Sleep deadline unknown.', ...overrides }), { quietingEnabled: true, transition: evidence })
    const text = JSON.stringify(root.toJSON())
    assert.doesNotMatch(text, /Radios left unchanged|Sleep deadline unknown/)
    assert.match(text, expected)
  }
})

function recoveryTransition(overrides = {}) {
  const radio = { baseline: 'on', disableAttempted: true, disableVerified: true, restoreAttempted: true, restoreVerified: true }
  return {
    phase: 'resuming_obd', active: true, recoveryRequired: true, bluetooth: radio, hotspot: radio,
    obdLogger: { quiesceCapable: true, quiesceAttempted: true, quiesceVerified: true, resumeAttempted: true, resumeVerified: false },
    createdAt: new Date().toISOString(), updatedAt: new Date().toISOString(), restoreEvidenceSource: 'server',
    unitReportedAt: null, unitSleepReportedAt: null, ...overrides,
  }
}

test('pending radio capture reports awaiting or reading instead of a completed failed read', async t => {
  const unknown = { baseline: 'unknown', disableAttempted: false, disableVerified: false, restoreAttempted: false, restoreVerified: false }
  for (const phase of ['preparing', 'finalising_obd', 'transferring_obd', 'capturing_radio_state']) {
    const transition = recoveryTransition({ phase, recoveryRequired: false, bluetooth: unknown, hotspot: unknown })
    const root = await mountBackup(t, status({ state: 'running', phase: 'preparing' }), { quietingEnabled: true, transition })
    const text = JSON.stringify(root.toJSON())
    assert.match(text, /Preparing a safe radio transition/)
    assert.match(text, new RegExp(`${phase === 'capturing_radio_state' ? 'Reading' : 'Awaiting'} Bluetooth`))
    assert.match(text, new RegExp(`${phase === 'capturing_radio_state' ? 'Reading' : 'Awaiting'} Hotspot`))
    assert.doesNotMatch(text, /could not be read|left untouched|did not need to change/)
  }
})

test('captured state during preparation does not prematurely claim the radio was left unchanged', async t => {
  const on = { baseline: 'on', disableAttempted: false, disableVerified: false, restoreAttempted: false, restoreVerified: false }
  for (const phase of ['capturing_radio_state', 'disabling_radios']) {
    const transition = recoveryTransition({ phase, recoveryRequired: false, bluetooth: on, hotspot: { ...on, baseline: 'transport' } })
    const root = await mountBackup(t, status({ state: 'running' }), { quietingEnabled: true, transition })
    const text = JSON.stringify(root.toJSON())
    assert.match(text, /Bluetooth was on when checked/)
    assert.match(text, /Hotspot is carrying the transfer connection/)
    if (phase === 'disabling_radios') assert.match(text, /Applying the radio changes using the captured starting state/)
    assert.doesNotMatch(text, /left it alone|did not need to change|left untouched/)
  }
})

test('a finished unsuccessful radio capture retains its conclusive explanation', async t => {
  const unknown = { baseline: 'unknown', disableAttempted: false, disableVerified: false, restoreAttempted: false, restoreVerified: false }
  const transition = recoveryTransition({ phase: 'failed', active: false, recoveryRequired: false, bluetooth: unknown, hotspot: unknown })
  const root = await mountBackup(t, status(), { quietingEnabled: true, transition })
  const text = JSON.stringify(root.toJSON())
  assert.match(text, /Radio quieting could not start/)
  assert.match(text, /Bluetooth could not be read before the backup, so it was left untouched/)
  assert.match(text, /Hotspot could not be read before the backup, so it was left untouched/)
  assert.doesNotMatch(text, /Awaiting Bluetooth|Reading Bluetooth/)
})

test('confirmed radios with logger recovery pending never claim a radio is off or the car has left', async t => {
  for (const restoreEvidenceSource of ['server', 'unit', null]) {
    const root = await mountBackup(t, status({ backlogKnown: true }), { quietingEnabled: true, transition: recoveryTransition({ restoreEvidenceSource }) })
    const statusTile = root.root.findByProps({ label: 'Status' })
    assert.equal(statusTile.props.value, 'Finishing backup recovery')
    assert.equal(statusTile.props.hint, 'New backups wait until recovery is confirmed')
    const text = JSON.stringify(root.toJSON())
    assert.match(text, /Radio restoration is confirmed/)
    assert.match(text, /confirming recovery and that the logger has resumed/)
    assert.doesNotMatch(text, /may still be off|could not switch|Radios need switching back on|until the car is back|went off the network|Up to date/)
  }
})

test('active recovery with restored radios gets recovery wording even without a recovery-required flag', async t => {
  for (const state of ['idle', 'running']) {
    const root = await mountBackup(t, status({ state }), { quietingEnabled: true, transition: recoveryTransition({ recoveryRequired: false }) })
    assert.equal(root.root.findByProps({ label: 'Status' }).props.value, state === 'running' ? 'Copying' : 'Finishing backup recovery')
    const text = JSON.stringify(root.toJSON())
    assert.match(text, /Finishing backup recovery/)
    assert.doesNotMatch(text, /Restoring the original radio state|Backup continuing while radios recover/)
  }
})

test('a genuinely unverified radio retains the warning ahead of logger recovery', async t => {
  const transition = recoveryTransition()
  transition.hotspot = { ...transition.hotspot, restoreVerified: false }
  const root = await mountBackup(t, status(), { quietingEnabled: true, transition })
  assert.equal(root.root.findByProps({ label: 'Status' }).props.value, 'Radios need switching back on')
  const text = JSON.stringify(root.toJSON())
  assert.match(text, /The hotspot may still be off/)
  assert.doesNotMatch(text, /Finishing backup recovery|Radio restoration is confirmed/)
})

test('logger-only recovery distinguishes untouched radios and already-resumed logger checks', async t => {
  const transition = recoveryTransition()
  const untouched = { baseline: 'off', disableAttempted: false, disableVerified: true, restoreAttempted: false, restoreVerified: false }
  transition.bluetooth = untouched
  transition.hotspot = untouched
  transition.obdLogger = { ...transition.obdLogger, resumeVerified: true }
  const root = await mountBackup(t, status(), { quietingEnabled: false, transition })
  const text = JSON.stringify(root.toJSON())
  assert.match(text, /Finishing backup recovery/)
  assert.match(text, /The radios did not need restoring/)
  assert.match(text, /completing the remaining recovery checks/)
  assert.doesNotMatch(text, /may still be off|that the logger has resumed|Radio quieting is off/)
})

test('Bluetooth restoration still requires verification of an originally off hotspot baseline', async t => {
  for (const restoreVerified of [false, true]) {
    const transition = recoveryTransition({ hotspot: { baseline: 'off', disableAttempted: false, disableVerified: true, restoreAttempted: false, restoreVerified } })
    const root = await mountBackup(t, status(), { quietingEnabled: true, transition })
    const text = JSON.stringify(root.toJSON())
    if (restoreVerified) {
      assert.equal(root.root.findByProps({ label: 'Status' }).props.value, 'Finishing backup recovery')
      assert.match(text, /Radio restoration is confirmed/)
    } else {
      assert.equal(root.root.findByProps({ label: 'Status' }).props.value, 'Radio restoration not confirmed')
      assert.match(text, /not yet confirmed that the hotspot returned to its original state/)
      assert.doesNotMatch(text, /Finishing backup recovery|Radio restoration is confirmed|may still be off|switching back on/)
    }
  }
})

test('an explicit unverified restore attempt remains pending without a disable attempt', async t => {
  for (const name of ['bluetooth', 'hotspot']) {
    const untouched = { baseline: 'off', disableAttempted: false, disableVerified: true, restoreAttempted: false, restoreVerified: false }
    const transition = recoveryTransition({ bluetooth: untouched, hotspot: untouched })
    transition[name] = { ...untouched, restoreAttempted: true }
    const root = await mountBackup(t, status(), { quietingEnabled: true, transition })
    assert.equal(root.root.findByProps({ label: 'Status' }).props.value, 'Radio restoration not confirmed')
    assert.doesNotMatch(JSON.stringify(root.toJSON()), /Finishing backup recovery|Radio restoration is confirmed/)
  }
})
