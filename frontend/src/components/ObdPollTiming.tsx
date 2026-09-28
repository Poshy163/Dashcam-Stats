function object(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {}
}

function number(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null
}

function milliseconds(value: unknown): string {
  const n = number(value)
  return n === null ? '—' : `${Math.round(n).toLocaleString()} ms`
}

const labels: Record<number, string> = {
  1: 'MIL / trouble-code count', 3: 'Fuel-system state', 4: 'Engine load',
  5: 'Coolant temperature', 6: 'Short-term fuel trim', 7: 'Long-term fuel trim',
  12: 'RPM', 13: 'Speed', 14: 'Timing advance', 15: 'Intake temperature',
  16: 'Airflow', 17: 'Throttle', 20: 'Oxygen sensor 1', 21: 'Oxygen sensor 2',
  33: 'Distance with MIL',
}

/** API conversion has already changed payload keys to camelCase. */
export function ObdPollTiming({ payload }: { payload: Record<string, unknown> }) {
  if (payload.schemaVersion !== 1 || !Array.isArray(payload.pids)) {
    return <span>Timing details unavailable for this format.</span>
  }
  const cycle = object(payload.cycleWorkMs)
  const interval = object(payload.cycleStartIntervalMs)
  const rows = payload.pids.slice(0, 18).map(object)
  return (
    <details>
      <summary className="cursor-pointer text-content">
        {number(payload.cyclesCompleted) ?? '—'} completed cycles ·{' '}
        {number(payload.overrunCount) ?? '—'} exceeded the{' '}
        {milliseconds(payload.targetCycleMs)} target
      </summary>
      <p className="my-2 max-w-3xl">
        This window: median cycle work {milliseconds(cycle.medianMs)}, 95th percentile{' '}
        {milliseconds(cycle.p95Ms)}. Median start interval {milliseconds(interval.medianMs)}.
        Request time includes adapter and Bluetooth waits. Value age is the time from a
        successful reply to the saved row timestamp; it is not the ECU sensor&apos;s internal age.
        Failed reads have no value-age measurement.
      </p>
      <table className="my-2 w-full text-left">
        <thead className="text-content-muted">
          <tr>
            <th className="p-2 font-medium">Reading</th>
            <th className="p-2 font-medium">Successful / requested</th>
            <th className="p-2 font-medium">Request median / p95</th>
            <th className="p-2 font-medium">Value age median / max</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => {
            const pid = number(row.pid)
            const request = object(row.requestMs)
            const age = object(row.valueAgeAtRowMs)
            return (
              <tr key={index} className="border-t border-border">
                <td className="p-2">{pid === null ? 'Unknown' : (labels[pid] ?? `PID ${pid}`)}</td>
                <td className="tabular p-2">
                  {number(row.successes) ?? '—'} / {number(row.attempts) ?? '—'}
                  {!!number(row.cooldownSkips) && ` · ${row.cooldownSkips} skipped during cooldown`}
                </td>
                <td className="tabular p-2">{milliseconds(request.medianMs)} / {milliseconds(request.p95Ms)}</td>
                <td className="tabular p-2">
                  {milliseconds(age.medianMs)} / {milliseconds(number(age.count) ? age.maxMs : null)}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </details>
  )
}
