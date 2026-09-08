import assert from 'node:assert/strict'
import fs from 'node:fs/promises'
import test from 'node:test'
import ts from 'typescript'

const source = await fs.readFile(new URL('../src/lib/carplay.ts', import.meta.url), 'utf8')
const code = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext } }).outputText
const { meanKnown, maxKnown, timingPeriods, timingSegments, inTimingPeriod, radioObservation } =
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
