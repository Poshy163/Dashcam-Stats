import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'

import Spinner from '@/components/Spinner'
import { EmptyState, ErrorState, PageHeader, Pagination } from '@/components/ui'
import { api } from '@/lib/api'
import { formatDateTime, formatDuration, formatSpeed } from '@/lib/format'

const IMPORT_STATE_STYLE: Record<string, { label: string; className: string }> = {
  imported: { label: 'In Home Assistant', className: 'bg-state-ok/15 text-state-ok' },
  ready_to_import: { label: 'Waiting for HA', className: 'bg-accent-muted text-accent' },
  retry_wait: { label: 'Retrying HA', className: 'bg-state-warn/15 text-state-warn' },
  importing: { label: 'Importing', className: 'bg-accent-muted text-state-busy' },
  failed: { label: 'Import failed', className: 'bg-state-error/15 text-state-error' },
  quarantined: { label: 'Quarantined', className: 'bg-state-error/15 text-state-error' },
}

function ImportBadge({ state }: { state: string }) {
  const style = IMPORT_STATE_STYLE[state] ?? {
    label: state.replace(/_/g, ' '),
    className: 'bg-surface-sunken text-content-muted',
  }
  return (
    <span className={`inline-block rounded-full px-2 py-0.5 text-xs font-medium ${style.className}`}>
      {style.label}
    </span>
  )
}

export default function ObdDrives() {
  const [page, setPage] = useState(1)
  const query = useQuery({
    queryKey: ['obd-drives', page],
    queryFn: () => api.obd.drives({ page, pageSize: 20 }),
    refetchInterval: 30_000,
  })

  if (query.isLoading) return <Spinner label="Loading drives…" className="py-24" />
  if (query.isError) return <ErrorState error={query.error} retry={() => query.refetch()} />
  if (!query.data) return null
  const data = query.data

  return (
    <div className="space-y-4">
      <PageHeader
        title="OBD drives"
        subtitle="Every recorded drive, kept at the logger's full sample resolution. Home Assistant holds the hourly rollups; the traces live here."
      />

      {data.items.length === 0 ? (
        <EmptyState
          title="No drives yet"
          description="Drives appear here after the head unit's OBD bundle has been collected and validated."
        />
      ) : (
        <div className="card overflow-x-auto">
          <table className="w-full min-w-[58rem] text-sm">
            <thead className="border-b border-border text-left text-xs text-content-muted">
              <tr>
                <th className="p-2 font-medium">Started</th>
                <th className="p-2 font-medium">Duration</th>
                <th className="p-2 font-medium">Distance</th>
                <th className="p-2 font-medium">Avg / max speed</th>
                <th className="p-2 font-medium">Max RPM</th>
                <th className="p-2 font-medium">Fuel</th>
                <th className="p-2 font-medium">Samples</th>
                <th className="p-2 font-medium">DTCs</th>
                <th className="p-2 font-medium">Import</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {data.items.map((drive) => (
                <tr key={drive.driveId} className="hover:bg-surface-sunken">
                  <td className="p-2">
                    <Link className="font-medium hover:text-accent" to={`/obd/${drive.driveId}`}>
                      {formatDateTime(drive.startedAt)}
                    </Link>
                    {!drive.cleanEnd && (
                      <span className="ml-2 text-xs text-state-warn">interrupted</span>
                    )}
                  </td>
                  <td className="tabular p-2">{formatDuration(drive.durationS)}</td>
                  <td className="tabular p-2">
                    {drive.distanceKm != null ? `${drive.distanceKm.toFixed(1)} km` : '—'}
                  </td>
                  <td className="tabular p-2">
                    {formatSpeed(drive.averageSpeedKmh)} / {formatSpeed(drive.maximumSpeedKmh)}
                  </td>
                  <td className="tabular p-2">
                    {drive.maximumRpm != null ? Math.round(drive.maximumRpm).toLocaleString() : '—'}
                  </td>
                  <td className="tabular p-2">
                    {drive.estimatedFuelUsedL != null ? `${drive.estimatedFuelUsedL.toFixed(2)} L` : '—'}
                  </td>
                  <td className="tabular p-2">
                    {drive.sampleCount.toLocaleString()}
                    {drive.receivedSamplePercentage != null && (
                      <span className="ml-1 text-xs text-content-faint">
                        ({Math.round(drive.receivedSamplePercentage)}%)
                      </span>
                    )}
                  </td>
                  <td className="p-2">
                    {drive.dtcsObserved.length > 0 ? (
                      <span className="text-state-warn">{drive.dtcsObserved.join(', ')}</span>
                    ) : (
                      <span className="text-content-faint">none</span>
                    )}
                  </td>
                  <td className="p-2">
                    <ImportBadge state={drive.importState} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <Pagination page={data.page} pages={data.pages} total={data.total} onChange={setPage} />
    </div>
  )
}
