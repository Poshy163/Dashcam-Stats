const STYLES: Record<string, { label: string; className: string; explanation?: string }> = {
  complete: { label: 'Complete', className: 'bg-state-ok/15 text-state-ok' },
  saved_for_backup: {
    label: 'Saved for backup',
    className: 'bg-accent-muted text-accent',
    explanation: 'The logger saved the current recording and released the connection for backup. This does not confirm the engine had stopped.',
  },
  shutdown_detected: {
    label: 'Likely engine shutdown',
    className: 'bg-accent-muted text-accent',
    explanation: 'Inferred from a stationary vehicle, RPM falling below running speed and battery voltage dropping before the connection ended. The original connection-loss record is retained.',
  },
  interrupted: { label: 'Interrupted', className: 'bg-state-warn/15 text-state-warn' },
  recovered: { label: 'Recovered', className: 'bg-accent-muted text-accent' },
  no_vehicle_data: { label: 'No vehicle data', className: 'bg-state-error/15 text-state-error' },
}

export function ObdLifecycleBadge({ status }: { status: string }) {
  const style = STYLES[status] ?? {
    label: status.replace(/_/g, ' '),
    className: 'bg-surface-sunken text-content-muted',
  }
  return (
    <span title={style.explanation} className={`inline-block rounded-full px-2 py-0.5 text-xs font-medium ${style.className}`}>
      {style.label}
    </span>
  )
}

export function ObdLifecycleReason({ status, reason }: { status: string; reason: string | null }) {
  if (status === 'saved_for_backup') return 'Recording saved at backup request'
  if (status === 'shutdown_detected') return 'Shutdown inferred from recorded signals'
  return reason?.replace(/_/g, ' ') ?? null
}

export function ObdLifecycleExplanation({ status }: { status: string }) {
  const explanation = STYLES[status]?.explanation
  return explanation ? <p className="mt-2 text-sm text-content-muted">{explanation}</p> : null
}
