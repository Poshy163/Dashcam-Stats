import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useSearchParams } from 'react-router-dom'

import Spinner from '@/components/Spinner'
import { DerivedHint, EmptyState, ErrorState, PageHeader, Pagination } from '@/components/ui'
import { api } from '@/lib/api'
import { invalidateAnalysisQueries } from '@/lib/queryInvalidation'
import { formatDate, formatDistance, formatDuration, formatSpeed, formatTime } from '@/lib/format'
import { journeyDatePreset, journeyDateRangeError, type JourneyDatePreset } from '@/lib/journeyFilters'
import { listPage } from '@/lib/listFilters'

const SORTS = ['started_desc', 'started_asc', 'distance_desc', 'duration_desc']
const DATE_PRESETS: [JourneyDatePreset, string][] = [['today', 'Today'], ['yesterday', 'Yesterday'], ['last7', 'Last 7 days']]

export default function Journeys() {
  const [params, setParams] = useSearchParams()
  const client = useQueryClient()
  const status = useQuery({ queryKey: ['status'], queryFn: api.status, staleTime: 300_000 })
  const motion = useQuery({
    queryKey: ['journey-motion-quality'],
    queryFn: () => api.journeys.motionQuality(),
    refetchInterval: 30_000,
  })

  const page = listPage(params.get('page'))
  const requestedSort = params.get('sort') ?? 'started_desc'
  const sort = SORTS.includes(requestedSort) ? requestedSort : 'started_desc'
  const includeParked = params.get('include_parked') === 'true'
  const dateFrom = params.get('date_from') ?? ''
  const dateTo = params.get('date_to') ?? ''
  const dateError = journeyDateRangeError(dateFrom, dateTo)
  const hasDates = Boolean(dateFrom || dateTo)
  const selectionScope = JSON.stringify([page, sort, includeParked, dateFrom, dateTo])
  const [selection, setSelection] = useState<{ scope: string; ids: number[] }>({ scope: selectionScope, ids: [] })
  const pendingMotion = motion.data?.pending

  useEffect(() => {
    if (pendingMotion !== undefined) void invalidateAnalysisQueries(client)
  }, [pendingMotion, client])

  const query = useQuery({
    queryKey: ['journeys', page, sort, includeParked, dateFrom, dateTo],
    queryFn: () => api.journeys.list({ page, pageSize: 25, sort, includeParked, dateFrom, dateTo }),
    enabled: !dateError,
  })
  const items = query.data?.items
  const selected = selection.scope === selectionScope
    ? selection.ids.filter((id) => items?.some((journey) => journey.id === id))
    : []

  useEffect(() => {
    setSelection((current) => {
      const ids = current.scope === selectionScope
        ? current.ids.filter((id) => items?.some((journey) => journey.id === id))
        : []
      return current.scope === selectionScope && ids.length === current.ids.length
        ? current
        : { scope: selectionScope, ids }
    })
  }, [selectionScope, items])

  const merge = useMutation({
    mutationFn: () => api.journeys.merge(selected),
    onSuccess: () => {
      setSelection({ scope: selectionScope, ids: [] })
      // Everything derived, not just this list. A merge deletes journey rows and re-parents
      // every recording's journeyId, so the journey detail view, the Recordings list's
      // journey filter and both map layers are all stale afterwards — which is precisely
      // the "one mutation remembers Vehicles and forgets Recordings" problem the shared
      // helper exists to end.
      void invalidateAnalysisQueries(client)
    },
  })

  const toggle = (id: number) => setSelection((current) => {
    const ids = current.scope === selectionScope ? current.ids : []
    return { scope: selectionScope, ids: ids.includes(id) ? ids.filter((x) => x !== id) : [...ids, id] }
  })

  const updateDates = (from: string, to: string) => {
    const next = new URLSearchParams(params)
    if (from) next.set('date_from', from)
    else next.delete('date_from')
    if (to) next.set('date_to', to)
    else next.delete('date_to')
    next.delete('page')
    setParams(next)
  }
  const choosePreset = (preset: JourneyDatePreset) => {
    if (!status.data?.timezone) return
    const dates = journeyDatePreset(preset, status.data.timezone)
    updateDates(dates.dateFrom, dates.dateTo)
  }
  const presetSelected = (preset: JourneyDatePreset) => {
    if (!status.data?.timezone) return false
    const dates = journeyDatePreset(preset, status.data.timezone)
    return dateFrom === dates.dateFrom && dateTo === dates.dateTo
  }

  return (
    <div className="space-y-4">
      <PageHeader
        title="Journeys"
        subtitle={!dateError && query.data ? `${query.data.total} journeys${hasDates ? ' in date range' : ''}` : undefined}
        actions={
          <>
            <label className="flex items-center gap-2 text-sm text-content-muted">
              <input
                type="checkbox"
                checked={includeParked}
                onChange={(e) => {
                  const next = new URLSearchParams(params)
                  if (e.target.checked) next.set('include_parked', 'true')
                  else next.delete('include_parked')
                  next.delete('page')
                  setParams(next)
                }}
              />
              Include parked / unconfirmed
            </label>
            <select
              aria-label="Sort journeys"
              className="input w-auto"
              value={sort}
              onChange={(e) => {
                const next = new URLSearchParams(params)
                next.set('sort', e.target.value)
                next.delete('page')
                setParams(next)
              }}
            >
              <option value="started_desc">Newest first</option>
              <option value="started_asc">Oldest first</option>
              <option value="distance_desc">Longest distance</option>
              <option value="duration_desc">Longest duration</option>
            </select>
          </>
        }
      />

      <section className="card p-4 sm:p-5" aria-label="Filter journeys by date">
        <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
          <div>
            <h2 className="section-title">Filter journeys</h2>
            <p className="mt-1 text-sm text-content-muted">
              Find journeys by their start date. Dates use {status.data?.timezone ?? 'the camera’s configured timezone'}.
            </p>
          </div>
          <div className="flex flex-wrap gap-1.5">
            {DATE_PRESETS.map(([preset, label]) => (
              <button key={preset} className="btn aria-pressed:border-accent/50 aria-pressed:bg-accent-muted" aria-pressed={presetSelected(preset)} disabled={!status.data?.timezone} onClick={() => choosePreset(preset)}>{label}</button>
            ))}
            <button className="btn aria-pressed:border-accent/50 aria-pressed:bg-accent-muted" aria-pressed={!hasDates} onClick={() => updateDates('', '')}>All dates</button>
          </div>
        </div>
        <div className="flex flex-wrap items-end gap-3">
          <label className="min-w-0 flex-1 sm:max-w-56">
            <span className="label mb-1 block text-xs">From date</span>
            <input type="date" aria-label="From date" className="input" value={dateFrom} max={dateTo || undefined} aria-invalid={Boolean(dateError)} aria-describedby={dateError ? 'journey-date-error' : undefined} onChange={(e) => updateDates(e.target.value, dateTo)} />
          </label>
          <label className="min-w-0 flex-1 sm:max-w-56">
            <span className="label mb-1 block text-xs">To date</span>
            <input type="date" aria-label="To date" className="input" value={dateTo} min={dateFrom || undefined} aria-invalid={Boolean(dateError)} aria-describedby={dateError ? 'journey-date-error' : undefined} onChange={(e) => updateDates(dateFrom, e.target.value)} />
          </label>
          {hasDates && <button className="btn" onClick={() => updateDates('', '')}>Clear dates</button>}
        </div>
        {dateError && <p id="journey-date-error" className="mt-3 text-sm text-state-error" role="alert">{dateError}</p>}
      </section>

      {selected.length > 0 && (
        <div className="flex flex-wrap items-center gap-3" role="status">
          <span className="text-sm text-content-muted">{selected.length} selected on this page</span>
          {selected.length > 1 && <button className="btn btn-primary" onClick={() => merge.mutate()} disabled={merge.isPending}>Merge {selected.length} journeys</button>}
          <button className="btn" onClick={() => setSelection({ scope: selectionScope, ids: [] })}>Clear selection</button>
        </div>
      )}

      {merge.isError && <ErrorState title="Could not merge these journeys" error={merge.error} />}
      {motion.isError && <ErrorState title="Could not load movement-check progress" error={motion.error} retry={() => void motion.refetch()} />}

      {motion.data && (
        <p className="text-sm text-content-muted">
          Movement checks: {motion.data.total - motion.data.pending} of {motion.data.total} sessions checked.
          {' '}{motion.data.pending > 0 ? 'Historical checks are running automatically.' : 'Historical checks complete.'}
          {' '}{motion.data.unconfirmed + motion.data.unknown} sessions without confirmed movement.
        </p>
      )}

      {!dateError && query.isLoading && <Spinner label="Loading journeys…" className="py-20" />}
      {!dateError && query.isError && <ErrorState error={query.error} retry={() => query.refetch()} />}
      {!dateError && query.data?.items.length === 0 && (
        <EmptyState
          title={hasDates ? 'No journeys match these dates' : 'No journeys yet'}
          description={hasDates ? 'Try a wider date range, or include parked / unconfirmed sessions.' : 'Journeys are built automatically from consecutive recordings once footage has been processed.'}
          action={hasDates ? <button className="btn" onClick={() => updateDates('', '')}>Clear dates</button> : undefined}
        />
      )}

      <div className="space-y-3">
        {!dateError && query.data?.items.map((journey) => (
          <div key={journey.id} className="card cockpit-panel flex items-start gap-4 p-4 transition-all hover:border-accent/60 hover:shadow-card sm:p-5">
            <input
              type="checkbox"
              className="mt-1.5 h-4 w-4 rounded border-border bg-surface-sunken text-accent focus:ring-accent"
              checked={selected.includes(journey.id)}
              onChange={() => toggle(journey.id)}
              aria-label={`Select journey ${journey.id}`}
            />
            <Link to={`/journeys/${journey.id}`} className="min-w-0 flex-1 group">
              <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                {journey.motionJson && journey.motionJson.status !== 'moving' && (
                  <span className="text-xs text-content-muted">Movement not confirmed</span>
                )}
                <span className="font-mono text-base font-black text-content group-hover:text-accent transition-colors">{formatDate(journey.startedAt)}</span>
                <span className="tabular font-mono text-xs text-content-muted">
                  {formatTime(journey.startedAt)} → {formatDate(journey.startedAt) !== formatDate(journey.endedAt) ? `${formatDate(journey.endedAt)} ` : ''}{formatTime(journey.endedAt)}
                </span>
                <span className="tabular font-mono text-xs text-cyan font-bold bg-cyan/10 border border-cyan/30 px-2 py-0.5 rounded">
                  {formatDuration(journey.durationS)}
                </span>
                {journey.manual && (
                  <span className="badge border border-accent/40 bg-accent/15 text-accent">manual</span>
                )}
              </div>
              <div className="tabular font-mono mt-3 flex flex-wrap gap-x-5 gap-y-1.5 text-xs text-content-muted">
                {journey.hasGps ? (
                  <>
                    <span className="text-content font-bold"><DerivedHint>{formatDistance(journey.distanceM)}</DerivedHint></span>
                    <span>avg <span className="text-content font-bold">{formatSpeed(journey.avgSpeedKmh)}</span></span>
                    <span>max <span className="text-accent font-bold">{formatSpeed(journey.maxSpeedKmh)}</span></span>
                  </>
                ) : (
                  <span className="text-content-faint">No GPS</span>
                )}
                <span>{journey.recordingCount} video file{journey.recordingCount === 1 ? '' : 's'}</span>
                <span>{journey.vehicleCount} vehicle sighting{journey.vehicleCount === 1 ? '' : 's'}</span>
                <span className="text-cyan font-bold">{journey.uniquePlateCount} plates</span>
              </div>
            </Link>
          </div>
        ))}
      </div>

      {!dateError && query.data && (
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
