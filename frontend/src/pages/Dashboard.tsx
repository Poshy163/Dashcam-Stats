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

  if (status.isLoading) return <Spinner label="Loading your dashboard…" className="py-24" />
  if (status.isError) return <ErrorState error={status.error} retry={() => status.refetch()} />
  if (!status.data) return null

  const { totals, processing, storage, latestJourney, hardware, features } = status.data
  const storagePct = storage.limitBytes ? storage.usedBytes / storage.limitBytes : 0
  const processTotal = processing.completed + processing.pending + processing.processing + processing.failed
  const analysedPct = processTotal ? processing.completed / processTotal : 0
  const activePct = processTotal ? processing.processing / processTotal : 0
  const blockedFeatures = features?.filter((feature) => feature.blockedReason) ?? []
  const hasAttention = processing.failed > 0 || processing.invalid > 0 || blockedFeatures.length > 0 || hardware.notes.length > 0

  const hour = new Date().getHours()
  const greeting = hour < 12 ? 'Good morning' : hour < 18 ? 'Good afternoon' : 'Good evening'

  return (
    <div className="space-y-6 pb-6">
      <PageHeader
        title={greeting}
        subtitle="Here’s what your dashcam library is doing."
        actions={
          <Link to="/recordings" className="btn">
            Browse recordings
            <ArrowIcon />
          </Link>
        }
      />

      <SystemStatus
        active={processing.processing}
        waiting={processing.pending}
        failed={processing.failed}
      />

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4 lg:gap-4">
        <StatTile label="Recordings" value={totals.recordings.toLocaleString()} icon={<VideoIcon />} />
        <StatTile label="Journeys" value={totals.journeys.toLocaleString()} icon={<JourneyIcon />} />
        <StatTile
          label="Footage"
          value={formatBytes(totals.footageBytes)}
          hint={`${formatDuration(totals.durationS)} total`}
          icon={<StorageIcon />}
        />
        <StatTile
          label="Plates"
          value={blockedFeatures.some((feature) => feature.key === 'plates') ? '—' : totals.plates.toLocaleString()}
          hint={blockedFeatures.some((feature) => feature.key === 'plates') ? 'Not available' : undefined}
          tone={blockedFeatures.some((feature) => feature.key === 'plates') ? 'warn' : 'default'}
          icon={<PlateIcon />}
        />
      </div>

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1.65fr)_minmax(19rem,1fr)]">
        <section className="card p-5 sm:p-6">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <h2 className="section-title">Library progress</h2>
              <p className="mt-1 text-sm text-content-muted">Analysis status across indexed recordings</p>
            </div>
            <Link to="/queue" className="text-sm font-semibold text-accent hover:underline">View queue</Link>
          </div>

          <div className="mt-6 grid items-center gap-7 sm:grid-cols-[10rem_minmax(0,1fr)]">
            <div className="relative mx-auto h-36 w-36 rounded-full p-3" style={{ background: progressRing(analysedPct, activePct) }}>
              <div className="grid h-full w-full place-items-center rounded-full bg-surface-raised text-center shadow-inner">
                <div>
                  <div className="tabular text-3xl font-bold tracking-tight">{processTotal.toLocaleString()}</div>
                  <div className="mt-0.5 text-xs font-medium text-content-muted">in library</div>
                </div>
              </div>
            </div>

            <div>
              <div className="grid grid-cols-2 gap-x-5 gap-y-4 sm:grid-cols-3">
                <ProgressMetric color="bg-state-ok" value={processing.completed} label="Analysed" />
                <ProgressMetric color="bg-surface-sunken ring-1 ring-border" value={processing.pending} label="Waiting" />
                <ProgressMetric color="bg-accent" value={processing.processing} label="Processing" />
                {processing.failed > 0 && <ProgressMetric color="bg-state-error" value={processing.failed} label="Failed" />}
                <ProgressMetric color="bg-state-warn" value={processing.invalid} label="Unusable" />
                <ProgressMetric color="bg-accent-muted" value={processing.recordingsToday} label="Added today" />
              </div>
              <div className="mt-5 rounded-lg bg-surface-sunken px-4 py-3 text-sm text-content-muted">
                {processing.throughputPerHour === null ? (
                  'Analysis rate will appear after more recordings finish.'
                ) : (
                  <>Current analysis rate <strong className="tabular font-semibold text-content">{processing.throughputPerHour.toFixed(1)} recordings/hour</strong></>
                )}
              </div>
            </div>
          </div>
        </section>

        <section className="card p-5 sm:p-6">
          <div className="flex items-start justify-between gap-3">
            <div>
              <h2 className="section-title">Storage</h2>
              <p className="mt-1 text-sm text-content-muted">Dashcam footage library</p>
            </div>
            <span className="grid h-11 w-11 place-items-center rounded-xl bg-accent-muted text-accent"><StorageIcon /></span>
          </div>
          <div className="mt-8">
            <div className="flex items-end justify-between gap-3">
              <div>
                <span className="tabular text-3xl font-bold tracking-tight">{formatBytes(storage.usedBytes)}</span>
                <span className="ml-2 text-sm text-content-muted">used</span>
              </div>
              <span className="tabular text-sm font-semibold text-content-muted">{Math.round(storagePct * 100)}%</span>
            </div>
            <ProgressBar value={storagePct} className="mt-4" />
            <div className="mt-2 text-xs text-content-muted">of {formatBytes(storage.limitBytes)} configured capacity</div>
          </div>
          <div className="mt-7 rounded-lg border border-border bg-surface-sunken/60 p-3.5 text-sm leading-relaxed text-content-muted">
            {storage.deletionEnabled ? (
              storage.footageWritable ? 'Automatic cleanup is enabled.' : 'Cleanup is enabled, but the footage folder is read-only.'
            ) : (
              <>Retention is in <strong className="font-semibold text-content">report-only mode</strong>. Nothing will be deleted.</>
            )}
          </div>
          <Link to="/settings" className="mt-4 inline-flex items-center gap-1.5 text-sm font-semibold text-accent hover:underline">
            Manage storage <ArrowIcon />
          </Link>
        </section>
      </div>

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1.65fr)_minmax(19rem,1fr)]">
        {latestJourney ? (
          <section className="card overflow-hidden">
            <div className="grid min-h-56 sm:grid-cols-[minmax(14rem,0.85fr)_minmax(0,1.4fr)]">
              <div className="relative grid min-h-44 place-items-center overflow-hidden bg-accent-muted p-6 text-accent">
                <div className="absolute inset-0 opacity-40 [background-image:radial-gradient(circle_at_center,rgb(var(--accent)/0.22)_1px,transparent_1px)] [background-size:20px_20px]" />
                <RoutePreviewIcon />
              </div>
              <div className="flex flex-col p-5 sm:p-6">
                <div className="flex items-start justify-between gap-3">
                  <div>
                    <div className="text-xs font-semibold uppercase tracking-[0.12em] text-content-faint">Latest journey</div>
                    <h2 className="mt-2 text-xl font-bold tracking-tight">{formatDateTime(latestJourney.startedAt)}</h2>
                    <p className="mt-1 text-sm text-content-muted">{latestJourney.recordingCount} recordings</p>
                  </div>
                  <span className="grid h-10 w-10 place-items-center rounded-full bg-state-ok/10 text-state-ok"><JourneyIcon /></span>
                </div>
                <div className="mt-6 grid grid-cols-2 gap-4 sm:grid-cols-4">
                  <JourneyMetric label="Duration" value={formatDuration(latestJourney.durationS)} />
                  <JourneyMetric label="Distance" value={formatDistance(latestJourney.distanceM)} />
                  <JourneyMetric label="Average" value={formatSpeed(latestJourney.avgSpeedKmh)} />
                  <JourneyMetric label="Maximum" value={formatSpeed(latestJourney.maxSpeedKmh)} />
                </div>
                <Link to={`/journeys/${latestJourney.id}`} className="btn btn-primary mt-6 self-start">
                  Open journey <ArrowIcon />
                </Link>
              </div>
            </div>
          </section>
        ) : (
          <EmptyState title="No journeys yet" description="Journeys appear here after your footage has been analysed." />
        )}

        <section className="card p-5 sm:p-6">
          <div className="flex items-center justify-between gap-3">
            <h2 className="section-title">Needs attention</h2>
            <span className={`h-2.5 w-2.5 rounded-full ${hasAttention ? 'bg-state-warn' : 'bg-state-ok'}`} />
          </div>
          {hasAttention ? (
            <div className="mt-5 space-y-3">
              {processing.invalid > 0 && <AttentionLink to="/recordings?state=invalid" value={processing.invalid} label="unusable files" />}
              {processing.failed > 0 && <AttentionLink to="/queue?state=failed" value={processing.failed} label="failed jobs" error />}
              {blockedFeatures.map((feature) => (
                <AttentionLink key={feature.key} to="/settings" label={`${feature.label} is unavailable`} />
              ))}
              {hardware.notes.map((note) => <div key={note} className="rounded-lg bg-state-warn/10 p-3 text-sm text-state-warn">{note}</div>)}
            </div>
          ) : (
            <div className="mt-6 flex items-center gap-3 rounded-lg bg-state-ok/10 p-4 text-sm text-state-ok">
              <CheckIcon />
              <span><strong className="font-semibold">All clear.</strong> There’s nothing you need to review.</span>
            </div>
          )}
          <div className="mt-5 border-t border-border pt-4 text-sm text-content-muted">
            <div className="flex justify-between gap-3"><span>GPU</span><span className="truncate font-medium text-content">{hardware.gpu.name ?? 'Not detected'}</span></div>
            <div className="mt-2 flex justify-between gap-3"><span>Decoder</span><span className="font-medium text-content">{hardware.decode.hardwareDecode ? 'Hardware accelerated' : 'Software'}</span></div>
          </div>
        </section>
      </div>

      <section className="card p-5 sm:p-6">
        <div className="mb-4 flex items-center justify-between gap-3">
          <div>
            <h2 className="section-title">Recent activity</h2>
            <p className="mt-1 text-sm text-content-muted">The latest work completed by your analyser</p>
          </div>
          <Link to="/logs" className="text-sm font-semibold text-accent hover:underline">View all</Link>
        </div>
        {jobs.isLoading && <Spinner className="py-6" />}
        {jobs.data && jobs.data.items.length === 0 && (
          <EmptyState title="Nothing processed yet" description="Run a scan from Settings to index your footage." />
        )}
        <ul className="divide-y divide-border">
          {jobs.data?.items.slice(0, 6).map((job) => (
            <li key={job.id} className="flex items-center gap-3 py-3 text-sm">
              <JobStateBadge state={job.state} />
              <span className="min-w-0 flex-1 truncate font-medium">
                {job.recordingId ? (
                  <Link to={`/recordings/${job.recordingId}`} className="hover:text-accent">{job.recordingFilename ?? `Recording ${job.recordingId}`}</Link>
                ) : job.kind}
              </span>
              {job.stageCurrent && <span className="hidden text-content-muted sm:inline">{friendlyStage(job.stageCurrent)}</span>}
              <span className="tabular shrink-0 text-xs text-content-faint">{formatRelative(job.finishedAt ?? job.startedAt ?? job.queuedAt)}</span>
            </li>
          ))}
        </ul>
      </section>
    </div>
  )
}

function progressRing(completed: number, active: number) {
  const completeEnd = Math.max(0, Math.min(100, completed * 100))
  const activeEnd = Math.max(completeEnd, Math.min(100, (completed + active) * 100))
  return `conic-gradient(rgb(var(--state-ok)) 0 ${completeEnd}%, rgb(var(--accent)) ${completeEnd}% ${activeEnd}%, rgb(var(--surface-sunken)) ${activeEnd}% 100%)`
}

function friendlyStage(stage: string) {
  return stage.replace(/_/g, ' ').replace(/^./, (letter) => letter.toUpperCase())
}

function SystemStatus({ active, waiting, failed }: { active: number; waiting: number; failed: number }) {
  const healthy = failed === 0
  return (
    <section className={`flex flex-wrap items-center gap-4 rounded-xl border p-4 sm:px-5 ${healthy ? 'border-state-ok/35 bg-state-ok/[0.07]' : 'border-state-warn/40 bg-state-warn/[0.08]'}`}>
      <span className={`grid h-11 w-11 shrink-0 place-items-center rounded-full text-white ${healthy ? 'bg-state-ok' : 'bg-state-warn'}`}>
        {healthy ? <CheckIcon /> : <AttentionIcon />}
      </span>
      <div className="min-w-0 flex-1">
        <h2 className={`font-semibold ${healthy ? 'text-state-ok' : 'text-state-warn'}`}>
          {healthy ? (active > 0 ? 'Analysis is running smoothly' : 'Your library is ready') : 'Some jobs need attention'}
        </h2>
        <p className="mt-0.5 text-sm text-content-muted">
          {active > 0 ? `${active} recording${active === 1 ? '' : 's'} processing · ${waiting} waiting` : `${waiting} recording${waiting === 1 ? '' : 's'} waiting`}
        </p>
      </div>
      <Link to="/queue" className="btn border-state-ok/30 bg-surface-raised">View queue</Link>
    </section>
  )
}

function ProgressMetric({ color, value, label }: { color: string; value: number; label: string }) {
  return <div><div className="flex items-center gap-2"><span className={`h-2.5 w-2.5 rounded-full ${color}`} /><span className="tabular text-xl font-bold">{value.toLocaleString()}</span></div><div className="mt-1 pl-[18px] text-xs text-content-muted">{label}</div></div>
}

function JourneyMetric({ label, value }: { label: string; value: string }) {
  return <div><div className="tabular text-base font-bold sm:text-lg">{value}</div><div className="mt-1 text-xs text-content-muted">{label}</div></div>
}

function AttentionLink({ to, value, label, error = false }: { to: string; value?: number; label: string; error?: boolean }) {
  return <Link to={to} className={`flex items-center gap-3 rounded-lg p-3.5 transition-colors ${error ? 'bg-state-error/10 text-state-error hover:bg-state-error/15' : 'bg-state-warn/10 text-state-warn hover:bg-state-warn/15'}`}><AttentionIcon /><span className="flex-1 text-sm font-semibold">{value !== undefined ? `${value} ${label}` : label}</span><ArrowIcon /></Link>
}

type IconProps = { className?: string }
const iconClass = 'h-5 w-5'
function VideoIcon({ className = iconClass }: IconProps) { return <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"><rect x="3" y="5" width="14" height="14" rx="2" /><path d="m17 10 4-2v8l-4-2z" strokeLinejoin="round" /></svg> }
function JourneyIcon({ className = iconClass }: IconProps) { return <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"><circle cx="6" cy="18" r="2.5" /><circle cx="18" cy="6" r="2.5" /><path d="M8.5 17c5 0 3-10 7-10" strokeLinecap="round" /></svg> }
function StorageIcon({ className = iconClass }: IconProps) { return <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"><ellipse cx="12" cy="5" rx="8" ry="3" /><path d="M4 5v7c0 1.7 3.6 3 8 3s8-1.3 8-3V5M4 12v7c0 1.7 3.6 3 8 3s8-1.3 8-3v-7" /></svg> }
function PlateIcon({ className = iconClass }: IconProps) { return <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"><rect x="2.5" y="6" width="19" height="12" rx="2" /><path d="M7 10v4m3-4v4m3-4v4m3-4v4" /></svg> }
function CheckIcon({ className = iconClass }: IconProps) { return <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="m5 12 4 4L19 6" strokeLinecap="round" strokeLinejoin="round" /></svg> }
function AttentionIcon({ className = iconClass }: IconProps) { return <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"><path d="M12 3 2.8 20h18.4z" strokeLinejoin="round" /><path d="M12 9v5m0 3v.1" strokeLinecap="round" /></svg> }
function ArrowIcon({ className = 'h-4 w-4' }: IconProps) { return <svg className={className} viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.8"><path d="M4 10h12m-4-4 4 4-4 4" strokeLinecap="round" strokeLinejoin="round" /></svg> }
function RoutePreviewIcon() { return <svg className="relative h-36 w-full max-w-sm" viewBox="0 0 320 150" fill="none"><path d="M15 95c35-60 70 26 103-22s69 36 101-11 50 24 84-38" stroke="rgb(var(--accent) / .16)" strokeWidth="18" strokeLinecap="round" /><path d="M15 95c35-60 70 26 103-22s69 36 101-11 50 24 84-38" stroke="currentColor" strokeWidth="4" strokeLinecap="round" strokeDasharray="2 8" /><circle cx="15" cy="95" r="9" fill="rgb(var(--surface-raised))" stroke="rgb(var(--state-ok))" strokeWidth="4" /><circle cx="303" cy="24" r="9" fill="rgb(var(--surface-raised))" stroke="currentColor" strokeWidth="4" /></svg> }
