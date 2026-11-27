import { useQuery } from '@tanstack/react-query'

import { ErrorState } from '@/components/ui'
import { api, type OBDIgnitionCoverage } from '@/lib/api'
import { formatDateTimeMillis } from '@/lib/format'

const phases = [
  ['accOn', 'Ignition on (ACC on)'],
  ['accOff', 'Shutdown monitoring (ACC off)'],
  ['unknown', 'Ignition unknown'],
  ['total', 'Whole recording'],
] as const satisfies ReadonlyArray<readonly [keyof OBDIgnitionCoverage, string]>

function StartupTiming({ startup }: { startup: OBDIgnitionCoverage['startup'] }) {
  const milestones = [
    ['Bluetooth adapter connected', startup.stages.bluetoothReadyS],
    ['Adapter initialized', startup.stages.adapterReadyS],
    ['Engine computer answered', startup.stages.ecuReadyS],
  ] as const
  return (
    <div className="mb-4 border-b border-border pb-4">
      <h2 className="section-title">Ignition to first recorded speed and RPM</h2>
      <p className="mt-1 text-sm tabular">
        {startup.accToFirstSpeedRpmS != null ? `${startup.accToFirstSpeedRpmS.toFixed(1)} s after observed ignition on`
          : !startup.firstSpeedRpmAt ? 'No recorded row contains both valid readings.'
          : 'Ignition timing unavailable for this recording.'}
      </p>
      <dl className="mt-2 flex flex-wrap gap-x-5 gap-y-1 text-xs tabular text-content-muted">
        {startup.observedAccOnAt && <div><dt>Observed ignition on</dt><dd>{formatDateTimeMillis(startup.observedAccOnAt)}</dd></div>}
        {milestones.map(([label, seconds]) => seconds != null && (
          <div key={label}><dt>{label}</dt><dd>+{seconds.toFixed(1)} s</dd></div>
        ))}
        {startup.firstSpeedRpmAt && <div><dt>First speed and RPM recorded</dt><dd>{formatDateTimeMillis(startup.firstSpeedRpmAt)}</dd></div>}
        {startup.driveToFirstSpeedRpmS != null && <div><dt>From recording start</dt><dd>{startup.driveToFirstSpeedRpmS.toFixed(1)} s</dd></div>}
        {!!startup.lowVoltageParkedWaits && <div><dt>Low-voltage parked waits</dt><dd>{startup.lowVoltageParkedWaits}</dd></div>}
        {!!startup.startupVoltageRechecks && <div><dt>Startup voltage rechecks</dt><dd>{startup.startupVoltageRechecks}</dd></div>}
      </dl>
    </div>
  )
}

export function ObdIgnitionCoverage({ driveId }: { driveId: string }) {
  const query = useQuery({
    queryKey: ['obd-ignition-coverage', driveId],
    queryFn: () => api.obd.ignitionCoverage(driveId),
  })
  if (query.isError) return <ErrorState title="Couldn’t load ignition coverage" error={query.error} retry={() => query.refetch()} />
  const coverage = query.data
  return (
    <section className="card overflow-x-auto p-4">
      {coverage?.startup && <StartupTiming startup={coverage.startup} />}
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
