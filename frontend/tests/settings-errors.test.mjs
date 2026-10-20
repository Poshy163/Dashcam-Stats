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

const directory = await mkdtemp(fileURLToPath(new URL('../node_modules/.settings-errors-test-', import.meta.url)))
after(() => rm(directory, { recursive: true, force: true }))
await build({
  stdin: {
    contents: `export { default as Settings } from '@/pages/Settings'; export { api } from '@/lib/api'; export { clearSettingError, settingsSaveErrors } from '@/lib/settingsErrors'`,
    resolveDir: fileURLToPath(new URL('..', import.meta.url)), loader: 'tsx',
  },
  outfile: path.join(directory, 'settings.mjs'), bundle: true, format: 'esm', platform: 'node', jsx: 'automatic',
  external: ['react', 'react/*', 'react-router-dom', '@tanstack/react-query'],
  alias: { '@': fileURLToPath(new URL('../src', import.meta.url)) },
})
const { Settings, api, clearSettingError, settingsSaveErrors } = await import(pathToFileURL(path.join(directory, 'settings.mjs')).href)

const field = (key, label, type, value) => ({
  key, label, type, value, default: value, description: '', minimum: 1, maximum: 60,
  choices: [], unit: null, dangerous: false, requires: null, read_only: false, is_default: false,
})
const categories = () => [
  { key: 'general', label: 'General', description: '', settings: [field('general.timezone', 'Timezone', 'string', 'Australia/Adelaide')] },
  { key: 'scanner', label: 'Scanner', description: '', settings: [field('scanner.interval_minutes', 'Scan interval', 'int', 5)] },
]
const text = node => typeof node === 'string' ? node : (node.children ?? []).map(text).join('')

async function mount(t, initial = 'scanner') {
  const client = new QueryClient({ defaultOptions: { queries: { enabled: false, retry: false, gcTime: Infinity }, mutations: { retry: false } } })
  client.setQueryData(['settings'], categories())
  const originalUpdate = api.settings.update
  const summaryFocus = []
  let root
  await act(async () => {
    root = create(React.createElement(QueryClientProvider, { client },
      React.createElement(MemoryRouter, { initialEntries: [`/settings?category=${initial}`] }, React.createElement(Settings))), {
      createNodeMock: element => element.props.role === 'alert' ? {
        focus: () => summaryFocus.push('focused'), scrollIntoView: () => {},
      } : null,
    })
  })
  t.after(async () => {
    await act(async () => root.unmount())
    client.clear()
    api.settings.update = originalUpdate
  })
  const button = label => root.root.findAll(node => node.type === 'button' && text(node) === label)[0]
  const input = key => root.root.findByProps({ id: `setting-${key.replaceAll('.', '-')}` })
  const edit = async (key, value) => act(async () => input(key).props.onChange({ target: { value } }))
  const click = async label => {
    assert.ok(button(label), `button ${label} exists`)
    await act(async () => { button(label).props.onClick(); await new Promise(resolve => setTimeout(resolve, 0)) })
  }
  const alerts = () => root.root.findAll(node => node.props.role === 'alert')
  return { root, client, button, input, edit, click, alerts, summaryFocus }
}

test('a failed save on another category keeps both drafts and exposes navigation to its invalid field', async t => {
  const view = await mount(t)
  let submitted
  api.settings.update = async values => {
    submitted = values
    throw new Error('scanner.interval_minutes must be at least 1')
  }
  await view.edit('scanner.interval_minutes', '0')
  await view.click('General')
  await view.edit('general.timezone', 'Australia/Sydney')
  await view.click('Save changes')

  assert.deepEqual(submitted, { 'scanner.interval_minutes': 0, 'general.timezone': 'Australia/Sydney' })
  assert.equal(view.input('general.timezone').props.value, 'Australia/Sydney')
  assert.equal(view.alerts().length, 1)
  assert.match(text(view.alerts()[0]), /Your unsaved changes have been kept/)
  assert.match(text(view.alerts()[0]), /scanner.interval_minutes must be at least 1/)
  assert.ok(view.button('Scanner1 error'), 'the other category visibly marks its error')
  assert.equal(view.button('Scanner1 error').props['aria-label'], 'Scanner, 1 error')
  assert.deepEqual(view.summaryFocus, ['focused'], 'the failure summary is brought into keyboard focus')

  const focused = []
  const originalDocument = globalThis.document
  globalThis.document = { getElementById: id => id === 'setting-scanner-interval_minutes' ? {
    matches: () => false, scrollIntoView: () => {}, focus: () => focused.push(id),
  } : null }
  try {
    await view.click('Scanner · Scan interval')
  } finally {
    if (originalDocument === undefined) delete globalThis.document
    else globalThis.document = originalDocument
  }
  assert.deepEqual(focused, ['setting-scanner-interval_minutes'])
  assert.equal(view.input('scanner.interval_minutes').props.value, 0)
  assert.equal(view.input('scanner.interval_minutes').props['aria-invalid'], true)
  assert.match(view.input('scanner.interval_minutes').props['aria-describedby'], /-error/)

  await view.edit('scanner.interval_minutes', '2')
  assert.equal(view.alerts().length, 0, 'editing the invalid field clears its old validation result')
  assert.equal(view.input('scanner.interval_minutes').props['aria-invalid'], false)
  assert.ok(view.button('Scanner'), 'the category badge clears with its field error')
  await view.click('General')
  assert.equal(view.input('general.timezone').props.value, 'Australia/Sydney')
})

test('general save failures remain visible independently of the save footer and discard clears them', async t => {
  const view = await mount(t, 'general')
  api.settings.update = async () => { throw new Error('Could not contact the settings service') }
  await view.edit('general.timezone', 'Australia/Sydney')
  await view.click('Save changes')
  assert.match(text(view.alerts()[0]), /Could not contact the settings service/)
  await view.click('Discard')
  assert.equal(view.alerts().length, 0)
  assert.equal(view.input('general.timezone').props.value, 'Australia/Adelaide')
})

test('read-only and removed fields keep a useful summary without offering an impossible input focus', async t => {
  const view = await mount(t)
  api.settings.update = async () => { throw new Error('scanner.interval_minutes must be at least 1') }
  await view.edit('scanner.interval_minutes', '0')
  await view.click('Save changes')
  const readOnly = categories()
  readOnly[1].settings[0].read_only = true
  await act(async () => { view.client.setQueryData(['settings'], readOnly) })

  const focused = []
  const originalDocument = globalThis.document
  globalThis.document = { getElementById: id => id.endsWith('-field') ? {
    scrollIntoView: () => {}, focus: () => focused.push(id),
  } : { matches: () => true } }
  try {
    await view.click('Scanner · Scan interval')
  } finally {
    if (originalDocument === undefined) delete globalThis.document
    else globalThis.document = originalDocument
  }
  assert.equal(view.input('scanner.interval_minutes').props.disabled, true)
  assert.deepEqual(focused, ['setting-scanner-interval_minutes-field'])

  await act(async () => {
    view.client.setQueryData(['settings'], categories().slice(0, 1))
    await new Promise(resolve => setTimeout(resolve, 0))
  })
  assert.match(text(view.alerts()[0]), /scanner.interval_minutes \(not available in this view\)/)
  assert.equal(view.button('Scanner · Scan interval'), undefined)
})

test('late failure does not mark a newer draft invalid and editing one error preserves the others', () => {
  assert.deepEqual(settingsSaveErrors(
    'scanner.interval_minutes must be positive',
    { 'scanner.interval_minutes': 0 },
    { 'scanner.interval_minutes': 2 },
  ), { _: 'The previous settings save failed. Review your current changes and try again.' })
  assert.deepEqual(settingsSaveErrors(
    'scanner.interval_minutes_extra is invalid',
    { 'scanner.interval_minutes': 0, 'scanner.interval_minutes_extra': 4 },
    { 'scanner.interval_minutes': 0, 'scanner.interval_minutes_extra': 4 },
  ), { 'scanner.interval_minutes_extra': 'scanner.interval_minutes_extra is invalid' })
  assert.deepEqual(clearSettingError({ a: 'invalid a', b: 'invalid b', _: 'request failed' }, 'a'), { b: 'invalid b' })
})
