import assert from 'node:assert/strict'
import { test } from 'node:test'
import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import postcss from 'postcss'
import tailwindcss from '@tailwindcss/postcss'

const base = fileURLToPath(new URL('..', import.meta.url))
const filename = fileURLToPath(new URL('../src/index.css', import.meta.url))
const compiled = await postcss([tailwindcss({ base, optimize: { minify: false } })])
  .process(await readFile(filename, 'utf8'), { from: filename })

function declarations(selector) {
  const values = new Map()
  compiled.root.walkRules(rule => {
    if (rule.selectors.includes(selector)) {
      rule.walkDecls(declaration => values.set(declaration.prop, declaration.value))
    }
  })
  return values
}

test('Tailwind compiles the existing config, theme tokens and responsive source classes', () => {
  assert.equal(declarations('.bg-surface').get('background-color'), 'rgb(var(--surface) / 1)')
  assert.equal(declarations('.text-content').get('color'), 'rgb(var(--content) / 1)')
  assert.match(declarations('.bg-accent\\/10').get('background-color'), /color-mix\(.*var\(--accent\).*10%/)
  assert.equal(declarations('.dark').get('--surface'), '9 11 15')
  assert.equal(declarations('.dark').get('color-scheme'), 'dark')
  assert.equal(declarations('.text-2xs').get('font-size'), '.6875rem')
  assert.equal(declarations('.sm\\:grid-cols-2').get('grid-template-columns'), 'repeat(2, minmax(0, 1fr))')
  assert.equal(declarations('.xl\\:grid-cols-5').get('grid-template-columns'), 'repeat(5, minmax(0, 1fr))')
  const unresolved = []
  compiled.root.walkAtRules(rule => {
    if (['apply', 'config', 'tailwind', 'theme'].includes(rule.name)) unresolved.push(rule.name)
  })
  assert.deepEqual(unresolved, [])
})

test('existing cards, controls and keyboard focus keep their compiled styling', () => {
  assert.equal(declarations('.card').get('border-width'), '1px')
  assert.equal(declarations('.card').get('background-color'), 'rgb(var(--surface-raised) / 1)')
  assert.match(declarations('.card').get('--tw-shadow'), /0 8px 24px/)
  assert.equal(declarations('.btn').get('display'), 'inline-flex')
  assert.equal(declarations('.btn').get('min-height'), 'calc(var(--spacing) * 10)')
  assert.equal(declarations('.btn:focus-visible').get('outline-width'), '2px')
  assert.equal(declarations('.btn:focus-visible').get('outline-color'), 'rgb(var(--accent) / 1)')
  assert.equal(declarations('.input').get('width'), '100%')
  assert.equal(declarations('.input:focus').get('outline'), '2px solid #0000')
  assert.match(declarations('.shadow-sm').get('--tw-shadow'), /^0 1px 2px 0 /)
  assert.equal(declarations('.rounded-sm').get('border-radius'), 'var(--radius-sm)')
  assert.equal(declarations('.backdrop-blur-sm').get('--tw-backdrop-blur'), 'blur(var(--blur-sm))')
  const variables = new Map()
  compiled.root.walkDecls(declaration => {
    if (['--radius-sm', '--blur-sm'].includes(declaration.prop)) variables.set(declaration.prop, declaration.value)
  })
  assert.equal(variables.get('--radius-sm'), '.125rem')
  assert.equal(variables.get('--blur-sm'), '4px')
})
