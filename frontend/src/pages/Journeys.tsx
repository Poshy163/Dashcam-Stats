import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useSearchParams } from 'react-router-dom'

import Spinner from '@/components/Spinner'
import { DerivedHint, EmptyState, ErrorState, PageHeader, Pagination } from '@/components/ui'
import { api } from '@/lib/api'
import { formatDate, formatDistance, formatDuration, formatSpeed, formatTime } from '@/lib/format'

export default function Journeys() {
  const [params, setParams] = useSearchParams()
  const [selected, setSelected] = useState<number[]>([])
  const client = useQueryClient()

  const page = Number(params.get('page') ?? 1)
  const sort = params.get('sort') ?? 'started_desc'

  const query = useQuery({
    queryKey: ['journeys', page, sort],
    queryFn: () => api.journeys.list({ page, pageSize: 25, sort }),
  })

  const merge = useMutation({
    mutationFn: () => api.journeys.merge(selected),
    onSuccess: () => {
      setSelected([])
      client.invalidateQueries({ queryKey: ['journeys'] })
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

      {query.isLoading && <Spinner className="py-20" />}
      {query.isError && <ErrorState error={query.error} retry={() => query.refetch()} />}
      {query.data?.items.length === 0 && (
        <EmptyState
          title="No journeys yet"
          description="Journeys are built automatically from consecutive recordings once footage has been processed."
        />
      )}

      <div className="space-y-2">
        {query.data?.items.map((journey) => (
          <div key={journey.id} className="card flex items-start gap-3 p-3">
            <input
              type="checkbox"
              className="mt-1"
              checked={selected.includes(journey.id)}
              onChange={() => toggle(journey.id)}
              aria-label={`Select journey ${journey.id}`}
            />
            <Link to={`/journeys/${journey.id}`} className="min-w-0 flex-1 hover:text-accent">
              <div className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5">
                <span className="font-medium">{formatDate(journey.startedAt)}</span>
                <span className="tabular text-sm text-content-muted">
                  {formatTime(journey.startedAt)} → {formatTime(journey.endedAt)}
                </span>
                <span className="tabular text-sm text-content-muted">
                  {formatDuration(journey.durationS)}
                </span>
                {journey.manual && (
                  <span className="badge bg-accent-muted text-accent">edited</span>
                )}
              </div>
              <div className="tabular mt-1 flex flex-wrap gap-x-4 gap-y-0.5 text-xs text-content-muted">
                {journey.hasGps ? (
                  <>
                    <DerivedHint>{formatDistance(journey.distanceM)}</DerivedHint>
                    <DerivedHint>avg {formatSpeed(journey.avgSpeedKmh)}</DerivedHint>
                    <span>max {formatSpeed(journey.maxSpeedKmh)}</span>
                  </>
                ) : (
                  <span className="text-content-faint">No GPS</span>
                )}
                {/* Files and sightings — see the notes on the journey detail tiles. */}
                <span>{journey.recordingCount} files</span>
                <span>{journey.vehicleCount} sightings</span>
                <span>{journey.uniquePlateCount} plates</span>
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
