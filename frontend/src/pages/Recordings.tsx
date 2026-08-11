import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link, useSearchParams } from 'react-router-dom'

import Spinner from '@/components/Spinner'
import { EmptyState, ErrorState, PageHeader, Pagination, StateBadge } from '@/components/ui'
import { api, mediaUrl, type RecordingFilters } from '@/lib/api'
import { cn } from '@/lib/cn'
import { formatBytes, formatDateTime, formatDuration } from '@/lib/format'
import type { Recording } from '@/lib/types'

const STATES = [
  ['', 'Any state'],
  ['completed', 'Completed'],
  ['processing', 'Processing'],
  ['queued', 'Queued'],
  ['discovered', 'Discovered'],
  ['failed', 'Failed'],
  // Its own filter because it is the answer to "why has nothing happened to these": they
  // are empty or carry no video stream, and no amount of waiting or retrying changes that.
  ['invalid', 'Unusable file'],
  ['settling', 'Still writing'],
] as const

export default function Recordings() {
  // Filters live in the URL so a filtered view can be bookmarked and shared.
  const [params, setParams] = useSearchParams()
  const [view, setView] = useState<'grid' | 'table'>(
    () => (localStorage.getItem('dashcam-recordings-view') as 'grid' | 'table') ?? 'grid',
  )

  const page = Number(params.get('page') ?? 1)
  const filters: RecordingFilters = {
    page,
    pageSize: 24,
    state: params.get('state') || undefined,
    search: params.get('search') || undefined,
    journeyId: params.get('journey_id') ? Number(params.get('journey_id')) : undefined,
    hasGps: params.get('has_gps') ? params.get('has_gps') === 'true' : undefined,
    hasDetections: params.get('has_detections') ? params.get('has_detections') === 'true' : undefined,
    dateFrom: params.get('date_from') || undefined,
    dateTo: params.get('date_to') || undefined,
  }

  const query = useQuery({
    queryKey: ['recordings', filters],
    queryFn: () => api.recordings.list(filters),
  })

  const update = (key: string, value: string) => {
    const next = new URLSearchParams(params)
    if (value) next.set(key, value)
    else next.delete(key)
    next.delete('page')
    setParams(next)
  }

  // The filename box is the one input that cannot be driven straight from the URL, because
  // it only commits on blur or Enter. It still has to follow the URL rather than only lead
  // it: Clear, the browser's Back button and an inbound filtered link all change the
  // filters without touching the box. Left uncontrolled, it kept showing "TRUCK" and a date
  // range over a list of all 678 recordings, while the three dropdowns beside it visibly
  // reset — the form claiming one thing and the results below it another.
  const urlSearch = params.get('search') ?? ''
  const [search, setSearch] = useState(urlSearch)
  const [lastUrlSearch, setLastUrlSearch] = useState(urlSearch)
  if (urlSearch !== lastUrlSearch) {
    setLastUrlSearch(urlSearch)
    setSearch(urlSearch)
  }

  const setView_ = (v: 'grid' | 'table') => {
    setView(v)
    localStorage.setItem('dashcam-recordings-view', v)
  }

  return (
    <div className="space-y-4">
      <PageHeader
        title="Recordings"
        subtitle={query.data ? `${query.data.total} recordings` : undefined}
        actions={
          <div className="flex gap-1">
            <button
              className={cn('btn', view === 'grid' && 'bg-surface-sunken')}
              onClick={() => setView_('grid')}
            >
              Grid
            </button>
            <button
              className={cn('btn', view === 'table' && 'bg-surface-sunken')}
              onClick={() => setView_('table')}
            >
              Table
            </button>
          </div>
        }
      />

      <section className="card p-4 sm:p-5">
        <div className="mb-4">
          <h2 className="section-title">Filter recordings</h2>
          <p className="mt-1 text-sm text-content-muted">Narrow the library by file, processing state, or captured data.</p>
        </div>
        <div className="flex flex-wrap items-end gap-3">
        <label className="min-w-[12rem] flex-1">
          <span className="label mb-1 block text-xs">Filename</span>
          <input
            className="input"
            placeholder="Search…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            onBlur={(e) => update('search', e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && update('search', e.currentTarget.value)}
          />
        </label>
        <label>
          <span className="label mb-1 block text-xs">State</span>
          <select className="input" value={params.get('state') ?? ''} onChange={(e) => update('state', e.target.value)}>
            {STATES.map(([value, label]) => (
              <option key={value} value={value}>{label}</option>
            ))}
          </select>
        </label>
        <label>
          <span className="label mb-1 block text-xs">GPS</span>
          <select className="input" value={params.get('has_gps') ?? ''} onChange={(e) => update('has_gps', e.target.value)}>
            <option value="">Any</option>
            <option value="true">Has GPS</option>
            <option value="false">No GPS</option>
          </select>
        </label>
        <label>
          <span className="label mb-1 block text-xs">Detections</span>
          <select
            className="input"
            value={params.get('has_detections') ?? ''}
            onChange={(e) => update('has_detections', e.target.value)}
          >
            <option value="">Any</option>
            <option value="true">With detections</option>
            <option value="false">Without</option>
          </select>
        </label>
        <label>
          <span className="label mb-1 block text-xs">From</span>
          <input type="date" className="input" value={params.get('date_from') ?? ''} onChange={(e) => update('date_from', e.target.value)} />
        </label>
        <label>
          <span className="label mb-1 block text-xs">To</span>
          <input type="date" className="input" value={params.get('date_to') ?? ''} onChange={(e) => update('date_to', e.target.value)} />
        </label>
        <button className="btn" onClick={() => setParams(new URLSearchParams())}>
          Clear
        </button>
        </div>
      </section>

      {query.isLoading && <Spinner label="Loading recordings…" className="py-20" />}
      {query.isError && <ErrorState error={query.error} retry={() => query.refetch()} />}
      {query.data?.items.length === 0 && (
        <EmptyState
          title="No recordings match"
          description="Adjust the filters, or run a scan from Settings if your footage has not been indexed yet."
        />
      )}

      {query.data && query.data.items.length > 0 && (
        view === 'grid' ? <Grid items={query.data.items} /> : <Table items={query.data.items} />
      )}

      {query.data && (
        <Pagination
          page={query.data.page}
          pages={query.data.pages}
          total={query.data.total}
          onChange={(p) => {
            const next = new URLSearchParams(params)
            next.set('page', String(p))
            setParams(next)
          }}
        />
      )}
    </div>
  )
}

function Thumb({ recording }: { recording: Recording }) {
  const src = mediaUrl(recording.thumbnailPath)
  if (!src) {
    // Distinguish "the camera wrote a broken file" from "not processed yet". Without
    // that, a damaged source reads as an application failure.
    return (
      <div className="flex aspect-video flex-col items-center justify-center gap-1 rounded bg-surface-sunken px-2 text-center text-2xs text-content-faint">
        {recording.sourceDamaged ? (
          <>
            <span className="text-state-warn">damaged file</span>
            <span>no frame could be decoded</span>
          </>
        ) : (
          <span>no thumbnail</span>
        )}
      </div>
    )
  }
  return (
    <div className="relative">
      <img src={src} alt="" loading="lazy" className="aspect-video w-full rounded object-cover" />
      {recording.sourceDamaged && (
        <span
          className="badge absolute left-1 top-1 bg-state-warn/85 text-white"
          title={recording.warnings.join('\n') || 'The source file is damaged'}
        >
          damaged
        </span>
      )}
    </div>
  )
}

function Meta({ recording }: { recording: Recording }) {
  return (
    <div className="tabular flex flex-wrap items-center gap-x-2 gap-y-0.5 text-2xs text-content-faint">
      <span>{formatDuration(recording.durationS)}</span>
      <span>{formatBytes(recording.sizeBytes)}</span>
      {recording.width && <span>{recording.width}×{recording.height}</span>}
      {recording.hasGps && <span className="text-state-ok">GPS</span>}
      {recording.vehicleCount > 0 && <span>{recording.vehicleCount} vehicles</span>}
      {recording.plateCount > 0 && <span>{recording.plateCount} plates</span>}
    </div>
  )
}

function Grid({ items }: { items: Recording[] }) {
  return (
    <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
      {items.map((recording) => (
        <Link key={recording.id} to={`/recordings/${recording.id}`} className="card group overflow-hidden p-3 transition-all hover:-translate-y-0.5 hover:border-accent/40 hover:shadow-md">
          <Thumb recording={recording} />
          <div className="mt-3 flex items-start justify-between gap-2">
            <div className="min-w-0">
              <div className="truncate text-sm font-semibold group-hover:text-accent">{recording.filename}</div>
              <div className="text-xs text-content-muted">
                {recording.camera?.name ?? 'Unknown camera'} · {formatDateTime(recording.startedAt)}
              </div>
            </div>
            <StateBadge state={recording.state} />
          </div>
          <div className="mt-1.5">
            <Meta recording={recording} />
          </div>
        </Link>
      ))}
    </div>
  )
}

function Table({ items }: { items: Recording[] }) {
  return (
    <div className="card overflow-x-auto">
      <table className="w-full min-w-[54rem] text-sm">
        <thead className="border-b border-border text-left text-xs text-content-muted">
          <tr>
            <th className="p-3 font-semibold">Recording</th>
            <th className="p-3 font-semibold">Camera</th>
            <th className="p-3 font-semibold">Date</th>
            <th className="p-3 font-semibold">Duration</th>
            <th className="p-3 font-semibold">Size</th>
            <th className="p-3 font-semibold">GPS</th>
            <th className="p-3 font-semibold">Vehicles</th>
            <th className="p-3 font-semibold">Plates</th>
            <th className="p-3 font-semibold">State</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-border">
          {items.map((r) => (
            <tr key={r.id} className="hover:bg-surface-sunken">
              <td className="p-2">
                <Link to={`/recordings/${r.id}`} className="hover:text-accent">{r.filename}</Link>
              </td>
              <td className="p-2 text-content-muted">{r.camera?.name ?? '—'}</td>
              <td className="tabular p-2 text-content-muted">{formatDateTime(r.startedAt)}</td>
              <td className="tabular p-2">{formatDuration(r.durationS)}</td>
              <td className="tabular p-2">{formatBytes(r.sizeBytes)}</td>
              <td className="p-2">{r.hasGps ? <span className="text-state-ok">yes</span> : <span className="text-content-faint">no</span>}</td>
              <td className="tabular p-2">{r.vehicleCount}</td>
              <td className="tabular p-2">{r.plateCount}</td>
              <td className="p-2"><StateBadge state={r.state} /></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
