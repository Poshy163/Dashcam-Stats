import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'

import { api } from '@/lib/api'
import type { MaintenanceWakeStatus } from '@/lib/api'
import { formatDuration } from '@/lib/format'

export function MaintenanceWakeEvidence({ data, error = false, checkedAtMs = Date.now(), nowMs = Date.now() }: {
  data?: MaintenanceWakeStatus
  error?: boolean
  checkedAtMs?: number
  nowMs?: number
}) {
  const age = (nowMs - checkedAtMs) / 1_000
  const remaining = Math.max(0, (data?.remainingS ?? 0) - Math.max(0, age))
  const active = !error && age >= 0 && data?.state === 'active' && data.active && data.verified && remaining > 0 && (data.evidenceValidForS ?? 0) > age
  return (
    <div role="status" className="mt-2 text-sm">
      <p className={active ? 'font-medium text-state-ok' : 'text-content-muted'}>
        {error ? 'Wake-hold status is unavailable.'
          : active ? `Verified wake hold · ${formatDuration(remaining)} remaining`
            : data?.state === 'restoring' ? 'Ending wake hold and restoring radios…'
              : data?.active ? 'Wake hold is not currently verified. Backup safety checks still apply.'
                : 'Normal backup sleep-window checks apply.'}
      </p>
      {data?.reason && <p className="mt-1 text-content-muted">{data.reason}</p>}
      {data?.requiresIgnitionCycle && !active && <p className="mt-1 text-content-muted">A fresh accessory-power ON → OFF cycle is needed to start a new normal sleep countdown.</p>}
    </div>
  )
}

export function MaintenanceWakeCard() {
  const [minutes, setMinutes] = useState(15)
  const client = useQueryClient()
  const state = useQuery({
    queryKey: ['maintenance-wake'], queryFn: api.ingest.maintenanceStatus,
    refetchInterval: 5_000, retry: 1,
  })
  const [now, setNow] = useState(Date.now)
  useEffect(() => {
    if (!state.data?.active) return
    const timer = window.setInterval(() => setNow(Date.now()), 1_000)
    return () => window.clearInterval(timer)
  }, [state.data?.active])
  const refresh = () => {
    void client.invalidateQueries({ queryKey: ['maintenance-wake'] })
    void client.invalidateQueries({ queryKey: ['ingest-status'] })
    void client.invalidateQueries({ queryKey: ['ingest-radio-status'] })
  }
  const start = useMutation({ mutationFn: () => api.ingest.startMaintenance(minutes * 60), onSuccess: refresh })
  const stop = useMutation({ mutationFn: api.ingest.stopMaintenance, onSuccess: refresh })
  const busy = start.isPending || stop.isPending
  const restoring = state.data?.state === 'restoring'
  return (
    <section className="card mb-6 px-5 py-4" aria-label="Maintenance wake hold">
      <h2 className="text-base font-semibold">Keep the dashcam awake</h2>
      <p className="mt-1 text-sm text-content-muted">For maintenance while parked. The server must observe and cancel the dashcam’s native countdown before it can verify a bounded wake hold.</p>
      <MaintenanceWakeEvidence data={state.data} error={state.isError} checkedAtMs={state.dataUpdatedAt} nowMs={Math.max(now, Date.now())} />
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <label className="text-sm" htmlFor="maintenance-duration">Duration</label>
        <select id="maintenance-duration" className="input w-auto" value={minutes} onChange={(event) => setMinutes(Number(event.target.value))} disabled={busy || state.data?.active || restoring}>
          <option value={5}>5 minutes</option><option value={15}>15 minutes</option><option value={30}>30 minutes</option>
        </select>
        <button type="button" className="btn" onClick={() => start.mutate()} disabled={busy || state.isPending || state.isError || state.data?.active || restoring}>
          {start.isPending ? 'Verifying countdown…' : 'Start verified wake hold'}
        </button>
        {(state.data?.active || restoring) && <button type="button" className="btn" onClick={() => stop.mutate()} disabled={busy}>{stop.isPending ? 'Restoring radios…' : restoring ? 'Retry radio restoration' : 'End wake hold'}</button>}
      </div>
      {(start.isError || stop.isError) && <p className="mt-2 text-sm text-state-error" role="alert">{(start.error ?? stop.error)?.message ?? 'The wake hold could not be changed.'}</p>}
      <p className="mt-2 text-xs text-content-faint">Ending a hold restores radio safety first. Cancelling the native countdown keeps this parked session awake until the next accessory-power cycle; ending the server hold does not force sleep.</p>
    </section>
  )
}
