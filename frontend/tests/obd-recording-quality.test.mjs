import assert from 'node:assert/strict'
import { after, test } from 'node:test'
import { mkdtemp, rm } from 'node:fs/promises'
import { fileURLToPath, pathToFileURL } from 'node:url'
import path from 'node:path'
import { build } from 'esbuild'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

const directory = await mkdtemp(fileURLToPath(new URL('../node_modules/.obd-recording-quality-', import.meta.url)))
after(() => rm(directory, { recursive: true, force: true }))
await build({
  stdin: {
    contents: `export { ObdRecordingGaps } from '@/components/ObdRecordingGaps'; export { ObdPollTiming } from '@/components/ObdPollTiming';`,
    resolveDir: fileURLToPath(new URL('..', import.meta.url)), loader: 'tsx',
  },
  outfile: path.join(directory, 'quality.mjs'), bundle: true, format: 'esm', platform: 'node', jsx: 'automatic',
  external: ['react', 'react/*'], alias: { '@': fileURLToPath(new URL('../src', import.meta.url)) },
})
const { ObdRecordingGaps, ObdPollTiming } = await import(pathToFileURL(path.join(directory, 'quality.mjs')).href)
const render = (component, props) => renderToStaticMarkup(React.createElement(component, props))
const gap = overrides => ({
  previousDriveId: 'previous-drive', nextDriveId: 'next-drive',
  previousObservationAt: '2026-10-23T00:15:39.510763Z', nextObservationAt: '2026-10-23T00:16:14.579550Z',
  observationIntervalS: 35.068787, nominalIntervalS: 5, unobservedExcessS: 30.068787,
  classification: 'likely_reconnect', interruptionReason: 'command_timeout',
  movingBefore: true, movingAfter: true, timestampBasis: 'row_observation', ...overrides,
})

test('a reconnect interval remains visible even with zero gaps inside the segment', () => {
  const html = render(ObdRecordingGaps, { drive: { gapCount: 0, recordingGapBefore: gap() } })
  assert.match(html, /35\.1 s between observations/)
  assert.match(html, /Likely reconnect/)
  assert.match(html, /Recorded row times/)
  assert.match(html, /href="\/obd\/previous-drive"/)
  assert.match(html, /Distance and fuel are not filled across them/)
})

test('the interrupted preceding segment links forward, and uncertainty is explicit', () => {
  const html = render(ObdRecordingGaps, { drive: { recordingGapAfter: gap({
    classification: 'unconfirmed_interruption', movingAfter: false,
    observationIntervalS: 24.601125, timestampBasis: 'response_completion',
  }) } })
  assert.match(html, /24\.6 s between observations/)
  assert.match(html, /After this segment/)
  assert.match(html, /Continuity unconfirmed/)
  assert.doesNotMatch(html, /Likely reconnect/)
  assert.match(html, /Recorded reply times/)
  assert.match(html, /href="\/obd\/next-drive"/)
})

test('older responses and invalid intervals do not invent reconnect evidence', () => {
  assert.equal(render(ObdRecordingGaps, { drive: {} }), '')
  for (const seconds of [0, -1, NaN, Infinity, 120.01]) {
    assert.equal(render(ObdRecordingGaps, { drive: { recordingGapBefore: gap({ observationIntervalS: seconds }) } }), '')
  }
  assert.equal(render(ObdRecordingGaps, { drive: { recordingGapBefore: gap({ classification: 'complete' }) } }), '')
})

const distribution = value => ({ count: 14, retained: 14, medianMs: value, p95Ms: value, maxMs: value, totalMs: value * 14 })
const stages = { criticalRequests: distribution(2400), auxiliaryRequests: distribution(0),
  quiesceChecks: distribution(2), persistence: distribution(30), diagnostics: distribution(0),
  statusPublish: distribution(15), schedulerOther: distribution(55) }
const timing = version => ({ schemaVersion: version, cyclesCompleted: 14, overrunCount: 5,
  targetCycleMs: 3000, cycleWorkMs: distribution(2502), cycleStartIntervalMs: distribution(3000),
  pids: [], ...(version === 2 ? { cycleStagesMs: stages } : {}) })

test('version two timing explains application work without mislabelling it as ECU request time', () => {
  const html = render(ObdPollTiming, { payload: timing(2) })
  assert.match(html, /14 completed cycles/)
  assert.match(html, /Polling cycle work breakdown/)
  assert.match(html, /Saving observations/)
  assert.match(html, /Status updates/)
  assert.match(html, /Speed, RPM and airflow requests/)
  assert.match(html, /Waiting for the next scheduled cycle is excluded/)
})

test('legacy timing stays readable and unrecognized versions or stage names are not exposed', () => {
  const legacy = render(ObdPollTiming, { payload: timing(1) })
  assert.match(legacy, /14 completed cycles/)
  assert.doesNotMatch(legacy, /Polling cycle work breakdown/)
  assert.match(render(ObdPollTiming, { payload: timing(3) }), /unavailable for this format/)
  const html = render(ObdPollTiming, { payload: { ...timing(2), cycleStagesMs: { ...stages, rawPrivatePayload: 'secret' } } })
  assert.doesNotMatch(html, /rawPrivatePayload|secret/)
})
