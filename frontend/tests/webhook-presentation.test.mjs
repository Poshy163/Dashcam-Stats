import assert from 'node:assert/strict'
import { after, test } from 'node:test'
import { mkdtemp, rm } from 'node:fs/promises'
import { fileURLToPath, pathToFileURL } from 'node:url'
import path from 'node:path'
import { build } from 'esbuild'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

const directory = await mkdtemp(fileURLToPath(new URL('../node_modules/.webhook-test-', import.meta.url)))
after(() => rm(directory, { recursive: true, force: true }))
await build({
  stdin: {
    contents: `export { ObdAppEventTimeline } from '@/components/ObdAppEventTimeline'; export { ObdWebhookStatus } from '@/components/ObdWebhookStatus';`,
    resolveDir: fileURLToPath(new URL('..', import.meta.url)), loader: 'tsx',
  },
  outfile: path.join(directory, 'webhook.mjs'), bundle: true, format: 'esm', platform: 'node', jsx: 'automatic',
  external: ['react', 'react/*', 'react-router-dom'], alias: { '@': fileURLToPath(new URL('../src', import.meta.url)) },
})
const { ObdAppEventTimeline, ObdWebhookStatus } = await import(pathToFileURL(path.join(directory, 'webhook.mjs')).href)
const event = (reasonCode, outcome = 'failed') => ({
  sequence: 42, occurredAt: '2026-10-01T00:00:00Z', receivedAt: '2026-10-02T00:00:00Z',
  kind: 'ingest.webhook', level: outcome === 'failed' ? 'warning' : 'info', outcome, reasonCode,
  driveId: null, metrics: {}, appVersionName: '0.3.7', appVersionCode: 20, buildGitSha: '8a23d7b',
})
const timeline = item => renderToStaticMarkup(React.createElement(ObdAppEventTimeline, { events: [item] }))
const status = props => renderToStaticMarkup(React.createElement(ObdWebhookStatus, props))

test('missing, unreadable and rejected credentials explain the dashcam action without exposing credentials', () => {
  for (const [reason, expected] of [
    ['webhook_api_key_required', /key was missing/],
    ['webhook_credential_unavailable', /key could not be read/],
    ['webhook_auth_rejected', /Server rejected the webhook key/],
  ]) {
    const html = timeline(event(reason))
    assert.match(html, /Backup notification/)
    assert.match(html, expected)
    assert.match(html, /On the dashcam, open OBD Logger/)
    assert.match(html, /Save and start/)
    assert.doesNotMatch(html, /webhook_api_key|required|webhook_credential/)
  }
  const unknown = timeline(event('unexpected-private-value'))
  assert.doesNotMatch(unknown, /unexpected-private-value/)
})

test('transient retry and exhausted failure give different recovery guidance', () => {
  for (const reason of ['webhook_timeout', 'webhook_network_error', 'webhook_server_error']) {
    const retrying = timeline(event(reason, 'retrying'))
    assert.match(retrying, /Retry scheduled/)
    assert.match(retrying, /app scheduled an automatic retry/)
    assert.doesNotMatch(retrying, /Automatic retries ended/)
    const failed = timeline(event(reason))
    assert.match(failed, /Automatic retries ended/)
    assert.match(failed, /Check the dashcam Wi-Fi and server/)
  }
  const rejected = timeline(event('webhook_auth_rejected'))
  assert.match(rejected, /not retried automatically/)
  assert.doesNotMatch(rejected, /Retry scheduled/)
})

test('accepted notification confirms delivery and does not imply a completed footage backup', () => {
  const html = status({ event: event('webhook_ok', 'dispatched') })
  assert.match(html, /Server accepted the notification/)
  assert.match(html, /Check backup progress separately to confirm footage was copied/)
  assert.match(html, /Last reported/)
  assert.match(html, /2026-10-01T00:00:00Z/)
  assert.match(html, /Historical result; settings may have changed/)
})

test('absent, loading and unavailable history never diagnose a missing credential', () => {
  const empty = status({ event: null })
  assert.match(empty, /Webhook configuration is unknown here/)
  assert.doesNotMatch(empty, /key was missing|not configured|rejected/)
  const loading = status({ loading: true })
  assert.match(loading, /Loading backup notification history/)
  assert.doesNotMatch(loading, /No backup notification has been received/)
  const failed = status({ error: true })
  assert.match(failed, /Could not load backup notification history/)
  assert.doesNotMatch(failed, /No backup notification has been received/)
})

test('a refresh error retains the observed result with explicit historical scope', () => {
  const html = status({ error: true, event: event('webhook_api_key_required'), checkedAt: '2026-10-20T00:00:00Z' })
  assert.match(html, /key was missing/)
  assert.match(html, /settings may have changed since this attempt/)
  assert.match(html, /History refresh failed. Showing the last known result/)
  assert.doesNotMatch(html, /History checked/)
})
