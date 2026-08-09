import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useSearchParams } from 'react-router-dom'

import Spinner from '@/components/Spinner'
import {
  EmptyState,
  ErrorState,
  JobStateBadge,
  PageHeader,
  ProgressBar,
  StatTile,
} from '@/components/ui'
import { api } from '@/lib/api'
import { formatDateTime, realtimeFactor } from '@/lib/format'

const STATES = ['', 'queued', 'running', 'failed', 'completed', 'cancelled']

export default function Queue() {
  const [params, setParams] = useSearchParams()
  const state = params.get('state') ?? ''
  const client = useQueryClient()

  const stats = useQuery({
    queryKey: ['queue-stats-page'],
    queryFn: api.jobs.stats,
    refetchInterval: 3_000,
  })
  const running = (stats.data?.running ?? 0) > 0

  const jobs = useQuery({
    queryKey: ['jobs', state],
    queryFn: () => api.jobs.list({ state: state || undefined, pageSize: 50 }),
    // Poll quickly while something is in flight, slowly when idle.
    refetchInterval: running ? 3_000 : 15_000,
  })

  const invalidate = () => {
    client.invalidateQueries({ queryKey: ['jobs'] })
    client.invalidateQueries({ queryKey: ['queue-stats-page'] })
    client.invalidateQueries({ queryKey: ['queue-stats'] })
  }

  const pause = useMutation({ mutationFn: api.jobs.pause, onSuccess: invalidate })
  const resume = useMutation({ mutationFn: api.jobs.resume, onSuccess: invalidate })
  const retryAll = useMutation({ mutationFn: api.jobs.retryFailed, onSuccess: invalidate })
  const retryOne = useMutation({ mutationFn: api.jobs.retry, onSuccess: invalidate })
  const cancel = useMutation({ mutationFn: api.jobs.cancel, onSuccess: invalidate })

  const active = jobs.data?.items.filter((j) => j.state === 'running') ?? []

  return (
    <div className="space-y-4">
      <PageHeader
        title="Processing queue"
        subtitle={stats.data?.paused ? 'Queue is paused' : undefined}
        actions={
          <>
            {stats.data?.paused ? (
              <button className="btn btn-primary" onClick={() => resume.mutate()} disabled={resume.isPending}>
                Resume queue
              </button>
            ) : (
              <button className="btn" onClick={() => pause.mutate()} disabled={pause.isPending}>
                Pause queue
              </button>
            )}
            <button
              className="btn"
              onClick={() => retryAll.mutate()}
              disabled={retryAll.isPending || !stats.data?.failed}
            >
              Retry failed{stats.data?.failed ? ` (${stats.data.failed})` : ''}
            </button>
          </>
        }
      />

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <StatTile label="Queued" value={stats.data?.queued ?? 0} />
        <StatTile label="Running" value={stats.data?.running ?? 0} tone={running ? 'busy' : 'default'} />
        <StatTile
          label="Failed"
          value={stats.data?.failed ?? 0}
          tone={stats.data?.failed ? 'error' : 'default'}
        />
        <StatTile label="Completed today" value={stats.data?.completedToday ?? 0} tone="ok" />
      </div>

      {active.length > 0 && (
        <section className="space-y-2">
          <h2 className="text-sm font-semibold">Currently processing</h2>
          {active.map((job) => (
            <div key={job.id} className="card p-3">
              <div className="flex flex-wrap items-baseline justify-between gap-2">
                <Link
                  to={job.recordingId ? `/recordings/${job.recordingId}` : '#'}
                  className="font-medium hover:text-accent"
                >
                  {job.recordingFilename ?? `Job ${job.id}`}
                </Link>
                <span className="tabular text-xs text-content-muted">
                  {Math.round(job.progress * 100)}%
                </span>
              </div>
              <div className="mt-2">
                <ProgressBar value={job.progress} />
              </div>
              <div className="tabular mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-content-muted">
                <span>Stage: {job.stageCurrent ?? '—'}</span>
                <span>{realtimeFactor(job.speedRealtime)}</span>
                {job.decoder && <span>Decoder: {job.decoder}</span>}
                {job.inferenceDevice && <span>AI: {job.inferenceDevice}</span>}
                {job.recordingId && (
                  // The one question worth asking about a job in flight is what the
                  // pipeline is actually reading, and until now the only route to that
                  // answer was to know the overlay reader existed, open the recording, and
                  // scroll for it. Straight from the job that raised the question instead.
                  <Link
                    to={`/recordings/${job.recordingId}?debug=1`}
                    className="text-accent hover:underline"
                  >
                    See what it sees
                  </Link>
                )}
              </div>
            </div>
          ))}
        </section>
      )}

      <div className="flex items-center gap-2">
        <span className="label text-xs">Filter</span>
        <select
          className="input w-auto"
          value={state}
          onChange={(e) => {
            const next = new URLSearchParams(params)
            if (e.target.value) next.set('state', e.target.value)
            else next.delete('state')
            setParams(next)
          }}
        >
          {STATES.map((s) => (
            <option key={s} value={s}>{s || 'All states'}</option>
          ))}
        </select>
      </div>

      {jobs.isLoading && <Spinner className="py-16" />}
      {jobs.isError && <ErrorState error={jobs.error} retry={() => jobs.refetch()} />}
      {jobs.data?.items.length === 0 && (
        <EmptyState title="No jobs" description="Nothing is queued. Run a scan to look for new footage." />
      )}

      {jobs.data && jobs.data.items.length > 0 && (
        <div className="card overflow-x-auto">
          <table className="w-full min-w-[46rem] text-sm">
            <thead className="border-b border-border text-left text-xs text-content-muted">
              <tr>
                <th className="p-2 font-medium">State</th>
                <th className="p-2 font-medium">Recording</th>
                <th className="p-2 font-medium">Stage</th>
                <th className="p-2 font-medium">Attempts</th>
                <th className="p-2 font-medium">Queued</th>
                <th className="p-2 font-medium">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {jobs.data.items.map((job) => (
                <tr key={job.id} className="align-top hover:bg-surface-sunken">
                  <td className="p-2"><JobStateBadge state={job.state} /></td>
                  <td className="p-2">
                    {job.recordingId ? (
                      <Link to={`/recordings/${job.recordingId}`} className="hover:text-accent">
                        {job.recordingFilename ?? `#${job.recordingId}`}
                      </Link>
                    ) : (
                      <span className="text-content-muted">{job.kind}</span>
                    )}
                    {job.errorMessage && (
                      <div className="mt-0.5 text-xs text-state-error">{job.errorMessage}</div>
                    )}
                  </td>
                  <td className="p-2 text-content-muted">{job.stageCurrent ?? '—'}</td>
                  <td className="tabular p-2">{job.attempts}/{job.maxAttempts}</td>
                  <td className="tabular p-2 text-content-muted">{formatDateTime(job.queuedAt)}</td>
                  <td className="p-2">
                    <div className="flex gap-1">
                      {(job.state === 'failed' || job.state === 'cancelled') && (
                        <button className="btn px-2 py-1 text-xs" onClick={() => retryOne.mutate(job.id)}>
                          Retry
                        </button>
                      )}
                      {(job.state === 'queued' || job.state === 'running') && (
                        <button className="btn px-2 py-1 text-xs" onClick={() => cancel.mutate(job.id)}>
                          Cancel
                        </button>
                      )}
                    </div>
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
