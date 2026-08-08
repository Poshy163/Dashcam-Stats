import { useQuery } from '@tanstack/react-query'
import { Link, useSearchParams } from 'react-router-dom'

import Spinner from '@/components/Spinner'
import { EmptyState, ErrorState, PageHeader, Pagination, PlateText } from '@/components/ui'
import { api, mediaUrl } from '@/lib/api'
import { formatDateTime } from '@/lib/format'

export default function Vehicles() {
  const [params, setParams] = useSearchParams()
  const page = Number(params.get('page') ?? 1)

  const query = useQuery({
    queryKey: ['vehicles', page],
    queryFn: () => api.vehicles.list({ page, pageSize: 24 }),
  })

  return (
    <div className="space-y-4">
      <PageHeader
        title="Vehicles"
        subtitle="Vehicles observed, independent of their plates"
      />

      {query.isLoading && <Spinner className="py-20" />}
      {query.isError && <ErrorState error={query.error} retry={() => query.refetch()} />}
      {query.data?.items.length === 0 && (
        <EmptyState
          title="No vehicles yet"
          description="Vehicles appear here once footage has been analysed with object detection enabled."
        />
      )}

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
        {query.data?.items.map((vehicle) => {
          const crop = mediaUrl(vehicle.representativeCropPath)
          // make/model/colour stay null unless a classifier produced them. Showing the
          // class alone is honest; "Unknown make" would imply we tried and failed.
          const descriptors = [vehicle.colour, vehicle.make, vehicle.model].filter(Boolean)
          return (
            <div key={vehicle.id} className="card overflow-hidden p-2">
              {crop ? (
                <img src={crop} alt="" loading="lazy" className="aspect-video w-full rounded object-cover" />
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
