import type { OBDLoggerStatus, OBDTrialChange } from '@/lib/api'
import { formatDateTime } from '@/lib/format'
import { loggerTimestampMillis } from '@/lib/obdLoggerPresentation'

const FALLBACKS: Record<string, string> = {
  required_pids_unavailable: 'Required readings unavailable',
  sustained_overrun: 'Repeated work-budget overload',
  critical_readings_missing: 'Critical readings missing',
  transport_fault: 'Uncertain adapter connection',
}

const SOURCES: Record<string, string> = {
  legacy_migration: 'Imported existing setting',
  operator: 'Explicit apply',
  health: 'Automatic fallback',
  unknown: 'Source unavailable',
}

const TRANSITIONS: Record<string, string> = {
  first_observation: 'First recorded startup',
  same_process: 'Same app process',
  process_restart: 'App process restarted',
  package_update: 'App updated',
  kernel_boot: 'New device boot observed',
  unknown: 'Startup continuity unavailable',
}

const BOOT_REASONS: Record<string, string> = {
  unknown: 'Unavailable', reboot: 'Generic reboot', cold: 'Cold start', power_key: 'Power key',
  usb: 'USB start', watchdog: 'Watchdog', kernel_panic: 'Kernel panic', thermal: 'Thermal category',
  hard_reset: 'Hard reset', adb: 'ADB request', user_requested: 'Requested restart',
  software_update: 'Software update', recovery: 'Recovery mode', bootloader: 'Bootloader mode',
}

const EXIT_REASONS: Record<string, string> = {
  unavailable: 'Unavailable', not_recorded: 'No correlated record', unknown: 'Unknown category',
  exit_self: 'Self exit', signaled: 'Signal termination', low_memory: 'Low memory', crash: 'App crash',
  native_crash: 'Native app crash', anr: 'App not responding', initialization_failure: 'Initialization failure',
  permission_change: 'Permission change', excessive_resource_usage: 'Resource limit',
  user_requested: 'Requested stop', user_stopped: 'App stopped', dependency_died: 'Dependency stopped',
  other: 'Other category', freezer: 'App freezer', package_state_change: 'Package state change',
  package_updated: 'Package update',
}

const label = (labels: Record<string, string>, value: string, fallback = 'Not reported') =>
  (Object.prototype.hasOwnProperty.call(labels, value) ? labels[value] : undefined) ?? fallback

function savedTrial(change: OBDTrialChange): string {
  if (change.fallbackReason) return `Fallback: ${label(FALLBACKS, change.fallbackReason, 'Reason unavailable')}`
  return change.enabled ? 'Armed for the next drive' : 'Baseline selected'
}

function RecordedAt({ value }: { value: string | null | undefined }) {
  return loggerTimestampMillis(value) === null
    ? <>Original change time unavailable</>
    : <time dateTime={value ?? undefined}>{formatDateTime(value)}</time>
}

export function CompanionDiagnostics({ logger }: { logger: OBDLoggerStatus | null | undefined }) {
  const trial = logger?.trialSettings
  const restart = logger?.restartEvidence
  const trialAvailable = trial?.storageState === 'ready'
  return (
    <div className="grid gap-4 border-t border-border px-5 py-4 sm:grid-cols-2" aria-label="Reported trial and restart evidence">
      <section aria-label="Trial setting history">
        <h3 className="text-xs font-medium uppercase tracking-wide text-content-faint">Trial setting · last reported</h3>
        <p className="mt-1 text-sm font-medium">
          {!trial ? 'Not reported by this companion' : trialAvailable ? savedTrial(trial) : 'Setting unavailable; baseline protected'}
        </p>
        {trial && <>
          <p className="mt-1 text-xs text-content-muted">{label(SOURCES, trial.source, SOURCES.unknown)} · <RecordedAt value={trial.changedAtUtc} /></p>
          {trial.error && <p className="mt-1 text-xs text-state-warn">The last setting operation needs review in the companion.</p>}
          {trial.recentChanges.length > 0 && <details className="mt-2 text-xs text-content-muted">
            <summary className="cursor-pointer">Recent trial changes</summary>
            <ul className="mt-2 space-y-2">
              {trial.recentChanges.slice(-8).map(change => <li key={change.revision}>
                {savedTrial(change)} · {label(SOURCES, change.source, SOURCES.unknown)}<br />
                <RecordedAt value={change.changedAtUtc} />
              </li>)}
            </ul>
          </details>}
        </>}
        <p className="mt-1 text-xs text-content-faint">The active drive keeps its recorded poll plan. Older changes cannot be reconstructed.</p>
      </section>
      <section aria-label="Device restart evidence">
        <h3 className="text-xs font-medium uppercase tracking-wide text-content-faint">Restart evidence · last reported</h3>
        <p className="mt-1 text-sm font-medium">
          {!restart ? 'Not reported by this companion' : restart.storageState !== 'ready'
            ? 'Startup evidence unavailable' : label(TRANSITIONS, restart.transition, TRANSITIONS.unknown)}
        </p>
        {restart && <>
          <p className="mt-1 text-xs text-content-muted"><RecordedAt value={restart.observedAtUtc} /></p>
          <p className="mt-1 text-xs text-content-muted">Firmware reason: {label(BOOT_REASONS, restart.bootReason, 'Unavailable')}</p>
          <p className="mt-1 text-xs text-content-muted">Previous app exit: {label(EXIT_REASONS, restart.previousProcessExit ?? '', 'Unavailable')}</p>
          <p className="mt-1 text-xs text-content-muted">
            {restart.causeConfidence === 'reported' ? 'Firmware or Android reported a restart category; physical cause unverified.' : 'Physical cause unknown.'}
          </p>
          <p className="mt-1 text-xs text-content-faint">An app exit record is separate from a device reboot.</p>
          {restart.history?.length > 0 && <details className="mt-2 text-xs text-content-muted">
            <summary className="cursor-pointer">Recent startup observations</summary>
            <ul className="mt-2 space-y-2">
              {restart.history.slice(-8).map((entry, index) => <li key={`${entry.observedAtUtc}-${index}`}>
                {label(TRANSITIONS, entry.transition, TRANSITIONS.unknown)} · {label(BOOT_REASONS, entry.bootReason, 'Unavailable')}<br />
                <RecordedAt value={entry.observedAtUtc} />
              </li>)}
            </ul>
          </details>}
        </>}
      </section>
    </div>
  )
}
