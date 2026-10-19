import type { OBDDriveSeries } from '@/lib/api'
import { obdDiagnosticState } from '@/lib/obdDiagnostics'

export default function ObdFaultSummary({ data, filtered = false }: { data: OBDDriveSeries; filtered?: boolean }) {
  const state = obdDiagnosticState(data, filtered)
  return (
    <section className="card p-4 sm:p-5" aria-label="Engine diagnostics">
      <h2 className="section-title">Engine diagnostics</h2>
      {state.warning && (
        <p className="mt-3 rounded-md bg-state-warn/10 p-3 text-sm text-state-warn">
          The ECU reported {state.storedCount != null && state.storedCount > 0
            ? `${state.storedCount} stored fault${state.storedCount === 1 ? '' : 's'}`
            : 'a check-engine warning'} during this drive.
          {state.codes.length === 0 && ' The actual trouble codes were not recorded. An empty code list does not mean the warning is clear.'}
        </p>
      )}
      <dl className="mt-4 grid gap-4 text-sm sm:grid-cols-3">
        <div>
          <dt className="text-xs text-content-faint">Check-engine lamp</dt>
          <dd className={`mt-1 ${state.lampOn ? 'text-state-warn' : ''}`}>
            {state.lampOn === true ? 'Observed on' : state.lampOn === false ? 'Observed off' : 'Not observed'}
          </dd>
        </div>
        <div>
          <dt className="text-xs text-content-faint">Stored fault count · highest observed</dt>
          <dd className={`mt-1 tabular ${state.storedCount ? 'text-state-warn' : ''}`}>
            {state.storedCount ?? 'Not observed'}
          </dd>
        </div>
        <div>
          <dt className="text-xs text-content-faint">Trouble codes</dt>
          <dd className={`mt-1 ${state.codes.length ? 'text-state-warn' : ''}`}>
            {state.codes.length ? state.codes.join(', ') : state.codeStatus}
          </dd>
        </div>
      </dl>
      <p className="mt-4 text-xs text-content-muted">
        The lamp and fault count are separate from reading the actual codes. They do not identify the cause.
        {state.partialEvidence && ' Lamp, count and scan status reflect the displayed samples and diagnostic page; other observations may be outside this view.'}
      </p>
    </section>
  )
}
