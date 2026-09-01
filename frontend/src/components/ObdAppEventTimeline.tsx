import { Link } from 'react-router-dom'

import { cn } from '@/lib/cn'
import { formatBytes, formatDateTime, formatDuration, formatRelative } from '@/lib/format'
import type { OBDLoggerEvent } from '@/lib/api'

const KIND_LABELS: Record<string, string> = {
  'app.boot': 'App boot',
  'app.service': 'Logger service',
  'network.wifi': 'Wi-Fi',
  'power.sleep_window': 'Sleep window',
  'obd.ble_connection': 'Bluetooth',
  'obd.elm_session': 'ELM session',
  'obd.ecu_session': 'ECU session',
  'obd.poll_health': 'Polling',
  'drive.lifecycle': 'Drive',
  'ingest.handoff': 'Backup handoff',
  'radio.observation': 'Radio state',
  'bundle.export': 'Drive export',
  'receipt.verification': 'Server receipt',
}

const humanize = (value: string) =>
  value
    .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
    .replaceAll('_', ' ')
    .replaceAll('.', ' · ')
    .toLowerCase()

function metricValue(key: string, value: number): string {
  if (key === 'bundleBytes') return formatBytes(value)
  if (key.endsWith('Ms')) return formatDuration(value / 1000)
  if (key.endsWith('S')) return formatDuration(value)
  if (key.endsWith('Percent')) return `${value.toFixed(1)}%`
  if (key === 'wifiFrequencyMhz') return `${Math.round(value)} MHz`
  return value.toLocaleString()
}

function eventTone(level: OBDLoggerEvent['level']): string {
  if (level === 'error') return 'border-state-error/50 bg-state-error/5'
  if (level === 'warning') return 'border-state-warn/50 bg-state-warn/5'
  return 'border-border/70 bg-surface-sunken/35'
}

export function ObdAppEventTimeline({
  events,
  emptyMessage = 'No app-owned lifecycle events have reached the server yet.',
  linkDrives = false,
}: {
  events: OBDLoggerEvent[]
  emptyMessage?: string
  linkDrives?: boolean
}) {
  if (events.length === 0) {
    return (
      <div className="rounded-xl border border-dashed border-border px-4 py-8 text-center text-sm text-content-muted">
        {emptyMessage}
      </div>
    )
  }

  return (
    <ol className="space-y-2" aria-label="OBD app activity">
      {events.map((event) => {
        const metrics = Object.entries(event.metrics)
        return (
          <li
            key={`${event.buildGitSha}-${event.sequence}-${event.occurredAt}`}
            className={cn('rounded-xl border px-4 py-3', eventTone(event.level))}
          >
            <div className="flex flex-wrap items-start justify-between gap-2">
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-medium">{KIND_LABELS[event.kind] ?? humanize(event.kind)}</span>
                  <span className="badge bg-surface-sunken text-content-muted">
                    {humanize(event.outcome)}
                  </span>
                  {event.level !== 'info' && (
                    <span
                      className={cn(
                        'badge',
                        event.level === 'error'
                          ? 'bg-state-error/15 text-state-error'
                          : 'bg-state-warn/15 text-state-warn',
                      )}
                    >
                      {event.level}
                    </span>
                  )}
                </div>
                {event.reasonCode && (
                  <div className="mt-1 text-sm text-content-muted">
                    {humanize(event.reasonCode)}
                  </div>
                )}
              </div>
              <time
                className="shrink-0 text-xs tabular text-content-faint"
                dateTime={event.occurredAt}
                title={formatDateTime(event.occurredAt)}
              >
                {formatRelative(event.occurredAt)}
              </time>
            </div>

            {(event.driveId || metrics.length > 0) && (
              <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-content-faint">
                {event.driveId && linkDrives && (
                  <Link className="font-medium text-accent hover:underline" to={`/obd/${event.driveId}`}>
                    View drive
                  </Link>
                )}
                {metrics.map(([key, value]) => (
                  <span key={key} className="tabular">
                    {humanize(key)} {metricValue(key, value)}
                  </span>
                ))}
              </div>
            )}

            <div className="mt-2 text-[11px] text-content-faint">
              App {event.appVersionName} · received {formatRelative(event.receivedAt)}
            </div>
          </li>
        )
      })}
    </ol>
  )
}
