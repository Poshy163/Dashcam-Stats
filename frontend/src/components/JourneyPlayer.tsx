import { useCallback, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'

import { api, type OBDSeriesSample } from '@/lib/api'
import { formatClock, formatDateTime, formatDuration, formatSpeed } from '@/lib/format'
import { buildPlayableTimeline, playbackAtTimestamp } from '@/lib/journeyPlayback'
import { ErrorState } from '@/components/ui'
import type { JourneyDetail, Recording } from '@/lib/types'

function sampleAt(samples: OBDSeriesSample[], timestampMs: number): OBDSeriesSample | null {
  let low = 0
  let high = samples.length - 1
  while (low <= high) {
    const middle = (low + high) >> 1
    if (Date.parse(samples[middle]!.t) <= timestampMs) low = middle + 1
    else high = middle - 1
  }
  if (high < 0) return null
  const sample = samples[high]!
  // Do not hold a value through an OBD outage and make it look live.
  return timestampMs - Date.parse(sample.t) <= 10_000 ? sample : null
}

function roleName(recording: Recording): string {
  const role = recording.camera?.role
  return role ? role[0]!.toUpperCase() + role.slice(1) : recording.camera?.name ?? 'Camera'
}

/** A journey-scale player backed by the original minute clips.
 *
 * Loading one enormous generated file would make first play expensive and duplicate footage
 * on disk. This presents the surviving clips as one continuous timeline instead: reaching
 * an edge (or scrubbing anywhere) selects the right file and offset automatically. Original
 * timestamps are retained separately so the OBD overlay remains synchronized when a deleted
 * parked interval is skipped.
 */
export default function JourneyPlayer({ journey, driveId }: { journey: JourneyDetail; driveId?: string }) {
  const video = useRef<HTMLVideoElement>(null)
  const pendingOffset = useRef(0)
  const cameras = useMemo(() => {
    const roles = new Set(journey.recordings.map((recording) => recording.camera?.role ?? 'other'))
    return Array.from(roles).filter((role) => buildPlayableTimeline(journey.recordings, role).length > 0)
  }, [journey.recordings])
  const [camera, setCamera] = useState(cameras.includes('front') ? 'front' : cameras[0] ?? 'other')
  const timeline = useMemo(
    () => buildPlayableTimeline(journey.recordings, camera),
    [camera, journey.recordings],
  )
  const duration = timeline.at(-1)?.timelineEndS ?? 0
  const [clipIndex, setClipIndex] = useState(0)
  const [elapsed, setElapsed] = useState(0)
  const [absoluteTimestampMs, setAbsoluteTimestampMs] = useState(timeline[0]?.startedAtMs ?? 0)
  const [continuePlaying, setContinuePlaying] = useState(false)
  const [cameraNotice, setCameraNotice] = useState<string | null>(null)
  const [playbackError, setPlaybackError] = useState(false)
  const segment = timeline[clipIndex]
  const clip = segment?.recording
  // Keep the exact samples needed for this clip and the 10-second lookback used by
  // sampleAt. Fetching a whole multi-hour drive for each player wastes memory and time.
  const windowStart = segment ? new Date(segment.startedAtMs - 10_000).toISOString() : undefined
  const windowEnd = segment ? new Date(segment.startedAtMs + segment.durationS * 1000).toISOString() : undefined

  const series = useQuery({
    queryKey: ['obd-series', driveId, windowStart, windowEnd],
    queryFn: () => api.obd.driveSeries(driveId!, { start: windowStart, end: windowEnd, signals: 'vehicle_speed,engine_rpm,engine_load', diagnosticPageSize: 1 }),
    enabled: Boolean(driveId && segment),
    staleTime: 300_000,
  })
  const obd = useMemo(
    () => sampleAt(series.data?.samples ?? [], absoluteTimestampMs),
    [absoluteTimestampMs, series.data?.samples],
  )

  const seekFootage = useCallback((target: number, play = true) => {
    if (timeline.length === 0) return
    const bounded = Math.max(0, Math.min(duration, target))
    let next = timeline.findIndex((item) => bounded >= item.timelineStartS && bounded < item.timelineEndS)
    if (next < 0) next = timeline.length - 1
    const item = timeline[next]!
    const offset = Math.max(0, Math.min(item.durationS, bounded - item.timelineStartS))
    pendingOffset.current = offset
    setContinuePlaying(play)
    setClipIndex(next)
    setElapsed(bounded)
    setAbsoluteTimestampMs(item.startedAtMs + offset * 1000)
    if (next === clipIndex && video.current) {
      video.current.currentTime = offset
      if (play) void video.current.play().catch(() => undefined)
    }
  }, [clipIndex, duration, timeline])

  const switchCamera = (role: typeof camera) => {
    if (role === camera) return
    const captureTime = segment && video.current ? segment.startedAtMs + Math.min(segment.durationS, video.current.currentTime) * 1000 : absoluteTimestampMs
    const target = playbackAtTimestamp(buildPlayableTimeline(journey.recordings, role), captureTime)
    if (!target) return
    setContinuePlaying(target.exact && video.current?.paused === false)
    pendingOffset.current = target.offsetS
    setCamera(role)
    setClipIndex(target.clipIndex)
    setElapsed(target.elapsedS)
    setAbsoluteTimestampMs(target.timestampMs)
    setPlaybackError(false)
    setCameraNotice(target.exact ? null : `This angle has no footage at ${formatDateTime(new Date(captureTime).toISOString())}. Paused at the nearest available moment: ${formatDateTime(new Date(target.timestampMs).toISOString())}.`)
  }

  if (!clip || !segment) return null

  return (
    <section className="card overflow-hidden" aria-label="Journey footage">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border p-3">
        <div>
          <h2 className="font-semibold">Journey playback</h2>
          <p className="text-xs text-content-muted">
            {formatDuration(duration)} available · deleted gaps skipped · OBD kept in sync
          </p>
        </div>
        {cameras.length > 1 && (
          <div className="flex rounded-lg bg-surface-sunken p-1" aria-label="Camera angle">
            {cameras.map((role) => (
              <button key={role} aria-pressed={camera === role} className={`rounded-md px-3 py-1.5 text-xs font-semibold ${camera === role ? 'bg-surface-raised shadow-sm' : 'text-content-muted'}`} onClick={() => switchCamera(role)}>
                {roleName(journey.recordings.find((r) => (r.camera?.role ?? 'other') === role)!)}
              </button>
            ))}
          </div>
        )}
      </div>
      {cameraNotice && <p className="border-b border-border p-3 text-sm text-state-warn" role="status">{cameraNotice}</p>}
      {series.isError && <ErrorState title="Could not load the playback telemetry" error={series.error} retry={() => void series.refetch()} />}
      {playbackError && <ErrorState title="Could not play this clip" error={new Error('The video could not be loaded or decoded. Try the recording detail or another camera angle.')} retry={() => { setPlaybackError(false); video.current?.load() }} />}
      <div className="relative bg-black">
        <video
          ref={video}
          key={clip.id}
          className="aspect-video w-full"
          src={api.recordings.streamUrl(clip.id)}
          controls
          preload="metadata"
          playsInline
          onLoadedMetadata={(event) => {
            setPlaybackError(false)
            event.currentTarget.currentTime = Math.min(pendingOffset.current, event.currentTarget.duration)
            if (continuePlaying) void event.currentTarget.play().catch(() => undefined)
          }}
          onError={() => setPlaybackError(true)}
          onTimeUpdate={(event) => {
            const offset = Math.min(segment.durationS, event.currentTarget.currentTime)
            setElapsed(Math.min(duration, segment.timelineStartS + offset))
            setAbsoluteTimestampMs(segment.startedAtMs + offset * 1000)
          }}
          onEnded={() => {
            if (clipIndex < timeline.length - 1) seekFootage(segment.timelineEndS + 0.001)
            else {
              setContinuePlaying(false)
              setElapsed(duration)
            }
          }}
        />
        <div className="pointer-events-none absolute left-3 top-3 flex gap-2">
          <span className="rounded-md bg-black/75 px-2 py-1 text-xs font-semibold text-white">{roleName(clip)}</span>
          <span className="rounded-md bg-black/75 px-2 py-1 text-xs tabular text-white">clip {clipIndex + 1} / {timeline.length}</span>
        </div>
        {driveId && (
          <div className="absolute bottom-14 right-3 min-w-36 rounded-xl border border-white/20 bg-black/80 p-3 text-white backdrop-blur-sm">
            <div className="text-3xl font-bold tabular">{obd?.vehicleSpeedKmh != null ? formatSpeed(obd.vehicleSpeedKmh) : '—'}</div>
            <div className="mt-1 flex gap-3 text-xs text-white/70">
              <span>{series.isLoading ? 'Loading OBD…' : obd?.engineRpm != null ? `${Math.round(obd.engineRpm).toLocaleString()} rpm` : 'No live OBD sample'}</span>
              {obd?.engineLoadPct != null && <span>{Math.round(obd.engineLoadPct)}% load</span>}
            </div>
          </div>
        )}
      </div>
      <div className="space-y-2 p-3">
        <input
          className="w-full accent-accent"
          type="range"
          min={0}
          max={duration || 1}
          step={0.1}
          value={elapsed}
          aria-label="Journey timeline"
          onChange={(event) => { setCameraNotice(null); seekFootage(Number(event.target.value), video.current?.paused === false) }}
        />
        <div className="flex justify-between text-xs text-content-muted">
          <span className="tabular">{formatClock(elapsed)} / {formatClock(duration)}</span>
          <span>{absoluteTimestampMs ? formatDateTime(new Date(absoluteTimestampMs).toISOString()) : '—'}</span>
        </div>
      </div>
    </section>
  )
}
