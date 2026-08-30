import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate, useParams } from 'react-router-dom'

import RouteMap from '@/components/RouteMap'
import JourneyPlayer from '@/components/JourneyPlayer'
import Spinner from '@/components/Spinner'
import { DerivedHint, EmptyState, ErrorState, PageHeader, StatTile, StateBadge } from '@/components/ui'
import { api } from '@/lib/api'
import { useMapSettings } from '@/lib/useMapSettings'
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

  const reprocess = useMutation({
    mutationFn: () => api.journeys.reprocess(journeyId, [stage]),
    onSuccess: () => {
      client.invalidateQueries({ queryKey: ['jobs'] })
      // The counts as well as the list. Without this the Queue page we are about to
      // navigate to shows the pre-request figures until its next poll.
      client.invalidateQueries({ queryKey: ['queue-stats'] })
      navigate('/queue')
    },
  })

  // Shared with the Heatmap and the plate map, which both used to hard-code their tiles.
  const mapSettings = useMapSettings()

  // The OBD drive covering the same span, matched server-side by time overlap. Footage
  // and telemetry are recorded by different devices, so the join lives on the server
  // where both clocks are already normalised to UTC.
  const obd = useQuery({
    queryKey: ['obd-for-journey', journeyId],
    queryFn: () => api.obd.driveForJourney(journeyId),
    enabled: Number.isFinite(journeyId),
  })

  // Coordinates only, for the map; the recording and offset on each point stay behind for
  // the click handler to resolve.
  const drawable = useMemo<[number, number][][]>(
    () => (query.data?.route ?? []).map((segment) => segment.map(([lat, lon]) => [lat, lon])),
    [query.data],
  )

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
        {/*
          "Video files", not "recordings": a two-minute stretch of driving is two files,
          one per camera, and calling that 2 recordings reads as two minutes of footage.
        */}
        <StatTile label="Video files" value={journey.recordingCount} hint="front and rear" />
        {/*
          Sightings, not vehicles. This counts tracked_objects rows: one per vehicle per
          clip it appears in, per camera that saw it. A car followed across two clips and
          seen by both cameras is four. Nothing in this pipeline re-identifies a vehicle
          between clips, so a unique count is not something the data can answer -- and a
          label promising one would be the lie, not the number.
        */}
        <StatTile
          label="Vehicle sightings"
          value={journey.vehicleCount}
          hint="not unique vehicles"
        />
        <StatTile label="Unique plates" value={journey.uniquePlateCount} />
      </div>

      <JourneyPlayer journey={journey} driveId={obd.data?.drive?.driveId} />

      {journey.hasGps ? (
        <RouteMap
          route={drawable}
          start={start}
          end={end}
          className="h-[26rem] w-full"
          {...mapSettings}
          onPointClick={(_lat, _lon, index, segment) => {
            // Every point says which recording it came from and how far into it. It used
            // to say neither: the click handler read the point's position in the array as
            // elapsed seconds into the journey's *first* recording, so on a nine-clip
            // journey every click anywhere on the route opened clip one within the first
            // few seconds — and past that clip's length the browser silently clamped to
            // its last frame. It looked like it worked every time.
            const point = journey.route[segment]?.[index]
            if (point) navigate(`/recordings/${point[2]}?t=${point[3]}`)
          }}
        />
      ) : (
        <EmptyState
          title="No GPS for this journey"
          description="The camera had no satellite fix during these recordings, so there is no route to draw."
        />
      )}

      {obd.data?.drive && (
        <section>
          <div className="mb-2 flex items-baseline justify-between gap-3">
            <h2 className="text-sm font-semibold">Engine telemetry</h2>
            <Link
              className="text-sm font-semibold text-accent hover:underline"
              to={`/obd/${obd.data.drive.driveId}`}
            >
              Full charts →
            </Link>
          </div>
          <div className="grid grid-cols-2 gap-3 md:grid-cols-3 lg:grid-cols-6">
            <StatTile
              label="Engine time"
              value={formatDuration(obd.data.drive.durationS)}
              hint={
                obd.data.drive.idleDurationS != null
                  ? `${formatDuration(obd.data.drive.idleDurationS)} idle`
                  : undefined
              }
            />
            <StatTile
              label="Fuel used"
              value={
                obd.data.drive.estimatedFuelUsedL != null
                  ? `${obd.data.drive.estimatedFuelUsedL.toFixed(2)} L`
                  : '—'
              }
              hint={
                obd.data.drive.averageFuelConsumptionL100km != null
                  ? `${obd.data.drive.averageFuelConsumptionL100km.toFixed(1)} L/100 km`
                  : undefined
              }
            />
            <StatTile
              label="Average RPM"
              value={
                obd.data.drive.averageRpm != null
                  ? Math.round(obd.data.drive.averageRpm).toLocaleString()
                  : '—'
              }
              hint={
                obd.data.drive.maximumRpm != null
                  ? `peak ${Math.round(obd.data.drive.maximumRpm).toLocaleString()}`
                  : undefined
              }
            />
            <StatTile
              label="Peak coolant"
              value={
                obd.data.drive.maximumCoolantTemperatureC != null
                  ? `${Math.round(obd.data.drive.maximumCoolantTemperatureC)}°C`
                  : '—'
              }
            />
            <StatTile
              label="OBD top speed"
              value={formatSpeed(obd.data.drive.maximumSpeedKmh)}
              hint="from the ECU, not GPS"
            />
            <StatTile
              label="Samples"
              value={obd.data.drive.sampleCount.toLocaleString()}
              tone={obd.data.drive.dtcsObserved.length > 0 ? 'warn' : 'default'}
              hint={
                obd.data.drive.dtcsObserved.length > 0
                  ? `DTCs: ${obd.data.drive.dtcsObserved.join(', ')}`
                  : undefined
              }
            />
          </div>
        </section>
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
