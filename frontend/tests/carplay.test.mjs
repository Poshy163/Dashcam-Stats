import assert from 'node:assert/strict'
import fs from 'node:fs/promises'
import test from 'node:test'
import ts from 'typescript'

const source = await fs.readFile(new URL('../src/lib/carplay.ts', import.meta.url), 'utf8')
const code = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext } }).outputText
const { meanKnown, maxKnown, timingPeriods, timingSegments, inTimingPeriod, radioObservation,
  gpsCaptureLabel, gpsProviderObservation, gpsFirstFixSummary, gpsObservationPage, peerTransportObservation } =
  await import(`data:text/javascript;base64,${Buffer.from(code).toString('base64')}`)
const minute = (time, sessionId = null) => ({ bucketStart: `2026-09-08T${time}:00Z`, layer: '#101', sessionId })

test('unavailable thermal readings remain unknown instead of becoming zero', () => {
  assert.equal(meanKnown([null, undefined]), null)
  assert.equal(maxKnown([null, undefined]), null)
  assert.equal(meanKnown([null, 80, 90]), 85)
  assert.equal(maxKnown([null, 0]), 0)
})

test('outbound and return are separated and a reused layer does not merge sampler captures', () => {
  const periods = timingPeriods([minute('01:44'), minute('01:45'), minute('01:56'), minute('01:57'), minute('01:57', 'new')])
  assert.equal(periods.length, 3)
  assert.equal(inTimingPeriod('2026-09-08T01:56:25Z', null, periods[0]), false)
  assert.equal(inTimingPeriod('2026-09-08T01:56:25Z', null, periods[1]), true)
  assert.equal(inTimingPeriod('2026-09-08T01:57:25Z', 'new', periods[1]), false)
})

test('chart never bridges missing readings, missing minutes or session changes', () => {
  const minutes = [minute('01:44', 'a'), minute('01:45', 'a'), minute('01:46', 'a'), minute('01:56', 'a'), minute('01:57', 'b')]
  assert.deepEqual(timingSegments(minutes, [24, null, 25, 26, 27]), [[0], [2], [3], [4]])
  assert.deepEqual(timingSegments(minutes.slice(0, 3), [24, 25, 26]), [[0, 1, 2]])
})

test('same-channel concurrency is not presented as channel switching', () => {
  assert.match(radioObservation(5240, 5240), /same frequency/)
  assert.match(radioObservation(5180, 5240), /does not prove/)
  assert.match(radioObservation(5180, null), /not reported/)
})

test('a newer capture containing only missing-surface events remains selectable', () => {
  const periods = timingPeriods([
    minute('01:44', 'old'),
    { bucketStart: '2026-09-08T01:56:24Z', sessionId: 'new' },
  ])
  assert.equal(periods.length, 2)
  assert.equal(periods.at(-1).sessionId, 'new')
  assert.equal(inTimingPeriod('2026-09-08T01:56:24Z', 'new', periods.at(-1)), true)
})

test('GPS fields without a successful capture remain unknown, including failed captures with old values', () => {
  for (const gpsCaptureStatus of [undefined, null, 'parser_error', 'dump_error', 'limit', 'unsupported', 'tool_unavailable', 'future_status']) {
    const event = { gpsCaptureStatus, locGpsEnabled: 1, locGpsAgeMs: 0, locGpsHaccM: 2 }
    assert.deepEqual(gpsProviderObservation(event, 'Gps'), {
      present: null, enabled: null, ageMs: null, accuracyM: null, satellites: null, zlinkListener: null,
    })
  }
  assert.equal(gpsCaptureLabel({}), 'Unknown')
  assert.equal(gpsCaptureLabel({ gpsCaptureStatus: 'future_status' }), 'Unknown')
  assert.equal(gpsCaptureLabel({ gpsCaptureStatus: 'ok' }), 'Captured')
})

test('GPS provider presence does not invent a fix; zero age and disabled state remain distinct from unknown', () => {
  const providerOnly = gpsProviderObservation({ gpsCaptureStatus: 'ok', locGpsPresent: 1, locGpsEnabled: 1 }, 'Gps')
  assert.equal(providerOnly.present, true)
  assert.equal(providerOnly.ageMs, null)
  assert.equal(providerOnly.accuracyM, null)
  const observed = gpsProviderObservation({
    gpsCaptureStatus: 'ok', locGpsPresent: 1, locGpsEnabled: 0, locGpsAgeMs: 0,
    locGpsHaccM: 0, locGpsSatellites: 0, locGpsZlinkListener: 0, locFusedAgeMs: 5000,
  }, 'Gps')
  assert.deepEqual(observed, { present: true, enabled: false, ageMs: 0, accuracyM: 0, satellites: 0, zlinkListener: false })
  assert.equal(gpsProviderObservation({ gpsCaptureStatus: 'ok', locFusedAgeMs: 5000 }, 'Fused').ageMs, 5000)
  assert.equal(gpsProviderObservation({ gpsCaptureStatus: 'ok', locGpsAgeMs: -1, locGpsHaccM: Infinity }, 'Gps').ageMs, null)
  assert.equal(gpsProviderObservation({ gpsCaptureStatus: 'ok', locGpsHaccM: Infinity }, 'Gps').accuracyM, null)
})

test('cumulative first-fix means require an observed positive report count', () => {
  assert.equal(gpsFirstFixSummary({ gpsCaptureStatus: 'ok', gnssTtffMeanS: 0 }), 'Unknown')
  assert.equal(gpsFirstFixSummary({ gpsCaptureStatus: 'ok', gnssTtffReports: 0, gnssTtffMeanS: 0 }), 'No first-fix reports')
  assert.equal(gpsFirstFixSummary({ gpsCaptureStatus: 'ok', gnssTtffReports: 2, gnssTtffMeanS: 31.5 }), '2 reports · mean 31.5 s')
  assert.equal(gpsFirstFixSummary({ gpsCaptureStatus: 'ok', gnssTtffReports: 1 }), '1 report · mean unknown')
  assert.equal(gpsFirstFixSummary({ gpsCaptureStatus: 'dump_error', gnssTtffReports: 2, gnssTtffMeanS: 31.5 }), 'Unknown')
})

test('current peer metrics include schema 7 and never fall back to legacy socket values', () => {
  assert.deepEqual(peerTransportObservation([
    { diagnosticSchema: 5, peerRxQueueBytes: 400, peerRecentRttMaxMs: 5 },
    { diagnosticSchema: 6, peerRxQueueBytes: 0, peerRecentRttMaxMs: 0 },
    { diagnosticSchema: 7, wirePeerRxQueueBytes: 8192, wirePeerTcpRttMaxMs: 15 },
  ]), { current: true, captures: 2, receiveQueue: 8192, rtt: 15 })
  assert.deepEqual(peerTransportObservation([
    { diagnosticSchema: 6, peerRxQueueBytes: 0, peerRecentRttMaxMs: 0 },
  ]), { current: true, captures: 1, receiveQueue: null, rtt: null })
  assert.deepEqual(peerTransportObservation([
    { diagnosticSchema: 5, peerRxQueueBytes: 0, peerRecentRttMaxMs: 5 },
  ]), { current: false, captures: 1, receiveQueue: 0, rtt: 5 })
})

const gpsEvent = (index) => ({
  kind: 'gps_context', sessionId: 'gps-only',
  occurredAt: new Date(Date.parse('2026-09-28T00:00:00Z') + index * 15_000).toISOString(),
})

test('GPS pagination starts at startup and exposes all observations in chronological bounded pages', () => {
  const expected = Array.from({ length: 53 }, (_, i) => gpsEvent(i))
  const input = [{ ...gpsEvent(0), kind: 'wireless_link' }, ...[...expected].reverse()]
  const originalOrder = [...input]
  const first = gpsObservationPage(input)
  assert.equal(first.page, 0)
  assert.equal(first.pageCount, 5)
  assert.equal(first.total, 53)
  assert.equal(first.start, 0)
  assert.equal(first.end, 12)
  assert.deepEqual(first.captures, expected.slice(0, 12))
  const traversed = Array.from({ length: first.pageCount }, (_, page) => {
    const result = gpsObservationPage(input, page)
    assert.ok(result.captures.length <= 12)
    return result.captures
  }).flat()
  assert.deepEqual(traversed, expected)
  assert.deepEqual(gpsObservationPage(input, 'latest').captures, expected.slice(48))
  assert.deepEqual(input, originalOrder)
})

test('GPS pagination handles empty or reduced history and latest mode follows new captures', () => {
  assert.deepEqual(gpsObservationPage([], 'latest'), { page: 0, pageCount: 0, total: 0, start: 0, end: 0, captures: [] })
  assert.equal(gpsObservationPage([gpsEvent(0)], 99).page, 0)
  assert.equal(gpsObservationPage([gpsEvent(0)], -1).page, 0)
  const initial = Array.from({ length: 24 }, (_, i) => gpsEvent(i))
  assert.equal(gpsObservationPage(initial, 'latest').page, 1)
  const updated = [...initial, gpsEvent(24)]
  assert.deepEqual(gpsObservationPage(updated, 'latest').captures, [gpsEvent(24)])
  assert.deepEqual(gpsObservationPage(updated).captures, initial.slice(0, 12))
})

test('a GPS-only observed period stays selectable and pageable without any frame samples', () => {
  const events = Array.from({ length: 15 }, (_, i) => gpsEvent(i))
  const periods = timingPeriods(events.map((event) => ({ bucketStart: event.occurredAt, sessionId: event.sessionId })))
  assert.equal(periods.length, 1)
  const selected = events.filter((event) => inTimingPeriod(event.occurredAt, event.sessionId, periods[0]))
  assert.equal(selected.length, 15)
  assert.equal(gpsObservationPage(selected).captures.length, 12)
  assert.equal(gpsObservationPage(selected, 1).captures.length, 3)
})
