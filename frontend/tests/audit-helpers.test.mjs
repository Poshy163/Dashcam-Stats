import assert from 'node:assert/strict'
import fs from 'node:fs/promises'
import { execFileSync } from 'node:child_process'
import test from 'node:test'
import ts from 'typescript'

async function moduleUrl(name) {
  const source = await fs.readFile(new URL(`../src/lib/${name}.ts`, import.meta.url), 'utf8')
  const code = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext } }).outputText
  return `data:text/javascript;base64,${Buffer.from(code).toString('base64')}`
}
const { buildPlayableTimeline, playbackAtTimestamp } = await import(await moduleUrl('journeyPlayback'))
const { apiHeaders, apiErrorMessage } = await import(await moduleUrl('apiTransport'))
const { backupAttention, capacityExceeded } = await import(await moduleUrl('operationalStatus'))
const { claimChunkReload, clearChunkReload } = await import(await moduleUrl('chunkRecovery'))
const { removeSavedDraft } = await import(await moduleUrl('settingsDraft'))
const formatUrl = await moduleUrl('format')
const { utcInputValue } = await import(formatUrl)
const origin = Date.parse('2026-09-30T00:00:00Z')
const clip = (id, role, start, duration, fileMissing = false) => ({ id, camera: { role }, startedAt: new Date(origin + start * 1000).toISOString(), durationS: duration, fileMissing })

test('camera switching finds capture time across unequal boundaries and missing clips', () => {
  const timeline = buildPlayableTimeline([
    clip(1, 'front', 0, 60), clip(2, 'rear', 20, 25), clip(3, 'rear', 45, 35),
    clip(4, 'rear', 80, 60, true), clip(5, 'rear', 140, 60),
  ], 'rear')
  const exact = playbackAtTimestamp(timeline, origin + 50_000)
  assert.equal(timeline[exact.clipIndex].recording.id, 3)
  assert.equal(exact.offsetS, 5)
  assert.equal(exact.elapsedS, 30)
  assert.equal(exact.timestampMs, origin + 50_000)
  assert.equal(exact.exact, true)
  const boundary = playbackAtTimestamp(timeline, origin + 45_000)
  assert.equal(timeline[boundary.clipIndex].recording.id, 3)
  assert.equal(boundary.offsetS, 0)
  const missing = playbackAtTimestamp(timeline, origin + 110_000)
  assert.equal(missing.exact, false)
  assert.equal(timeline[missing.clipIndex].recording.id, 5)
  assert.equal(missing.timestampMs, origin + 140_000)
})

test('camera fallback handles shorter angles and empty or invalid timelines', () => {
  const timeline = buildPlayableTimeline([clip(1, 'rear', 20, 30)], 'rear')
  assert.equal(playbackAtTimestamp(timeline, origin).offsetS, 0)
  const after = playbackAtTimestamp(timeline, origin + 90_000)
  assert.equal(after.exact, false)
  assert.ok(after.offsetS < 30 && after.offsetS > 29.99)
  assert.equal(playbackAtTimestamp([], origin), null)
  assert.equal(playbackAtTimestamp(timeline, NaN), null)
})

test('API headers merge Headers, tuples and objects while respecting explicit content type', () => {
  for (const headers of [{ 'X-Request': 'audit' }, [['X-Request', 'audit']], new Headers({ 'X-Request': 'audit' })]) {
    const result = apiHeaders(headers)
    assert.equal(result.get('Content-Type'), 'application/json')
    assert.equal(result.get('X-Request'), 'audit')
  }
  assert.equal(apiHeaders({ 'content-type': 'application/octet-stream' }).get('Content-Type'), 'application/octet-stream')
})

test('validation errors name invalid fields without reflecting submitted secrets', () => {
  assert.equal(apiErrorMessage({ detail: [{ loc: ['body', 'password'], msg: 'Too short', input: 'do-not-echo' }, { loc: ['query', 'page'], msg: 'Must be positive' }] }, 'HTTP 422'), 'password: Too short; page: Must be positive')
  assert.equal(apiErrorMessage({ detail: 'Read-only mode' }, 'HTTP 403'), 'Read-only mode')
  assert.equal(apiErrorMessage({ detail: [{ input: 'do-not-echo' }] }, 'HTTP 422'), 'HTTP 422')
})

test('operational warnings distinguish an offline car from failed or stale backup work', () => {
  const now = Date.parse('2026-09-30T12:00:00Z')
  const normal = { state: 'offline', lastError: null, backlogKnown: true, backlogFiles: 0, lastSuccessTs: null }
  assert.deepEqual(backupAttention(normal, now), [])
  assert.equal(backupAttention({ ...normal, lastError: 'failed' }, now).length, 1)
  assert.equal(backupAttention({ ...normal, backlogFiles: 3, lastSuccessTs: '2026-09-28T12:00:00Z' }, now).length, 1)
  assert.equal(backupAttention({ ...normal, backlogFiles: 3, lastSuccessTs: '2026-09-30T11:00:00Z' }, now).length, 0)
  assert.equal(backupAttention({ ...normal, state: 'running', lastError: 'previous failure' }, now).length, 0)
  const radioHold = { ...normal, state: 'idle', radioQuietingHold: true, lastError: 'Waiting for radio shutdown: sleep timing unknown' }
  assert.deepEqual(backupAttention(radioHold, now), [], 'an admission hold is not a failed backup')
  assert.equal(backupAttention({ ...radioHold, backlogFiles: 3, lastSuccessTs: '2026-09-28T12:00:00Z' }, now).length, 1, 'a radio hold does not hide stale backlog')
  assert.equal(backupAttention({ ...radioHold, state: 'error' }, now).length, 1, 'a stale hold flag cannot hide a terminal error')
  assert.equal(capacityExceeded(459, 200), true)
  assert.equal(capacityExceeded(459, 0), false)
  assert.equal(capacityExceeded(NaN, 200), false)
})

test('slow settings saves preserve edits made after submission', () => {
  assert.deepEqual(removeSavedDraft({ width: 480, limit: 300, timezone: 'UTC' }, { width: 320, timezone: 'UTC' }), { width: 480, limit: 300 })
  assert.deepEqual(removeSavedDraft({ width: 320 }, { width: 320 }), {})
})

test('chunk recovery refuses to reload without a persistent guard', () => {
  const storage = new Map()
  const target = { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key) }
  assert.equal(claimChunkReload(target), true)
  assert.equal(claimChunkReload(target), false)
  clearChunkReload(target)
  assert.equal(claimChunkReload(target), true)
  assert.equal(claimChunkReload({ getItem() { throw new Error('blocked') } }), false)
  assert.equal(claimChunkReload({ getItem: () => null, setItem() { throw new Error('blocked') } }), false)
})

test('camera dates and DST transitions remain identical in different browser timezones', () => {
  const script = `import { setDisplayTimeZone, formatDateTime, formatDate, formatTime } from ${JSON.stringify(formatUrl)}; setDisplayTimeZone('Australia/Adelaide'); console.log(JSON.stringify(['2026-09-30T15:00:00Z', '2026-10-03T16:30:00Z'].map(t => [formatDateTime(t), formatDate(t), formatTime(t)])))`
  const render = zone => execFileSync(process.execPath, ['--input-type=module', '-e', script], { encoding: 'utf8', env: { ...process.env, TZ: zone } }).trim()
  assert.equal(render('UTC'), render('Pacific/Honolulu'))
  const actual = JSON.parse(render('UTC'))
  const stamp = '2026-09-30T15:00:00Z'
  assert.equal(actual[0][1], new Date(stamp).toLocaleDateString(undefined, { timeZone: 'Australia/Adelaide', year: 'numeric', month: 'short', day: 'numeric' }))
  assert.notEqual(actual[0][1], new Date(stamp).toLocaleDateString(undefined, { timeZone: 'UTC', year: 'numeric', month: 'short', day: 'numeric' }))
})

test('UTC filter fields normalize offset timestamps instead of changing the requested instant', () => {
  assert.equal(utcInputValue('2026-09-30T10:30:00+09:30'), '2026-09-30T01:00')
  assert.equal(utcInputValue('2026-09-30T01:00:00Z'), '2026-09-30T01:00')
  assert.equal(utcInputValue('bad'), '')
  assert.equal(utcInputValue(undefined), '')
})

test('blocked sessionStorage getter is handled inside the recovery guard', t => {
  const old = Object.getOwnPropertyDescriptor(globalThis, 'sessionStorage')
  Object.defineProperty(globalThis, 'sessionStorage', { configurable: true, get() { throw new Error('SecurityError') } })
  t.after(() => { if (old) Object.defineProperty(globalThis, 'sessionStorage', old); else delete globalThis.sessionStorage })
  assert.equal(claimChunkReload(), false)
  assert.doesNotThrow(() => clearChunkReload())
})
