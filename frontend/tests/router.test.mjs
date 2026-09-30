import assert from 'node:assert/strict'
import { after, test } from 'node:test'
import { mkdtemp, rm } from 'node:fs/promises'
import { fileURLToPath, pathToFileURL } from 'node:url'
import path from 'node:path'
import { build } from 'esbuild'
import React from 'react'
import { act, create } from 'react-test-renderer'
import { MemoryRouter, useLocation, useNavigate } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

// Exercise the real App route/auth composition against the installed Router. Leaf pages
// are replaced so navigation tests never need media, browser maps or a running API.
const directory = await mkdtemp(fileURLToPath(new URL('../node_modules/.router-test-', import.meta.url)))
after(() => rm(directory, { recursive: true, force: true }))
await build({
  entryPoints: [fileURLToPath(new URL('../src/App.tsx', import.meta.url))],
  outfile: path.join(directory, 'app.mjs'),
  bundle: true,
  format: 'esm',
  platform: 'node',
  external: ['react', 'react/*', 'react-router-dom', '@tanstack/react-query'],
  alias: { '@': fileURLToPath(new URL('../src', import.meta.url)) },
  jsx: 'automatic',
  plugins: [{
    name: 'route-leaves',
    setup(builder) {
      builder.onResolve({ filter: /^@\/(pages\/|components\/Layout$|lib\/(api|useTheme)$)/ }, ({ path: module }) => ({ path: module, namespace: 'route-test' }))
      builder.onLoad({ filter: /.*/, namespace: 'route-test' }, ({ path: module }) => {
        if (module === '@/lib/api') return { contents: 'export const api = globalThis.__routerTest.api; export function setUnauthorizedHandler() {}' }
        if (module === '@/lib/useTheme') return { contents: 'export const useTheme = () => ({ theme: "dark", toggleTheme() {} });' }
        if (module === '@/components/Layout') return { contents: 'export default function Layout({ children }) { return children; }' }
        if (module === '@/pages/Login') return { contents: 'export default function Login(props) { globalThis.__routerTest.login = props.onSignedIn; return "Sign in"; }' }
        return {
          contents: `import React from 'react'; import { useParams } from 'react-router-dom'; export default function Page() { return React.createElement('output', { 'data-page': ${JSON.stringify(module.split('/').at(-1))} }, JSON.stringify(useParams())); }`,
        }
      })
    },
  }],
})

const state = { authenticated: true, required: true, statusCalls: 0 }
globalThis.__routerTest = {
  api: {
    auth: { state: async () => ({ required: state.required, authenticated: state.authenticated }) },
    status: async () => { state.statusCalls++; return { timezone: 'Australia/Adelaide' } },
  },
}
const { default: App } = await import(pathToFileURL(path.join(directory, 'app.mjs')).href)
const oldDocument = globalThis.document
const oldStorage = globalThis.sessionStorage
globalThis.document = { title: '' }
globalThis.sessionStorage = { getItem: () => null, setItem() {}, removeItem() {} }
after(() => {
  if (oldDocument === undefined) delete globalThis.document
  else globalThis.document = oldDocument
  if (oldStorage === undefined) delete globalThis.sessionStorage
  else globalThis.sessionStorage = oldStorage
  delete globalThis.__routerTest
})

async function settle() {
  // TanStack Query schedules observer notifications on the next task.
  for (let n = 0; n < 4; n++) await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
}

async function mount(t, initialPath) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } })
  const navigation = {}
  function Observer() {
    navigation.location = useLocation()
    navigation.navigate = useNavigate()
    return null
  }
  let root
  await act(async () => {
    root = create(React.createElement(QueryClientProvider, { client },
      React.createElement(MemoryRouter, { initialEntries: [initialPath] },
        React.createElement(Observer), React.createElement(App))))
  })
  t.after(async () => { await act(async () => root.unmount()); client.clear() })
  await settle()
  return { root, client, navigation }
}

function page(root) {
  return root.root.findByType('output').props['data-page']
}

test('sign-in preserves the requested recording and seek query without pre-auth status reads', async t => {
  state.authenticated = false
  state.statusCalls = 0
  const { root, navigation } = await mount(t, '/recordings/27?t=42&camera=rear')
  assert.equal(root.toJSON(), 'Sign in')
  assert.equal(state.statusCalls, 0)
  state.authenticated = true
  await act(async () => { await globalThis.__routerTest.login() })
  await settle()
  assert.equal(page(root), 'RecordingViewer')
  assert.equal(root.root.findByType('output').children.join(''), '{"id":"27"}')
  assert.equal(navigation.location.pathname + navigation.location.search, '/recordings/27?t=42&camera=rear')
  assert.equal(document.title, 'Recording · Dashcam Analyser')
  assert.ok(state.statusCalls > 0)
})

test('query navigation and browser history preserve bookmarked plate filters', async t => {
  const { root, navigation } = await mount(t, '/plates?page=2&q=AUD')
  assert.equal(page(root), 'Plates')
  await act(async () => navigation.navigate('/plates?page=3&q=AUD'))
  assert.equal(navigation.location.search, '?page=3&q=AUD')
  await act(async () => navigation.navigate(-1))
  assert.equal(navigation.location.search, '?page=2&q=AUD')
  await act(async () => navigation.navigate(1))
  assert.equal(navigation.location.search, '?page=3&q=AUD')
})

test('deep links, route parameters, aliases and unknown pages survive the router migration', async t => {
  const { root, navigation } = await mount(t, '/journeys/5171?camera=rear&t=75')
  assert.equal(page(root), 'JourneyDetail')
  for (const [url, expectedPage, params] of [
    ['/plates/72', 'PlateDetail', { id: '72' }],
    ['/obd/audit%20drive', 'ObdDriveDetail', { driveId: 'audit drive' }],
    ['/search?q=AUD%20001', 'Search', {}],
    ['/not-a-route', 'NotFound', { '*': 'not-a-route' }],
    ['/index.html', 'Dashboard', {}],
    ['/login', 'Dashboard', {}],
  ]) {
    await act(async () => navigation.navigate(url))
    await settle()
    assert.equal(page(root), expectedPage, url)
    assert.deepEqual(JSON.parse(root.root.findByType('output').children.join('')), params)
  }
  assert.equal(navigation.location.pathname, '/')
})
