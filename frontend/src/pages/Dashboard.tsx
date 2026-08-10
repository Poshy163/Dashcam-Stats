import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'

import Spinner from '@/components/Spinner'
import { EmptyState, ErrorState, JobStateBadge, PageHeader, ProgressBar, StatTile } from '@/components/ui'
import { api } from '@/lib/api'
import {
  formatBytes,
  formatDateTime,
  formatDistance,
  formatDuration,
  formatRelative,
  formatSpeed,
} from '@/lib/format'

export default function Dashboard() {
  const status = useQuery({
    queryKey: ['status'],
    queryFn: api.status,
    refetchInterval: 10_000,
  })
  const jobs = useQuery({
    queryKey: ['jobs', 'recent'],
    queryFn: () => api.jobs.list({ pageSize: 8 }),
    refetchInterval: 10_000,
  })

  if (status.isLoading) return <Spinner label="Loading dashboard…" className="py-24" />
  if (status.isError) return <ErrorState error={status.error} retry={() => status.refetch()} />
  if (!status.data) return null

  const { totals, processing, storage, latestJourney, hardware, features } = status.data
  const storagePct = storage.limitBytes ? storage.usedBytes / storage.limitBytes : 0

  // A count is only worth reading once the thing producing it was working. Detection ran
  // against models that could not be downloaded for the whole of this library's history,
  // so "0 vehicles seen" over 674 recordings was accurate and told the user the opposite
  // of the truth. Where a feature cannot vouch for its own number, the tile says so
  // instead of showing it.
  const blocked = (key: string) => features?.find((f) => f.key === key && f.blockedReason)

  return (
    <div className="space-y-5">
      <PageHeader
        title="Dashboard"
        subtitle={`${totals.recordings} recordings · ${formatDuration(totals.durationS)} of footage`}
      />

      {hardware.notes.length > 0 && (
        <div className="card border-state-warn/40 p-3">
          <div className="text-xs font-medium uppercase tracking-wide text-state-warn">
            Hardware notes
          </div>
          <ul className="mt-1.5 space-y-1">
            {hardware.notes.map((note) => (
              <li key={note} className="text-sm text-content-muted">
                {note}
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 lg:grid-cols-6">
        <StatTile label="Recordings" value={totals.recordings} />
        <StatTile label="Journeys" value={totals.journeys} />
        <StatTile label="Footage" value={formatBytes(totals.footageBytes)} hint={`${totals.footageFiles} files`} />
        <StatTile
          label="Vehicles seen"
          value={blocked('detection') ? '—' : totals.trackedObjects}
          tone={blocked('detection') ? 'warn' : 'default'}
          hint={blocked('detection') ? 'Not available' : undefined}
        />
        <StatTile
          label="Unique plates"
          value={blocked('plates') ? '—' : totals.plates}
          tone={blocked('plates') ? 'warn' : 'default'}
          hint={blocked('plates') ? 'Not available' : undefined}
        />
        <StatTile label="Telemetry points" value={totals.telemetryPoints.toLocaleString()} />
      </div>

      {features && features.some((f) => f.blockedReason) && (
        <div className="card border-state-warn/40 p-3">
          <div className="text-xs font-medium uppercase tracking-wide text-state-warn">
            Analysis not running
          </div>
          <ul className="mt-1.5 space-y-1.5">
            {features
              .filter((f) => f.blockedReason)
              .map((f) => (
                <li key={f.key} className="text-sm text-content-muted">
                  <span className="font-medium text-content">{f.label}:</span> {f.blockedReason}
                  {f.ready && f.results === 0 && (
                    <>
                      {' '}
                      <Link className="text-accent hover:underline" to="/settings">
                        Reprocess the library
                      </Link>{' '}
                      to fill it in.
                    </>
                  )}
                </li>
              ))}
          </ul>
        </div>
      )}

      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 lg:grid-cols-5">
        <StatTile label="Analysed" value={processing.completed} tone="ok" />
        <StatTile label="Awaiting" value={processing.pending} />
        <StatTile label="Processing" value={processing.processing} tone={processing.processing ? 'busy' : 'default'} />
        <StatTile
          label="Failures"
          value={processing.failed}
          tone={processing.failed ? 'error' : 'default'}
          href={processing.failed ? '/queue?state=failed' : undefined}
        />
        {/*
          Shown only when there are any, and never folded into Failures. These are files
          nothing can do anything with — empty, or with no video stream — so they are not
          waiting for a retry and counting them as failures invited exactly the reprocess
          that kept handing them fresh attempts. Links to the recordings list rather than
          the queue, because the answer is on the file, not on a job.
        */}
        {processing.invalid > 0 && (
          <StatTile
            label="Unusable files"
            value={processing.invalid}
            tone="warn"
            href="/recordings?state=invalid"
          />
        )}
        <StatTile label="Recordings today" value={processing.recordingsToday} />
      </div>

      <div className="grid gap-4 lg:grid-cols-3">
        <section className="card p-4 lg:col-span-2">
          <div className="mb-2 flex items-baseline justify-between">
            <h2 className="text-sm font-semibold">Storage</h2>
            <span className="tabular text-xs text-content-muted">
              {formatBytes(storage.usedBytes)} of {formatBytes(storage.limitBytes)}
            </span>
          </div>
          <ProgressBar value={storagePct} />
          <p className="hint mt-2">
            {storage.deletionEnabled ? (
              storage.footageWritable ? (
                <>Automatic cleanup is <strong className="text-content">enabled</strong>. Oldest eligible footage is removed once the limit is exceeded.</>
              ) : (
                <>Deletion is enabled but the footage directory is mounted read-only, so nothing can be removed.</>
              )
            ) : (
              <>Retention is in <strong className="text-content">report-only</strong> mode — it shows what it would remove but deletes nothing. Enable it in Settings &rsaquo; Storage.</>
            )}
          </p>
        </section>

        <section className="card p-4">
          <h2 className="mb-2 text-sm font-semibold">Processing hardware</h2>
          <dl className="space-y-1.5 text-sm">
            <div className="flex justify-between gap-3">
              <dt className="text-content-muted">GPU</dt>
              <dd className="truncate text-right">{hardware.gpu.name ?? 'none detected'}</dd>
            </div>
            <div className="flex justify-between gap-3">
              <dt className="text-content-muted">Decoder</dt>
              <dd>{hardware.decode.hardwareDecode ? 'VAAPI (iGPU)' : 'software'}</dd>
            </div>
            <div className="flex justify-between gap-3">
              <dt className="text-content-muted">Inference</dt>
              <dd>{hardware.inference.preferredDevice}</dd>
            </div>
          </dl>
        </section>
      </div>

      {latestJourney && (
        <section className="card p-4">
          <h2 className="mb-2 text-sm font-semibold">Most recent journey</h2>
          <Link to={`/journeys/${latestJourney.id}`} className="block hover:text-accent">
            <div className="flex flex-wrap items-baseline gap-x-5 gap-y-1 text-sm">
              <span className="font-medium">{formatDateTime(latestJourney.startedAt)}</span>
              <span className="tabular text-content-muted">{formatDuration(latestJourney.durationS)}</span>
              <span className="tabular text-content-muted">{formatDistance(latestJourney.distanceM)}</span>
              <span className="tabular text-content-muted">max {formatSpeed(latestJourney.maxSpeedKmh)}</span>
              <span className="tabular text-content-muted">{latestJourney.recordingCount} recordings</span>
              <span className="tabular text-content-muted">{latestJourney.uniquePlateCount} plates</span>
            </div>
          </Link>
        </section>
      )}

      <section className="card p-4">
        <h2 className="mb-3 text-sm font-semibold">Recent activity</h2>
        {jobs.isLoading && <Spinner className="py-6" />}
        {jobs.data && jobs.data.items.length === 0 && (
          <EmptyState title="Nothing processed yet" description="Run a scan from Settings to index your footage." />
        )}
        <ul className="divide-y divide-border">
          {jobs.data?.items.map((job) => (
            <li key={job.id} className="flex items-center gap-3 py-2 text-sm">
              <JobStateBadge state={job.state} />
              <span className="min-w-0 flex-1 truncate">
                {job.recordingId ? (
                  <Link to={`/recordings/${job.recordingId}`} className="hover:text-accent">
                    {job.recordingFilename ?? `Recording ${job.recordingId}`}
                  </Link>
                ) : (
                  job.kind
                )}
              </span>
              {job.stageCurrent && <span className="text-content-faint">{job.stageCurrent}</span>}
              <span className="tabular shrink-0 text-xs text-content-faint">
                {formatRelative(job.finishedAt ?? job.startedAt ?? job.queuedAt)}
              </span>
            </li>
          ))}
        </ul>
      </section>
    </div>
  )
}
