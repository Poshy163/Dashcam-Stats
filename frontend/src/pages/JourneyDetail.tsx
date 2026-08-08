import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate, useParams } from 'react-router-dom'

import RouteMap from '@/components/RouteMap'
import Spinner from '@/components/Spinner'
import { DerivedHint, EmptyState, ErrorState, PageHeader, StatTile, StateBadge } from '@/components/ui'
import { api } from '@/lib/api'
import {
  formatDate,
  formatDateTime,
  formatDistance,
  formatDuration,
  formatSpeed,
  formatTime,
} from '@/lib/format'

const REPROCESS_OPTIONS = [
  ['everything', 'Everything'],
  ['metadata', 'Metadata only'],
  ['telemetry', 'Telemetry only'],
  ['detection', 'Object detection'],
  ['plates', 'Licence plate detection'],
] as const

export default function JourneyDetail() {
  const { id } = useParams()
  const journeyId = Number(id)
  const navigate = useNavigate()
  const client = useQueryClient()
  const [stage, setStage] = useState<string>('everything')

  const query = useQuery({
    queryKey: ['journey', journeyId],
    queryFn: () => api.journeys.get(journeyId),
    enabled: Number.isFinite(journeyId),
  })

  const settings = useQuery({ queryKey: ['settings'], queryFn: api.settings.get })

  const reprocess = useMutation({
    mutationFn: () => api.journeys.reprocess(journeyId, [stage]),
    onSuccess: () => {
      client.invalidateQueries({ queryKey: ['jobs'] })
      navigate('/queue')
    },
  })

  const mapSettings = useMemo(() => {
    const maps = settings.data?.find((c) => c.key === 'maps')
    const value = (key: string) => maps?.settings.find((s) => s.key === key)?.value
    return {
      tileUrl: (value('maps.tile_url') as string) || undefined,
      attribution: (value('maps.attribution') as string) || undefined,
      maxZoom: (value('maps.max_zoom') as number) || undefined,
    }
  }, [settings.data])

  if (query.isLoading) return <Spinner label="Loading journey…" className="py-24" />
  if (query.isError) return <ErrorState error={query.error} retry={() => query.refetch()} />
  if (!query.data) return null

  const journey = query.data
  const start = journey.startLat != null && journey.startLon != null
    ? ([journey.startLat, journey.startLon] as [number, number])
    : null
  const end = journey.endLat != null && journey.endLon != null
    ? ([journey.endLat, journey.endLon] as [number, number])
    : null

  return (
    <div className="space-y-4">
      <PageHeader
        title={formatDate(journey.startedAt)}
        subtitle={`${formatTime(journey.startedAt)} → ${formatTime(journey.endedAt)} · ${formatDuration(journey.durationS)}`}
        actions={
          <>
            <select className="input w-auto" value={stage} onChange={(e) => setStage(e.target.value)}>
              {REPROCESS_OPTIONS.map(([value, label]) => (
                <option key={value} value={value}>{label}</option>
              ))}
            </select>
            <button className="btn" onClick={() => reprocess.mutate()} disabled={reprocess.isPending}>
              Reprocess journey
            </button>
          </>
        }
      />

      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 lg:grid-cols-6">
        <StatTile
          label="Distance"
          value={<DerivedHint>{formatDistance(journey.distanceM)}</DerivedHint>}
        />
        <StatTile
          label="Average speed"
          value={<DerivedHint>{formatSpeed(journey.avgSpeedKmh)}</DerivedHint>}
          hint="while moving"
        />
        <StatTile label="Maximum speed" value={formatSpeed(journey.maxSpeedKmh)} />
        <StatTile label="Recordings" value={journey.recordingCount} />
        <StatTile label="Vehicles" value={journey.vehicleCount} />
        <StatTile label="Unique plates" value={journey.uniquePlateCount} />
      </div>

      {journey.hasGps ? (
        <RouteMap
          route={journey.route}
          start={start}
          end={end}
          className="h-[26rem] w-full"
          {...mapSettings}
          onPointClick={(_lat, _lon, index) => {
            // Map the clicked fix back to a moment: the route is time-ordered, so the
            // index approximates elapsed seconds at the telemetry sample rate.
            const first = journey.recordings[0]
            if (first) navigate(`/recordings/${first.id}?t=${index}`)
          }}
        />
      ) : (
        <EmptyState
          title="No GPS for this journey"
          description="The camera had no satellite fix during these recordings, so there is no route to draw."
        />
      )}

      <section>
        <h2 className="mb-2 text-sm font-semibold">Recordings</h2>
        <div className="card divide-y divide-border">
          {journey.recordings.map((recording) => (
            <Link
              key={recording.id}
              to={`/recordings/${recording.id}`}
              className="flex flex-wrap items-center gap-3 p-2.5 hover:bg-surface-sunken"
            >
              <span className="min-w-0 flex-1 truncate font-medium">{recording.filename}</span>
              <span className="text-xs text-content-muted">{recording.camera?.name ?? '—'}</span>
              <span className="tabular text-xs text-content-muted">{formatDateTime(recording.startedAt)}</span>
              <span className="tabular text-xs text-content-muted">{formatDuration(recording.durationS)}</span>
              <span className="tabular text-xs text-content-muted">{recording.plateCount} plates</span>
              <StateBadge state={recording.state} />
            </Link>
          ))}
        </div>
      </section>
    </div>
  )
}
