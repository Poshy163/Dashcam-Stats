import { useEffect, useState } from 'react'
import { cn } from '@/lib/cn'
import { formatDateTime, formatRelative } from '@/lib/format'
import { loggerEvidenceFreshness, loggerTimestampMillis } from '@/lib/obdLoggerPresentation'
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

function voltageQuality(logger: OBDLoggerStatus, nowMs: number): { label: string; tone: Tone } {
  if (logger.batteryVoltageQuality === 'failed') return { label: 'Probe failed', tone: 'error' }
  if (logger.batteryVoltageQuality === 'invalid') return { label: 'Invalid reply', tone: 'warn' }
  if (logger.batteryVoltageQuality === 'unavailable') {
    return { label: 'Unavailable', tone: 'default' }
  }
  const sampledAt = loggerTimestampMillis(logger.batteryVoltageSampleAtUtc)
  // The ATRV contract has its own 75-second TTL, independent of the status heartbeat.
  const sampleAge = sampledAt === null ? null : nowMs - sampledAt
  if (logger.batteryVoltageFresh === true && sampleAge !== null && sampleAge >= -5_000) {
    return sampleAge <= 75_000 ? { label: 'Fresh', tone: 'ok' } : { label: 'Stale', tone: 'warn' }
  }
  if (logger.batteryVoltageFresh === true) return { label: 'Sample time unavailable', tone: 'warn' }
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
  current,
}: {
  label: string
  value: boolean | undefined
  activeLabel: string
  inactiveLabel: string
  activeHint: string
  inactiveHint: string
  current: boolean
}) {
  const known = typeof value === 'boolean'
  const tone: Tone = current && value === true ? 'ok' : 'default'
  const reportedLabel = known ? (value ? activeLabel : inactiveLabel) : 'Not reported'
  const stateLabel = current ? reportedLabel : known ? `Last reported: ${reportedLabel}` : 'Unknown'
  const hint = !current ? 'Current state is unknown until a fresh logger report arrives.'
    : known ? (value ? activeHint : inactiveHint) : 'Requires logger status schema v4.'

  return (
    <div className="rounded-xl border border-border/70 bg-surface-sunken/45 px-4 py-3">
      <div className="text-xs font-medium uppercase tracking-wide text-content-faint">{label}</div>
      <div className={cn('mt-1 text-base font-semibold', TONE_TEXT[tone])}>{stateLabel}</div>
      <div className="mt-1 text-xs leading-relaxed text-content-faint">{hint}</div>
    </div>
  )
}

function buildText(value: unknown): string {
  return typeof value === 'string' && value.trim().length > 0 && value.length <= 64
    ? value : 'Not reported'
}

function revision(value: unknown): string {
  return typeof value === 'string' && /^[a-f0-9]{7,40}$/i.test(value)
    ? value.slice(0, 12) : 'Not reported'
}

function VersionEvidence({
  logger, backendInfo, backendInfoLoading, backendInfoError, backendCheckedAt,
}: {
  logger?: OBDLoggerStatus | null
  backendInfo?: Record<string, unknown>
  backendInfoLoading?: boolean
  backendInfoError?: boolean
  backendCheckedAt?: string | null
}) {
  return (
    <div className="grid gap-4 border-t border-border px-5 py-4 sm:grid-cols-2" aria-label="Installed software versions">
      <div>
        <div className="text-xs font-medium uppercase tracking-wide text-content-faint">Companion · last reported</div>
        <div className="mt-1 text-sm font-medium">
          {buildText(logger?.appVersionName ?? logger?.loggerVersion)}
          {Number.isSafeInteger(logger?.appVersionCode) && (logger?.appVersionCode ?? -1) >= 0
            ? ` (code ${logger?.appVersionCode})` : ''}
        </div>
        <div className="mt-1 text-xs text-content-muted">
          Build <span className="font-mono">{revision(logger?.buildGitSha)}</span>
          {' · '}Poll plan {Number.isSafeInteger(logger?.pollPlanVersion) && (logger?.pollPlanVersion ?? -1) >= 0
            ? logger?.pollPlanVersion : 'not reported'}
        </div>
        {logger?.pollPlanVersion === 8 && <p className="mt-1 text-xs text-content-muted">3-second speed, RPM and air-flow trial · last reported configuration</p>}
      </div>
      <div>
        <div className="text-xs font-medium uppercase tracking-wide text-content-faint">Backend · running server</div>
        <div className="mt-1 text-sm font-medium">
          {backendInfoError ? 'Version check unavailable' : backendInfoLoading ? 'Checking version…' : buildText(backendInfo?.version)}
        </div>
        <div className="mt-1 text-xs text-content-muted">
          Revision <span className="font-mono">{revision(backendInfo?.sourceRevision)}</span>
          {backendInfoError && backendInfo ? ' · last successful check' : ''}
        </div>
        <div className="mt-1 text-xs text-content-faint">
          {loggerTimestampMillis(backendCheckedAt) !== null
            ? <>Last checked <time dateTime={backendCheckedAt ?? undefined} title={formatDateTime(backendCheckedAt)}>{formatRelative(backendCheckedAt)}</time></>
            : 'Check time not reported'}
        </div>
      </div>
    </div>
  )
}

export function ObdLoggerCard({
  logger,
  checkedAt,
  unitOnline,
  backendInfo,
  backendInfoLoading,
  backendInfoError,
  backendCheckedAt,
}: {
  logger: OBDLoggerStatus | null | undefined
  checkedAt: string | null | undefined
  unitOnline?: boolean | null
  backendInfo?: Record<string, unknown>
  backendInfoLoading?: boolean
  backendInfoError?: boolean
  backendCheckedAt?: string | null
}) {
  const [nowMs, setNowMs] = useState(Date.now)
  useEffect(() => {
    const timer = setInterval(() => setNowMs(Date.now()), 1_000)
    return () => clearInterval(timer)
  }, [])
  const freshness = loggerEvidenceFreshness(logger?.updatedAtUtc, unitOnline, nowMs)
  const versions = <VersionEvidence logger={logger} backendInfo={backendInfo}
    backendInfoLoading={backendInfoLoading} backendInfoError={backendInfoError} backendCheckedAt={backendCheckedAt} />
  if (!logger) {
    return (
      <section className="card mb-6 px-5 py-4" aria-label="OBD logger state">
        <div className="font-medium">Logger state unavailable</div>
        <p className="mt-1 text-sm text-content-muted">
          The server has not read a status document from the dashcam yet. No adapter, ECU, or
          engine connection is being inferred.
        </p>
        {versions}
      </section>
    )
  }

  const quality = voltageQuality(logger, nowMs)
  const owner = ownerStatus(logger)
  const voltage =
    typeof logger.batteryVoltage === 'number' && Number.isFinite(logger.batteryVoltage)
      ? `${logger.batteryVoltage.toFixed(1)} V`
      : 'Unavailable'
  const voltageAge = loggerTimestampMillis(logger.batteryVoltageSampleAtUtc) !== null
    ? `sampled ${formatRelative(logger.batteryVoltageSampleAtUtc)}`
    : 'sample time not reported'
  const adapterReachable = logger.adapterReachable === true ? quality.label === 'Fresh' : logger.adapterReachable
  const adapterOnly = freshness.current && adapterReachable === true && logger.ecuConnected !== true

  return (
    <section className="card mb-6 overflow-hidden" aria-label="OBD logger state">
      <div className="flex flex-wrap items-start justify-between gap-3 border-b border-border px-5 py-4">
        <div>
          <h3 className="font-semibold">{freshness.current ? 'Live logger evidence' : 'Last reported logger evidence'}</h3>
          <p className="mt-0.5 text-sm text-content-muted">
            Adapter voltage, BLE ownership, and ECU evidence are reported independently.
          </p>
          <p className={cn('mt-2 text-sm', TONE_TEXT[freshness.tone])} role="status">
            {freshness.label}. {freshness.hint}
          </p>
        </div>
        <span className={cn('badge', TONE_BADGE[freshness.current ? stateTone(logger.state) : 'default'])}>
          {logger.state ? `${freshness.current ? '' : 'Last reported: '}${humanize(logger.state)}` : 'State not reported'}
        </span>
      </div>

      <div className="grid gap-4 p-5 lg:grid-cols-2">
        <div className="rounded-xl border border-border/70 p-4">
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

        <div className="rounded-xl border border-border/70 p-4">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="text-sm font-medium text-content-muted">BLE owner (last report)</div>
            <span className={cn('badge', TONE_BADGE[freshness.current ? owner.tone : 'default'])}>{owner.label}</span>
          </div>
          <div className={cn('mt-2 text-2xl font-bold', TONE_TEXT[freshness.current ? owner.tone : 'default'])}>
            {freshness.current ? owner.label : 'Current owner unknown'}
          </div>
          <p className="mt-2 text-xs leading-relaxed text-content-muted">
            {freshness.current ? owner.hint : `Last reported owner: ${owner.label}. A fresh logger report is needed to confirm BLE ownership.`}
          </p>
          {logger.voltageOnlyMode === true && (
            <div className="mt-3 rounded-lg bg-accent-muted/50 px-3 py-2 text-xs text-content">
              {freshness.current ? 'Controlled ATRV-only mode is active; ECU initialisation is fenced off.'
                : 'The last report had controlled ATRV-only mode enabled.'}
            </div>
          )}
          <div className="mt-3 text-xs text-content-faint">
            {logger.headUnitState
              ? `Head unit last reported ${humanize(logger.headUnitState).toLowerCase()}`
              : 'Head-unit state not reported'}
            {loggerTimestampMillis(logger.updatedAtUtc) !== null
              ? ` · logger reported ${formatRelative(logger.updatedAtUtc)}`
              : ' · producer time not reported'}
            {loggerTimestampMillis(checkedAt) !== null ? ` · server checked ${formatRelative(checkedAt)}` : ''}
          </div>
        </div>
      </div>

      <div className="grid gap-3 px-5 pb-5 sm:grid-cols-2 xl:grid-cols-4">
        <ConnectionState
          label="Adapter reachable"
          current={freshness.current}
          value={adapterReachable}
          activeLabel="ATRV reply"
          inactiveLabel="No fresh reply"
          activeHint="The ELM adapter answered a recent voltage-only request."
          inactiveHint="No fresh valid adapter-voltage reply is available."
        />
        <ConnectionState
          label="BLE session"
          current={freshness.current}
          value={logger.adapterConnected}
          activeLabel="Connected"
          inactiveLabel="Closed"
          activeHint="A temporary or full BLE GATT session is open."
          inactiveHint="The BLE GATT session is closed after the parked read."
        />
        <ConnectionState
          label="ECU"
          current={freshness.current}
          value={logger.ecuConnected}
          activeLabel="Online"
          inactiveLabel="Offline"
          activeHint="A checksum-valid ECU response has been accepted."
          inactiveHint="No checksum-valid ECU response is active."
        />
        <ConnectionState
          label="Engine"
          current={freshness.current}
          value={logger.engineRunning}
          activeLabel="Running"
          inactiveLabel="Off"
          activeHint="The active drive has live ECU evidence."
          inactiveHint="No live engine-running evidence is active."
        />
      </div>

      {versions}

      {adapterOnly && (
        <div className="border-t border-border bg-accent-muted/35 px-5 py-3 text-sm text-content-muted">
          <span className="font-medium text-content">Voltage-only adapter contact.</span> The ELM
          answered ATRV, while the full OBD session, ECU, and engine remain separate states.
        </div>
      )}
    </section>
  )
}
