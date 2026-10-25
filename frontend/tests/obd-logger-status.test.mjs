import assert from 'node:assert/strict'
import { after, test } from 'node:test'
import { mkdtemp, rm } from 'node:fs/promises'
import { fileURLToPath, pathToFileURL } from 'node:url'
import path from 'node:path'
import { build } from 'esbuild'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { act, create } from 'react-test-renderer'

const directory = await mkdtemp(fileURLToPath(new URL('../node_modules/.obd-status-test-', import.meta.url)))
after(() => rm(directory, { recursive: true, force: true }))
await build({
  stdin: {
    contents: `export { ObdLoggerCard } from '@/components/ObdLoggerCard'; export { loggerEvidenceFreshness } from '@/lib/obdLoggerPresentation';`,
    resolveDir: fileURLToPath(new URL('..', import.meta.url)), loader: 'tsx',
  },
  outfile: path.join(directory, 'components.mjs'), bundle: true, format: 'esm', platform: 'node', jsx: 'automatic',
  external: ['react', 'react/*'],
  alias: { '@': fileURLToPath(new URL('../src', import.meta.url)) },
})
const { ObdLoggerCard, loggerEvidenceFreshness } = await import(pathToFileURL(path.join(directory, 'components.mjs')).href)

const observedAt = Date.parse('2026-10-20T04:00:00Z')
const at = (offset = 0) => new Date(observedAt + offset).toISOString()
const logger = (overrides = {}) => ({
  schemaVersion: 7, state: 'ecu_online', adapterReachable: true, adapterConnected: true,
  ecuConnected: true, engineRunning: true, bleOwner: 'dashcam_full_obd', updatedAtUtc: at(),
  appVersionName: '0.3.7', appVersionCode: 20, buildGitSha: 'abc123456789', pollPlanVersion: 6,
  batteryVoltage: 14.2, batteryVoltageFresh: true, batteryVoltageQuality: 'valid',
  batteryVoltageSource: 'dashcam_elm_atrv', batteryVoltageSampleAtUtc: at(), ...overrides,
})

function markup(props = {}) {
  const originalNow = Date.now
  Date.now = () => observedAt
  try {
    return renderToStaticMarkup(React.createElement(ObdLoggerCard, {
      logger: logger(), checkedAt: at(), unitOnline: true, ...props,
    }))
  } finally { Date.now = originalNow }
}

const trialSettings = (overrides = {}) => ({
  schemaVersion: 1, enabled: true, fallbackReason: null, revision: 2,
  source: 'operator', changedAtUtc: at(), storageState: 'ready', error: null,
  legacyKeyPresent: true, recentChanges: [], ...overrides,
})

test('saved trial intent remains separate from the active or parked poll plan', () => {
  const html = markup({ logger: logger({ pollPlanVersion: 7, trialSettings: trialSettings() }) })
  assert.match(html, /Poll plan 7/)
  assert.match(html, /Trial setting · last reported/)
  assert.match(html, /Armed for the next drive/)
  assert.match(html, /Explicit apply/)
  assert.match(html, /The active drive keeps its recorded poll plan/)
})

test('named fallback, explicit baseline and imported unknown-time history stay distinct', () => {
  const html = markup({ logger: logger({ trialSettings: trialSettings({
    enabled: false, fallbackReason: 'critical_readings_missing', source: 'health',
    recentChanges: [{ revision: 1, enabled: false, fallbackReason: null,
      source: 'legacy_migration', changedAtUtc: null }],
  }) }) })
  assert.match(html, /Fallback: Critical readings missing/)
  assert.match(html, /Automatic fallback/)
  assert.match(html, /Imported existing setting/)
  assert.match(html, /Original change time unavailable/)
  const off = markup({ logger: logger({ trialSettings: trialSettings({ enabled: false }) }) })
  assert.match(off, /Baseline selected/)
  assert.doesNotMatch(off, /Automatic fallback/)
})

test('unavailable settings never masquerade as an operator disable or an armed trial', () => {
  const html = markup({ logger: logger({ trialSettings: trialSettings({
    storageState: 'unavailable', source: 'unknown', changedAtUtc: null, error: 'corrupt_record', enabled: false,
  }) }) })
  assert.match(html, /Setting unavailable; baseline protected/)
  assert.match(html, /Source unavailable/)
  assert.doesNotMatch(html, /Armed for the next drive|Explicit apply/)
})

test('generic firmware reboot remains an observation with unknown physical cause', () => {
  const html = markup({ logger: logger({ restartEvidence: {
    schemaVersion: 1, storageState: 'ready', transition: 'kernel_boot',
    observedAtUtc: at(), bootCount: 27, bootCountDelta: 1, bootReason: 'reboot',
    bootReasonSource: 'sys_boot_reason', causeConfidence: 'unknown',
    previousProcessExit: 'not_recorded', history: [],
  } }) })
  assert.match(html, /New device boot observed/)
  assert.match(html, /Physical cause unknown/)
  assert.doesNotMatch(html, /thermal caused|verified cause|crash caused/i)
})

test('old companions and unrecognised category strings stay bounded and explicitly unavailable', () => {
  assert.match(markup(), /Not reported by this companion/)
  const html = markup({ logger: logger({
    trialSettings: trialSettings({ source: 'constructor', fallbackReason: '<private-exception>' }),
    restartEvidence: { storageState: 'ready', transition: 'constructor', observedAtUtc: 'private timestamp',
      causeConfidence: 'unknown' },
  }) })
  assert.match(html, /Reason unavailable|Source unavailable/)
  assert.match(html, /Startup continuity unavailable/)
  assert.doesNotMatch(html, /private-exception|private timestamp|function Object/)
})

test('producer freshness handles stale, absent, invalid, timezone-less and future reports', () => {
  assert.equal(loggerEvidenceFreshness(at(-90_000), true, observedAt).current, true)
  assert.equal(loggerEvidenceFreshness(at(-90_001), true, observedAt).label, 'Logger status stale')
  for (const timestamp of [undefined, null, '', 'not-a-time', '2026-10-20T04:00:00', '2026-02-30T04:00:00Z', '2026-10-20T24:00:00Z']) {
    assert.equal(loggerEvidenceFreshness(timestamp, true, observedAt).label, 'Status time unavailable')
  }
  assert.equal(loggerEvidenceFreshness(at(5_001), true, observedAt).label, 'Logger clock mismatch')
  assert.equal(loggerEvidenceFreshness(at(4_000), true, observedAt).current, true)
  assert.equal(loggerEvidenceFreshness('2026-10-20T14:30:00+10:30', true, observedAt).current, true)
  assert.equal(loggerEvidenceFreshness(at(), false, observedAt).label, 'Dashcam offline')
  assert.equal(loggerEvidenceFreshness(at(), undefined, observedAt).current, false)
})

test('offline and stale logger reports never present cached engine, ECU or ownership as current', () => {
  for (const props of [
    { unitOnline: false },
    { logger: logger({ updatedAtUtc: at(-120_000) }) },
    { logger: logger({ updatedAtUtc: at(60_000) }) },
    { logger: logger({ updatedAtUtc: undefined }) },
  ]) {
    const html = markup(props)
    assert.match(html, /Last reported logger evidence/)
    assert.match(html, /Last reported: Running/)
    assert.match(html, /Last reported: Online/)
    assert.match(html, /Last reported: Connected/)
    assert.match(html, /Current owner unknown/)
    assert.doesNotMatch(html, /Live logger evidence/)
    assert.doesNotMatch(html, /The dashcam owns BLE and may open a full OBD session/)
    assert.doesNotMatch(html, /Voltage-only adapter contact/)
  }
  const lastOff = markup({ unitOnline: false, logger: logger({ engineRunning: false, ecuConnected: false }) })
  assert.match(lastOff, /Last reported: Off/)
  assert.match(lastOff, /Current state is unknown/)
})

test('fresh ATRV evidence remains independent from ECU and engine proof', () => {
  const html = markup({ logger: logger({ ecuConnected: false, engineRunning: false }) })
  assert.match(html, /Live logger evidence/)
  assert.match(html, /Fresh logger report/)
  assert.match(html, /14\.2 V/)
  assert.match(html, /Voltage-only adapter contact/)
  assert.match(html, />Offline</)
  assert.match(html, />Off</)
  assert.doesNotMatch(html, />Running</)
  // A cached report is historical without throwing away its independent voltage measurement.
  assert.match(markup({ unitOnline: false }), /14\.2 V/)
})

test('companion and backend versions remain separate and missing builds stay unknown', () => {
  const html = markup({
    backendInfo: { version: '0.7.0', sourceRevision: 'def123456789' + 'a'.repeat(28) },
    backendCheckedAt: at(-45_000),
  })
  assert.match(html, /Companion · last reported/)
  assert.match(html, /0\.3\.7 \(code 20\)/)
  assert.match(html, /abc123456789/)
  assert.match(html, /Poll plan 6/)
  assert.match(html, /Backend · running server/)
  assert.match(html, /0\.7\.0/)
  assert.match(html, /def123456789/)
  assert.match(html, /Last checked <time/)
  assert.ok(html.includes(at(-45_000)))
  assert.match(html, /45 seconds ago/)
  const unavailable = markup({ logger: null, backendInfoError: true })
  assert.match(unavailable, /Logger state unavailable/)
  assert.match(unavailable, /Version check unavailable/)
  assert.doesNotMatch(unavailable, /0\.3\.7/)
  assert.match(markup({ backendInfoLoading: true }), /Checking version/)
  const invalidCheckTime = markup({ backendCheckedAt: 'not-a-time' })
  assert.match(invalidCheckTime, /Check time not reported/)
  assert.doesNotMatch(invalidCheckTime, /not-a-time/)
})

test('an unchanged response ages out without another network response and its timer is cleaned up', async () => {
  const originalNow = Date.now
  const originalSetInterval = globalThis.setInterval
  const originalClearInterval = globalThis.clearInterval
  let currentTime = observedAt
  let tick
  let cleared = false
  Date.now = () => currentTime
  globalThis.setInterval = callback => { tick = callback; return 123 }
  globalThis.clearInterval = timer => { if (timer === 123) cleared = true }
  let root
  try {
    await act(async () => { root = create(React.createElement(ObdLoggerCard, { logger: logger(), checkedAt: at(), unitOnline: true })) })
    assert.match(JSON.stringify(root.toJSON()), /Fresh logger report/)
    currentTime += 75_001
    await act(async () => tick())
    assert.match(JSON.stringify(root.toJSON()), /Fresh logger report/)
    assert.match(JSON.stringify(root.toJSON()), /No fresh reply/)
    assert.doesNotMatch(JSON.stringify(root.toJSON()), /"children":\["Fresh"\]/)
    currentTime = observedAt + 90_001
    await act(async () => tick())
    assert.match(JSON.stringify(root.toJSON()), /Logger status stale/)
    assert.match(JSON.stringify(root.toJSON()), /Last reported: Running/)
  } finally {
    if (root) await act(async () => root.unmount())
    Date.now = originalNow
    globalThis.setInterval = originalSetInterval
    globalThis.clearInterval = originalClearInterval
  }
  assert.equal(cleared, true)
})
