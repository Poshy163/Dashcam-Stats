import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useSearchParams } from 'react-router-dom'

import Spinner from '@/components/Spinner'
import { EmptyState, ErrorState, Pagination } from '@/components/ui'
import { api } from '@/lib/api'
import { cn } from '@/lib/cn'
import { formatDateTime } from '@/lib/format'

/**
 * Android log levels as the unit emits them. The capture keeps error and fatal by
 * default, so W and below only appear for a tag the operator has deliberately raised.
 */
const LEVELS: Array<{ value: string; label: string }> = [
  { value: '', label: 'All levels' },
  { value: 'F', label: 'Fatal' },
  { value: 'E', label: 'Error' },
  { value: 'W', label: 'Warning' },
  { value: 'I', label: 'Info' },
]

const LEVEL_STYLE: Record<string, string> = {
  F: 'text-state-error',
  E: 'text-state-error',
  W: 'text-state-warn',
  I: 'text-content-muted',
  D: 'text-content-faint',
  V: 'text-content-faint',
}

const LEVEL_LABEL: Record<string, string> = {
  F: 'FATAL',
  E: 'ERROR',
  W: 'WARN',
  I: 'INFO',
  D: 'DEBUG',
  V: 'VERBOSE',
}

/**
 * The head unit's own system log — the built-in recorder, the platform services and the
 * kernel. Separate from this server's log because they fail independently and for
 * different reasons: a gap in the footage is usually explained here, not there.
 */
export default function UnitLogView({ live }: { live: boolean }) {
  const [params, setParams] = useSearchParams()
  const page = Number(params.get('page') ?? 1)
  const level = params.get('level') ?? ''
  const tag = params.get('tag') ?? ''
  const search = params.get('search') ?? ''

  const [draft, setDraft] = useState(search)
  const [lastSearch, setLastSearch] = useState(search)
  if (search !== lastSearch) {
    setLastSearch(search)
    setDraft(search)
  }

  const query = useQuery({
    queryKey: ['unit-logs', page, level, tag, search],
    queryFn: () =>
      api.unitLogs.list({
        page,
        pageSize: 100,
        level: level || undefined,
        tag: tag || undefined,
        search: search || undefined,
      }),
    refetchInterval: live ? 5_000 : false,
  })

  // Counts come from the server so the filter offers the tags that actually exist on
  // this firmware rather than a list guessed at build time.
  const tags = useQuery({ queryKey: ['unit-log-tags'], queryFn: () => api.unitLogs.tags() })

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
      <div className="card flex flex-wrap items-end gap-3 p-3">
        <label>
          <span className="label mb-1 block text-xs">Level</span>
          <select
            className="input w-auto"
            value={level}
            onChange={(e) => update('level', e.target.value)}
          >
            {LEVELS.map((l) => (
              <option key={l.value} value={l.value}>
                {l.label}
              </option>
            ))}
          </select>
        </label>
        <label>
          <span className="label mb-1 block text-xs">Tag</span>
          <select className="input w-auto" value={tag} onChange={(e) => update('tag', e.target.value)}>
            <option value="">All tags</option>
            {(tags.data ?? []).map((t) => (
              <option key={t.tag} value={t.tag}>
                {t.tag} ({t.count})
              </option>
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
      {query.data?.items.length === 0 && (
        <EmptyState
          title="Nothing collected from the head unit yet"
          description="The unit ships with Android logging switched off. It is turned back on and a filtered capture is started the next time the car is seen; whatever it recorded arrives on the following visit."
        />
      )}

      {query.data && query.data.items.length > 0 && (
        <div className="card divide-y divide-border overflow-hidden">
          {query.data.items.map((entry) => (
            <div key={entry.id} className="px-3 py-1.5 text-sm hover:bg-surface-sunken">
              <div className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5">
                <span className="tabular shrink-0 text-xs text-content-faint">
                  {formatDateTime(entry.occurredAt)}
                </span>
                <span
                  className={cn(
                    'shrink-0 text-2xs font-semibold uppercase',
                    LEVEL_STYLE[entry.level] ?? '',
                  )}
                >
                  {LEVEL_LABEL[entry.level] ?? entry.level}
                </span>
                <button
                  className="shrink-0 text-xs text-content-faint hover:text-accent"
                  title="Filter to this tag"
                  onClick={() => update('tag', entry.tag)}
                >
                  {entry.tag}
                </button>
                <span className="min-w-0 flex-1 break-words">{entry.message}</span>
                <span className="tabular shrink-0 text-2xs text-content-faint">
                  {entry.pid}/{entry.tid}
                </span>
              </div>
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
    </div>
  )
}
