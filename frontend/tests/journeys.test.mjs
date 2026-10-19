import assert from 'node:assert/strict'
import { after, test } from 'node:test'
import { mkdtemp, rm } from 'node:fs/promises'
import { fileURLToPath, pathToFileURL } from 'node:url'
import path from 'node:path'
import { build } from 'esbuild'
import React from 'react'
import { act, create } from 'react-test-renderer'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, useLocation, useNavigate } from 'react-router-dom'

const directory = await mkdtemp(fileURLToPath(new URL('../node_modules/.journeys-test-', import.meta.url)))
after(() => rm(directory, { recursive: true, force: true }))
await build({
  stdin: {
    contents: `export { default as Journeys } from '@/pages/Journeys'; export { journeyDatePreset, journeyDateRangeError } from '@/lib/journeyFilters';`,
    resolveDir: fileURLToPath(new URL('..', import.meta.url)),
    loader: 'tsx',
  },
  outfile: path.join(directory, 'journeys.mjs'), bundle: true, format: 'esm', platform: 'node', jsx: 'automatic',
  external: ['react', 'react/*', 'react-router-dom', '@tanstack/react-query'],
  alias: { '@': fileURLToPath(new URL('../src', import.meta.url)) },
})
const { Journeys, journeyDatePreset, journeyDateRangeError } = await import(pathToFileURL(path.join(directory, 'journeys.mjs')).href)

async function settle() {
  for (let n = 0; n < 4; n++) await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
}

const journey = id => ({ id, started_at: '2026-10-18T23:00:00Z', ended_at: '2026-10-18T23:15:00Z', duration_s: 900, has_gps: false, recording_count: 2, vehicle_count: 0, unique_plate_count: 0 })

test('a background list refresh removes hidden journeys from merge selection', async t => {
  const view = await mountJourneys(t)
  await view.selectAll()
  assert.ok(view.button('Merge 2 journeys'))
  await act(async () => {
    view.client.setQueriesData({ queryKey: ['journeys'] }, (data) => data && ({ ...data, items: data.items.slice(1) }))
  })
  await settle()
  assert.equal(view.button('Merge 2 journeys'), undefined)
  assert.equal(view.root.root.findByProps({ role: 'status' }).findByType('span').children.join(''), '1 selected on this page')
  await act(async () => {
    view.client.setQueriesData({ queryKey: ['journeys'] }, (data) => data && ({ ...data, items: [journey(11), ...data.items] }))
  })
  await settle()
  assert.equal(view.selections.find(input => input.props['aria-label'] === 'Select journey 11').props.checked, false)
})

async function mountJourneys(t, { entries = ['/journeys'], empty = false, timezone = 'Australia/Adelaide' } = {}) {
  const requests = []
  const oldFetch = globalThis.fetch
  globalThis.fetch = async (url, init) => {
    const request = new URL(url, 'http://localhost')
    requests.push({ url: request, init })
    if (request.pathname === '/api/status') return new Response(JSON.stringify({ timezone }))
    if (request.pathname === '/api/journeys/motion-quality') return new Response(JSON.stringify({ total: 4, pending: 0, moving: 4, unconfirmed: 0, unknown: 0 }))
    assert.equal(request.pathname, '/api/journeys', `Unexpected request: ${request}`)
    assert.equal(init?.method ?? 'GET', 'GET', 'selection tests must never merge journeys')
    const page = Number(request.searchParams.get('page') ?? 1)
    return new Response(JSON.stringify({ items: empty ? [] : [journey(page * 10 + 1), journey(page * 10 + 2)], total: empty ? 0 : 60, page, pages: empty ? 0 : 3, page_size: 25 }))
  }
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } })
  let root, location, navigate
  function Observer() { location = useLocation(); navigate = useNavigate(); return null }
  t.after(async () => {
    if (root) await act(async () => root.unmount())
    client.clear()
    globalThis.fetch = oldFetch
  })
  await act(async () => {
    root = create(React.createElement(QueryClientProvider, { client }, React.createElement(MemoryRouter, { initialEntries: entries }, React.createElement(Observer), React.createElement(Journeys))))
  })
  await settle()
  return {
    root, client, requests,
    get params() { return new URLSearchParams(location.search) },
    get lists() { return requests.filter(request => request.url.pathname === '/api/journeys') },
    input: label => root.root.findByProps({ 'aria-label': label }),
    button: label => root.root.findAllByType('button').find(button => button.children.join('') === label),
    get selections() { return root.root.findAllByType('input').filter(input => /^Select journey \d+$/.test(input.props['aria-label'] ?? '')) },
    async back() { await act(async () => navigate(-1)); await settle() },
    async change(label, value) {
      await act(async () => root.root.findByProps({ 'aria-label': label }).props.onChange({ target: { value } }))
      await settle()
    },
    async selectAll() {
      for (const input of this.selections) await act(async () => input.props.onChange({ target: { checked: true } }))
    },
  }
}

test('journey date presets follow camera dates across midnight, DST, year boundaries and leap days', () => {
  const now = new Date('2026-10-18T14:00:00Z') // Monday in Adelaide, still Sunday in UTC.
  assert.deepEqual(journeyDatePreset('today', 'Australia/Adelaide', now), { dateFrom: '2026-10-19', dateTo: '2026-10-19' })
  assert.deepEqual(journeyDatePreset('yesterday', 'Australia/Adelaide', now), { dateFrom: '2026-10-18', dateTo: '2026-10-18' })
  assert.deepEqual(journeyDatePreset('last7', 'Australia/Adelaide', now), { dateFrom: '2026-10-13', dateTo: '2026-10-19' })
  assert.deepEqual(journeyDatePreset('today', 'UTC', now), { dateFrom: '2026-10-18', dateTo: '2026-10-18' })
  assert.deepEqual(journeyDatePreset('last7', 'Australia/Adelaide', new Date('2026-10-03T17:00:00Z')), { dateFrom: '2026-09-28', dateTo: '2026-10-04' })
  assert.deepEqual(journeyDatePreset('last7', 'Australia/Adelaide', new Date('2026-12-31T14:00:00Z')), { dateFrom: '2026-12-26', dateTo: '2027-01-01' })
  assert.deepEqual(journeyDatePreset('yesterday', 'UTC', new Date('2028-03-01T00:00:00Z')), { dateFrom: '2028-02-29', dateTo: '2028-02-29' })
})

test('journey date validation accepts open bounds and rejects impossible calendar days and reversed ranges', () => {
  for (const [from, to] of [['', ''], ['2026-10-01', ''], ['', '2026-10-19'], ['2024-02-29', '2024-02-29']]) assert.equal(journeyDateRangeError(from, to), null)
  for (const [from, to] of [['2026-02-30', ''], ['', '2026-02-29'], ['not-a-date', ''], ['2026-1-02', '']]) assert.ok(journeyDateRangeError(from, to), `${from} / ${to} must be rejected`)
  assert.match(journeyDateRangeError('2026-10-20', '2026-10-19'), /From date must be on or before To date/i)
})

test('bookmarked journey dates reach the API and changing either bound creates a distinct cached query', async t => {
  const view = await mountJourneys(t, { entries: ['/journeys?page=2&sort=duration_desc&include_parked=true&date_from=2026-10-01&date_to=2026-10-19'] })
  assert.equal(view.input('From date').props.value, '2026-10-01')
  assert.equal(view.input('To date').props.value, '2026-10-19')
  const first = view.lists.at(-1).url.searchParams
  assert.equal(first.get('date_from'), '2026-10-01')
  assert.equal(first.get('date_to'), '2026-10-19')
  assert.equal(first.get('sort'), 'duration_desc')
  assert.equal(first.get('include_parked'), 'true')
  assert.equal(first.get('page'), '2')
  await view.selectAll()
  assert.ok(view.button('Merge 2 journeys'))
  await view.change('From date', '2026-10-02')
  assert.equal(view.params.has('page'), false)
  assert.equal(view.params.get('sort'), 'duration_desc')
  assert.equal(view.params.get('include_parked'), 'true')
  assert.equal(view.params.get('date_to'), '2026-10-19')
  assert.equal(view.lists.at(-1).url.searchParams.get('date_from'), '2026-10-02')
  assert.equal(view.lists.at(-1).url.searchParams.get('page'), '1')
  assert.ok(view.selections.every(input => !input.props.checked))
  assert.equal(view.button('Merge 2 journeys'), undefined)
  await view.change('To date', '2026-10-18')
  const keys = view.client.getQueryCache().getAll().filter(query => query.queryKey[0] === 'journeys').map(query => JSON.stringify(query.queryKey))
  assert.ok(keys.some(key => key.includes('2026-10-01') && key.includes('2026-10-19')))
  assert.ok(keys.some(key => key.includes('2026-10-02') && key.includes('2026-10-19')))
  assert.ok(keys.some(key => key.includes('2026-10-02') && key.includes('2026-10-18')))
})

test('changing journey page, sort or parked visibility clears merge selection', async t => {
  const view = await mountJourneys(t)
  await view.selectAll()
  assert.ok(view.button('Merge 2 journeys'))
  await act(async () => view.button('Next').props.onClick())
  await settle()
  assert.equal(view.params.get('page'), '2')
  assert.equal(view.button('Merge 2 journeys'), undefined)
  assert.ok(view.selections.every(input => !input.props.checked))
  await view.selectAll()
  await view.change('Sort journeys', 'distance_desc')
  assert.equal(view.params.has('page'), false)
  assert.equal(view.button('Merge 2 journeys'), undefined)
  assert.ok(view.selections.every(input => !input.props.checked))
  await view.selectAll()
  const parked = view.root.root.findAllByType('input').find(input => input.props.type === 'checkbox' && !input.props['aria-label'])
  await act(async () => parked.props.onChange({ target: { checked: true } }))
  await settle()
  assert.equal(view.params.get('include_parked'), 'true')
  assert.equal(view.button('Merge 2 journeys'), undefined)
  assert.ok(view.selections.every(input => !input.props.checked))
})

test('empty journey date results explain the filter and can clear dates while preserving other filters', async t => {
  const view = await mountJourneys(t, { entries: ['/journeys?sort=distance_desc&include_parked=true&date_from=2026-10-01&date_to=2026-10-19'], empty: true })
  assert.match(JSON.stringify(view.root.toJSON()), /No journeys match these dates/)
  assert.doesNotMatch(JSON.stringify(view.root.toJSON()), /No journeys yet/)
  await act(async () => view.button('Clear dates').props.onClick())
  await settle()
  assert.equal(view.params.has('date_from'), false)
  assert.equal(view.params.has('date_to'), false)
  assert.equal(view.params.get('sort'), 'distance_desc')
  assert.equal(view.params.get('include_parked'), 'true')
  assert.equal(view.input('From date').props.value, '')
  assert.equal(view.input('To date').props.value, '')
  assert.match(JSON.stringify(view.root.toJSON()), /No journeys yet/)
})

test('invalid or reversed bookmarked journey dates show an alert without fetching the list', async t => {
  for (const search of ['date_from=not-a-date', 'date_to=2026-02-30', 'date_from=2026-10-20&date_to=2026-10-19']) {
    await t.test(search, async t => {
      const view = await mountJourneys(t, { entries: [`/journeys?${search}`] })
      assert.equal(view.lists.length, 0)
      const alerts = view.root.root.findAllByProps({ role: 'alert' })
      assert.ok(alerts.length > 0, 'invalid bookmarked dates need a readable alert')
      assert.match(JSON.stringify(view.root.toJSON()), /date/i)
      assert.doesNotMatch(JSON.stringify(view.root.toJSON()), /No journeys yet/)
    })
  }
})

test('malformed journey page and sort bookmarks fall back to safe API values', async t => {
  for (const page of ['0', '-1', '1.5', 'NaN', 'Infinity', '1000000001', '9007199254740992']) {
    await t.test(page, async t => {
      const view = await mountJourneys(t, { entries: [`/journeys?page=${page}&sort=unsupported`] })
      assert.equal(view.lists.at(-1).url.searchParams.get('page'), '1')
      assert.equal(view.lists.at(-1).url.searchParams.get('sort'), 'started_desc')
      assert.equal(view.input('Sort journeys').props.value, 'started_desc')
    })
  }
})

test('editing a loaded date range into an invalid range hides old results and does not fetch it', async t => {
  const view = await mountJourneys(t, { entries: ['/journeys?date_from=2026-10-01&date_to=2026-10-19'] })
  await view.selectAll()
  const before = view.lists.length
  await view.change('From date', '2026-10-20')
  assert.equal(view.lists.length, before)
  assert.match(JSON.stringify(view.root.toJSON()), /From date must be on or before To date/)
  assert.equal(view.selections.length, 0, 'cached journeys must not appear under an invalid range')
  assert.equal(view.button('Merge 2 journeys'), undefined)
  assert.equal(view.input('From date').props['aria-invalid'], true)
  assert.equal(view.input('To date').props['aria-invalid'], true)
})

test('browser back restores journey date inputs, API bounds and an empty merge selection', async t => {
  const view = await mountJourneys(t, { entries: ['/journeys?date_from=2026-10-01&date_to=2026-10-10', '/journeys?date_from=2026-10-11&date_to=2026-10-19'] })
  await view.selectAll()
  await view.back()
  assert.equal(view.input('From date').props.value, '2026-10-01')
  assert.equal(view.input('To date').props.value, '2026-10-10')
  assert.equal(view.lists.at(-1).url.searchParams.get('date_from'), '2026-10-01')
  assert.equal(view.lists.at(-1).url.searchParams.get('date_to'), '2026-10-10')
  assert.equal(view.button('Merge 2 journeys'), undefined)
  assert.ok(view.selections.every(input => !input.props.checked))
})

test('date shortcuts apply a complete range, reset the page, and all dates clears both bounds', async t => {
  const view = await mountJourneys(t, { entries: ['/journeys?page=2&sort=started_asc'] })
  for (const [label, days] of [['Today', 1], ['Yesterday', 1], ['Last 7 days', 7]]) {
    const button = view.button(label)
    assert.ok(button)
    assert.equal(Boolean(button.props.disabled), false)
    await act(async () => button.props.onClick())
    await settle()
    assert.equal(view.params.has('page'), false)
    assert.equal(view.params.get('sort'), 'started_asc')
    const from = view.params.get('date_from')
    const to = view.params.get('date_to')
    assert.match(from, /^\d{4}-\d{2}-\d{2}$/)
    assert.equal(Date.parse(to) - Date.parse(from), (days - 1) * 86_400_000)
    assert.equal(view.lists.at(-1).url.searchParams.get('date_from'), from)
    assert.equal(view.lists.at(-1).url.searchParams.get('date_to'), to)
  }
  await act(async () => view.button('All dates').props.onClick())
  await settle()
  assert.equal(view.params.has('date_from'), false)
  assert.equal(view.params.has('date_to'), false)
  assert.equal(view.lists.at(-1).url.searchParams.has('date_from'), false)
  assert.equal(view.lists.at(-1).url.searchParams.has('date_to'), false)
})

test('camera-relative shortcuts wait for a timezone while manual date filters remain usable', async t => {
  const view = await mountJourneys(t, { timezone: null })
  for (const label of ['Today', 'Yesterday', 'Last 7 days']) assert.equal(view.button(label).props.disabled, true)
  assert.equal(Boolean(view.input('From date').props.disabled), false)
  assert.equal(Boolean(view.input('To date').props.disabled), false)
  await view.change('From date', '2026-10-01')
  assert.equal(view.lists.at(-1).url.searchParams.get('date_from'), '2026-10-01')
})
