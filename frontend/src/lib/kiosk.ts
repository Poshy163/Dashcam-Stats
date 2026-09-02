/**
 * Whether this page is the head unit's constrained kiosk view.
 *
 * The dashcam's in-dash browser recomposites the whole 1080p surface every frame for as
 * long as any CSS animation runs, which on its weak GPU costs a large fraction of a core
 * indefinitely and steals it from the recorder and CarPlay. The server sends the unit a
 * URL carrying `kiosk=1` (see `origin.backup_url`), and everything gated on this flag drops
 * the perpetual motion — the pulsing status dot, the spinner — so the browser can idle
 * between the few updates a minute it actually needs. Desktop clients never carry the flag.
 *
 * Read once and remembered: the key is redeemed and stripped from the address bar on
 * arrival, and client-side navigation drops the query, so the flag is persisted the first
 * time it is seen. Every access is guarded because `sessionStorage` throws in some embedded
 * WebViews and private modes.
 */
function detectKiosk(): boolean {
  if (typeof window === 'undefined') return false
  let stored = false
  try {
    stored = window.sessionStorage.getItem('kiosk') === '1'
  } catch {
    stored = false
  }
  let flagged = false
  try {
    flagged = new URLSearchParams(window.location.search).has('kiosk')
  } catch {
    flagged = false
  }
  if (flagged && !stored) {
    try {
      window.sessionStorage.setItem('kiosk', '1')
    } catch {
      /* best effort — the flag still holds for this page load via `flagged` */
    }
  }
  return flagged || stored
}

export const KIOSK = detectKiosk()

/** Returns `classes` only outside the kiosk view, so callers can drop perpetual animation. */
export function motion(classes: string): string {
  return KIOSK ? '' : classes
}
