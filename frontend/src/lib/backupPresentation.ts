import type { IngestRadioStatus, IngestStatus } from './api'

export function unverifiedRadioRestores(transition: IngestRadioStatus['transition'] | undefined): ('bluetooth' | 'hotspot')[] {
  if (!transition) return []
  const { bluetooth, hotspot } = transition
  const pending: ('bluetooth' | 'hotspot')[] = []
  if ((bluetooth.disableAttempted || bluetooth.restoreAttempted) && !bluetooth.restoreVerified) pending.push('bluetooth')
  // Restoring Bluetooth can re-enable an originally off hotspot on this unit.
  // The coordinator verifies that baseline even if it never disabled the hotspot.
  if ((bluetooth.disableAttempted || hotspot.disableAttempted || hotspot.restoreAttempted) && !hotspot.restoreVerified) pending.push('hotspot')
  return pending
}

export function backupRecoveryNotice(transition: IngestRadioStatus['transition'] | undefined): { label: string; detail: string } | null {
  if (!transition || !(transition.recoveryRequired ||
      (transition.active && ['restoring_radios', 'resuming_obd'].includes(transition.phase)))) return null
  const radios = [transition.bluetooth, transition.hotspot]
  if (unverifiedRadioRestores(transition).length) return null
  const radioEvidence = radios.some((radio) => radio.disableAttempted || radio.restoreAttempted)
    ? 'Radio restoration is confirmed.'
    : 'The radios did not need restoring.'
  const logger = transition.obdLogger
  const loggerPending = (logger?.quiesceAttempted || logger?.quiesceVerified) && !logger?.resumeVerified
  return {
    label: 'Finishing backup recovery',
    detail: `${radioEvidence} ${loggerPending
      ? 'The app is confirming recovery and that the logger has resumed.'
      : 'The app is completing the remaining recovery checks.'}`,
  }
}

export function radioQuietingNotice(status: IngestStatus | undefined, transition: IngestRadioStatus['transition'] | undefined): string | null {
  if (!status?.unitOnline || status.state !== 'running' || !status.radioQuietingHold ||
      transition?.recoveryRequired || transition?.active) return null
  return status.radioQuietingHoldReason || 'The remaining awake time is too short or not known.'
}

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
