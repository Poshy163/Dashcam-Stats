import { useQuery } from '@tanstack/react-query'

import { ErrorState } from '@/components/ui'
import { api, type OBDIgnitionCoverage } from '@/lib/api'

const phases = [
  ['accOn', 'Ignition on (ACC on)'],
  ['accOff', 'Shutdown monitoring (ACC off)'],
  ['unknown', 'Ignition unknown'],
  ['total', 'Whole recording'],
] as const satisfies ReadonlyArray<readonly [keyof OBDIgnitionCoverage, string]>

export function ObdIgnitionCoverage({ driveId }: { driveId: string }) {
  const query = useQuery({
    queryKey: ['obd-ignition-coverage', driveId],
    queryFn: () => api.obd.ignitionCoverage(driveId),
  })
  if (query.isError) return <ErrorState title="Couldn’t load ignition coverage" error={query.error} retry={() => query.refetch()} />
  const coverage = query.data
  return (
    <section className="card overflow-x-auto p-4">
      <h2 className="section-title">Speed and RPM coverage</h2>
      <p className="mt-1 text-xs text-content-muted">
        Coverage is the share of recorded rows with both speed and RPM. Ignition comes from the
        logger’s accessory-power (ACC) journal. Missing ignition evidence appears as unknown.
      </p>
      {coverage ? (
        <table className="mt-3 w-full min-w-[42rem] text-left text-sm tabular">
          <thead className="border-b border-border text-xs text-content-muted">
            <tr>{['Recording phase', 'Rows', 'Speed', 'RPM', 'Both', 'Coverage', 'Voltage', 'Voltage only'].map(label =>
              <th key={label} scope="col" className="p-2 font-medium">{label}</th>)}</tr>
          </thead>
          <tbody className="divide-y divide-border">
            {phases.map(([key, label]) => {
              const phase = coverage[key]
              if (!phase.rows && key !== 'total') return null
              return (
                <tr key={key}>
                  <th scope="row" className="p-2 font-medium">{label}</th>
                  {[phase.rows, phase.speedRows, phase.rpmRows, phase.speedAndRpmRows].map((count, i) =>
                    <td key={i} className="p-2">{count.toLocaleString()}</td>)}
                  <td className="p-2">{phase.rows ? `${(100 * phase.speedAndRpmRows / phase.rows).toFixed(1)}%` : '—'}</td>
                  <td className="p-2">{phase.voltageRows.toLocaleString()}</td>
                  <td className="p-2">{phase.voltageOnlyRows.toLocaleString()}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      ) : <p className="mt-3 text-sm text-content-muted">Loading coverage…</p>}
    </section>
  )
}
