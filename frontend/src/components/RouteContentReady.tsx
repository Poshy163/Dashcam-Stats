import { useEffect, type ReactNode } from 'react'
import { clearChunkReload } from '@/lib/chunkRecovery'

/** Mounted by lazyRoute only after its import resolves; a Suspense wrapper is too early. */
export default function RouteContentReady({ children }: { children: ReactNode }) {
  useEffect(() => { clearChunkReload() }, [])
  return children
}
