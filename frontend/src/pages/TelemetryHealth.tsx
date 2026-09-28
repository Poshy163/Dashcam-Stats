import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'

import Spinner from '@/components/Spinner'
import { EmptyState, ErrorState, PageHeader, StatTile } from '@/components/ui'
import { api } from '@/lib/api'
import { formatDateTime } from '@/lib/format'

const reasonLabels: Record<string, string> = {
  analysis_pending: 'Analysis unfinished',
  analysis_outdated: 'Analysis needs updating',
  no_samples: 'No telemetry samples',
  gps_no_fix: 'Camera reported no fix',
  gps_unreadable: 'GPS text unreadable',
  gps_rejected: 'Rejected positions',
  gps_coverage_incomplete: 'Missing GPS samples',
  telemetry_warnings: 'Telemetry warnings',
}

export default function TelemetryHealth() {
  const query = useQuery({
    queryKey: ['telemetry-quality'],
    queryFn: api.telemetryQuality,
    refetchInterval: 10_000,
  })
  if (query.isLoading) return <Spinner label="Checking telemetry…" className="py-24" />
  if (query.isError) return <ErrorState error={query.error} retry={() => query.refetch()} />
  if (!query.data) return null
  const data = query.data
  const coverage = data.gpsCoverage
  const coveragePercent = coverage.totalPoints > 0
    ? `${(100 * coverage.acceptedPoints / coverage.totalPoints).toFixed(1)}%`
    : 'Unknown'

  return (
    <div className="space-y-4">
      <PageHeader
        title="Telemetry health"
        subtitle={`${data.recordings.toLocaleString()} recordings in the visible library. Front and rear clips count separately.`}
      />
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <StatTile label="Healthy recordings" value={data.healthy} tone="ok" />
        <StatTile label="Degraded recordings" value={data.degraded} tone={data.degraded ? 'warn' : 'default'} />
        <StatTile label="No-fix recordings" value={data.noFix} hint="Every sample reports no GPS fix" />
        <StatTile label="Pending recordings" value={data.pending} tone={data.pending ? 'busy' : 'default'} />
      </div>
      <p className="text-sm text-content-muted">
        Degraded can mean missing GPS samples, telemetry warnings or analysis that needs updating.
        One warning can affect a recording even when every sample has a position.
        {' '}{data.outdatedRecordings} recordings need updated analysis; {data.emptyRecordings} current analyses contain no samples.
      </p>

      <div className="card space-y-3 p-4">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <h2 className="font-medium">GPS sample coverage</h2>
          <span className="tabular text-sm">{coveragePercent} · {coverage.acceptedPoints.toLocaleString()} / {coverage.totalPoints.toLocaleString()} samples have positions</span>
        </div>
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          <StatTile label="Full GPS coverage" value={coverage.fullCoverageRecordings} hint="Recordings; warnings may still apply" />
          <StatTile label="GPS coverage gaps" value={coverage.gapRecordings} hint="Recordings with missing or rejected positions" />
          <StatTile label="Warnings with full GPS" value={coverage.warningOnlyRecordings} hint="Recordings with positions for every sample" />
          <StatTile label="Warning samples" value={coverage.problemSamples.toLocaleString()} hint={`Across ${coverage.warningRecordings} recordings`} />
        </div>
        <p className="text-xs text-content-muted">
          No-fix samples: {coverage.noFixPoints.toLocaleString()} · unreadable GPS samples: {coverage.ocrUnreadablePoints.toLocaleString()} · rejected samples: {coverage.rejectedPoints.toLocaleString()}.
          {' '}Coverage uses {coverage.recordings.toLocaleString()} current, finished analyses containing samples.
          Accepted positions include repairs and interpolation; this measures coverage, not position accuracy.
          Warnings can concern timestamps, speed, OCR or repaired GPS.
        </p>
        <p className="text-xs text-content-muted">
          Library totals: {data.totalGaps.toLocaleString()} GPS gap runs · {data.pairedRecoveries.toLocaleString()} points recovered from the paired camera.
          {' '}This page checks text extracted from recorded footage. It does not measure live Android GPS or CarPlay location delay.
        </p>
      </div>

      {data.issues.length === 0 ? (
        <EmptyState title={data.recordings ? 'Telemetry looks healthy' : 'No recordings to assess'} description={data.recordings ? 'No recordings need attention.' : 'Results appear after recordings are analysed.'} />
      ) : (
        <div className="card overflow-x-auto">
          <p className="px-3 py-3 text-xs text-content-muted">
            Showing {data.issues.length} of {data.issueTotal.toLocaleString()} recordings needing attention (limit {data.issueLimit}), ordered by longest GPS gap, then warning count.
          </p>
          <table className="w-full min-w-[52rem] text-sm">
            <thead className="border-b border-border text-left text-xs text-content-muted">
              <tr>
                <th className="p-2 font-medium">Recording</th>
                <th className="p-2 font-medium">Status</th>
                <th className="p-2 font-medium">GPS positions / samples</th>
                <th className="p-2 font-medium">Gap runs</th>
                <th className="p-2 font-medium">Longest</th>
                <th className="p-2 font-medium">Recovered points</th>
                <th className="p-2 font-medium">Warning samples</th>
                <th className="p-2 font-medium">Reasons</th>
                <th className="p-2 font-medium">Recorded</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {data.issues.map((item) => (
                <tr key={item.recordingId} className="hover:bg-surface-sunken">
                  <td className="p-2">
                    <Link className="font-medium hover:text-accent" to={`/recordings/${item.recordingId}`}>
                      {item.filename}
                    </Link>
                  </td>
                  <td className="p-2 capitalize text-content-muted">{item.status.replace('_', ' ')}</td>
                  <td className="tabular p-2">{item.fixes}/{item.points}</td>
                  <td className="tabular p-2">{item.gaps}</td>
                  <td className="tabular p-2">{item.longestGapS.toFixed(0)}s</td>
                  <td className="tabular p-2">{item.recovered}</td>
                  <td className="tabular p-2">{item.problems}</td>
                  <td className="p-2 text-xs text-content-muted">
                    <div>{item.reasons.map((reason) => reasonLabels[reason] ?? reason.replaceAll('_', ' ')).join(' · ')}</div>
                    {item.realGpsLoss > 0 && <span className="mr-2">No-fix samples: {item.realGpsLoss}</span>}
                    {item.ocrUnreadable > 0 && <span className="mr-2">Unreadable samples: {item.ocrUnreadable}</span>}
                    {item.rejected > 0 && <span>Rejected samples: {item.rejected}</span>}
                  </td>
                  <td className="tabular p-2 text-content-muted">{formatDateTime(item.startedAt)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
