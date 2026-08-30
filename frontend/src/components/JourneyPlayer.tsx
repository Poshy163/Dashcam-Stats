import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'

import { api, type OBDSeriesSample } from '@/lib/api'
import { formatClock, formatDateTime, formatSpeed } from '@/lib/format'
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
 * on disk. This presents the clips as one wall-clock timeline instead: reaching an edge (or
 * scrubbing anywhere in the journey) selects the right file and offset automatically.
 */
export default function JourneyPlayer({ journey, driveId }: { journey: JourneyDetail; driveId?: string }) {
  const video = useRef<HTMLVideoElement>(null)
  const journeyStart = Date.parse(journey.startedAt)
  const duration = Math.max(0, (Date.parse(journey.endedAt) - journeyStart) / 1000)
  const cameras = useMemo(
    () => Array.from(new Set(journey.recordings.map((r) => r.camera?.role ?? 'other'))),
    [journey.recordings],
  )
  const [camera, setCamera] = useState(cameras.includes('front') ? 'front' : cameras[0] ?? 'other')
  const clips = useMemo(
    () => journey.recordings
      .filter((r) => (r.camera?.role ?? 'other') === camera && !r.fileMissing && r.startedAt)
      .sort((a, b) => Date.parse(a.startedAt!) - Date.parse(b.startedAt!)),
    [camera, journey.recordings],
  )
  const [clipIndex, setClipIndex] = useState(0)
  const [elapsed, setElapsed] = useState(0)
  const [continuePlaying, setContinuePlaying] = useState(false)
  const clip = clips[clipIndex]

  const series = useQuery({
    queryKey: ['obd-series', driveId],
    queryFn: () => api.obd.driveSeries(driveId!),
    enabled: Boolean(driveId),
    staleTime: 300_000,
  })
  const obd = useMemo(
    () => sampleAt(series.data?.samples ?? [], journeyStart + elapsed * 1000),
    [elapsed, journeyStart, series.data?.samples],
  )

  const seekJourney = useCallback((target: number, play = true) => {
    if (clips.length === 0) return
    const bounded = Math.max(0, Math.min(duration, target))
    let next = clips.findIndex((item) => {
      const start = (Date.parse(item.startedAt!) - journeyStart) / 1000
      return bounded >= start && bounded < start + (item.durationS ?? 0)
    })
    // A camera can miss a segment. Move to the next available clip rather than freezing.
    if (next < 0) next = clips.findIndex((item) => Date.parse(item.startedAt!) >= journeyStart + bounded * 1000)
    if (next < 0) next = clips.length - 1
    const item = clips[next]!
    const offset = Math.max(0, bounded - (Date.parse(item.startedAt!) - journeyStart) / 1000)
    setContinuePlaying(play)
    setClipIndex(next)
    setElapsed(bounded)
    requestAnimationFrame(() => {
      if (!video.current) return
      video.current.currentTime = Math.min(offset, item.durationS ?? offset)
      if (play) void video.current.play().catch(() => undefined)
    })
  }, [clips, duration, journeyStart])

  useEffect(() => {
    setClipIndex(0)
    seekJourney(elapsed, false)
  // The current elapsed time is deliberately retained while switching cameras.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [camera])

  if (!clip) return null
  const clipStart = (Date.parse(clip.startedAt!) - journeyStart) / 1000

  return (
    <section className="card overflow-hidden" aria-label="Journey footage">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border p-3">
        <div>
          <h2 className="font-semibold">Journey playback</h2>
          <p className="text-xs text-content-muted">All clips on one synchronized timeline</p>
        </div>
        {cameras.length > 1 && (
          <div className="flex rounded-lg bg-surface-sunken p-1" aria-label="Camera angle">
            {cameras.map((role) => (
              <button key={role} className={`rounded-md px-3 py-1.5 text-xs font-semibold ${camera === role ? 'bg-surface-raised shadow-sm' : 'text-content-muted'}`} onClick={() => setCamera(role)}>
                {roleName(journey.recordings.find((r) => (r.camera?.role ?? 'other') === role)!)}
              </button>
            ))}
          </div>
        )}
      </div>
      <div className="relative bg-black">
        <video
          ref={video}
          key={clip.id}
          className="aspect-video w-full"
          src={api.recordings.streamUrl(clip.id)}
          controls
          preload="metadata"
          playsInline
          onLoadedMetadata={() => { if (continuePlaying) void video.current?.play().catch(() => undefined) }}
          onTimeUpdate={(event) => setElapsed(Math.min(duration, clipStart + event.currentTarget.currentTime))}
          onEnded={() => seekJourney(clipStart + (clip.durationS ?? 0) + 0.05)}
        />
        <div className="pointer-events-none absolute left-3 top-3 flex gap-2">
          <span className="rounded-md bg-black/75 px-2 py-1 text-xs font-semibold text-white">{roleName(clip)}</span>
          <span className="rounded-md bg-black/75 px-2 py-1 text-xs tabular text-white">clip {clipIndex + 1} / {clips.length}</span>
        </div>
        {driveId && (
          <div className="absolute bottom-14 right-3 min-w-36 rounded-xl border border-white/20 bg-black/80 p-3 text-white backdrop-blur-sm">
            <div className="text-3xl font-bold tabular">{obd?.vehicleSpeedKmh != null ? formatSpeed(obd.vehicleSpeedKmh) : '—'}</div>
            <div className="mt-1 flex gap-3 text-xs text-white/70">
              <span>{obd?.engineRpm != null ? `${Math.round(obd.engineRpm).toLocaleString()} rpm` : 'No live OBD sample'}</span>
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
          onChange={(event) => seekJourney(Number(event.target.value), false)}
        />
        <div className="flex justify-between text-xs text-content-muted">
          <span className="tabular">{formatClock(elapsed)} / {formatClock(duration)}</span>
          <span>{formatDateTime(new Date(journeyStart + elapsed * 1000).toISOString())}</span>
        </div>
      </div>
    </section>
  )
}
