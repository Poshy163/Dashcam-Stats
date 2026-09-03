import { useEffect, useState } from 'react'
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'

import { ObdLoggerCard } from '@/components/ObdLoggerCard'
import { ObdAppEventTimeline } from '@/components/ObdAppEventTimeline'
import Spinner from '@/components/Spinner'
import { EmptyState, ErrorState, PageHeader, ProgressBar, StatTile } from '@/components/ui'
import { api } from '@/lib/api'
import type {
  IngestRadioDeviceState,
  IngestRadioStatus,
  IngestStatus,
  OBDBundle,
} from '@/lib/api'
import { formatBytes, formatDateTime, formatDuration, formatRelative } from '@/lib/format'

/** How the state reads to a person, and how alarming it should look. */
const STATES: Record<IngestStatus['state'], { label: string; tone: 'default' | 'ok' | 'warn' | 'error' | 'busy' }> = {
  disabled: { label: 'Off', tone: 'default' },
  idle: { label: 'Up to date', tone: 'ok' },
  running: { label: 'Copying', tone: 'busy' },
  ok: { label: 'Up to date', tone: 'ok' },
  partial: { label: 'Partly copied', tone: 'warn' },
  error: { label: 'Failed', tone: 'error' },
  offline: { label: 'Car not here', tone: 'default' },
  unauthorized: { label: 'Not authorised', tone: 'error' },
  cancelled: { label: 'Cancelled', tone: 'warn' },
}

/**
 * What a running transfer is doing right now.
 *
 * Worth showing separately from the state because the first few seconds of a window are
 * not idle time and used to look like it: "Copying" appeared the moment the car did, while
 * the app was still listing the card, and a progress bar sitting at zero with no
 * explanation reads as a transfer that is not working.
 */
const PHASES: Record<IngestStatus['phase'], string> = {
  idle: 'Waiting',
  connecting: 'Connecting to the dashcam',
  scanning: 'Reading the memory card',
  preparing: 'Working out what to copy',
  transferring: 'Copying',
  verifying: 'Checking what arrived',
}

type RadioTone = 'default' | 'ok' | 'warn' | 'error' | 'busy'

function radioSummary(status: IngestRadioStatus, backupRunning: boolean): {
  title: string
  detail: string
  tone: RadioTone
} {
  const transition = status.transition
  // Durable recovery wins over the current setting. A user may switch quieting off after
  // an interrupted run, but that does not cancel the obligation to restore what it changed.
  if (transition?.recoveryRequired) {
    return {
      title: 'Radio recovery still required',
      detail:
        'The original Bluetooth or hotspot state has not been verified yet. Recovery will retry when the dashcam is reachable.',
      tone: 'error',
    }
  }
  const changed =
    transition?.bluetooth.disableAttempted || transition?.hotspot.disableAttempted || false
  const restored =
    !!transition &&
    (!transition.bluetooth.disableAttempted || transition.bluetooth.restoreVerified) &&
    (!transition.hotspot.disableAttempted || transition.hotspot.restoreVerified)
  const quietVerified =
    !!transition && transition.bluetooth.disableVerified && transition.hotspot.disableVerified
  if (transition?.active) {
    if (['restoring_radios', 'resuming_obd'].includes(transition.phase)) {
      if (backupRunning) {
        return {
          title: 'Backup continuing while radios recover',
          detail:
            'The quiet-radio safety window ended before the copy did. The remaining files are still transferring while the original radio state is restored and verified.',
          tone: 'warn',
        }
      }
      return {
        title: 'Restoring the original radio state',
        detail: 'The backup has finished using the quiet-radio window and is verifying recovery.',
        tone: 'busy',
      }
    }
    if (transition.phase === 'ingesting') {
      if (changed && restored) {
        return {
          title: 'Backup continuing with radios restored',
          detail:
            'The safety deadline restored the radios while the remaining files finish copying.',
          tone: 'warn',
        }
      }
      if (!changed && quietVerified) {
        return {
          title: 'Backup transfer active',
          detail: 'The radios were already in a safe state, so no change was needed.',
          tone: 'busy',
        }
      }
      return {
        title: 'Backup radio window active',
        detail: 'Any radio changed by the app will be returned to its captured starting state.',
        tone: 'busy',
      }
    }
    return {
      title: 'Preparing a safe radio transition',
      detail: 'The starting state is captured before Bluetooth or the hotspot can be changed.',
      tone: 'busy',
    }
  }
  if (!status.quietingEnabled) {
    return {
      title: 'Radio quieting is off',
      detail: 'Backups leave Bluetooth and the hotspot in their current state.',
      tone: 'default',
    }
  }
  if (!transition) {
    return {
      title: 'Radio quieting is ready',
      detail: 'No backup has needed to change either radio yet.',
      tone: 'ok',
    }
  }

  if (transition.phase === 'failed') {
    if (changed && restored) {
      return {
        title: 'Radios restored after an interrupted transition',
        detail: 'Every attempted radio change was restored, but the transition ended early.',
        tone: 'warn',
      }
    }
    if (!changed && quietVerified) {
      return {
        title: 'No radio change was needed',
        detail: 'The captured radio state was already safe, but the transition ended early.',
        tone: 'warn',
      }
    }
    if (!changed) {
      return {
        title: 'Radio quieting could not start',
        detail: 'The safety checks stopped the latest transition before either radio was changed.',
        tone: 'warn',
      }
    }
    return {
      title: 'Radio restore was not verified',
      detail: 'The latest transition failed and its recorded restore evidence is incomplete.',
      tone: 'error',
    }
  }
  if (changed && restored) {
    return {
      title: 'Radio recovery checks passed',
      detail: 'The latest backup passed every required Bluetooth and hotspot recovery check.',
      tone: 'ok',
    }
  }
  if (!changed) {
    return {
      title: 'Radios left unchanged',
      detail: 'The latest transition made no radio change that needed restoring.',
      tone: 'ok',
    }
  }
  return {
    title: 'Radio restore was not verified',
    detail: 'The latest transition is closed, but its recorded restore evidence is incomplete.',
    tone: 'error',
  }
}

function radioEvidence(label: string, radio: IngestRadioDeviceState, active: boolean): string {
  const baseline =
    radio.baseline === 'transport'
      ? 'carried the transfer'
      : radio.baseline === 'unknown'
        ? 'was not captured'
        : `was ${radio.baseline}`

  if (!radio.disableAttempted) {
    if (radio.disableVerified && radio.baseline === 'off') return `${label} was already off.`
    if (radio.disableVerified && radio.baseline === 'transport') {
      return `${label} carried the transfer and was intentionally left alone.`
    }
    return `${label} baseline ${baseline}; no change was attempted.`
  }

  const quiet = radio.disableVerified ? 'off verified' : 'off not verified'
  const restore = radio.restoreAttempted
    ? radio.restoreVerified
      ? 'restore verified'
      : 'restore not verified'
    : active
      ? 'restore pending'
      : 'restore not attempted'
  return `${label} ${baseline}; ${quiet}; ${restore}.`
}

export default function Backup() {
  const client = useQueryClient()

  const status = useQuery({
    queryKey: ['ingest-status'],
    queryFn: api.ingest.status,
    // The window is only a minute or two, so track it closely while it is open and stop
    // hammering the endpoint the rest of the day.
    refetchInterval: (query) => (query.state.data?.state === 'running' ? 1_500 : 15_000),
  })

  const history = useQuery({
    queryKey: ['ingest-history'],
    queryFn: () => api.ingest.history({ pageSize: 20 }),
    refetchInterval: status.data?.state === 'running' ? 10_000 : 60_000,
  })

  const radioStatus = useQuery({
    queryKey: ['ingest-radio-status'],
    queryFn: api.ingest.radioStatus,
    refetchInterval: status.data?.state === 'running' ? 1_500 : 15_000,
  })

  const obdStatus = useQuery({
    queryKey: ['obd-status'],
    queryFn: api.obd.status,
    refetchInterval: 10_000,
  })
  const obdBundles = useQuery({
    queryKey: ['obd-bundles'],
    queryFn: () => api.obd.bundles({ pageSize: 10 }),
    refetchInterval: 15_000,
  })
  const obdEvents = useInfiniteQuery({
    queryKey: ['obd-app-events'],
    queryFn: ({ pageParam }) => api.obd.events({ page: pageParam, pageSize: 40 }),
    initialPageParam: 1,
    getNextPageParam: (lastPage) =>
      lastPage.page < lastPage.pages ? lastPage.page + 1 : undefined,
    refetchInterval: 10_000,
  })
  const obdEventItems = obdEvents.data?.pages.flatMap((page) => page.items) ?? []
  const obdEventTotal = obdEvents.data?.pages[0]?.total ?? 0

  const invalidate = () => {
    client.invalidateQueries({ queryKey: ['ingest-status'] })
    client.invalidateQueries({ queryKey: ['ingest-history'] })
    client.invalidateQueries({ queryKey: ['ingest-radio-status'] })
    client.invalidateQueries({ queryKey: ['obd-status'] })
    client.invalidateQueries({ queryKey: ['obd-bundles'] })
    client.invalidateQueries({ queryKey: ['obd-app-events'] })
  }
  const pullNow = useMutation({ mutationFn: api.ingest.run, onSuccess: invalidate })
  const cancel = useMutation({ mutationFn: api.ingest.cancel, onSuccess: invalidate })
  // Deliberately not invalidating anything: this changes what is on the car's screen, not
  // anything this page displays.
  const showTest = useMutation({ mutationFn: api.ingest.showTest })
  const retryObd = useMutation({ mutationFn: (id: number) => api.obd.retry(id), onSuccess: invalidate })
  const validateObd = useMutation({
    mutationFn: (id: number) => api.obd.validate(id),
    onSuccess: invalidate,
  })
  const rebuildObd = useMutation({ mutationFn: api.obd.rebuild, onSuccess: invalidate })

  const data = status.data
  const [countdownOffset, setCountdownOffset] = useState<number>(0)

  useEffect(() => {
    setCountdownOffset(0)
  }, [data?.sleepCountdownRemainingS, data?.ignitionState])

  useEffect(() => {
    if (
      !data?.unitOnline ||
      data?.ignitionState === 'on' ||
      data?.sleepCountdownRemainingS === null ||
      data?.sleepCountdownRemainingS === undefined
    ) {
      return
    }
    const timer = setInterval(() => {
      setCountdownOffset((prev) => prev + 1)
    }, 1000)
    return () => clearInterval(timer)
  }, [data?.unitOnline, data?.ignitionState, data?.sleepCountdownRemainingS])

  if (status.isError) return <ErrorState error={status.error} retry={() => status.refetch()} />

  const liveCountdown =
    data?.sleepCountdownRemainingS !== null && data?.sleepCountdownRemainingS !== undefined
      ? Math.max(0, data.sleepCountdownRemainingS - countdownOffset)
      : null
  const prediction = data?.sleepWindowPrediction
  const eventSequenceGap = obdStatus.data?.eventStream?.sequenceGap ?? 0
  const running = data?.state === 'running'
  const transition = radioStatus.data?.transition
  const recoveryBlocked = !running && transition?.recoveryRequired === true
  const transitionBusy = !running && transition?.active === true
  const descriptor = recoveryBlocked
    ? { label: 'Recovery needed', tone: 'error' as const }
    : transitionBusy
      ? ['restoring_radios', 'resuming_obd'].includes(transition?.phase ?? '')
        ? { label: 'Restoring radios', tone: 'busy' as const }
        : { label: 'Preparing backup', tone: 'busy' as const }
      : data
        ? STATES[data.state] ?? STATES.idle
        : { label: 'Checking', tone: 'busy' as const }
  const statusHint = running && data
    ? PHASES[data.phase]
    : recoveryBlocked
      ? 'Backup blocked until radios recover'
      : transitionBusy
        ? ['restoring_radios', 'resuming_obd'].includes(transition?.phase ?? '')
          ? 'Verifying the original radio state'
          : 'Making the radio transition safe'
        : undefined
  const backlogKnown = data?.backlogKnown === true
  const backlogHint = backlogKnown && data
    ? `${data.backlogFiles} file${data.backlogFiles === 1 ? '' : 's'}`
    : recoveryBlocked
      ? 'Not checked; waiting for radio recovery'
      : 'Not checked yet'
  // Clamped: the byte counter meters the socket, so it also carries the tar headers and
  // padding that the file sizes it is measured against do not. That is a few kilobytes on
  // a full card — invisible, until it renders as 100.01% and a bar overshooting its track.
  const fraction =
    data && data.bytesTotal > 0 ? Math.min(1, data.bytesDone / data.bytesTotal) : 0
  const radio = radioStatus.data ? radioSummary(radioStatus.data, running) : null
  const radioToneClass =
    radio?.tone === 'error'
      ? 'border-state-error/50'
      : radio?.tone === 'warn'
        ? 'border-state-warn/40'
        : radio?.tone === 'ok'
          ? 'border-state-ok/40'
          : radio?.tone === 'busy'
            ? 'border-accent/40'
            : ''
  const radioTitleClass =
    radio?.tone === 'error'
      ? 'text-state-error'
      : radio?.tone === 'warn'
        ? 'text-state-warn'
        : radio?.tone === 'ok'
          ? 'text-state-ok'
          : radio?.tone === 'busy'
            ? 'text-accent'
            : ''

  return (
    <div>
      <PageHeader
        title="Backup"
        subtitle="Copies new recordings off the dashcam while the car is on the driveway."
        actions={
          <>
            <Link className="btn" to="/settings?category=ingest">
              Settings
            </Link>
            <button
              className="btn"
              disabled={showTest.isPending}
              onClick={() => showTest.mutate()}
              title="Open this page on the dashcam's own screen right now"
            >
              {showTest.isPending ? 'Opening…' : 'Test car screen'}
            </button>
            {running ? (
              <button className="btn" disabled={cancel.isPending} onClick={() => cancel.mutate()}>
                Cancel
              </button>
            ) : (
              <button
                className="btn btn-primary"
                disabled={pullNow.isPending || data?.state === 'disabled'}
                onClick={() => pullNow.mutate()}
              >
                Pull now
              </button>
            )}
          </>
        }
      />

      {showTest.isSuccess && (
        <div className="card mb-6 border-state-ok/40 px-5 py-4 text-sm">
          <span className="font-medium text-state-ok">Opened on the dashcam.</span>{' '}
          <span className="text-content-muted">
            The car&rsquo;s screen should now be showing{' '}
            <code className="text-xs">{showTest.data.url}</code>. Nothing puts the previous
            screen back afterwards.
          </span>
        </div>
      )}
      {showTest.isError && (
        <div className="card mb-6 border-state-error/40 px-5 py-4 text-sm">
          <span className="font-medium text-state-error">Could not open it on the dashcam.</span>{' '}
          <span className="text-content-muted">
            {showTest.error instanceof Error ? showTest.error.message : 'Something went wrong'}
          </span>
        </div>
      )}

      {data?.state === 'disabled' && (
        <div className="card mb-6 px-5 py-4 text-sm text-content-muted">
          Backup is switched off. Turn it on in{' '}
          <Link className="link" to="/settings?category=ingest">
            Settings → Backup / Ingest
          </Link>{' '}
          and give it the head unit&rsquo;s address.
        </div>
      )}

      {data?.wifiBandHold && data.state !== 'disabled' && (
        <div className="card mb-6 border-state-warn/40 px-5 py-4 text-sm">
          <div className="font-medium text-state-warn">
            Waiting for 5&nbsp;GHz WiFi
            {data.wifiFrequencyMhz ? ` — the dashcam joined on ${data.wifiFrequencyMhz} MHz` : ''}
          </div>
          <div className="mt-1 text-content-muted">
            {data.wifiBandHoldReason ??
              '2.4 GHz moves about 5 MB/s against 32 on 5 GHz, so the transfer is held rather than spent on the slow band. It is re-checked every half minute while the car is here.'}
          </div>
        </div>
      )}

      {data?.arrivalHold && data.state !== 'disabled' && (
        <div className="card mb-6 border-state-warn/40 px-5 py-4 text-sm">
          <div className="font-medium text-state-warn">Waiting until you&rsquo;re home</div>
          <div className="mt-1 text-content-muted">
            {data.arrivalHoldReason ??
              'The dashcam has only just powered on, which usually means the car is setting off. The backup waits until it has been running a while — so footage is pulled when you arrive rather than as you leave — and re-checks every few seconds while the car is here.'}
          </div>
        </div>
      )}

      {data?.ignitionHold && data.state !== 'disabled' && (
        <div className="card mb-6 border-state-warn/40 px-5 py-4 text-sm">
          <div className="font-medium text-state-warn">Waiting for the ignition to go off</div>
          <div className="mt-1 text-content-muted">
            {data.ignitionHoldReason ??
              'A backup turns the dashcam’s Bluetooth off and drops its hotspot, and wireless CarPlay runs over both. So while the car is switched on nothing is touched; the copy runs in the window after the ignition goes off, and is re-checked every half minute while the car is here.'}
          </div>
        </div>
      )}

      {data?.recorderHealth && data.state !== 'disabled' && (
        <div
          className={`card mb-6 px-5 py-4 text-sm ${
            data.recorderHealthOk ? 'border-state-ok/40' : 'border-state-error/50'
          }`}
        >
          <div
            className={`font-medium ${
              data.recorderHealthOk ? 'text-state-ok' : 'text-state-error'
            }`}
          >
            {data.recorderHealthOk ? 'Recorder healthy' : 'Recording problem detected'}
          </div>
          <div className="mt-1 break-words text-content-muted">{data.recorderHealth}</div>
          {data.recorderHealthAt && (
            <div className="mt-1 text-xs text-content-muted">
              Checked {formatRelative(data.recorderHealthAt)}
            </div>
          )}
        </div>
      )}

      {radio && (
        <div className={`card mb-6 px-5 py-4 text-sm ${radioToneClass}`}>
          <div className={`font-medium ${radioTitleClass}`}>{radio.title}</div>
          <div className="mt-1 text-content-muted">{radio.detail}</div>
          {radioStatus.data?.transition && (
            <div className="mt-2 space-y-0.5 text-xs text-content-faint">
              <div>
                {radioEvidence(
                  'Bluetooth',
                  radioStatus.data.transition.bluetooth,
                  radioStatus.data.transition.active,
                )}
              </div>
              <div>
                {radioEvidence(
                  'Hotspot',
                  radioStatus.data.transition.hotspot,
                  radioStatus.data.transition.active,
                )}
              </div>
              <div>Updated {formatRelative(radioStatus.data.transition.updatedAt)}</div>
            </div>
          )}
        </div>
      )}

      {radioStatus.isError && (
        <div className="card mb-6 border-state-warn/40 px-5 py-4 text-sm">
          <div className="font-medium text-state-warn">Radio safety status unavailable</div>
          <div className="mt-1 text-content-muted">
            Backup progress is still available, but its durable radio-transition evidence could
            not be loaded.
          </div>
        </div>
      )}

      {data?.state === 'unauthorized' && (
        <div className="card mb-6 border-state-error/40 px-5 py-4 text-sm">
          <div className="font-medium text-state-error">The head unit has not authorised this app.</div>
          <div className="mt-1 text-content-muted">
            Start the car, then accept the &ldquo;Allow USB debugging?&rdquo; prompt on the
            dashcam&rsquo;s own screen and tick &ldquo;Always allow&rdquo;. The key is kept on the
            data volume, so this only has to be done once.
          </div>
        </div>
      )}

      <div className="mb-6 grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
        <StatTile
          label="Status"
          value={descriptor.label}
          hint={statusHint}
          tone={descriptor.tone}
        />
        <StatTile
          label="Speed"
          value={running ? `${(data?.speedMbsRecent ?? 0).toFixed(1)} MB/s` : '—'}
          hint={
            running
              ? data?.etaSeconds
                ? `${formatDuration(data.etaSeconds)} left`
                : 'off the head unit'
              : undefined
          }
          tone={running ? 'busy' : 'default'}
        />
        <StatTile
          label="Wi-Fi"
          value={
            !data?.unitOnline
              ? '—'
              : data.wifiFrequencyMhz
                ? data.wifiFrequencyMhz >= 4900
                  ? '5 GHz'
                  : '2.4 GHz'
                : 'Connected'
          }
          hint={
            !data?.unitOnline
              ? 'Car is not here'
              : data.wifiFrequencyMhz
                ? data.wifiFrequencyMhz >= 4900
                  ? `${data.wifiFrequencyMhz} MHz • Fast link`
                  : `${data.wifiFrequencyMhz} MHz • Slow link`
                : 'Reading frequency…'
          }
          tone={
            !data?.unitOnline
              ? 'default'
              : data.wifiFrequencyMhz && data.wifiFrequencyMhz >= 4900
                ? 'ok'
                : data.wifiFrequencyMhz
                  ? 'warn'
                  : 'default'
          }
        />
        <StatTile
          label="Sleep countdown"
          value={
            !data?.unitOnline
              ? '—'
              : data.ignitionState === 'on'
                ? 'Engine on'
                : liveCountdown !== null && liveCountdown !== undefined
                  ? formatDuration(liveCountdown)
                  : data.sleepCountdownRemainingS !== null && data.sleepCountdownRemainingS !== undefined
                    ? formatDuration(data.sleepCountdownRemainingS)
                    : data.sleepWindowSeconds
                      ? formatDuration(data.sleepWindowSeconds)
                      : '—'
          }
          hint={
            !data?.unitOnline
              ? 'Car is not here'
              : data.ignitionState === 'on'
                ? `${formatDuration(data.sleepWindowSeconds ?? 1200)} window ready`
                : prediction?.summary ??
                  (liveCountdown !== null && liveCountdown !== undefined && liveCountdown > 0
                    ? 'Counting down to sleep'
                    : 'Awake window ended')
          }
          tone={
            !data?.unitOnline
              ? 'default'
              : data.ignitionState === 'on'
                ? 'default'
                : prediction
                  ? prediction.willPass
                    ? 'ok'
                    : 'warn'
                  : running
                    ? 'busy'
                    : 'default'
          }
        />
        <StatTile
          label="Still on the camera"
          value={backlogKnown && data ? formatBytes(data.backlogBytes) : '—'}
          hint={backlogHint}
          tone={
            backlogKnown
              ? (data?.backlogFiles ?? 0) > 0
                ? 'warn'
                : 'ok'
              : recoveryBlocked
                ? 'error'
                : 'default'
          }
        />
        <StatTile
          label="Last copied"
          value={data?.lastSuccessTs ? formatRelative(data.lastSuccessTs) : 'Never'}
          hint={data?.unitOnline ? 'car is on the network' : 'car is not here'}
          tone={data?.unitOnline ? 'ok' : 'default'}
        />
      </div>

      {running && data && (
        <div className="card mb-6 px-5 py-4">
          <div className="mb-2 flex flex-wrap items-baseline justify-between gap-2">
            <div className="flex flex-wrap items-center gap-2 text-sm font-medium">
              <span>{PHASES[data.phase]}</span>
              {data.filesTotal > 0 && (
                <span className="font-normal text-content-muted">
                  {data.filesDone} of {data.filesTotal} files
                </span>
              )}
              {data.wifiFrequencyMhz && (
                <span
                  className={`badge ${
                    data.wifiFrequencyMhz >= 4900
                      ? 'bg-state-ok/15 text-state-ok'
                      : 'bg-state-warn/15 text-state-warn'
                  }`}
                >
                  {data.wifiFrequencyMhz >= 4900 ? '5 GHz' : '2.4 GHz'} ({data.wifiFrequencyMhz} MHz)
                </span>
              )}
              {liveCountdown !== null && liveCountdown !== undefined && (
                <span
                  className={`badge ${
                    prediction?.willPass
                      ? 'bg-state-ok/15 text-state-ok'
                      : prediction?.willPass === false
                        ? 'bg-state-warn/15 text-state-warn'
                        : 'bg-surface-sunken text-content-muted'
                  }`}
                  title={prediction?.summary}
                >
                  ⏱️ Sleep in {formatDuration(liveCountdown)}
                  {prediction &&
                    (prediction.willPass
                      ? ` • Will pass (+${formatDuration(prediction.headroomS)})`
                      : ` • Short by ${formatDuration(Math.abs(prediction.headroomS))}`)}
                </span>
              )}
            </div>
            <div className="tabular text-sm text-content-muted">
              {formatBytes(data.bytesDone)} of {formatBytes(data.bytesTotal)}
            </div>
          </div>
          <ProgressBar value={fraction} />
          {prediction && (
            <div
              className={`mt-2 flex items-center gap-1.5 text-xs font-medium ${
                prediction.willPass ? 'text-state-ok' : 'text-state-warn'
              }`}
            >
              <span>{prediction.willPass ? '✓' : '⚠'}</span>
              <span>{prediction.summary}</span>
            </div>
          )}
          <div className="mt-2 flex flex-wrap items-baseline justify-between gap-2 text-xs text-content-faint">
            <span className="truncate">{data.currentFile ?? ' '}</span>
            {data.activeSkipped > 0 && (
              <span>
                {data.activeSkipped} still recording, left alone
              </span>
            )}
          </div>
        </div>
      )}

      {data?.lastError && !running && !data.wifiBandHold && (
        <div className="card mb-6 px-5 py-4 text-sm">
          <div className="font-medium text-state-warn">Last attempt reported a problem</div>
          <div className="mt-1 break-words text-content-muted">{data.lastError}</div>
        </div>
      )}

      <div className="mb-3 mt-8 flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold">OBD telemetry</h2>
          <p className="mt-0.5 text-sm text-content-muted">
            Immutable drive bundles are copied separately, validated, then retried to Home
            Assistant without holding up footage.
          </p>
        </div>
        <button
          className="btn"
          disabled={rebuildObd.isPending}
          onClick={() => rebuildObd.mutate()}
        >
          {rebuildObd.isPending ? 'Rebuilding…' : 'Rebuild OBD queue'}
        </button>
      </div>

      {obdStatus.isError ? (
        <ErrorState error={obdStatus.error} retry={() => obdStatus.refetch()} />
      ) : (
        <>
          <div className="mb-6 grid grid-cols-2 gap-3 lg:grid-cols-4 xl:grid-cols-8">
            <StatTile
              label="Logger state"
              value={obdStatus.data?.logger?.state ?? (data?.unitOnline ? 'Unknown' : 'Car away')}
              hint={
                obdStatus.data?.logger?.lastDriveFinishedAtUtc
                  ? `last drive ${formatRelative(obdStatus.data.logger.lastDriveFinishedAtUtc)}`
                  : 'best-effort status from the unit'
              }
              tone={
                obdStatus.data?.logger?.ecuConnected === true ||
                (obdStatus.data?.logger?.ecuConnected === undefined &&
                  obdStatus.data?.logger?.state === 'ecu_online')
                  ? 'busy'
                  : 'default'
              }
            />
            <StatTile
              label="Waiting on camera"
              value={String(obdStatus.data?.waitingOnUnit ?? 0)}
              hint="completed drive bundles"
              tone={(obdStatus.data?.waitingOnUnit ?? 0) > 0 ? 'warn' : 'ok'}
            />
            <StatTile
              label="Waiting for HA"
              value={String(obdStatus.data?.waitingForHomeAssistant ?? 0)}
              hint={obdStatus.data?.currentImport ?? 'oldest drive is sent first'}
              tone={(obdStatus.data?.waitingForHomeAssistant ?? 0) > 0 ? 'busy' : 'ok'}
            />
            <StatTile
              label="Imported drives"
              value={String(obdStatus.data?.importedDriveCount ?? 0)}
              hint={
                obdStatus.data?.lastSuccessfulHomeAssistantSync
                  ? `last sync ${formatRelative(obdStatus.data.lastSuccessfulHomeAssistantSync)}`
                  : 'no successful sync yet'
              }
              tone="ok"
            />
            <StatTile
              label="Duplicate acks"
              value={String(obdStatus.data?.duplicateCount ?? 0)}
              hint={
                (obdStatus.data?.copyThroughputMbs ?? 0) > 0
                  ? `last copy ${obdStatus.data?.copyThroughputMbs.toFixed(1)} MB/s`
                  : 'safe idempotent HA replays'
              }
              tone="default"
            />
            <StatTile
              label="Failed bundles"
              value={String(obdStatus.data?.failedCount ?? 0)}
              hint={`${obdStatus.data?.importsLastHour ?? 0} imported in the last hour`}
              tone={(obdStatus.data?.failedCount ?? 0) > 0 ? 'error' : 'ok'}
            />
            <StatTile
              label="Last completed drive"
              value={
                obdStatus.data?.lastCompletedDrive?.driveId ??
                obdStatus.data?.logger?.lastDriveId ??
                'None'
              }
              hint={
                obdStatus.data?.lastCompletedDrive?.driveFinishedAt
                  ? `server copy ${formatRelative(obdStatus.data.lastCompletedDrive.driveFinishedAt)}`
                  : obdStatus.data?.logger?.lastDriveFinishedAtUtc
                    ? `unit ${formatRelative(obdStatus.data.logger.lastDriveFinishedAtUtc)}`
                    : 'no completed drive observed'
              }
              tone={obdStatus.data?.lastCompletedDrive ? 'ok' : 'default'}
            />
            <StatTile
              label="Home Assistant auth"
              value={
                obdStatus.data?.homeAssistantAuthentication === 'configured'
                  ? 'Configured'
                  : obdStatus.data?.homeAssistantAuthentication === 'invalid'
                    ? 'Invalid'
                    : 'Not configured'
              }
              hint={
                obdStatus.data?.homeAssistantConfigurationError ??
                'token loaded from a protected file; never displayed'
              }
              tone={
                obdStatus.data?.homeAssistantAuthentication === 'configured' ? 'ok' : 'warn'
              }
            />
          </div>

          <ObdLoggerCard
            logger={obdStatus.data?.logger}
            checkedAt={obdStatus.data?.loggerCheckedAt}
          />

          <section className="card mb-6 overflow-hidden" aria-label="Head-unit OBD app activity">
            <div className="flex flex-wrap items-start justify-between gap-3 border-b border-border px-5 py-4">
              <div>
                <h3 className="font-semibold">Head-unit activity</h3>
                <p className="mt-0.5 text-sm text-content-muted">
                  App-owned boot, connection, drive, handoff, and receipt evidence mirrored when
                  the car reaches home Wi-Fi.
                </p>
              </div>
              <span className="text-xs text-content-faint">
                {obdStatus.data?.eventStream?.lastReceivedAt
                  ? `synced ${formatRelative(obdStatus.data.eventStream.lastReceivedAt)}`
                  : obdEventItems.length > 0
                    ? 'waiting for the next device sync'
                  : obdStatus.data?.eventStream?.available
                    ? 'stream is empty'
                    : 'waiting for a compatible app'}
              </span>
            </div>
            <div className="p-5">
              {obdStatus.data?.eventStream?.lastError && (
                <div className="mb-3 rounded-lg bg-state-warn/10 px-3 py-2 text-sm text-state-warn">
                  The latest app event snapshot could not be mirrored. Backup and OBD data remain
                  independent and will retry next visit.
                </div>
              )}
              {eventSequenceGap > 0 && (
                <div className="mb-3 rounded-lg bg-state-warn/10 px-3 py-2 text-sm text-state-warn">
                  {eventSequenceGap.toLocaleString()} older app event(s) were no longer in the
                  device ring before the server could mirror them.
                </div>
              )}
              {obdEvents.isPending ? (
                <Spinner label="Loading head-unit activity" className="py-8" />
              ) : obdEvents.isError ? (
                <ErrorState error={obdEvents.error} retry={() => obdEvents.refetch()} />
              ) : (
                <>
                  <ObdAppEventTimeline events={obdEventItems} />
                  {obdEventTotal > 0 && (
                    <div className="mt-4 flex flex-wrap items-center justify-between gap-3 border-t border-border pt-4 text-xs text-content-faint">
                      <span>
                        Showing {obdEventItems.length.toLocaleString()} of{' '}
                        {obdEventTotal.toLocaleString()} retained events
                      </span>
                      {obdEvents.hasNextPage && (
                        <button
                          type="button"
                          className="btn"
                          disabled={obdEvents.isFetchingNextPage}
                          onClick={() => obdEvents.fetchNextPage()}
                        >
                          {obdEvents.isFetchingNextPage ? 'Loading…' : 'Load earlier activity'}
                        </button>
                      )}
                    </div>
                  )}
                </>
              )}
            </div>
          </section>

          {obdStatus.data?.homeAssistantAuthentication !== 'configured' && (
            <div className="card mb-6 border-state-warn/40 px-5 py-4 text-sm">
              <div className="font-medium text-state-warn">
                Home Assistant import is{' '}
                {obdStatus.data?.homeAssistantAuthentication === 'invalid'
                  ? 'misconfigured'
                  : 'not configured'}
              </div>
              <div className="mt-1 text-content-muted">
                {obdStatus.data?.homeAssistantConfigurationError ??
                  'Set HA_URL and mount HA_TOKEN_FILE as a Docker secret. The token is never shown here.'}
              </div>
            </div>
          )}

          {obdStatus.data && !obdStatus.data.workerRunning && (
            <div className="card mb-6 border-state-error/40 px-5 py-4 text-sm">
              <div className="font-medium text-state-error">OBD import worker is not running</div>
              <div className="mt-1 text-content-muted">
                Verified samples remain safe on this server, but Home Assistant delivery
                needs the service to be restarted or its server error investigated.
              </div>
            </div>
          )}

          {(obdStatus.data?.lastImportError || obdStatus.data?.lastCopyError) && (
            <div className="card mb-6 px-5 py-4 text-sm">
              <div className="font-medium text-state-warn">Last OBD problem</div>
              <div className="mt-1 break-words text-content-muted">
                {obdStatus.data.lastImportError ?? obdStatus.data.lastCopyError}
              </div>
            </div>
          )}
        </>
      )}

      {validateObd.data && (
        <div
          role="status"
          aria-live="polite"
          className={`card mb-4 px-4 py-3 text-sm ${
            validateObd.data.valid ? 'border-state-ok/40' : 'border-state-error/50'
          }`}
        >
          <div
            className={`font-medium ${
              validateObd.data.valid ? 'text-state-ok' : 'text-state-error'
            }`}
          >
            {validateObd.data.valid ? 'OBD bundle validated' : 'OBD bundle is still invalid'}
          </div>
          <div className="mt-1 break-words text-content-muted">
            {validateObd.data.valid
              ? `The retained archive passed validation. Current state: ${validateObd.data.bundle.state.replaceAll('_', ' ')}.`
              : (validateObd.data.bundle.lastError ??
                'The retained archive failed validation again. No data was deleted.')}
          </div>
        </div>
      )}

      {validateObd.isError && (
        <div
          role="alert"
          className="card mb-4 border-state-error/50 px-4 py-3 text-sm"
        >
          <div className="font-medium text-state-error">OBD validation request failed</div>
          <div className="mt-1 break-words text-content-muted">
            {validateObd.error instanceof Error ? validateObd.error.message : 'Something went wrong'}
          </div>
        </div>
      )}

      {obdBundles.data?.items.length ? (
        <div className="card mb-8 overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs uppercase tracking-wide text-content-faint">
                <th className="px-4 py-3 font-medium">Drive</th>
                <th className="px-4 py-3 font-medium">Samples</th>
                <th className="px-4 py-3 font-medium">State</th>
                <th className="px-4 py-3 font-medium">Attempts</th>
                <th className="px-4 py-3 font-medium"><span className="sr-only">Actions</span></th>
              </tr>
            </thead>
            <tbody>
              {obdBundles.data.items.map((bundle: OBDBundle) => (
                <tr key={bundle.id} className="border-t border-border">
                  <td className="whitespace-nowrap px-4 py-3">
                    {formatDateTime(bundle.driveStartedAt)}
                    <div className="max-w-56 truncate text-xs text-content-faint">
                      {bundle.driveId}
                    </div>
                  </td>
                  <td className="tabular px-4 py-3">{bundle.sampleCount}</td>
                  <td className="px-4 py-3">
                    <span
                      className={
                        bundle.state === 'imported'
                          ? 'text-state-ok'
                          : bundle.state === 'failed' || bundle.state === 'quarantined'
                            ? 'text-state-error'
                            : 'text-state-warn'
                      }
                    >
                      {bundle.state.replaceAll('_', ' ')}
                    </span>
                    {bundle.lastError && (
                      <div className="mt-0.5 max-w-md truncate text-xs text-content-faint">
                        {bundle.lastError}
                      </div>
                    )}
                    {!bundle.metadataTrusted && (
                      <div className="mt-0.5 text-xs text-content-faint">
                        Manifest untrusted — repair and validate before import
                      </div>
                    )}
                  </td>
                  <td className="tabular px-4 py-3">{bundle.attempts}</td>
                  <td className="whitespace-nowrap px-4 py-3 text-right">
                    <button
                      className="btn mr-2"
                      disabled={
                        validateObd.isPending ||
                        ['waiting_for_backup', 'copying', 'validating', 'importing'].includes(
                          bundle.state,
                        )
                      }
                      onClick={() => {
                        validateObd.reset()
                        validateObd.mutate(bundle.id)
                      }}
                    >
                      {validateObd.isPending && validateObd.variables === bundle.id
                        ? 'Validating…'
                        : 'Validate'}
                    </button>
                    {['ready_to_import', 'retry_wait', 'failed'].includes(bundle.state) && (
                      <button
                        className="btn"
                        disabled={retryObd.isPending}
                        onClick={() => retryObd.mutate(bundle.id)}
                      >
                        Retry
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}

      <h2 className="mb-3 text-lg font-semibold">Recent transfers</h2>
      {history.isError ? (
        <ErrorState error={history.error} retry={() => history.refetch()} />
      ) : !history.data?.items.length ? (
        <EmptyState
          title="Nothing copied yet"
          description="A transfer starts on its own the next time the car is on the driveway with the engine running."
        />
      ) : (
        <div className="card overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs uppercase tracking-wide text-content-faint">
                <th className="px-4 py-3 font-medium">When</th>
                <th className="px-4 py-3 font-medium">Result</th>
                <th className="px-4 py-3 font-medium">Files</th>
                <th className="px-4 py-3 font-medium">Size</th>
                <th className="px-4 py-3 font-medium">Speed</th>
              </tr>
            </thead>
            <tbody>
              {history.data.items.map((run) => (
                <tr key={run.id} className="border-t border-border">
                  <td className="whitespace-nowrap px-4 py-3">{formatDateTime(run.startedAt)}</td>
                  <td className="px-4 py-3">
                    <span className={run.state === 'ok' ? 'text-state-ok' : 'text-state-warn'}>
                      {STATES[run.state as IngestStatus['state']]?.label ?? run.state}
                    </span>
                    {run.error && (
                      <div className="mt-0.5 max-w-md truncate text-xs text-content-faint">
                        {run.error}
                      </div>
                    )}
                  </td>
                  <td className="tabular px-4 py-3">{run.filesTransferred}</td>
                  <td className="tabular px-4 py-3">{formatBytes(run.bytesTransferred)}</td>
                  <td className="tabular px-4 py-3">
                    {run.throughputMbsAvg ? `${run.throughputMbsAvg.toFixed(1)} MB/s` : '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
