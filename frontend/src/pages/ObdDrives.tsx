import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'

import Spinner from '@/components/Spinner'
import { EmptyState, ErrorState, PageHeader, Pagination, StatTile } from '@/components/ui'
import { api } from '@/lib/api'
import { formatDateTime, formatDuration, formatRelative, formatSpeed } from '@/lib/format'

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

const LIFECYCLE_STYLE: Record<string, { label: string; className: string }> = {
  complete: { label: 'Complete', className: 'bg-state-ok/15 text-state-ok' },
  interrupted: { label: 'Interrupted', className: 'bg-state-warn/15 text-state-warn' },
  recovered: { label: 'Recovered', className: 'bg-accent-muted text-accent' },
  // The bus went silent while the adapter kept answering, so the drive recorded samples
  // but no vehicle data. Styled as an error rather than a warning: a drive with no
  // statistics is a drive that was not captured, however cleanly it ended.
  no_vehicle_data: { label: 'No vehicle data', className: 'bg-state-error/15 text-state-error' },
}

function LifecycleBadge({ status }: { status: string }) {
  const style = LIFECYCLE_STYLE[status] ?? {
    label: status.replace(/_/g, ' '),
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
  const totals = useQuery({
    queryKey: ['obd-drives-summary'],
    queryFn: api.obd.drivesSummary,
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

      {totals.data && totals.data.driveCount > 0 && (
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-6">
          <StatTile
            label="Drives"
            value={totals.data.driveCount.toLocaleString()}
            hint={
              totals.data.lastDriveAt ? `last ${formatRelative(totals.data.lastDriveAt)}` : undefined
            }
          />
          <StatTile
            label="Distance"
            value={`${totals.data.totalDistanceKm.toFixed(1)} km`}
            hint={`${formatDuration(totals.data.totalDurationS)} driven`}
          />
          <StatTile
            label="Fuel used"
            value={`${totals.data.totalFuelUsedL.toFixed(2)} L`}
            hint={
              totals.data.averageFuelConsumptionL100km != null
                ? `${totals.data.averageFuelConsumptionL100km.toFixed(1)} L/100 km overall`
                : undefined
            }
          />
          <StatTile
            label="Idle time"
            value={formatDuration(totals.data.totalIdleDurationS)}
            hint={
              totals.data.totalDurationS > 0
                ? `${Math.round((totals.data.totalIdleDurationS / totals.data.totalDurationS) * 100)}% of driving time`
                : undefined
            }
          />
          <StatTile label="Top speed" value={formatSpeed(totals.data.maximumSpeedKmh)} />
          <StatTile
            label="Samples stored"
            value={totals.data.totalSampleCount.toLocaleString()}
            hint="full resolution, kept forever"
          />
        </div>
      )}

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
                <th className="p-2 font-medium">Status</th>
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
                  </td>
                  <td className="p-2">
                    <LifecycleBadge status={drive.lifecycleStatus} />
                    {drive.interruptionReason && (
                      <div className="mt-1 text-xs text-content-faint">
                        {drive.interruptionReason.replace(/_/g, ' ')}
                      </div>
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
                    {drive.dataCompletenessPercentage != null && (
                      <div className="text-xs text-content-faint">
                        {Math.round(drive.dataCompletenessPercentage)}% signal coverage ·{' '}
                        {drive.gapCount} cadence gaps
                      </div>
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
