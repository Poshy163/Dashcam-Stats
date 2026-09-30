import type { Recording } from '@/lib/types'

export interface PlayableClip {
  recording: Recording
  durationS: number
  startedAtMs: number
  timelineStartS: number
  timelineEndS: number
}

function playableDuration(recording: Recording): number {
  if (recording.fileMissing || !recording.startedAt) return 0
  const duration = recording.durationS ?? (
    recording.endedAt
      ? (Date.parse(recording.endedAt) - Date.parse(recording.startedAt)) / 1000
      : 0
  )
  return Number.isFinite(duration) && duration > 0 ? duration : 0
}

/**
 * Put the surviving files for one camera back-to-back.
 *
 * The journey's stored duration is intentionally a wall-clock envelope: route and OBD
 * matching need to know that a drive ran from 12:12 to 12:37 even after retention removes
 * parked footage. Playback needs the opposite representation. A deleted 18-minute gap is
 * not 18 minutes a viewer should have to scrub through.
 */
export function buildPlayableTimeline(recordings: Recording[], camera: string): PlayableClip[] {
  let cursor = 0
  return recordings
    .filter((recording) => (recording.camera?.role ?? 'other') === camera)
    .map((recording) => ({
      recording,
      durationS: playableDuration(recording),
      startedAtMs: Date.parse(recording.startedAt ?? ''),
    }))
    .filter((item) => item.durationS > 0 && Number.isFinite(item.startedAtMs))
    .sort((a, b) => a.startedAtMs - b.startedAtMs)
    .map((item) => {
      const clip = {
        ...item,
        timelineStartS: cursor,
        timelineEndS: cursor + item.durationS,
      }
      cursor = clip.timelineEndS
      return clip
    })
}

/** Playable duration per camera, so front and rear copies are never counted together. */
export function availableFootageDurations(recordings: Recording[]): Record<string, number> {
  const cameras = new Set(recordings.map((recording) => recording.camera?.role ?? 'other'))
  const durations: Record<string, number> = {}
  for (const camera of cameras) {
    const timeline = buildPlayableTimeline(recordings, camera)
    const duration = timeline.at(-1)?.timelineEndS ?? 0
    if (duration > 0) durations[camera] = duration
  }
  return durations
}

/** Resolve a camera change by capture time, including explicit nearest-footage fallback. */
export function playbackAtTimestamp(timeline: PlayableClip[], timestampMs: number) {
  if (!timeline.length || !Number.isFinite(timestampMs)) return null
  let clipIndex = timeline.findIndex((clip) => timestampMs >= clip.startedAtMs && timestampMs < clip.startedAtMs + clip.durationS * 1000)
  const exact = clipIndex >= 0
  if (!exact) {
    let distance = Infinity
    timeline.forEach((clip, index) => {
      const end = clip.startedAtMs + clip.durationS * 1000
      const candidate = Math.max(clip.startedAtMs - timestampMs, timestampMs - end, 0)
      // Prefer later footage in a tie, rather than the last frame before a missing span.
      if (candidate <= distance) { distance = candidate; clipIndex = index }
    })
  }
  const clip = timeline[clipIndex]!
  const offsetS = Math.max(0, Math.min(clip.durationS - 0.001, (timestampMs - clip.startedAtMs) / 1000))
  return { clipIndex, offsetS, elapsedS: clip.timelineStartS + offsetS, timestampMs: clip.startedAtMs + offsetS * 1000, exact }
}
