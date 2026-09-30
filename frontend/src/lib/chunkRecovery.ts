const RELOAD_FLAG = 'dashcam:chunk-reloaded'
type ReloadStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>

export function isStaleChunk(error: unknown): boolean {
  const message = error instanceof Error ? `${error.name}: ${error.message}` : String(error ?? '')
  return /dynamically imported module|module script failed|ChunkLoadError|Loading chunk \S+ failed/i.test(message)
}

/** Claim the single automatic reload before navigating; unavailable storage means no reload. */
export function claimChunkReload(storage?: ReloadStorage): boolean {
  try {
    const target = storage ?? globalThis.sessionStorage
    if (target.getItem(RELOAD_FLAG) !== null) return false
    target.setItem(RELOAD_FLAG, '1')
    return true
  } catch { return false }
}

export function clearChunkReload(storage?: ReloadStorage): void {
  try { (storage ?? globalThis.sessionStorage).removeItem(RELOAD_FLAG) } catch { /* blocked storage cannot trigger reloads */ }
}
