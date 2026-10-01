import type { IngestStatus } from './api'

export function backupHold(status: IngestStatus | undefined): { label: string; reason: string } | null {
  if (!status?.unitOnline || status.state === 'running' || status.state === 'disabled') return null
  if (status.ignitionHold) {
    return {
      label: status.ignitionState === 'on' ? 'Waiting for ignition off' : 'Waiting for ignition status',
      reason: status.ignitionHoldReason || 'Backup waits until the dashcam confirms the ignition is off.',
    }
  }
  if (status.wifiBandHold) {
    return { label: 'Waiting for 5 GHz WiFi', reason: status.wifiBandHoldReason || 'Backup is waiting for a 5 GHz connection.' }
  }
  if (status.arrivalHold) {
    return { label: 'Waiting after startup', reason: status.arrivalHoldReason || 'Automatic backup waits until the dashcam has been running long enough.' }
  }
  return null
}

export function backupIdleLabel(status: IngestStatus): string {
  if (status.backlogKnown && status.backlogFiles === 0) return 'Up to date'
  return status.state === 'ok' ? 'Backup completed' : 'Waiting for backup'
}
