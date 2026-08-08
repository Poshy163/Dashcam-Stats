import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'

import Spinner from '@/components/Spinner'
import {
  DerivedHint,
  ErrorState,
  PageHeader,
  PlateText,
  StateBadge,
} from '@/components/ui'
import { api, mediaUrl } from '@/lib/api'
import { cn } from '@/lib/cn'
import {
  formatBytes,
  formatClock,
  formatCoords,
  formatDateTime,
  formatDuration,
  formatSpeed,
} from '@/lib/format'
import type { TelemetryPoint, TrackedObject } from '@/lib/types'

const REPROCESS_OPTIONS = [
  ['everything', 'Everything'],
  ['metadata', 'Metadata only'],
  ['telemetry', 'Telemetry only'],
  ['detection', 'Object detection'],
  ['plates', 'Licence plate detection'],
] as const

const CLASS_COLOUR: Record<string, string> = {
  car: 'bg-state-busy',
  truck: 'bg-state-warn',
  bus: 'bg-state-warn',
  motorcycle: 'bg-state-ok',
  bicycle: 'bg-state-ok',
  person: 'bg-state-error',
}

/** Nearest telemetry sample at or before `t`. Binary search: this runs on every tick. */
function findPoint(points: TelemetryPoint[], t: number): TelemetryPoint | null {
  if (points.length === 0) return null
  let low = 0
  let high = points.length - 1
  let best: TelemetryPoint | null = null
  while (low <= high) {
    const mid = (low + high) >> 1
    const point = points[mid]!
    if (point.tOffsetS <= t) {
      best = point
      low = mid + 1
    } else {
      high = mid - 1
    }
  }
  return best ?? points[0]!
}

export default function RecordingViewer() {
  const { id } = useParams()
  const recordingId = Number(id)
  const [params] = useSearchParams()
  const navigate = useNavigate()
  const client = useQueryClient()
  const videoRef = useRef<HTMLVideoElement>(null)
  const [currentTime, setCurrentTime] = useState(0)
  const [stage, setStage] = useState<string>('everything')

  const recording = useQuery({
    queryKey: ['recording', recordingId],
    queryFn: () => api.recordings.get(recordingId),
    enabled: Number.isFinite(recordingId),
  })
  const telemetry = useQuery({
    queryKey: ['recording-telemetry', recordingId],
    queryFn: () => api.recordings.telemetry(recordingId),
    enabled: Number.isFinite(recordingId),
  })
  const detections = useQuery({
    queryKey: ['recording-detections', recordingId],
    queryFn: () => api.recordings.detections(recordingId),
    enabled: Number.isFinite(recordingId),
  })
  const plates = useQuery({
    queryKey: ['recording-plates', recordingId],
    queryFn: () => api.recordings.plates(recordingId),
    enabled: Number.isFinite(recordingId),
  })

  const reprocess = useMutation({
    mutationFn: () => api.recordings.reprocess(recordingId, [stage]),
    onSuccess: () => {
      client.invalidateQueries({ queryKey: ['recording', recordingId] })
      client.invalidateQueries({ queryKey: ['jobs'] })
      navigate('/queue')
    },
  })

  const seek = useCallback((t: number) => {
    const video = videoRef.current
    if (!video) return
    video.currentTime = Math.max(0, t)
    void video.play().catch(() => undefined)
  }, [])

  // Deep links from plate sightings and journey maps carry ?t=<seconds>.
  const requestedTime = Number(params.get('t') ?? NaN)
  useEffect(() => {
    if (Number.isFinite(requestedTime) && videoRef.current) {
      videoRef.current.currentTime = requestedTime
    }
  }, [requestedTime, recording.data?.id])

  // The browser fires timeupdate several times a second; throttling keeps React from
  // re-rendering the whole panel on every frame.
  useEffect(() => {
    const video = videoRef.current
    if (!video) return
    let last = 0
    const onTime = () => {
      const now = performance.now()
      if (now - last < 250) return
      last = now
      setCurrentTime(video.currentTime)
    }
    video.addEventListener('timeupdate', onTime)
    return () => video.removeEventListener('timeupdate', onTime)
  }, [recording.data?.id])

  const point = useMemo(
    () => findPoint(telemetry.data ?? [], currentTime),
    [telemetry.data, currentTime],
  )

  if (recording.isLoading) return <Spinner label="Loading recording…" className="py-24" />
  if (recording.isError) return <ErrorState error={recording.error} retry={() => recording.refetch()} />
  if (!recording.data) return null

  const r = recording.data
  const duration = r.durationS ?? 0
  const tracks = detections.data ?? []

  return (
    <div className="space-y-4">
      <PageHeader
        title={r.filename}
        subtitle={
          <span className="flex flex-wrap items-center gap-2">
            <StateBadge state={r.state} />
            <span>{r.camera?.name ?? 'Unknown camera'}</span>
            <span>{formatDateTime(r.startedAt)}</span>
            {r.timeFromOsd && (
              <span className="text-xs text-content-faint" title="Time read from the camera's on-screen overlay">
                clock from overlay
              </span>
            )}
          </span>
        }
        actions={
          <>
            <select className="input w-auto" value={stage} onChange={(e) => setStage(e.target.value)}>
              {REPROCESS_OPTIONS.map(([value, label]) => (
                <option key={value} value={value}>{label}</option>
              ))}
            </select>
            <button className="btn" onClick={() => reprocess.mutate()} disabled={reprocess.isPending}>
              Reprocess
            </button>
          </>
        }
      />

      {r.state === 'failed' && r.errorMessage && (
        <div className="card border-state-error/40 p-3">
          <div className="text-sm font-medium text-state-error">Processing failed</div>
          <p className="mt-1 text-sm text-content-muted">{r.errorMessage}</p>
        </div>
      )}

      <div className="grid gap-4 lg:grid-cols-[minmax(0,2fr)_minmax(18rem,1fr)]">
        <div className="space-y-3">
          {r.fileMissing ? (
            <div className="card flex aspect-video items-center justify-center text-sm text-content-muted">
              This recording is no longer on disk.
            </div>
          ) : (
            <video
              ref={videoRef}
              className="w-full rounded-lg bg-black"
              src={api.recordings.streamUrl(r.id)}
              controls
              preload="metadata"
              playsInline
            />
          )}

          <section className="card p-3">
            <div className="mb-2 flex items-baseline justify-between">
              <h2 className="text-sm font-semibold">Detections</h2>
              <span className="tabular text-xs text-content-faint">
                {formatClock(currentTime)} / {formatClock(duration)}
              </span>
            </div>

            {tracks.length === 0 ? (
              <p className="hint">No objects detected in this recording.</p>
            ) : (
              <div className="relative h-14 w-full overflow-hidden rounded bg-surface-sunken">
                {tracks.map((track) => {
                  const left = duration ? (track.firstSeenOffsetS / duration) * 100 : 0
                  const width = duration
                    ? Math.max(0.8, ((track.lastSeenOffsetS - track.firstSeenOffsetS) / duration) * 100)
                    : 1
                  const row = ['car', 'truck', 'bus'].includes(track.classLabel) ? 0 : 1
                  return (
                    <button
                      key={track.id}
                      className={cn(
                        'absolute h-4 rounded-sm opacity-80 transition-opacity hover:opacity-100',
                        CLASS_COLOUR[track.classLabel] ?? 'bg-state-idle',
                      )}
                      style={{ left: `${left}%`, width: `${width}%`, top: row === 0 ? 6 : 30 }}
                      title={`${track.classLabel} · ${formatClock(track.firstSeenOffsetS)}`}
                      onClick={() => seek(track.firstSeenOffsetS)}
                      aria-label={`Seek to ${track.classLabel} at ${formatClock(track.firstSeenOffsetS)}`}
                    />
                  )
                })}
                {duration > 0 && (
                  <div
                    className="pointer-events-none absolute top-0 h-full w-px bg-content"
                    style={{ left: `${(currentTime / duration) * 100}%` }}
                  />
                )}
              </div>
            )}
          </section>
        </div>

        <div className="space-y-3">
          <section className="card p-3">
            <h2 className="mb-2 text-sm font-semibold">Telemetry</h2>
            {telemetry.data && telemetry.data.length > 0 ? (
              <dl className="space-y-1.5 text-sm">
                <Row label="Time" value={point?.capturedAt ? formatDateTime(point.capturedAt) : '—'} />
                <Row
                  label="Position"
                  value={point?.hasFix ? formatCoords(point.lat, point.lon) : 'No GPS fix'}
                />
                <Row label="Speed" value={formatSpeed(point?.speedKmh)} />
                <Row
                  label="Heading"
                  value={
                    point?.headingDeg != null ? (
                      <DerivedHint>{Math.round(point.headingDeg)}°</DerivedHint>
                    ) : (
                      '—'
                    )
                  }
                />
                <Row
                  label="OCR confidence"
                  value={point?.ocrConfidence != null ? `${Math.round(point.ocrConfidence * 100)}%` : '—'}
                />
              </dl>
            ) : (
              <p className="hint">
                No telemetry for this recording. The camera writes GPS as an on-screen
                overlay, so it is only available when that overlay could be read.
              </p>
            )}
          </section>

          <section className="card p-3">
            <h2 className="mb-2 text-sm font-semibold">
              Licence plates {plates.data?.length ? `(${plates.data.length})` : ''}
            </h2>
            {plates.data && plates.data.length > 0 ? (
              <ul className="space-y-2">
                {plates.data.map((observation) => (
                  <li key={observation.id} className="flex items-center gap-2">
                    {mediaUrl(observation.plateCropPath) && (
                      <img
                        src={mediaUrl(observation.plateCropPath)}
                        alt=""
                        className="h-8 rounded border border-border object-contain"
                      />
                    )}
                    <button className="min-w-0 flex-1 text-left" onClick={() => seek(observation.tOffsetS)}>
                      <PlateText text={observation.normalisedText} confidence={observation.ocrConfidence} />
                      <div className="tabular text-2xs text-content-faint">
                        {formatClock(observation.tOffsetS)}
                      </div>
                    </button>
                    <Link to={`/plates/${observation.plateId}`} className="text-xs text-accent">
                      history
                    </Link>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="hint">No plates read in this recording.</p>
            )}
          </section>

          <section className="card p-3">
            <h2 className="mb-2 text-sm font-semibold">Details</h2>
            <dl className="space-y-1.5 text-sm">
              <Row label="Duration" value={formatDuration(r.durationS)} />
              <Row label="Size" value={formatBytes(r.sizeBytes)} />
              <Row label="Codec" value={r.videoCodec ?? '—'} />
              <Row label="Resolution" value={r.width ? `${r.width}×${r.height}` : '—'} />
              <Row label="Frame rate" value={r.fps ? `${r.fps.toFixed(2)} fps` : '—'} />
              <Row label="Audio" value={r.hasAudio ? 'yes' : 'none'} />
              {r.journeyId && (
                <div className="flex justify-between gap-3">
                  <dt className="text-content-muted">Journey</dt>
                  <dd>
                    <Link to={`/journeys/${r.journeyId}`} className="text-accent hover:underline">
                      view
                    </Link>
                  </dd>
                </div>
              )}
            </dl>
          </section>
        </div>
      </div>
    </div>
  )
}

function Row({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="flex justify-between gap-3">
      <dt className="shrink-0 text-content-muted">{label}</dt>
      <dd className="tabular truncate text-right">{value}</dd>
    </div>
  )
}
