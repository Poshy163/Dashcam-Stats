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
  driving_or_power_unknown: 'Full clip analysis is waiting for confirmed parked conditions. Power and movement observations continue.',
  ingestion_deferred: 'Clip indexing is paused for backup coordination. Unverified footage remains eligible for normal backup.',
}

const analysisReasons: Record<string, string> = {
  cpuTemperatureGuard: 'Processor temperature guard prevented a complete analysis',
  cpuTemperatureHardLimit: 'Processor temperature reached the analysis hard stop',
  batteryTemperatureGuard: 'Battery at or above the 42°C analysis limit',
  systemThermalGuard: 'Android thermal protection active',
  cpuTemperatureUnavailable: 'Processor temperature probe unavailable',
  batteryTemperatureUnavailable: 'Battery temperature probe unavailable',
  systemThermalUnavailable: 'Android thermal status unavailable',
  memoryProbeUnavailable: 'Memory status unavailable',
  memoryGuard: 'Insufficient free memory',
  policyNotPermitted: 'Recording or power safety check blocked analysis',
  hardwareDecoderUnavailable: 'Compatible hardware decoder unavailable',
  sourceUnavailable: 'Clip could not be opened',
  formatUnsupported: 'Video format unsupported',
  decoderFailure: 'Video decoder failed',
  imageUnavailable: 'Decoded image unavailable',
  frameQuality: 'Image too dark, bright or low contrast',
  coverageIncomplete: 'Full clip coverage could not be proved',
  analysisTimeout: 'Analysis time limit reached',
  frameLimit: 'Analysis frame limit reached',
  clipChanged: 'Clip changed during analysis',
  backgroundPriorityUnavailable: 'Background decoder priority unavailable',
  analysisStorageUnavailable: 'Analysis result could not be saved',
  resourceCooldown: 'Cooling before a bounded analysis retry',
  unrecognizedReason: 'Companion reported an unrecognized analysis reason',
  reasonNotReported: 'Companion did not report the refusal reason',
}

export function MovementManifestEvidence({ data, loading = false, error = false }: {
  data?: MovementManifestStatus
  loading?: boolean
  error?: boolean
}) {
  const current = !error && data?.state === 'ready' && data.reportStatus === 'ready'
  const summary = current ? data.inventory : null
  const setupMessage = data?.state === 'ready' && data.reportStatus ? setupMessages[data.reportStatus] : undefined
  const exclusions = !error ? data?.intentionalExclusions : undefined
  const filtering = exclusions?.policyEnabled === true
  const currentExclusions = filtering && !exclusions.stale && exclusions.status === 'ready'
  const analysis = !error ? data?.policyAnalysis : undefined
  const refusalCounts = Object.entries(analysis?.reasonCounts ?? {}).filter(([reason, count]) =>
    analysisReasons[reason] && count > 0)
  return (
    <section className="card mb-6 px-5 py-4" aria-label="On-camera movement reports">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-base font-semibold">On-camera movement</h2>
        <span className="text-xs font-medium text-content-muted">{filtering ? 'Quiet-clip filtering enabled' : 'Report only'}</span>
      </div>
      <p className="mt-1 text-sm text-content-muted">
        Small clip summaries use ignition and movement readings recorded on the dashcam.
        {' '}These movement labels alone never exclude footage from backup.
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
      {exclusions && <div className="mt-4 border-t border-line pt-4" aria-label="Quiet footage transfer decisions">
        <h3 className="text-sm font-semibold">Quiet parked footage</h3>
        <p className="mt-1 text-sm text-content-muted">
          {currentExclusions
            ? `${exclusions.currentCount} clips (${formatBytes(exclusions.currentBytes)}) excluded in the latest transfer plan.`
            : filtering
              ? 'Waiting for a fresh, verified quiet-clip transfer plan. Unverified footage uploads normally.'
              : 'Skipping is disabled. All footage remains eligible for backup.'}
        </p>
        <p className="mt-2 text-xs text-content-muted">Both camera views must pass the visual check with ignition off. Activity, locked clips, darkness and incomplete checks upload normally. Excluded files stay on the memory card and are not counted as backed up or sent for processing.</p>
        <p className="mt-2 text-xs text-content-faint">This experimental scene-change check can miss very small or distant activity.</p>
        {analysis && refusalCounts.length > 0 && <div className="mt-3 text-xs text-content-muted" aria-label="Visual analysis diagnostics">
          <p>{analysis.stale || analysis.status !== 'ready' ? 'Last reported visual checks' : 'Latest visual checks'}: footage without a complete quiet proof uploads normally.</p>
          <ul className="mt-1 list-disc pl-4">{refusalCounts.map(([reason, count]) =>
            <li key={reason}>{analysisReasons[reason]}: {count} {count === 1 ? 'clip' : 'clips'}</li>)}</ul>
          {analysis.checkedAt && <p className="mt-1 text-content-faint">Checked {formatRelative(analysis.checkedAt)}{analysis.stale ? ' · saved diagnostics, current camera conditions unverified' : ''}.</p>}
        </div>}
        {currentExclusions && exclusions.locallyIndexedCount > 0 && <p className="mt-2 text-xs text-content-muted">{exclusions.locallyIndexedCount} of these clips also have a local copy from another transfer and can be processed normally.</p>}
        {exclusions.historicalCount > 0 && <p className="mt-2 text-xs text-content-faint">Saved exclusion decisions cover {exclusions.historicalCount} unique clips ({formatBytes(exclusions.historicalBytes)}). This history is separate from the current card inventory.</p>}
      </div>}
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
