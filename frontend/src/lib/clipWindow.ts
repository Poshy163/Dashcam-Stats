/**
 * A fixed-length export window centred on the playhead.
 *
 * Clamping each edge independently shortened the clip whenever the playhead sat near
 * either end: at 0:00 the old maths produced `start=0&end=15`, a 15-second export sold as
 * 30. The window is now slid back inside the recording so it keeps its full length until
 * the recording itself is shorter than the window, in which case the whole clip is used.
 */
export function clipWindow(
  currentTime: number,
  lengthS: number,
  durationS: number | null | undefined,
): { start: number; end: number | undefined } {
  const half = lengthS / 2
  const safeTime = Number.isFinite(currentTime) ? Math.max(0, currentTime) : 0
  if (durationS === null || durationS === undefined || !(durationS > 0)) {
    return { start: Math.max(0, safeTime - half), end: undefined }
  }
  if (durationS <= lengthS) return { start: 0, end: durationS }
  let start = safeTime - half
  let end = safeTime + half
  if (start < 0) {
    end -= start
    start = 0
  }
  if (end > durationS) {
    start -= end - durationS
    end = durationS
  }
  return { start: Math.max(0, start), end }
}
