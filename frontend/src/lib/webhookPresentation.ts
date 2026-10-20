import type { OBDLoggerEvent } from '@/lib/api'

type WebhookPresentation = {
  title: string
  detail: string
  outcome: string
  tone: 'ok' | 'warn' | 'muted'
}

const DASHCAM_SETTINGS = 'On the dashcam, open OBD Logger, update the webhook settings, then choose Save and start.'

/** Interpret the result of one historical attempt, never the current saved configuration. */
export function webhookEventPresentation(event: OBDLoggerEvent): WebhookPresentation {
  const retrying = event.outcome === 'retrying'
  const outcome = retrying ? 'Retry scheduled' : event.outcome === 'dispatched' ? 'Accepted' : 'Failed'
  const base = { outcome, tone: retrying ? 'muted' as const : 'warn' as const }
  const retryAdvice = retrying
    ? 'The app scheduled an automatic retry for this attempt.'
    : 'Automatic retries ended for this notification. Check the dashcam Wi-Fi and server before the next ignition-off notification.'

  switch (event.reasonCode) {
    case 'webhook_ok':
      return {
        title: 'Server accepted the notification', outcome: 'Accepted', tone: 'ok',
        detail: 'This confirms the notification reached the server. Check backup progress separately to confirm footage was copied.',
      }
    case 'webhook_api_key_required':
      return { ...base, title: 'Webhook API key was missing', detail: `Enter the server webhook API key in the dashcam app. ${DASHCAM_SETTINGS}` }
    case 'webhook_credential_unavailable':
      return { ...base, title: 'Saved webhook key could not be read', detail: `Re-enter the server webhook API key in the dashcam app. ${DASHCAM_SETTINGS}` }
    case 'webhook_auth_rejected':
      return { ...base, title: 'Server rejected the webhook key', detail: `Check that the dashcam uses this server's current API key. ${DASHCAM_SETTINGS} Authentication failures are not retried automatically.` }
    case 'webhook_api_key_too_long':
      return { ...base, title: 'Webhook API key was too long', detail: `Check that only the API key was pasted into the key field. ${DASHCAM_SETTINGS}` }
    case 'invalid_webhook_url':
      return { ...base, title: 'Webhook URL was invalid', detail: `Use the server /api/ingest/webhook endpoint over HTTPS, or HTTP with a private LAN IP address. ${DASHCAM_SETTINGS}` }
    case 'webhook_timeout':
      return { ...base, title: retrying ? 'Server reply timed out; retry scheduled' : 'Server reply timed out', detail: retryAdvice }
    case 'webhook_network_error':
      return { ...base, title: retrying ? 'Network request failed; retry scheduled' : 'Network request failed', detail: retryAdvice }
    case 'webhook_server_error':
      return { ...base, title: retrying ? 'Server error; retry scheduled' : 'Server returned an error', detail: retryAdvice }
    case 'webhook_client_error':
      return { ...base, title: 'Server rejected the notification', detail: `Check the webhook endpoint and server logs. ${DASHCAM_SETTINGS} This request was not retried automatically.` }
    case 'webhook_unexpected_response':
      return { ...base, title: 'Unexpected server response', detail: `Check that the URL points directly to /api/ingest/webhook; redirects are not followed. ${DASHCAM_SETTINGS}` }
    case 'webhook_security_error':
      return { ...base, title: 'Dashcam blocked the webhook request', detail: `Check the dashcam app's network permissions and webhook URL. ${DASHCAM_SETTINGS}` }
    default:
      return { ...base, title: 'Backup notification failed', detail: `Review the dashcam app and server logs, then check the webhook settings. ${DASHCAM_SETTINGS}` }
  }
}
