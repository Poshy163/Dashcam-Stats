import type { OBDLoggerEvent } from '@/lib/api'
import { cn } from '@/lib/cn'
import { formatDateTime, formatRelative } from '@/lib/format'
import { webhookEventPresentation } from '@/lib/webhookPresentation'

/** Pass the newest result from a kind-filtered query, not a page of mixed app activity. */
export function ObdWebhookStatus({ event, loading = false, error = false, checkedAt }: {
  event?: OBDLoggerEvent | null
  loading?: boolean
  error?: boolean
  checkedAt?: string | null
}) {
  const result = event ? webhookEventPresentation(event) : null
  return (
    <section className="card mb-6 px-5 py-4" aria-label="Backup notification history">
      <h3 className="text-sm font-medium">Last reported backup notification</h3>
      {result && event ? (
        <>
          <div className={cn('mt-1 text-sm font-medium', result.tone === 'ok' ? 'text-state-ok' : result.tone === 'warn' ? 'text-state-warn' : 'text-content-muted')}>
            {result.title}
          </div>
          <p className="mt-1 text-sm text-content-muted">{result.detail}</p>
          <div className="mt-2 text-xs text-content-faint">
            Last reported <time dateTime={event.occurredAt} title={formatDateTime(event.occurredAt)}>{formatRelative(event.occurredAt)}</time>
            {' · '}received {formatRelative(event.receivedAt)}
          </div>
          <p className="mt-1 text-xs text-content-faint">Historical result; settings may have changed since this attempt.</p>
        </>
      ) : (
        <p className="mt-1 text-sm text-content-muted">
          {loading ? 'Loading backup notification history…' : error ? 'Could not load backup notification history.' : 'No backup notification has been received by the server yet. Webhook configuration is unknown here.'}
        </p>
      )}
      {error && event && <p className="mt-2 text-xs text-state-warn">History refresh failed. Showing the last known result.</p>}
      {!loading && !error && checkedAt && <div className="mt-1 text-xs text-content-faint">History checked {formatRelative(checkedAt)}</div>}
    </section>
  )
}
