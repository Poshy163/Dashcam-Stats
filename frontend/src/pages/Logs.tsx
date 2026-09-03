import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link, useSearchParams } from 'react-router-dom'

import CarPlayTimingView from '@/components/CarPlayTimingView'
import Spinner from '@/components/Spinner'
import UnitLogView from '@/components/UnitLogView'
import { EmptyState, ErrorState, PageHeader, Pagination } from '@/components/ui'
import { api } from '@/lib/api'
import { cn } from '@/lib/cn'
import { formatDateTime } from '@/lib/format'

const LEVELS = ['', 'DEBUG', 'INFO', 'WARNING', 'ERROR']

const LEVEL_STYLE: Record<string, string> = {
  DEBUG: 'text-content-faint',
  INFO: 'text-content-muted',
  WARNING: 'text-state-warn',
  ERROR: 'text-state-error',
  CRITICAL: 'text-state-error',
}

export default function Logs() {
  const [params, setParams] = useSearchParams()
  const [live, setLive] = useState(false)
  const [expanded, setExpanded] = useState<number | null>(null)

  const rawSource = params.get('source')
  const source = rawSource === 'unit' || rawSource === 'carplay' ? rawSource : 'server'
  const page = Number(params.get('page') ?? 1)
  const level = params.get('level') ?? ''
  const search = params.get('search') ?? ''
  // Follows the URL as well as leading it — see the note in Recordings.tsx. Back, forward
  // and an inbound filtered link all change the filter without touching this box.
  const [draft, setDraft] = useState(search)
  const [lastSearch, setLastSearch] = useState(search)
  if (search !== lastSearch) {
    setLastSearch(search)
    setDraft(search)
  }

  const setSource = (value: string) => {
    const next = new URLSearchParams(params)
    if (value === 'unit' || value === 'carplay') next.set('source', value)
    else next.delete('source')
    // Level and tag vocabularies differ between the two sources (WARNING vs W, logger
    // vs tag), so carrying a filter across would silently return nothing.
    next.delete('page')
    next.delete('level')
    next.delete('tag')
    setParams(next)
  }

  const query = useQuery({
    queryKey: ['logs', page, level, search],
    queryFn: () =>
      api.logs.list({
        page,
        pageSize: 100,
        level: level || undefined,
        search: search || undefined,
      }),
    // Don't fetch the server log while the unit log is on screen.
    enabled: source === 'server',
    refetchInterval: live && source === 'server' ? 5_000 : false,
  })

  /**
   * Changing a *filter* resets to page 1, which is right — the old page number means
   * nothing against a different result set. Changing the page must not go through here:
   * it would set `page` and then delete it on the next line, leaving the URL untouched
   * and the Next button apparently inert. Use `setPage` for that.
   */
  const update = (key: string, value: string) => {
    const next = new URLSearchParams(params)
    if (value) next.set(key, value)
    else next.delete(key)
    next.delete('page')
    setParams(next)
  }

  const setPage = (value: number) => {
    const next = new URLSearchParams(params)
    next.set('page', String(value))
    setParams(next)
  }

  return (
    <div className="space-y-4">
      <PageHeader
        title="Logs"
        subtitle={
          source === 'unit'
            ? "The head unit's own system log: the built-in recorder, the platform and the kernel."
            : 'Application and processing events. Frame-level detail is deliberately not logged.'
        }
        actions={
          <div className="flex items-center gap-4">
            <div className="flex items-center gap-1 rounded-md bg-surface-sunken p-0.5 text-sm">
              {[
                { value: 'server', label: 'Server' },
                { value: 'unit', label: 'Head unit' },
                { value: 'carplay', label: 'CarPlay timing' },
              ].map((option) => (
                <button
                  key={option.value}
                  onClick={() => setSource(option.value)}
                  className={cn(
                    'rounded px-2 py-1',
                    source === option.value
                      ? 'bg-surface text-content shadow-sm'
                      : 'text-content-muted hover:text-content',
                  )}
                >
                  {option.label}
                </button>
              ))}
            </div>
            <label className="flex items-center gap-2 text-sm">
              <input type="checkbox" checked={live} onChange={(e) => setLive(e.target.checked)} />
              Auto-refresh
            </label>
          </div>
        }
      />

      {source === 'unit' && <UnitLogView live={live} />}
      {source === 'carplay' && <CarPlayTimingView live={live} />}

      {source === 'server' && (
        <>
      <div className="card flex flex-wrap items-end gap-3 p-3">
        <label>
          <span className="label mb-1 block text-xs">Level</span>
          <select className="input w-auto" value={level} onChange={(e) => update('level', e.target.value)}>
            {LEVELS.map((l) => (
              <option key={l} value={l}>{l || 'All levels'}</option>
            ))}
          </select>
        </label>
        <label className="min-w-[14rem] flex-1">
          <span className="label mb-1 block text-xs">Message contains</span>
          <input
            className="input"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && update('search', e.currentTarget.value)}
            onBlur={(e) => update('search', e.target.value)}
          />
        </label>
      </div>

      {query.isLoading && <Spinner className="py-16" />}
      {query.isError && <ErrorState error={query.error} retry={() => query.refetch()} />}
      {query.data?.items.length === 0 && <EmptyState title="No log entries match" />}

      {query.data && query.data.items.length > 0 && (
        <div className="card divide-y divide-border overflow-hidden">
          {query.data.items.map((entry) => (
            <div key={entry.id} className="px-3 py-1.5 text-sm hover:bg-surface-sunken">
              <div className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5">
                <span className="tabular shrink-0 text-xs text-content-faint">
                  {formatDateTime(entry.ts)}
                </span>
                <span className={cn('shrink-0 text-2xs font-semibold uppercase', LEVEL_STYLE[entry.level] ?? '')}>
                  {entry.level}
                </span>
                <span className="shrink-0 text-xs text-content-faint">{entry.logger}</span>
                <span className="min-w-0 flex-1">{entry.message}</span>
                {entry.recordingId && (
                  <Link to={`/recordings/${entry.recordingId}`} className="shrink-0 text-xs text-accent">
                    recording
                  </Link>
                )}
                {entry.context && Object.keys(entry.context).length > 0 && (
                  <button
                    className="shrink-0 text-xs text-content-faint hover:text-content"
                    onClick={() => setExpanded(expanded === entry.id ? null : entry.id)}
                  >
                    {expanded === entry.id ? 'hide' : 'context'}
                  </button>
                )}
              </div>
              {expanded === entry.id && entry.context && (
                <pre className="mt-1 overflow-x-auto rounded bg-surface-sunken p-2 text-2xs text-content-muted">
                  {JSON.stringify(entry.context, null, 2)}
                </pre>
              )}
            </div>
          ))}
        </div>
      )}

      {query.data && (
        <Pagination
          page={query.data.page}
          pages={query.data.pages}
          total={query.data.total}
          onChange={setPage}
        />
      )}
        </>
      )}
    </div>
  )
}
