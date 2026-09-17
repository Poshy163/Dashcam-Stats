import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useSearchParams } from 'react-router-dom'

import Spinner from '@/components/Spinner'
import { DerivedHint, EmptyState, ErrorState, PageHeader, Pagination } from '@/components/ui'
import { api } from '@/lib/api'
import { invalidateAnalysisQueries } from '@/lib/queryInvalidation'
import { formatDate, formatDistance, formatDuration, formatSpeed, formatTime } from '@/lib/format'

export default function Journeys() {
  const [params, setParams] = useSearchParams()
  const [selected, setSelected] = useState<number[]>([])
  const client = useQueryClient()
  const motion = useQuery({
    queryKey: ['journey-motion-quality'],
    queryFn: () => api.journeys.motionQuality(),
    refetchInterval: 30_000,
  })

  const page = Number(params.get('page') ?? 1)
  const sort = params.get('sort') ?? 'started_desc'
  const includeParked = params.get('include_parked') === 'true'
  const pendingMotion = motion.data?.pending

  useEffect(() => {
    if (pendingMotion !== undefined) void invalidateAnalysisQueries(client)
  }, [pendingMotion, client])

  const query = useQuery({
    queryKey: ['journeys', page, sort, includeParked],
    queryFn: () => api.journeys.list({ page, pageSize: 25, sort, includeParked }),
  })

  const merge = useMutation({
    mutationFn: () => api.journeys.merge(selected),
    onSuccess: () => {
      setSelected([])
      // Everything derived, not just this list. A merge deletes journey rows and re-parents
      // every recording's journeyId, so the journey detail view, the Recordings list's
      // journey filter and both map layers are all stale afterwards — which is precisely
      // the "one mutation remembers Vehicles and forgets Recordings" problem the shared
      // helper exists to end.
      void invalidateAnalysisQueries(client)
    },
  })

  const toggle = (id: number) =>
    setSelected((current) =>
      current.includes(id) ? current.filter((x) => x !== id) : [...current, id],
    )

  return (
    <div className="space-y-4">
      <PageHeader
        title="Journeys"
        subtitle={query.data ? `${query.data.total} journeys` : undefined}
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
            {selected.length > 1 && (
              <button className="btn btn-primary" onClick={() => merge.mutate()} disabled={merge.isPending}>
                Merge {selected.length} journeys
              </button>
            )}
          </>
        }
      />

      {motion.data && (
        <p className="text-sm text-content-muted">
          Movement checks: {motion.data.total - motion.data.pending} of {motion.data.total} sessions checked.
          {' '}{motion.data.pending > 0 ? 'Historical checks are running automatically.' : 'Historical checks complete.'}
          {' '}{motion.data.unconfirmed + motion.data.unknown} sessions without confirmed movement.
        </p>
      )}

      {query.isLoading && <Spinner className="py-20" />}
      {query.isError && <ErrorState error={query.error} retry={() => query.refetch()} />}
      {query.data?.items.length === 0 && (
        <EmptyState
          title="No journeys yet"
          description="Journeys are built automatically from consecutive recordings once footage has been processed."
        />
      )}

      <div className="space-y-3">
        {query.data?.items.map((journey) => (
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
                <span className="font-mono text-base font-black text-white group-hover:text-accent transition-colors">{formatDate(journey.startedAt)}</span>
                <span className="tabular font-mono text-xs text-content-muted">
                  {formatTime(journey.startedAt)} → {formatTime(journey.endedAt)}
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
                    <span className="text-white font-bold"><DerivedHint>{formatDistance(journey.distanceM)}</DerivedHint></span>
                    <span>avg <span className="text-content font-bold">{formatSpeed(journey.avgSpeedKmh)}</span></span>
                    <span>max <span className="text-accent font-bold">{formatSpeed(journey.maxSpeedKmh)}</span></span>
                  </>
                ) : (
                  <span className="text-content-faint">No GPS</span>
                )}
                <span>{journey.recordingCount} clips</span>
                <span>{journey.vehicleCount} vehicles</span>
                <span className="text-cyan font-bold">{journey.uniquePlateCount} plates</span>
              </div>
            </Link>
          </div>
        ))}
      </div>

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
