import assert from 'node:assert/strict'
import fs from 'node:fs/promises'
import test from 'node:test'
import ts from 'typescript'

async function moduleUrl(name) {
  const source = await fs.readFile(new URL(`../src/lib/${name}.ts`, import.meta.url), 'utf8')
  const code = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext } }).outputText
  return `data:text/javascript;base64,${Buffer.from(code).toString('base64')}`
}
const { clipWindow } = await import(await moduleUrl('clipWindow'))

test('the export window keeps its full length when the playhead is near either end', () => {
  assert.deepEqual(clipWindow(0, 30, 59), { start: 0, end: 30 }, 'playhead at 0:00 slides forward')
  assert.deepEqual(clipWindow(5, 30, 59), { start: 0, end: 30 })
  assert.deepEqual(clipWindow(30, 30, 59), { start: 15, end: 45 }, 'centred when room on both sides')
  assert.deepEqual(clipWindow(58, 30, 59), { start: 29, end: 59 }, 'playhead at the end slides back')
})

test('a recording shorter than the window exports in full', () => {
  assert.deepEqual(clipWindow(10, 30, 20), { start: 0, end: 20 })
  assert.deepEqual(clipWindow(0, 30, 30), { start: 0, end: 30 })
})

test('an unknown duration cannot be clamped at the end but still clamps at zero', () => {
  assert.deepEqual(clipWindow(5, 30, null), { start: 0, end: undefined })
  assert.deepEqual(clipWindow(40, 30, undefined), { start: 25, end: undefined })
  assert.deepEqual(clipWindow(Number.NaN, 30, 59), { start: 0, end: 30 })
})
