import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useParams } from 'react-router-dom'

import RouteMap, { type MapMarker } from '@/components/RouteMap'
import Spinner from '@/components/Spinner'
import {
  ConfidenceBadge,
  EmptyState,
  ErrorState,
  PageHeader,
  Pagination,
  PlateText,
  StatTile,
} from '@/components/ui'
import { api, mediaUrl } from '@/lib/api'
import { formatCoords, formatDateTime } from '@/lib/format'

export default function PlateDetail() {
  const { id } = useParams()
  const plateId = Number(id)
  const client = useQueryClient()
  const [page, setPage] = useState(1)
  const [notes, setNotes] = useState<string | null>(null)

  const plate = useQuery({
    queryKey: ['plate', plateId],
    queryFn: () => api.plates.get(plateId),
    enabled: Number.isFinite(plateId),
  })

  const observations = useQuery({
    queryKey: ['plate-observations', plateId, page],
    queryFn: () => api.plates.observations(plateId, { page, pageSize: 25 }),
    enabled: Number.isFinite(plateId),
  })

  const patch = useMutation({
    mutationFn: (body: { flagged?: boolean; notes?: string }) => api.plates.update(plateId, body),
    onSuccess: () => client.invalidateQueries({ queryKey: ['plate', plateId] }),
  })

  if (plate.isLoading) return <Spinner label="Loading plate…" className="py-24" />
  if (plate.isError) return <ErrorState error={plate.error} retry={() => plate.refetch()} />
  if (!plate.data) return null

  const p = plate.data
  const items = observations.data?.items ?? []
  const markers: MapMarker[] = items
    .filter((o) => o.lat != null && o.lon != null)
    .map((o) => ({
      lat: o.lat as number,
      lon: o.lon as number,
      label: `${formatDateTime(o.capturedAt)} — ${o.recordingFilename ?? ''}`,
      kind: 'plate' as const,
    }))

  return (
    <div className="space-y-4">
      <PageHeader
        title={p.displayText}
        subtitle={
          <span className="flex flex-wrap items-center gap-2">
            <ConfidenceBadge confidence={p.bestConfidence} />
            {p.patternName ? (
              <span className="text-xs">matched {p.patternName}{p.region ? ` (${p.region})` : ''}</span>
            ) : (
              <span className="text-xs text-state-warn">
                did not match a known Australian plate format
              </span>
            )}
          </span>
        }
        actions={
          <button
            className={p.flagged ? 'btn btn-danger' : 'btn'}
            onClick={() => patch.mutate({ flagged: !p.flagged })}
            disabled={patch.isPending}
          >
            {p.flagged ? 'Remove flag' : 'Flag this plate'}
          </button>
        }
      />

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <StatTile label="First seen" value={<span className="text-base">{formatDateTime(p.firstSeenAt)}</span>} />
        <StatTile label="Last seen" value={<span className="text-base">{formatDateTime(p.lastSeenAt)}</span>} />
        <StatTile label="Sightings" value={p.observationCount} />
        <StatTile label="Journeys" value={p.journeyCount} />
      </div>

      {markers.length > 0 ? (
        <RouteMap markers={markers} className="h-80 w-full" />
      ) : (
        <EmptyState
          title="No mapped sightings"
          description="None of this plate's sightings have a GPS fix, so there is nothing to place on a map."
        />
      )}

      <section className="card p-3">
        <label className="label mb-1 block text-xs">Notes</label>
        <textarea
          className="input min-h-[4rem]"
          value={notes ?? p.notes ?? ''}
          onChange={(e) => setNotes(e.target.value)}
          placeholder="Anything worth remembering about this plate"
        />
        {notes !== null && notes !== (p.notes ?? '') && (
          <button
            className="btn btn-primary mt-2"
            onClick={() => patch.mutate({ notes })}
            disabled={patch.isPending}
          >
            Save notes
          </button>
        )}
      </section>

      <section>
        <h2 className="mb-2 text-sm font-semibold">Every sighting</h2>
        {observations.isLoading && <Spinner className="py-10" />}
        {items.length === 0 && !observations.isLoading && (
          <EmptyState title="No sightings recorded" />
        )}

        <div className="space-y-2">
          {items.map((o) => (
            <div key={o.id} className="card flex flex-wrap items-start gap-3 p-3">
              <div className="flex shrink-0 gap-2">
                {mediaUrl(o.plateCropPath) && (
                  <img src={mediaUrl(o.plateCropPath)} alt="Plate crop" className="h-12 rounded border border-border object-contain" />
                )}
                {mediaUrl(o.vehicleCropPath) && (
                  <img src={mediaUrl(o.vehicleCropPath)} alt="Vehicle" className="h-12 w-20 rounded object-cover" />
                )}
              </div>

              <div className="min-w-0 flex-1 space-y-1">
                <div className="flex flex-wrap items-center gap-2">
                  <PlateText text={o.normalisedText} confidence={o.ocrConfidence} />
                  {/* The untouched OCR output is always kept and shown: normalisation is a
                      best guess, and the reader deserves to see what was actually read. */}
                  {o.rawText && o.rawText !== o.normalisedText && (
                    <span className="tabular text-xs text-content-faint">
                      raw OCR: <span className="font-mono">{o.rawText}</span>
                    </span>
                  )}
                  {o.voteCount > 1 && (
                    <span className="badge bg-surface-sunken text-content-muted">
                      {o.voteCount} reads agreed
                    </span>
                  )}
                </div>
                <div className="tabular flex flex-wrap gap-x-4 gap-y-0.5 text-xs text-content-muted">
                  <span>{formatDateTime(o.capturedAt)}</span>
                  <span>{formatCoords(o.lat, o.lon)}</span>
                  {o.cameraName && <span>{o.cameraName}</span>}
                  <span>detection {Math.round(o.detectionConfidence * 100)}%</span>
                </div>
                <div className="flex flex-wrap gap-3 text-xs">
                  <Link
                    to={`/recordings/${o.recordingId}?t=${o.tOffsetS}`}
                    className="text-accent hover:underline"
                  >
                    Jump to this moment
                  </Link>
                  {o.journeyId && (
                    <Link to={`/journeys/${o.journeyId}`} className="text-accent hover:underline">
                      View journey
                    </Link>
                  )}
                </div>
              </div>
            </div>
          ))}
        </div>

        {observations.data && (
          <Pagination
            page={observations.data.page}
            pages={observations.data.pages}
            total={observations.data.total}
            onChange={setPage}
          />
        )}
      </section>
    </div>
  )
}
