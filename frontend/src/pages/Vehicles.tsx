import { useQuery } from '@tanstack/react-query'
import { Link, useSearchParams } from 'react-router-dom'

import Spinner from '@/components/Spinner'
import { EmptyState, ErrorState, PageHeader, Pagination, PlateText } from '@/components/ui'
import { api, mediaUrl } from '@/lib/api'
import { formatDateTime } from '@/lib/format'

const CLASSES = ['', 'car', 'truck', 'bus', 'motorcycle']

export default function Vehicles() {
  const [params, setParams] = useSearchParams()
  const page = Number(params.get('page') ?? 1)
  const classLabel = params.get('class_label') ?? ''
  const hasPlate = params.get('has_plate') ?? ''
  const dateFrom = params.get('date_from') ?? ''
  const dateTo = params.get('date_to') ?? ''

  const query = useQuery({
    queryKey: ['vehicles', page, classLabel, hasPlate, dateFrom, dateTo],
    queryFn: () =>
      api.vehicles.list({
        page,
        pageSize: 24,
        classLabel: classLabel || undefined,
        hasPlate: hasPlate ? hasPlate === 'yes' : undefined,
        dateFrom: dateFrom || undefined,
        dateTo: dateTo || undefined,
      }),
  })

  // Changing a filter resets to page one: page 12 of "trucks with plates" has nothing to
  // do with page 12 of everything, and landing on an empty page reads as no results.
  const setFilter = (key: string, value: string) => {
    const next = new URLSearchParams(params)
    if (value) next.set(key, value)
    else next.delete(key)
    next.delete('page')
    setParams(next)
  }

  return (
    <div className="space-y-4">
      <PageHeader title="Vehicles" subtitle="Vehicles observed, independent of their plates" />

      <div className="flex flex-wrap items-end gap-2">
        <label className="flex flex-col gap-1">
          <span className="label text-xs">Type</span>
          <select
            className="input w-auto"
            value={classLabel}
            onChange={(e) => setFilter('class_label', e.target.value)}
          >
            {CLASSES.map((c) => (
              <option key={c} value={c}>
                {c || 'All types'}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1">
          <span className="label text-xs">Plate</span>
          <select
            className="input w-auto"
            value={hasPlate}
            onChange={(e) => setFilter('has_plate', e.target.value)}
          >
            <option value="">Any</option>
            <option value="yes">Plate read</option>
            <option value="no">No plate</option>
          </select>
        </label>
        <label className="flex flex-col gap-1">
          <span className="label text-xs">From</span>
          <input
            type="date"
            className="input w-auto"
            value={dateFrom}
            onChange={(e) => setFilter('date_from', e.target.value)}
          />
        </label>
        <label className="flex flex-col gap-1">
          <span className="label text-xs">To</span>
          <input
            type="date"
            className="input w-auto"
            value={dateTo}
            onChange={(e) => setFilter('date_to', e.target.value)}
          />
        </label>
        {(classLabel || hasPlate || dateFrom || dateTo) && (
          <button className="btn" onClick={() => setParams(new URLSearchParams())}>
            Clear
          </button>
        )}
      </div>

      {query.isLoading && <Spinner className="py-20" />}
      {query.isError && <ErrorState error={query.error} retry={() => query.refetch()} />}
      {query.data?.items.length === 0 && (
        <EmptyState
          title="No vehicles match"
          description="Vehicles appear here once footage has been analysed with object detection enabled."
        />
      )}

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
        {query.data?.items.map((vehicle) => {
          const crop = mediaUrl(vehicle.representativeCropPath)
          // make/model/colour stay null unless a classifier produced them. Showing the
          // class alone is honest; "Unknown make" would imply we tried and failed.
          const descriptors = [vehicle.colour, vehicle.make, vehicle.model].filter(Boolean)
          // Back to the moment it was seen. The sighting knows the clip and the offset;
          // without this the page showed the evidence and withheld every route to it.
          const source =
            vehicle.recordingId != null
              ? `/recordings/${vehicle.recordingId}?t=${vehicle.firstSeenOffsetS ?? 0}`
              : null
          return (
            <div key={vehicle.id} className="card overflow-hidden p-2">
              {crop ? (
                source ? (
                  <Link to={source} title="Open this moment in the recording">
                    <img
                      src={crop}
                      alt=""
                      loading="lazy"
                      className="aspect-video w-full rounded object-cover transition hover:opacity-90"
                    />
                  </Link>
                ) : (
                  <img src={crop} alt="" loading="lazy" className="aspect-video w-full rounded object-cover" />
                )
              ) : (
                <div className="flex aspect-video items-center justify-center rounded bg-surface-sunken text-2xs text-content-faint">
                  no image
                </div>
              )}
              <div className="mt-2 space-y-1">
                <div className="text-sm font-medium capitalize">
                  {descriptors.length > 0 ? descriptors.join(' ') : vehicle.classLabel ?? 'Vehicle'}
                </div>
                {descriptors.length > 0 && vehicle.classifier && (
                  <div className="text-2xs text-content-faint">
                    classified by {vehicle.classifier}
                  </div>
                )}
                {vehicle.primaryPlate ? (
                  <Link to={`/plates/${vehicle.primaryPlate.id}`} className="block hover:text-accent">
                    <PlateText
                      text={vehicle.primaryPlate.displayText}
                      confidence={vehicle.primaryPlate.bestConfidence}
                      matchedPattern={vehicle.primaryPlate.patternName}
                    />
                  </Link>
                ) : (
                  <div className="text-xs text-content-faint">No plate read</div>
                )}
                <div className="tabular text-2xs text-content-faint">
                  Seen {vehicle.observationCount}× · last {formatDateTime(vehicle.lastSeenAt)}
                </div>
                {source && (
                  <Link to={source} className="block text-2xs text-accent hover:underline">
                    Watch this moment
                  </Link>
                )}
              </div>
            </div>
          )
        })}
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
