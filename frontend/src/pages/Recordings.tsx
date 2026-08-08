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

      <div className="card flex flex-wrap items-end gap-3 p-3">
        <label className="min-w-[12rem] flex-1">
          <span className="label mb-1 block text-xs">Filename</span>
          <input
            className="input"
            placeholder="Search…"
            defaultValue={params.get('search') ?? ''}
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
          <input type="date" className="input" defaultValue={params.get('date_from') ?? ''} onChange={(e) => update('date_from', e.target.value)} />
        </label>
        <label>
          <span className="label mb-1 block text-xs">To</span>
          <input type="date" className="input" defaultValue={params.get('date_to') ?? ''} onChange={(e) => update('date_to', e.target.value)} />
        </label>
        <button className="btn" onClick={() => setParams(new URLSearchParams())}>
          Clear
        </button>
      </div>

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
    return (
      <div className="flex aspect-video items-center justify-center rounded bg-surface-sunken text-2xs text-content-faint">
        no thumbnail
      </div>
    )
  }
  return (
    <img
      src={src}
      alt=""
      loading="lazy"
      className="aspect-video w-full rounded object-cover"
    />
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
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
      {items.map((recording) => (
        <Link key={recording.id} to={`/recordings/${recording.id}`} className="card overflow-hidden p-2 transition-colors hover:border-accent/50">
          <Thumb recording={recording} />
          <div className="mt-2 flex items-start justify-between gap-2">
            <div className="min-w-0">
              <div className="truncate text-sm font-medium">{recording.filename}</div>
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
            <th className="p-2 font-medium">Recording</th>
            <th className="p-2 font-medium">Camera</th>
            <th className="p-2 font-medium">Date</th>
            <th className="p-2 font-medium">Duration</th>
            <th className="p-2 font-medium">Size</th>
            <th className="p-2 font-medium">GPS</th>
            <th className="p-2 font-medium">Vehicles</th>
            <th className="p-2 font-medium">Plates</th>
            <th className="p-2 font-medium">State</th>
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
