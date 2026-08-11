import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'

import Spinner from '@/components/Spinner'
import { EmptyState, ErrorState, PageHeader, StatTile } from '@/components/ui'
import { api } from '@/lib/api'
import { formatDateTime } from '@/lib/format'

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

  return (
    <div className="space-y-4">
      <PageHeader
        title="Telemetry health"
        subtitle="Separates genuine GPS loss from OCR gaps and paired-camera recoveries."
      />
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-6">
        <StatTile label="Healthy" value={data.healthy} tone="ok" />
        <StatTile label="Degraded" value={data.degraded} tone={data.degraded ? 'error' : 'default'} />
        <StatTile label="No GPS fix" value={data.noFix} />
        <StatTile label="Pending" value={data.pending} tone={data.pending ? 'busy' : 'default'} />
        <StatTile label="GPS gaps" value={data.totalGaps} />
        <StatTile label="Recovered" value={data.pairedRecoveries} tone="ok" />
      </div>

      {data.issues.length === 0 ? (
        <EmptyState title="Telemetry looks healthy" description="No recordings need attention." />
      ) : (
        <div className="card overflow-x-auto">
          <table className="w-full min-w-[52rem] text-sm">
            <thead className="border-b border-border text-left text-xs text-content-muted">
              <tr>
                <th className="p-2 font-medium">Recording</th>
                <th className="p-2 font-medium">Status</th>
                <th className="p-2 font-medium">Fixes</th>
                <th className="p-2 font-medium">Gaps</th>
                <th className="p-2 font-medium">Longest</th>
                <th className="p-2 font-medium">Paired recovery</th>
                <th className="p-2 font-medium">Why missing</th>
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
                  <td className="p-2 text-xs text-content-muted">
                    {item.realGpsLoss > 0 && <span className="mr-2">GPS loss {item.realGpsLoss}</span>}
                    {item.ocrUnreadable > 0 && <span className="mr-2">OCR {item.ocrUnreadable}</span>}
                    {item.rejected > 0 && <span>rejected {item.rejected}</span>}
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
