import { cn } from '@/lib/cn'
import { formatRelative } from '@/lib/format'
import type { OBDLoggerStatus } from '@/lib/api'

type Tone = 'default' | 'ok' | 'warn' | 'error' | 'busy'

const TONE_TEXT: Record<Tone, string> = {
  default: 'text-content',
  ok: 'text-state-ok',
  warn: 'text-state-warn',
  error: 'text-state-error',
  busy: 'text-state-busy',
}

const TONE_BADGE: Record<Tone, string> = {
  default: 'bg-surface-sunken text-content-muted',
  ok: 'bg-state-ok/15 text-state-ok',
  warn: 'bg-state-warn/15 text-state-warn',
  error: 'bg-state-error/15 text-state-error',
  busy: 'bg-accent-muted text-state-busy',
}

const humanize = (value: string) =>
  value
    .replace(/_/g, ' ')
    .replace(/\b\w/g, (letter) => letter.toUpperCase())

function stateTone(state: string | undefined): Tone {
  if (state === 'ecu_online' || state === 'probing') return 'busy'
  if (state === 'startup_failed' || state === 'invalid_status') return 'error'
  if (state === 'backoff' || state === 'status_unavailable') return 'warn'
  return 'default'
}

function voltageQuality(logger: OBDLoggerStatus): { label: string; tone: Tone } {
  if (logger.batteryVoltageQuality === 'failed') return { label: 'Probe failed', tone: 'error' }
  if (logger.batteryVoltageQuality === 'invalid') return { label: 'Invalid reply', tone: 'warn' }
  if (logger.batteryVoltageQuality === 'unavailable') {
    return { label: 'Unavailable', tone: 'default' }
  }
  if (logger.batteryVoltageFresh === true) return { label: 'Fresh', tone: 'ok' }
  if (logger.batteryVoltageQuality === 'stale' || logger.batteryVoltageFresh === false) {
    return { label: 'Stale', tone: 'warn' }
  }
  return { label: 'Unavailable', tone: 'default' }
}

function voltageSource(source: string | null | undefined): string {
  if (source === 'dashcam_elm_atrv') return 'Dashcam ELM · ATRV only'
  return source ? humanize(source) : 'Source not reported'
}

function ownerStatus(logger: OBDLoggerStatus): {
  label: string
  hint: string
  tone: Tone
} {
  switch (logger.bleOwner) {
    case 'dashcam_voltage_only':
      return {
        label: 'Dashcam · ATRV only',
        hint: 'The dashcam owns this temporary BLE check and cannot initialise the ECU.',
        tone: 'ok',
      }
    case 'dashcam_full_obd':
      return {
        label: 'Dashcam',
        hint: 'The dashcam owns BLE and may open a full OBD session after the voltage gate.',
        tone: 'ok',
      }
    case 'home_assistant_voltage_only':
      return {
        label: 'Home Assistant',
        hint: 'Home Assistant owns voltage-only BLE access; ECU state is reported separately.',
        tone: 'ok',
      }
    case 'phone_reserved':
      return {
        label: 'Phone reserved',
        hint: 'A phone is the designated BLE owner.',
        tone: 'default',
      }
    case 'transitioning':
      return {
        label: 'Transitioning',
        hint: 'BLE ownership is changing; connection state may be temporarily unavailable.',
        tone: 'warn',
      }
    case 'conflict_detected':
      return {
        label: 'Conflict detected',
        hint: 'More than one client may be trying to own the adapter.',
        tone: 'error',
      }
    case 'unowned':
      return {
        label: 'Released',
        hint: 'The logger has released BLE ownership.',
        tone: 'default',
      }
    default:
      if (logger.ownershipEnabled === true) {
        return {
          label: 'Dashcam',
          hint: 'Legacy ownership flag; this logger does not report the detailed owner state.',
          tone: 'ok',
        }
      }
      if (logger.ownershipEnabled === false) {
        return {
          label: 'Released',
          hint: 'Legacy ownership flag; this logger does not report the detailed owner state.',
          tone: 'default',
        }
      }
      return {
        label: 'Not reported',
        hint: 'No authoritative BLE ownership state is available.',
        tone: 'warn',
      }
  }
}

function ConnectionState({
  label,
  value,
  activeLabel,
  inactiveLabel,
  activeHint,
  inactiveHint,
}: {
  label: string
  value: boolean | undefined
  activeLabel: string
  inactiveLabel: string
  activeHint: string
  inactiveHint: string
}) {
  const known = value !== undefined
  const tone: Tone = value === true ? 'ok' : 'default'
  const stateLabel = known ? (value ? activeLabel : inactiveLabel) : 'Not reported'
  const hint = known ? (value ? activeHint : inactiveHint) : 'Requires logger status schema v4.'

  return (
    <div className="rounded-xl border border-line/70 bg-surface-sunken/45 px-4 py-3">
      <div className="text-xs font-medium uppercase tracking-wide text-content-faint">{label}</div>
      <div className={cn('mt-1 text-base font-semibold', TONE_TEXT[tone])}>{stateLabel}</div>
      <div className="mt-1 text-xs leading-relaxed text-content-faint">{hint}</div>
    </div>
  )
}

export function ObdLoggerCard({
  logger,
  checkedAt,
}: {
  logger: OBDLoggerStatus | null | undefined
  checkedAt: string | null | undefined
}) {
  if (!logger) {
    return (
      <section className="card mb-6 px-5 py-4" aria-label="OBD logger live state">
        <div className="font-medium">Logger state unavailable</div>
        <p className="mt-1 text-sm text-content-muted">
          The server has not read a status document from the dashcam yet. No adapter, ECU, or
          engine connection is being inferred.
        </p>
      </section>
    )
  }

  const quality = voltageQuality(logger)
  const owner = ownerStatus(logger)
  const voltage =
    typeof logger.batteryVoltage === 'number' && Number.isFinite(logger.batteryVoltage)
      ? `${logger.batteryVoltage.toFixed(1)} V`
      : 'Unavailable'
  const voltageAge = logger.batteryVoltageSampleAtUtc
    ? `sampled ${formatRelative(logger.batteryVoltageSampleAtUtc)}`
    : 'sample time not reported'
  const adapterOnly = logger.adapterReachable === true && logger.ecuConnected !== true

  return (
    <section className="card mb-6 overflow-hidden" aria-label="OBD logger live state">
      <div className="flex flex-wrap items-start justify-between gap-3 border-b border-line px-5 py-4">
        <div>
          <h3 className="font-semibold">Live logger evidence</h3>
          <p className="mt-0.5 text-sm text-content-muted">
            Adapter voltage, BLE ownership, and ECU evidence are reported independently.
          </p>
        </div>
        <span className={cn('badge', TONE_BADGE[stateTone(logger.state)])}>
          {logger.state ? humanize(logger.state) : 'State not reported'}
        </span>
      </div>

      <div className="grid gap-4 p-5 lg:grid-cols-2">
        <div className="rounded-xl border border-line/70 p-4">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="text-sm font-medium text-content-muted">Battery voltage at OBD port</div>
            <span className={cn('badge', TONE_BADGE[quality.tone])}>{quality.label}</span>
          </div>
          <div className={cn('tabular mt-2 text-3xl font-bold', TONE_TEXT[quality.tone])}>
            {voltage}
          </div>
          <div className="mt-2 text-xs text-content-faint">
            {voltageSource(logger.batteryVoltageSource)} · {voltageAge}
          </div>
          <p className="mt-3 text-xs leading-relaxed text-content-muted">
            ATRV reads adapter supply voltage. It is not a checksum-valid ECU response and does
            not prove that the engine is running.
          </p>
        </div>

        <div className="rounded-xl border border-line/70 p-4">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="text-sm font-medium text-content-muted">BLE owner (last report)</div>
            <span className={cn('badge', TONE_BADGE[owner.tone])}>{owner.label}</span>
          </div>
          <div className={cn('mt-2 text-2xl font-bold', TONE_TEXT[owner.tone])}>{owner.label}</div>
          <p className="mt-2 text-xs leading-relaxed text-content-muted">{owner.hint}</p>
          {logger.voltageOnlyMode === true && (
            <div className="mt-3 rounded-lg bg-accent-muted/50 px-3 py-2 text-xs text-content">
              Controlled ATRV-only mode is active; ECU initialisation is fenced off.
            </div>
          )}
          <div className="mt-3 text-xs text-content-faint">
            {logger.headUnitState
              ? `Head unit last reported ${humanize(logger.headUnitState).toLowerCase()}`
              : 'Head-unit state not reported'}
            {logger.updatedAtUtc
              ? ` · logger reported ${formatRelative(logger.updatedAtUtc)}`
              : ' · producer time not reported'}
            {checkedAt ? ` · server checked ${formatRelative(checkedAt)}` : ''}
          </div>
        </div>
      </div>

      <div className="grid gap-3 px-5 pb-5 sm:grid-cols-2 xl:grid-cols-4">
        <ConnectionState
          label="Adapter reachable"
          value={logger.adapterReachable}
          activeLabel="ATRV reply"
          inactiveLabel="No fresh reply"
          activeHint="The ELM adapter answered a recent voltage-only request."
          inactiveHint="No fresh valid adapter-voltage reply is available."
        />
        <ConnectionState
          label="BLE session"
          value={logger.adapterConnected}
          activeLabel="Connected"
          inactiveLabel="Closed"
          activeHint="A temporary or full BLE GATT session is open."
          inactiveHint="The BLE GATT session is closed after the parked read."
        />
        <ConnectionState
          label="ECU"
          value={logger.ecuConnected}
          activeLabel="Online"
          inactiveLabel="Offline"
          activeHint="A checksum-valid ECU response has been accepted."
          inactiveHint="No checksum-valid ECU response is active."
        />
        <ConnectionState
          label="Engine"
          value={logger.engineRunning}
          activeLabel="Running"
          inactiveLabel="Off"
          activeHint="The active drive has live ECU evidence."
          inactiveHint="No live engine-running evidence is active."
        />
      </div>

      {adapterOnly && (
        <div className="border-t border-line bg-accent-muted/35 px-5 py-3 text-sm text-content-muted">
          <span className="font-medium text-content">Voltage-only adapter contact.</span> The ELM
          answered ATRV, while the full OBD session, ECU, and engine remain separate states.
        </div>
      )}
    </section>
  )
}
