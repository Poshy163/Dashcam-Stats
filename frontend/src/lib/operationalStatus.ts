import type { IngestStatus } from './api'

/** An absent car is normal. Alert on recorded failures and an outstanding, stale backlog. */
export function backupAttention(status: Pick<IngestStatus, 'state' | 'lastError' | 'backlogKnown' | 'backlogFiles' | 'lastSuccessTs'> | undefined, now = Date.now()): string[] {
  if (!status || status.state === 'disabled' || status.state === 'running') return []
  const warnings: string[] = []
  if (status.lastError || status.state === 'error' || status.state === 'unauthorized') warnings.push('The last backup attempt reported an error. View Backup for details.')
  if (status.backlogKnown && status.backlogFiles > 0) {
    const last = status.lastSuccessTs ? Date.parse(status.lastSuccessTs) : NaN
    if (!Number.isFinite(last)) warnings.push(`${status.backlogFiles} files are waiting; no successful backup time is recorded.`)
    else if (now - last > 86_400_000) warnings.push(`${status.backlogFiles} files are waiting and the last successful backup was over 24 hours ago.`)
  }
  return warnings
}

export function capacityExceeded(used: number, limit: number): boolean {
  return Number.isFinite(used) && Number.isFinite(limit) && limit > 0 && used > limit
}
