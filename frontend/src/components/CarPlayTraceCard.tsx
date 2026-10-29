import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '@/lib/api'
import { formatDateTime } from '@/lib/format'

export default function CarPlayTraceCard() {
  const client = useQueryClient()
  const state = useQuery({
    queryKey: ['carplay-trace'], queryFn: api.ingest.carplayTraceStatus,
    refetchInterval: 5_000, retry: 1,
  })
  const refresh = () => { void client.invalidateQueries({ queryKey: ['carplay-trace'] }) }
  const start = useMutation({ mutationFn: () => api.ingest.startCarplayTrace(), onSuccess: refresh })
  const stop = useMutation({ mutationFn: api.ingest.stopCarplayTrace, onSuccess: refresh })
  const busy = start.isPending || stop.isPending
  const requested = state.data?.requested === true
  return (
    <section className="card px-5 py-4" aria-label="Heavy CarPlay diagnostics">
      <h2 className="text-base font-semibold">Heavy CarPlay diagnostics</h2>
      <p className="mt-1 text-sm text-content-muted">Off during normal driving. Short codec traces add diagnostic work and may affect performance. Passive frame, GPS and temperature observations continue separately.</p>
      <p className="mt-2 text-sm" role="status">
        {state.isError ? 'Diagnostic request status is unavailable.'
          : state.isPending ? 'Checking diagnostic request…'
            : requested && state.data?.expiresAt ? `Requested until ${formatDateTime(state.data.expiresAt)}`
              : 'Heavy tracing is not requested.'}
      </p>
      {state.data && !state.data.passiveEnabled && <p className="mt-1 text-sm text-content-muted">Enable passive CarPlay timing and unit logs in Settings before requesting a trace.</p>}
      <div className="mt-3 flex flex-wrap gap-2">
        <button type="button" className="btn" disabled={busy || state.isPending || state.isError || !state.data?.passiveEnabled || requested} onClick={() => start.mutate()}>
          {start.isPending ? 'Requesting…' : 'Enable for 15 minutes'}
        </button>
        <button type="button" className="btn" disabled={busy || !requested} onClick={() => stop.mutate()}>
          {stop.isPending ? 'Stopping request…' : 'Stop heavy tracing'}
        </button>
      </div>
      {(start.isError || stop.isError) && <p className="mt-2 text-sm text-state-error" role="alert">{(start.error ?? stop.error)?.message ?? 'The diagnostic request could not be changed.'}</p>}
      <p className="mt-2 text-xs text-content-faint">The camera applies requests when reachable. Captured events confirm whether a trace ran. Requests expire automatically, including while offline; enabling diagnostics does not keep the camera awake.</p>
    </section>
  )
}
