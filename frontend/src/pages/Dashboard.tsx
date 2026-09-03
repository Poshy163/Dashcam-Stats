import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'

import Spinner from '@/components/Spinner'
import { EmptyState, ErrorState, JobStateBadge, PageHeader, ProgressBar, StatTile } from '@/components/ui'
import { api } from '@/lib/api'
import type { IngestStatus } from '@/lib/api'
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
  const ingest = useQuery({
    queryKey: ['ingest-status'],
    queryFn: api.ingest.status,
    refetchInterval: (query) => (query.state.data?.state === 'running' ? 2_000 : 10_000),
  })
  // Completed jobs, which is what the heading says. /api/jobs orders by state rank first —
  // running, then failed, then next-to-be-claimed — deliberately, for the Queue page. With
  // no state filter, page one of a backlog is entirely work that has *not* finished, so
  // "the latest work completed" listed queued jobs that had never run, each showing a
  // plausible "5 minutes ago" beside a `queued` badge, and showed nothing that had actually
  // completed until the queue emptied.
  const jobs = useQuery({
    queryKey: ['jobs', 'recent'],
    queryFn: () => api.jobs.list({ state: 'completed', pageSize: 6 }),
    refetchInterval: 10_000,
  })

  if (status.isLoading) return <Spinner label="Loading your dashboard…" className="py-24" />
  if (status.isError) return <ErrorState error={status.error} retry={() => status.refetch()} />
  if (!status.data) return null

  const { totals, processing, storage, latestJourney, hardware, features } = status.data
  const storagePct = storage.limitBytes ? storage.usedBytes / storage.limitBytes : 0
  // Every state the ring draws is in the total it is drawn against. "Unusable" was
  // rendered as a slice of a denominator that excluded it, so the percentages never
  // summed to the ring and the centre number disagreed with the Recordings tile above.
  const processTotal =
    processing.completed +
    processing.pending +
    processing.processing +
    processing.failed +
    processing.invalid +
    processing.settling
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

      {ingest.data && (ingest.data.unitOnline || ingest.data.state === 'running') && (
        <DashcamStatusBanner status={ingest.data} />
      )}

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
        <section className="card cockpit-panel p-5 sm:p-6">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <div className="hud-tag">
                <span className="h-1.5 w-1.5 rounded-full bg-cyan animate-pulse"></span>
                CAN-BUS TELEMETRY PIPELINE
              </div>
              <h2 className="section-title mt-1">Library Analysis Index</h2>
              <p className="mt-0.5 text-xs font-mono text-content-muted">Processing status across indexed recordings</p>
            </div>
            <Link to="/queue" className="font-mono text-xs font-bold text-accent hover:underline flex items-center gap-1">
              OPEN QUEUE <ArrowIcon />
            </Link>
          </div>

          <div className="mt-6 grid items-center gap-7 sm:grid-cols-[10rem_minmax(0,1fr)]">
            <div className="relative mx-auto h-36 w-36 rounded-full p-3 shadow-glow-orange" style={{ background: progressRing(analysedPct, activePct) }}>
              <div className="grid h-full w-full place-items-center rounded-full bg-surface-raised text-center shadow-inner border border-border">
                <div>
                  <div className="tabular font-mono text-3xl font-black tracking-tight">{processTotal.toLocaleString()}</div>
                  <div className="mt-0.5 font-mono text-2xs uppercase tracking-wider text-content-muted">CLIPS INDEXED</div>
                </div>
              </div>
            </div>

            <div>
              <div className="grid grid-cols-2 gap-x-5 gap-y-4 sm:grid-cols-3">
                <ProgressMetric color="bg-state-ok shadow-[0_0_8px_rgba(16,210,130,0.5)]" value={processing.completed} label="Analysed" />
                <ProgressMetric color="bg-surface-sunken ring-1 ring-border" value={processing.pending} label="Waiting" />
                <ProgressMetric color="bg-cyan shadow-glow-cyan" value={processing.processing} label="Processing" />
                {processing.failed > 0 && <ProgressMetric color="bg-state-error" value={processing.failed} label="Failed" />}
                <ProgressMetric color="bg-state-warn" value={processing.invalid} label="Unusable" />
                {processing.settling > 0 && (
                  <ProgressMetric color="bg-surface-sunken ring-1 ring-border" value={processing.settling} label="Still writing" />
                )}
                <ProgressMetric color="bg-accent-muted border border-accent/40" value={processing.recordingsToday} label="Added today" />
              </div>
              <div className="mt-5 rounded-lg border border-border/80 bg-surface-sunken/80 px-4 py-3 font-mono text-xs text-content-muted flex items-center justify-between">
                <span>ANALYSIS THROUGHPUT:</span>
                {processing.throughputPerHour === null ? (
                  <span className="text-content-faint">Calibrating…</span>
                ) : (
                  <strong className="tabular font-bold text-cyan text-sm">{processing.throughputPerHour.toFixed(1)} clips / hr</strong>
                )}
              </div>
            </div>
          </div>
        </section>

        <section className="card cockpit-panel p-5 sm:p-6">
          <div className="flex items-start justify-between gap-3">
            <div>
              <div className="hud-tag">
                <span className="h-1.5 w-1.5 rounded-full bg-accent"></span>
                NVME / STORAGE HUD
              </div>
              <h2 className="section-title mt-1">Footage Capacity</h2>
              <p className="mt-0.5 text-xs font-mono text-content-muted">Dashcam footage library</p>
            </div>
            <span className="grid h-11 w-11 place-items-center rounded-xl border border-accent/30 bg-accent/10 text-accent shadow-sm"><StorageIcon /></span>
          </div>
          <div className="mt-7">
            <div className="flex items-end justify-between gap-3">
              <div>
                <span className="tabular font-mono text-3xl font-black tracking-tight">{formatBytes(storage.usedBytes)}</span>
                <span className="ml-2 font-mono text-xs text-content-muted uppercase">used</span>
              </div>
              <span className="tabular font-mono text-sm font-bold text-accent">{Math.round(storagePct * 100)}%</span>
            </div>
            <ProgressBar value={storagePct} className="mt-4" />
            <div className="mt-2 font-mono text-2xs text-content-muted">Capacity limit: {formatBytes(storage.limitBytes)}</div>
          </div>
          <div className="mt-6 rounded-lg border border-border bg-surface-sunken/70 p-3 text-xs font-mono leading-relaxed text-content-muted">
            {storage.deletionEnabled ? (
              storage.footageWritable ? 'Automatic cleanup active.' : 'Cleanup enabled; footage mount is read-only.'
            ) : (
              <>Retention policy: <strong className="font-semibold text-content">Report-only mode</strong></>
            )}
          </div>
          <Link to="/settings" className="mt-4 inline-flex items-center gap-1.5 font-mono text-xs font-bold text-accent hover:underline">
            MANAGE STORAGE <ArrowIcon />
          </Link>
        </section>
      </div>

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1.65fr)_minmax(19rem,1fr)]">
        {latestJourney ? (
          <section className="card cockpit-panel overflow-hidden">
            <div className="grid min-h-56 sm:grid-cols-[minmax(14rem,0.85fr)_minmax(0,1.4fr)]">
              <div className="relative grid min-h-44 place-items-center overflow-hidden bg-gradient-to-br from-surface-sunken to-accent-muted p-6 text-accent">
                <div className="absolute inset-0 opacity-25 [background-image:radial-gradient(circle_at_center,rgb(var(--accent)/0.3)_1px,transparent_1px)] [background-size:16px_16px]" />
                <RoutePreviewIcon />
              </div>
              <div className="flex flex-col p-5 sm:p-6">
                <div className="flex items-start justify-between gap-3">
                  <div>
                    <div className="hud-tag">
                      <span className="h-1.5 w-1.5 rounded-full bg-accent animate-ping"></span>
                      LATEST RUN TELEMETRY
                    </div>
                    <h2 className="mt-1 font-mono text-xl font-black tracking-tight">{formatDateTime(latestJourney.startedAt)}</h2>
                    <p className="mt-0.5 font-mono text-xs text-content-muted">{latestJourney.recordingCount} recordings · Trip #{latestJourney.id}</p>
                  </div>
                  <span className="grid h-10 w-10 place-items-center rounded-xl border border-state-ok/30 bg-state-ok/10 text-state-ok"><JourneyIcon /></span>
                </div>
                <div className="mt-6 grid grid-cols-2 gap-4 sm:grid-cols-4 font-mono">
                  <JourneyMetric label="Duration" value={formatDuration(latestJourney.durationS)} />
                  <JourneyMetric label="Distance" value={formatDistance(latestJourney.distanceM)} />
                  <JourneyMetric label="Average" value={formatSpeed(latestJourney.avgSpeedKmh)} />
                  <JourneyMetric label="Max Speed" value={formatSpeed(latestJourney.maxSpeedKmh)} />
                </div>
                <Link to={`/journeys/${latestJourney.id}`} className="btn btn-primary mt-6 self-start">
                  Open telemetry run <ArrowIcon />
                </Link>
              </div>
            </div>
          </section>
        ) : (
          <EmptyState title="No journeys yet" description="Journeys appear here after your footage has been analysed." />
        )}

        <section className="card cockpit-panel p-5 sm:p-6">
          <div className="flex items-center justify-between gap-3">
            <div>
              <div className="hud-tag">
                <span className="h-1.5 w-1.5 rounded-full bg-cyan"></span>
                DIAGNOSTIC STATUS
              </div>
              <h2 className="section-title mt-1">System Health</h2>
            </div>
            <span className={`h-2.5 w-2.5 rounded-full ${hasAttention ? 'bg-state-warn shadow-glow-orange' : 'bg-state-ok shadow-[0_0_8px_rgba(16,210,130,0.6)]'}`} />
          </div>
          {hasAttention ? (
            <div className="mt-5 space-y-2.5">
              {processing.invalid > 0 && <AttentionLink to="/recordings?state=invalid" value={processing.invalid} label="unusable files" />}
              {processing.failed > 0 && <AttentionLink to="/queue?state=failed" value={processing.failed} label="failed jobs" error />}
              {blockedFeatures.map((feature) => (
                <AttentionLink key={feature.key} to="/settings" label={`${feature.label} is unavailable`} />
              ))}
              {hardware.notes.map((note) => <div key={note} className="rounded-lg bg-state-warn/10 p-3 text-xs font-mono text-state-warn">{note}</div>)}
            </div>
          ) : (
            <div className="mt-5 flex items-center gap-3 rounded-lg border border-state-ok/30 bg-state-ok/10 p-4 text-xs font-mono text-state-ok">
              <CheckIcon />
              <span><strong className="font-bold">ALL SYSTEMS NOMINAL.</strong> No alerts active.</span>
            </div>
          )}
          <div className="mt-5 border-t border-border/80 pt-4 font-mono text-xs text-content-muted space-y-2">
            <div className="flex justify-between gap-3">
              <span>ECU / CPU</span>
              <span className="truncate font-bold text-content">{hardware.cpu.model ?? 'Multi-Core'}</span>
            </div>
            <div className="flex justify-between gap-3">
              <span>VISION ACCELERATOR</span>
              <span className="truncate font-bold text-cyan">{hardware.gpu.name ?? 'OpenVINO Device'}</span>
            </div>
            <div className="flex justify-between gap-3">
              <span>CODEC ENGINE</span>
              <span className="font-bold text-content">{hardware.decode.hardwareDecode ? 'VAAPI / QSV Hardware' : 'Software'}</span>
            </div>
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
          {jobs.data?.items.map((job) => (
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
  return `conic-gradient(from 220deg, rgb(var(--state-ok)) 0 ${completeEnd}%, rgb(var(--cyan)) ${completeEnd}% ${activeEnd}%, rgb(var(--surface-sunken)) ${activeEnd}% 100%)`
}

function friendlyStage(stage: string) {
  return stage.replace(/_/g, ' ').replace(/^./, (letter) => letter.toUpperCase())
}

function SystemStatus({ active, waiting, failed }: { active: number; waiting: number; failed: number }) {
  const healthy = failed === 0
  return (
    <section className={`relative flex flex-wrap items-center gap-4 rounded-xl border p-4 sm:px-5 overflow-hidden ${healthy ? 'border-state-ok/40 bg-state-ok/[0.06]' : 'border-state-warn/40 bg-state-warn/[0.08]'}`}>
      <div className={`absolute top-0 left-0 right-0 h-[1px] bg-gradient-to-r from-transparent ${healthy ? 'via-state-ok/60' : 'via-state-warn/60'} to-transparent`} />
      <span className={`grid h-11 w-11 shrink-0 place-items-center rounded-xl text-white shadow-sm ${healthy ? 'bg-state-ok shadow-[0_0_12px_rgba(16,210,130,0.4)]' : 'bg-state-warn shadow-glow-orange'}`}>
        {healthy ? <CheckIcon /> : <AttentionIcon />}
      </span>
      <div className="min-w-0 flex-1 font-mono">
        <h2 className={`text-xs sm:text-sm font-bold tracking-wider uppercase ${healthy ? 'text-state-ok' : 'text-state-warn'}`}>
          {healthy ? (active > 0 ? 'TELEMETRY PIPELINE RUNNING · PROCESSING ACTIVE' : 'INSTRUMENT CLUSTER READY · ALL SYSTEMS NOMINAL') : 'SYSTEM ALERT · ATTENTION REQUIRED'}
        </h2>
        <p className="mt-0.5 text-xs text-content-muted">
          {active > 0 ? `${active} stream${active === 1 ? '' : 's'} running · ${waiting} clips waiting` : `${waiting} recording${waiting === 1 ? '' : 's'} queued`}
        </p>
      </div>
      <Link to="/queue" className="btn font-mono text-xs font-bold border-state-ok/40 hover:border-state-ok">VIEW QUEUE</Link>
    </section>
  )
}

function ProgressMetric({ color, value, label }: { color: string; value: number; label: string }) {
  return <div><div className="flex items-center gap-2"><span className={`h-2.5 w-2.5 rounded-full ${color}`} /><span className="tabular font-mono text-xl font-black">{value.toLocaleString()}</span></div><div className="mt-1 pl-[18px] font-mono text-2xs uppercase tracking-wider text-content-muted">{label}</div></div>
}

function JourneyMetric({ label, value }: { label: string; value: string }) {
  return <div><div className="tabular font-mono text-base font-black sm:text-lg text-content">{value}</div><div className="mt-1 font-mono text-2xs uppercase tracking-wider text-content-muted">{label}</div></div>
}

function AttentionLink({ to, value, label, error = false }: { to: string; value?: number; label: string; error?: boolean }) {
  return <Link to={to} className={`flex items-center gap-3 rounded-lg p-3 font-mono text-xs transition-all ${error ? 'bg-state-error/10 text-state-error hover:bg-state-error/20 border border-state-error/30' : 'bg-state-warn/10 text-state-warn hover:bg-state-warn/20 border border-state-warn/30'}`}><AttentionIcon /><span className="flex-1 font-bold">{value !== undefined ? `${value} ${label}` : label}</span><ArrowIcon /></Link>
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
function RoutePreviewIcon() {
  return (
    <svg className="relative h-36 w-full max-w-sm" viewBox="0 0 320 150" fill="none">
      <defs>
        <filter id="routeGlow" x="-20%" y="-20%" width="140%" height="140%">
          <feGaussianBlur stdDeviation="4" result="blur" />
          <feMerge>
            <feMergeNode in="blur" />
            <feMergeNode in="SourceGraphic" />
          </feMerge>
        </filter>
      </defs>
      <path d="M15 95c35-60 70 26 103-22s69 36 101-11 50 24 84-38" stroke="rgb(var(--accent) / .25)" strokeWidth="18" strokeLinecap="round" />
      <path d="M15 95c35-60 70 26 103-22s69 36 101-11 50 24 84-38" stroke="rgb(var(--accent))" strokeWidth="3.5" strokeLinecap="round" filter="url(#routeGlow)" />
      <circle cx="15" cy="95" r="7" fill="rgb(var(--cyan))" stroke="rgb(var(--surface-raised))" strokeWidth="2.5" />
      <circle cx="303" cy="24" r="7" fill="rgb(var(--accent))" stroke="rgb(var(--surface-raised))" strokeWidth="2.5" />
    </svg>
  )
}
function CarIcon({ className = iconClass }: IconProps) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
      <path d="M5 17h14m-12 0a2 2 0 1 0 4 0m8 0a2 2 0 1 0 4 0M4 17l1.5-6h13l1.5 6M6 11l2-5h8l2 5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}

function DashcamStatusBanner({ status }: { status: IngestStatus }) {
  const [countdownOffset, setCountdownOffset] = useState<number>(0)

  useEffect(() => {
    setCountdownOffset(0)
  }, [status.sleepCountdownRemainingS, status.ignitionState])

  useEffect(() => {
    if (
      !status.unitOnline ||
      status.ignitionState === 'on' ||
      status.sleepCountdownRemainingS === null ||
      status.sleepCountdownRemainingS === undefined
    ) {
      return
    }
    const timer = setInterval(() => {
      setCountdownOffset((prev) => prev + 1)
    }, 1000)
    return () => clearInterval(timer)
  }, [status.unitOnline, status.ignitionState, status.sleepCountdownRemainingS])

  const liveCountdown =
    status.sleepCountdownRemainingS !== null && status.sleepCountdownRemainingS !== undefined
      ? Math.max(0, status.sleepCountdownRemainingS - countdownOffset)
      : null
  const prediction = status.sleepWindowPrediction
  const running = status.state === 'running'

  return (
    <div className="card flex flex-wrap items-center justify-between gap-4 border-accent/40 bg-surface-raised p-4 sm:p-5">
      <div className="flex items-center gap-3">
        <span className="grid h-10 w-10 shrink-0 place-items-center rounded-xl bg-accent-muted text-accent">
          <CarIcon />
        </span>
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="text-sm font-bold tracking-tight text-content">
              {running ? 'Dashcam backup in progress' : 'Dashcam connected'}
            </h3>
            {status.wifiFrequencyMhz && (
              <span
                className={`badge ${
                  status.wifiFrequencyMhz >= 4900
                    ? 'bg-state-ok/15 text-state-ok'
                    : 'bg-state-warn/15 text-state-warn'
                }`}
              >
                {status.wifiFrequencyMhz >= 4900 ? '5 GHz' : '2.4 GHz'} ({status.wifiFrequencyMhz} MHz)
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
              >
                ⏱️ Sleep countdown: {formatDuration(liveCountdown)}
              </span>
            )}
          </div>
          <p className="mt-1 text-xs text-content-muted">
            {prediction
              ? prediction.summary
              : status.ignitionState === 'on'
                ? `Ignition is ON • ${formatDuration(status.sleepWindowSeconds ?? 1200)} countdown will start when parked`
                : running
                  ? `${status.filesDone} of ${status.filesTotal} files • ${formatBytes(status.bytesDone)} of ${formatBytes(status.bytesTotal)}`
                  : 'Ready on your local network'}
          </p>
        </div>
      </div>
      <Link to="/backup" className="btn btn-sm self-center">
        View backup <ArrowIcon />
      </Link>
    </div>
  )
}
