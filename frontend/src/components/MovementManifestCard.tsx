import { useQuery } from '@tanstack/react-query'

import { api } from '@/lib/api'
import type { MovementManifestStatus } from '@/lib/api'
import { formatBytes, formatRelative } from '@/lib/format'

const classes = [
  ['movingObserved', 'Movement observed'],
  ['sampledStationary', 'No movement in sampled readings'],
  ['accOffCandidate', 'Ignition-off candidate'],
  ['unknown', 'Insufficient evidence'],
] as const

const setupMessages: Record<string, string> = {
  directory_required: 'Open the companion on the dashcam and select the memory card’s DCIM folder with read-only access.',
  directory_unavailable: 'The selected footage folder is unavailable. Reconnect the card or select its DCIM folder again in the companion.',
  storage_unavailable: 'The companion cannot publish a report until the memory card is available.',
  scan_failed: 'The companion could not finish reading clip metadata. Footage remains eligible for normal backup.',
}

export function MovementManifestEvidence({ data, loading = false, error = false }: {
  data?: MovementManifestStatus
  loading?: boolean
  error?: boolean
}) {
  const current = !error && data?.state === 'ready' && data.reportStatus === 'ready'
  const summary = current ? data.inventory : null
  const setupMessage = data?.state === 'ready' && data.reportStatus ? setupMessages[data.reportStatus] : undefined
  return (
    <section className="card mb-6 px-5 py-4" aria-label="On-camera movement reports">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-base font-semibold">On-camera movement</h2>
        <span className="text-xs font-medium text-content-muted">Report only</span>
      </div>
      <p className="mt-1 text-sm text-content-muted">
        Small clip summaries use ignition and movement readings recorded on the dashcam.
        Every recording remains eligible for backup, including stationary candidates.
      </p>
      {summary ? (
        <>
          <dl className="mt-4 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            {classes.map(([key, label]) => (
              <div key={key}>
                <dt className="text-xs text-content-muted">{label}</dt>
                <dd className="mt-1 text-lg font-semibold">{summary.classCounts[key] ?? 0}</dd>
                <dd className="text-xs text-content-faint">{formatBytes(summary.classBytes[key] ?? 0)}</dd>
              </div>
            ))}
          </dl>
          <p className="mt-3 text-xs text-content-muted">
            {summary.matchedClips} of {summary.clipCount} inventoried clips matched a report.
            {' '}Missing or changed clips remain unknown. Sampled stillness does not rule out an event between readings.
          </p>
          {!data?.cardIdentityVerified && <p className="mt-2 text-xs text-content-muted">The selected card could not be matched to the backup source. Its current classifications remain unknown.</p>}
        </>
      ) : (
        <p className="mt-3 text-sm text-content-muted" role="status">
          {loading && !data ? 'Checking for movement reports…'
            : error ? 'Movement report refresh failed. Current classifications are unavailable; normal backup continues.'
              : setupMessage ?? (data?.state === 'stale'
                ? data.requiresLiveVerification
                  ? 'The saved report needs a fresh check against the dashcam. Current classifications are unavailable; normal backup continues.'
                  : 'The last movement report is stale. Current classifications are unavailable; normal backup continues.'
                : data?.state === 'invalid' || data?.state === 'error'
                  ? 'The movement report could not be verified. Normal backup continues.'
                  : 'No usable movement report has been received yet. Older footage without recorded movement evidence remains unknown.')}
        </p>
      )}
      {data?.truncated && <p className="mt-2 text-xs text-state-warn">This report covers part of the card. Other clips remain eligible for backup.</p>}
      {data?.receivedAt && <p className="mt-2 text-xs text-content-faint">Last report received {formatRelative(data.receivedAt)}{data.producerVersion ? ` · companion ${data.producerVersion}` : ''}.</p>}
    </section>
  )
}

export function MovementManifestCard() {
  const report = useQuery({
    queryKey: ['movement-manifest-status'],
    queryFn: api.ingest.movementStatus,
    refetchInterval: 15_000,
    retry: 1,
  })
  return <MovementManifestEvidence data={report.data} loading={report.isPending} error={report.isError} />
}
