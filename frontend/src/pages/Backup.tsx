import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'

import { EmptyState, ErrorState, PageHeader, ProgressBar, StatTile } from '@/components/ui'
import { api } from '@/lib/api'
import type { IngestStatus } from '@/lib/api'
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

  const invalidate = () => {
    client.invalidateQueries({ queryKey: ['ingest-status'] })
    client.invalidateQueries({ queryKey: ['ingest-history'] })
  }
  const pullNow = useMutation({ mutationFn: api.ingest.run, onSuccess: invalidate })
  const cancel = useMutation({ mutationFn: api.ingest.cancel, onSuccess: invalidate })
  // Deliberately not invalidating anything: this changes what is on the car's screen, not
  // anything this page displays.
  const showTest = useMutation({ mutationFn: api.ingest.showTest })

  if (status.isError) return <ErrorState error={status.error} retry={() => status.refetch()} />

  const data = status.data
  const running = data?.state === 'running'
  const descriptor = data ? STATES[data.state] ?? STATES.idle : STATES.idle
  // Clamped: the byte counter meters the socket, so it also carries the tar headers and
  // padding that the file sizes it is measured against do not. That is a few kilobytes on
  // a full card — invisible, until it renders as 100.01% and a bar overshooting its track.
  const fraction =
    data && data.bytesTotal > 0 ? Math.min(1, data.bytesDone / data.bytesTotal) : 0

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

      <div className="mb-6 grid grid-cols-2 gap-3 lg:grid-cols-4">
        <StatTile
          label="Status"
          value={descriptor.label}
          hint={running && data ? PHASES[data.phase] : undefined}
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
          label="Still on the camera"
          value={formatBytes(data?.backlogBytes ?? 0)}
          hint={`${data?.backlogFiles ?? 0} file${(data?.backlogFiles ?? 0) === 1 ? '' : 's'}`}
          tone={(data?.backlogFiles ?? 0) > 0 ? 'warn' : 'ok'}
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
            <div className="text-sm font-medium">
              {PHASES[data.phase]}
              {data.filesTotal > 0 && (
                <span className="ml-2 font-normal text-content-muted">
                  {data.filesDone} of {data.filesTotal} files
                </span>
              )}
            </div>
            <div className="tabular text-sm text-content-muted">
              {formatBytes(data.bytesDone)} of {formatBytes(data.bytesTotal)}
            </div>
          </div>
          <ProgressBar value={fraction} />
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

      {data?.lastError && !running && (
        <div className="card mb-6 px-5 py-4 text-sm">
          <div className="font-medium text-state-warn">Last attempt reported a problem</div>
          <div className="mt-1 break-words text-content-muted">{data.lastError}</div>
        </div>
      )}

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
