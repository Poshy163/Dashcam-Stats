import assert from 'node:assert/strict'
import { after, test } from 'node:test'
import { mkdtemp, rm } from 'node:fs/promises'
import { fileURLToPath, pathToFileURL } from 'node:url'
import path from 'node:path'
import { build } from 'esbuild'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

const directory = await mkdtemp(fileURLToPath(new URL('../node_modules/.obd-diagnostics-test-', import.meta.url)))
after(() => rm(directory, { recursive: true, force: true }))
await build({
  stdin: {
    contents: `export { obdDiagnosticState } from '@/lib/obdDiagnostics'; export { default as ObdFaultSummary } from '@/components/ObdFaultSummary';`,
    resolveDir: fileURLToPath(new URL('..', import.meta.url)), loader: 'tsx',
  },
  outfile: path.join(directory, 'diagnostics.mjs'), bundle: true, format: 'esm', platform: 'node', jsx: 'automatic',
  external: ['react', 'react/*'], alias: { '@': fileURLToPath(new URL('../src', import.meta.url)) },
})
const { obdDiagnosticState, ObdFaultSummary } = await import(pathToFileURL(path.join(directory, 'diagnostics.mjs')).href)
const evidence = overrides => ({
  drive: { dtcsObserved: [] }, samples: [], diagnostics: [], diagnosticTotal: 0, diagnosticPage: 1,
  sampling: { downsampled: false }, ...overrides,
})
const event = (kind, payload) => ({ kind, payload, observedAt: null })
const markup = (data, filtered = false) => renderToStaticMarkup(React.createElement(ObdFaultSummary, { data, filtered }))

test('MIL and one stored fault remain visible when no actual codes were retrieved', () => {
  const data = evidence({ samples: [{ milOn: true, dtcCount: 1 }, { milOn: null, dtcCount: null }] })
  const state = obdDiagnosticState(data)
  assert.equal(state.warning, true)
  assert.equal(state.lampOn, true)
  assert.equal(state.storedCount, 1)
  assert.equal(state.codeStatus, 'Codes not retrieved')
  const html = markup(data)
  assert.match(html, /1 stored fault/)
  assert.match(html, /Observed on/)
  assert.match(html, /Codes not retrieved/)
  assert.match(html, /does not mean the warning is clear/)
  assert.doesNotMatch(html, /No faults|No codes returned/)
})

test('a later zero/off reply cannot erase an earlier positive drive observation', () => {
  const state = obdDiagnosticState(evidence({ samples: [
    { milOn: true, dtcCount: 2 }, { milOn: false, dtcCount: 0 },
  ] }))
  assert.equal(state.warning, true)
  assert.equal(state.lampOn, true)
  assert.equal(state.storedCount, 2)
})

test('scan completion is explicit; an empty list or just one empty mode is not a clean scan', () => {
  for (const diagnostics of [[], [event('confirmed_dtcs', { codes: [] })], [event('dtc_scan_complete', { modes: [3] })]]) {
    const state = obdDiagnosticState(evidence({ diagnostics, diagnosticTotal: diagnostics.length }))
    assert.equal(state.scanComplete, false)
    assert.equal(state.codeStatus, 'Codes not retrieved')
  }
  const complete = evidence({
    samples: [{ milOn: false, dtcCount: 0 }],
    diagnostics: [event('dtc_scan_complete', { modes: [3, 7, 10] })], diagnosticTotal: 1,
  })
  assert.equal(obdDiagnosticState(complete).codeStatus, 'No codes returned')
  assert.match(markup(complete), /Observed off/)
  assert.doesNotMatch(markup(complete), /No faults|No check-engine warning/)
  // A successful empty scan still cannot contradict a positive lamp/count observation.
  const contradictory = { ...complete, samples: [{ milOn: true, dtcCount: 1 }] }
  assert.equal(obdDiagnosticState(contradictory).warning, true)
  assert.match(markup(contradictory), /does not mean the warning is clear/)
})

test('diagnostic readiness evidence is used when sparse chart samples lack the slow PID', () => {
  const data = evidence({ diagnostics: [
    event('mil_state', { on: true }), event('readiness', { confirmedDtcCount: 1 }),
  ], diagnosticTotal: 2 })
  assert.equal(obdDiagnosticState(data).lampOn, true)
  assert.equal(obdDiagnosticState(data).storedCount, 1)
  assert.match(markup(data), /1 stored fault/)
})

test('filtered, downsampled or paginated absence stays unknown and is labelled with its scope', () => {
  for (const data of [
    evidence({ diagnosticTotal: 101 }), evidence({ diagnosticPage: 2 }), evidence({ sampling: { downsampled: true } }),
  ]) {
    const state = obdDiagnosticState(data)
    assert.equal(state.partialEvidence, true)
    assert.equal(state.lampOn, null)
    assert.equal(state.storedCount, null)
    assert.equal(state.codeStatus, 'No codes recorded in summary')
    assert.match(markup(data), /other observations may be outside this view/)
  }
  assert.equal(obdDiagnosticState(evidence(), true).partialEvidence, true)
})

test('retained code strings remain visible across filters and unknown values never become zero', () => {
  const data = evidence({ drive: { dtcsObserved: ['P0135', 'P0135', 'P0300'] }, samples: [
    { milOn: null, dtcCount: null }, { dtcCount: -1 }, { dtcCount: 128 }, { dtcCount: true },
  ] })
  const state = obdDiagnosticState(data, true)
  assert.deepEqual(state.codes, ['P0135', 'P0300'])
  assert.equal(state.storedCount, null)
  assert.match(markup(data, true), /P0135, P0300/)
  assert.match(markup(data), /Not observed/)
})
