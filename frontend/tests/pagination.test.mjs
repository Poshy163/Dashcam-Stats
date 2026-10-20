import assert from 'node:assert/strict'
import { after, test } from 'node:test'
import { mkdtemp, rm } from 'node:fs/promises'
import { fileURLToPath, pathToFileURL } from 'node:url'
import path from 'node:path'
import { build } from 'esbuild'
import React from 'react'
import { act, create } from 'react-test-renderer'

const directory = await mkdtemp(fileURLToPath(new URL('../node_modules/.pagination-test-', import.meta.url)))
after(() => rm(directory, { recursive: true, force: true }))
await build({
  stdin: {
    contents: `export { Pagination } from '@/components/ui'`,
    resolveDir: fileURLToPath(new URL('..', import.meta.url)), loader: 'tsx',
  },
  outfile: path.join(directory, 'ui.mjs'), bundle: true, format: 'esm', platform: 'node', jsx: 'automatic',
  external: ['react', 'react/*', 'react-router-dom', '@tanstack/react-query'],
  alias: { '@': fileURLToPath(new URL('../src', import.meta.url)) }, loader: { '.css': 'empty' },
})
const { Pagination } = await import(pathToFileURL(path.join(directory, 'ui.mjs')).href)

function mount({ page, pages, total = pages * 25 }) {
  const changes = []
  let root
  act(() => { root = create(React.createElement(Pagination, { page, pages, total, onChange: next => changes.push(next) })) })
  const nav = root.root.findByProps({ 'aria-label': 'Pagination' })
  const button = label => nav.findAll(node => node.type === 'button' && (node.props['aria-label'] === label || node.children.join('') === label))[0]
  const input = () => nav.findByProps({ 'aria-label': 'Go to page' })
  return { root, nav, changes, button, input }
}

test('the status line still comes first and reads page x of y with the result count', () => {
  const view = mount({ page: 2, pages: 563, total: 14064 })
  assert.equal(view.nav.findAllByType('div')[0].children.join(''), 'Page 2 of 563 · 14064 results')
})

test('first and last jump to the ends and are disabled at them', () => {
  const view = mount({ page: 7, pages: 20 })
  act(() => view.button('First page').props.onClick())
  act(() => view.button('Last page').props.onClick())
  assert.deepEqual(view.changes, [1, 20])

  const first = mount({ page: 1, pages: 20 })
  assert.equal(first.button('First page').props.disabled, true)
  assert.equal(first.button('Previous').props.disabled, true)
  assert.equal(first.button('Last page').props.disabled, false)

  const last = mount({ page: 20, pages: 20 })
  assert.equal(last.button('Next').props.disabled, true)
  assert.equal(last.button('Last page').props.disabled, true)
})

test('typing a page and pressing Enter navigates, clamped to the available range', () => {
  const view = mount({ page: 3, pages: 20 })
  let prevented = 0
  const enter = { key: 'Enter', preventDefault: () => { prevented += 1 } }
  act(() => view.input().props.onChange({ target: { value: '12' } }))
  act(() => view.input().props.onKeyDown(enter))
  act(() => view.input().props.onChange({ target: { value: '999' } }))
  act(() => view.input().props.onKeyDown(enter))
  act(() => view.input().props.onChange({ target: { value: '0' } }))
  act(() => view.input().props.onKeyDown(enter))
  assert.deepEqual(view.changes, [12, 20, 1])
  assert.equal(prevented, 3)
})

test('an empty or unchanged jump does nothing and restores the current page number', () => {
  const view = mount({ page: 3, pages: 20 })
  act(() => view.input().props.onChange({ target: { value: '' } }))
  act(() => view.input().props.onBlur())
  assert.deepEqual(view.changes, [])
  assert.equal(view.input().props.value, '3')
  act(() => view.input().props.onChange({ target: { value: '3' } }))
  act(() => view.input().props.onBlur())
  assert.deepEqual(view.changes, [])
})

test('a single page renders only the result count', () => {
  let root
  act(() => { root = create(React.createElement(Pagination, { page: 1, pages: 1, total: 9, onChange: () => {} })) })
  assert.equal(root.root.findAll(node => node.props['aria-label'] === 'Pagination').length, 0)
  assert.equal(root.toJSON().children.join(''), '9 results')
})
