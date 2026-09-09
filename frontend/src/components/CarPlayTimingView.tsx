/**
 * CarPlay frame timing, as sampled on the head unit itself.
 *
 * Candidate surfaces need owner mapping before their cadence can be attributed to
 * CarPlay. Keep separate capture periods, missing observations and radio context visible
 * so a chart cannot imply continuity or a cause the measurements have not established.
 */
import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'

import Spinner from '@/components/Spinner'
import { EmptyState, ErrorState } from '@/components/ui'
import { api } from '@/lib/api'
import { cn } from '@/lib/cn'
import { formatDateTime } from '@/lib/format'
import { inTimingPeriod, maxKnown, meanKnown, radioObservation, timingPeriods, timingSegments } from '@/lib/carplay'
import type { CarPlayTimingMinute } from '@/lib/types'

const CHART_W = 720
const CHART_H = 200
const PAD_L = 36
const PAD_R = 36
const PAD_T = 10
const PAD_B = 22

type Series = {
  label: string
  colorClass: string
  /** Fixed axis so the shape is comparable between drives. */
  min: number
  max: number
  values: (number | null)[]
  right?: boolean
}

function MiniChart({ minutes, series }: { minutes: CarPlayTimingMinute[]; series: Series[] }) {
  const first = minutes[0]
  const final = minutes[minutes.length - 1]
  if (minutes.length < 2 || !first || !final) return null
  const t0 = new Date(first.bucketStart).getTime()
  const t1 = new Date(final.bucketStart).getTime()
  const domain = t1 - t0 || 1
  const at = (i: number) => new Date(minutes[i]?.bucketStart ?? first.bucketStart).getTime()
  const x = (i: number) => PAD_L + ((at(i) - t0) / domain) * (CHART_W - PAD_L - PAD_R)
  const y = (v: number, s: Series) => PAD_T + (1 - (v - s.min) / (s.max - s.min)) * (CHART_H - PAD_T - PAD_B)
  const ticks = [0, 0.5, 1]
  const left = series.find((s) => !s.right)
  const right = series.find((s) => s.right)
  return (
    <svg viewBox={`0 0 ${CHART_W} ${CHART_H}`} className="mt-3 w-full" role="img" aria-label="CarPlay frame timing over time">
      {ticks.map((f) => {
        const yy = PAD_T + (1 - f) * (CHART_H - PAD_T - PAD_B)
        return (
          <g key={f}>
            <line x1={PAD_L} x2={CHART_W - PAD_R} y1={yy} y2={yy} className="stroke-border" strokeWidth={1} />
            {left && (
              <text x={PAD_L - 6} y={yy + 3} textAnchor="end" fontSize={10} fill="currentColor" className="text-content-faint">
                {Math.round(left.min + (left.max - left.min) * f)}
              </text>
            )}
            {right && (
              <text x={CHART_W - PAD_R + 6} y={yy + 3} textAnchor="start" fontSize={10} fill="currentColor" className="text-content-faint">
                {Math.round(right.min + (right.max - right.min) * f)}
              </text>
            )}
          </g>
        )
      })}
      {[0, 0.5, 1].map((f) => {
        const i = Math.min(minutes.length - 1, Math.round((minutes.length - 1) * f))
        return (
          <text key={f} x={x(i)} y={CHART_H - 6} textAnchor="middle" fontSize={10} fill="currentColor" className="text-content-faint">
            {new Date(at(i)).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
          </text>
        )
      })}
      {series.map((s) => {
        return (
          <g key={s.label} className={s.colorClass}>
            {timingSegments(minutes, s.values).map((indices) => (
              <polyline
                key={indices[0]}
                points={indices.map((i) => `${x(i).toFixed(1)},${y(Math.max(s.min, Math.min(s.max, s.values[i]!)), s).toFixed(1)}`).join(' ')}
                fill="none" stroke="currentColor" strokeWidth={1.75} strokeLinejoin="round"
              />
            ))}
          </g>
        )
      })}
    </svg>
  )
}

export default function CarPlayTimingView({ live }: { live: boolean }) {
  const [hours, setHours] = useState(24)
  const [surface, setSurface] = useState('')
  const [periodId, setPeriodId] = useState('')
  const query = useQuery({
    queryKey: ['carplay-timing', hours],
    queryFn: () => api.unitLogs.carplayTiming({ hours }),
    refetchInterval: live ? 15_000 : false,
  })

  if (query.isLoading) return <Spinner />
  if (query.isError) return <ErrorState error={query.error} />
  const data = query.data
  const allMinutes = data?.minutes ?? []
  // A capture that saw no frames must still be selectable and visible as missing timing.
  const periods = timingPeriods([
    ...allMinutes,
    ...(data?.events ?? []).map((e) => ({ bucketStart: e.occurredAt, sessionId: e.sessionId })),
  ])
  const selectedPeriod = periodId === 'all' ? null : periods.find((p) => p.id === periodId) ?? periods.at(-1)
  const observedMinutes = selectedPeriod
    ? allMinutes.filter((m) => inTimingPeriod(m.bucketStart, m.sessionId, selectedPeriod))
    : allMinutes

  // The unit always draws on more than one SurfaceView, and nothing in the sampler's
  // output says which is CarPlay's video: the layer's `#N` is reassigned between
  // sessions and the name carries no package. Measured live, two surfaces in the same
  // minute ran 35 ms and 53 ms cadences, so an average across them described neither.
  // They are shown one at a time instead, busiest first, and the operator picks.
  const layers = [...new Set(observedMinutes.map((m) => m.layer).filter(Boolean))].sort(
    (a, b) =>
      observedMinutes.filter((m) => m.layer === b).length -
      observedMinutes.filter((m) => m.layer === a).length,
  )
  const layer = layers.includes(surface) ? surface : layers[0] ?? ''
  const minutes = layer ? observedMinutes.filter((m) => m.layer === layer) : observedMinutes
  const samples = (data?.samples ?? []).filter((s) => (!layer || s.layer === layer) &&
    (!selectedPeriod || inTimingPeriod(s.occurredAt, s.sessionId, selectedPeriod)))
  const events = (data?.events ?? []).filter((e) => !selectedPeriod ||
    inTimingPeriod(e.occurredAt, e.sessionId, selectedPeriod))
  const diagnosticRows = [...samples, ...events].filter((s) => s.diagnosticSchema === 2)
  const displayWait = maxKnown(samples.map((s) => s.readyToPresentMaxMs ?? null))
  const receiveQueue = maxKnown(diagnosticRows.map((s) => s.zlinkRxQueueBytes ?? null))
  const spans = samples.map((s) => s.spanS).filter((s): s is number => s != null && s >= 0)
  const sampledSeconds = spans.length ? spans.reduce((a, b) => a + b, 0) : null

  const meanFps = meanKnown(minutes.map((m) => m.fps))
  const meanLate = meanKnown(minutes.map((m) => m.latePct))
  const worstLate = maxKnown(minutes.map((m) => m.latePct))
  const hottest = maxKnown(minutes.map((m) => m.socC))
  const longestHold = maxKnown(minutes.map((m) => m.maxMs))
  const surfaceKinds = [...new Set(samples.map((s) => s.surfaceKind).filter(Boolean))]
  const surfaceDescription = surfaceKinds.length === 1 && surfaceKinds[0] === 'package_window'
    ? 'ZLink package window; its presentation rate is not the decoded CarPlay frame rate.'
    : 'Surface ownership is unverified; this may be a recorder or another video surface.'
  const last = minutes[minutes.length - 1]

  const series: Series[] = [
    { label: 'Late frames %', colorClass: 'text-state-warn', min: 0, max: 100, values: minutes.map((m) => m.latePct) },
    { label: 'Frames / s', colorClass: 'text-state-ok', min: 0, max: 60, values: minutes.map((m) => m.fps) },
    { label: 'SoC °C', colorClass: 'text-content-muted', min: 30, max: 110, values: minutes.map((m) => m.socC), right: true },
  ]

  return (
    <div className="space-y-4">
      <div className="card flex flex-wrap items-center gap-3 p-3 text-sm">
        <span className="label text-xs">Window</span>
        {[6, 24, 72, 168].map((h) => (
          <button
            key={h}
            onClick={() => setHours(h)}
            className={cn('rounded px-2 py-1', hours === h ? 'bg-surface text-content shadow-sm' : 'text-content-muted hover:text-content')}
          >
            {h < 48 ? `${h} h` : `${h / 24} d`}
          </button>
        ))}
        {periods.length > 0 && (
          <label className="flex items-center gap-2 text-content-muted">
            Observed period
            <select
              aria-label="Observed period"
              className="rounded border border-border bg-surface px-2 py-1 text-content"
              value={selectedPeriod?.id ?? 'all'}
              onChange={(e) => setPeriodId(e.target.value)}
            >
              <option value="all">All periods</option>
              {[...periods].reverse().map((p) => (
                <option key={p.id} value={p.id}>
                  {formatDateTime(new Date(p.start).toISOString())} – {new Date(p.end + 60_000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
                </option>
              ))}
            </select>
          </label>
        )}
        {layers.length > 1 && (
          <>
            <span className="label ml-3 text-xs">Surface</span>
            {layers.map((l) => (
              <button
                key={l}
                onClick={() => setSurface(l)}
                title="Each surface is shown separately. Package-window and unverified SurfaceView observations have different meanings."
                className={cn(
                  'rounded px-2 py-1',
                  l === layer ? 'bg-surface text-content shadow-sm' : 'text-content-muted hover:text-content',
                )}
              >
                {l}
              </button>
            ))}
          </>
        )}
        <span className="ml-auto text-content-muted">
          {samples.length} samples · {minutes.length} observed minutes
        </span>
      </div>

      {diagnosticRows.length > 0 && (
        <div className="card p-4 text-sm">
          <div className="font-medium">Video delay diagnostics</div>
          <p className="mt-2 text-content-muted">
            Longest wait from buffer ready to display: {displayWait == null ? 'unavailable' : `${displayWait.toFixed(0)} ms`}.
            {' '}Largest unread ZLink TCP queue: {receiveQueue == null ? 'unavailable' : `${(receiveQueue / 1024).toFixed(1)} KiB`}.
          </p>
          <p className="mt-2 text-xs text-content-muted">
            Captured on the head unit without home Wi-Fi and recovered when parked at home.
            These are local display and socket measurements, not the full delay from a tap or the phone.
            Unavailable means Android did not expose a measurement. CPU, memory and I/O context is retained in the diagnostic log.
          </p>
        </div>
      )}

      {minutes.length === 0 ? (
        <EmptyState
          title="No CarPlay timing yet"
          description="Samples appear after the unit collects surface timing and brings it home. Missing timing does not establish that CarPlay was smooth or disconnected; check the capture events below."
        />
      ) : (
        <>
          <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
            <div className="card p-3">
              <div className="label text-xs">Late frames</div>
              <div className="text-2xl font-semibold tabular-nums">{meanLate == null ? '—' : `${meanLate.toFixed(0)}%`}</div>
              <div className="text-xs text-content-muted">minute mean · worst {worstLate == null ? '—' : `${worstLate.toFixed(0)}%`}</div>
            </div>
            <div className="card p-3">
              <div className="label text-xs">Delivered</div>
              <div className="text-2xl font-semibold tabular-nums">{meanFps == null ? '—' : `${meanFps.toFixed(1)} fps`}</div>
              <div className="text-xs text-content-muted">selected surface · minute mean</div>
            </div>
            <div className="card p-3">
              <div className="label text-xs">Hottest</div>
              <div className="text-2xl font-semibold tabular-nums">{hottest == null ? '—' : `${hottest.toFixed(0)} °C`}</div>
              <div className="text-xs text-content-muted">SoC, worst minute</div>
            </div>
            <div className="card p-3">
              <div className="label text-xs">Radio roles</div>
              <div className="text-2xl font-semibold tabular-nums">
                {last?.apMhz ?? '—'} / {last?.staMhz ?? '—'}
              </div>
              <div className="text-xs text-content-muted">hotspot MHz / Wi-Fi MHz (last)</div>
            </div>
          </div>

          <div className="card p-4">
            <div className="flex flex-wrap items-baseline justify-between gap-2">
              <div className="font-medium">Frame timing over time</div>
              <div className="flex flex-wrap gap-3 text-xs">
                {series.map((s) => (
                  <span key={s.label} className={cn('flex items-center gap-1', s.colorClass)}>
                    <span className="inline-block h-0.5 w-4 bg-current" /> {s.label}
                    {s.right ? ' (right axis)' : ''}
                  </span>
                ))}
              </div>
            </div>
            <MiniChart minutes={minutes} series={series} />
            <p className="mt-2 text-xs text-content-muted">
              Longest observed hold: {longestHold == null ? 'unknown' : `${longestHold.toFixed(0)} ms`}.
              {' '}{sampledSeconds == null ? 'Sampled duration is unavailable for this capture.' : `${sampledSeconds.toFixed(0)} seconds of frame intervals recorded${spans.length < samples.length ? ' (some sample durations are unknown)' : ''}.`}
              {' '}Gaps stay blank. Observed periods separate gaps in the data; they are not verified drive boundaries.
            </p>
            <p className="mt-2 text-xs text-content-muted">
              The current sampler counts holds above the surface’s median interval plus 1.5 display periods as late;
              older sampler thresholds differ. Surface names alone do not identify CarPlay, and these measurements
              do not include touch-to-response or audio latency. {radioObservation(last?.apMhz, last?.staMhz)}
            </p>
            <p className="mt-2 text-xs text-content-muted">{surfaceDescription}</p>
          </div>

          <div className="card overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-xs text-content-muted">
                  <th className="px-3 py-2">Minute</th>
                  <th className="px-3 py-2 text-right">fps</th>
                  <th className="px-3 py-2 text-right">late %</th>
                  <th className="px-3 py-2 text-right">p95 ms</th>
                  <th className="px-3 py-2 text-right">max ms</th>
                  <th className="px-3 py-2 text-right">SoC °C</th>
                  <th className="px-3 py-2 text-right">load</th>
                  <th className="px-3 py-2 text-right">Zlink %</th>
                  <th className="px-3 py-2 text-right">kbit/s</th>
                  <th className="px-3 py-2 text-right">Wi-Fi MHz</th>
                </tr>
              </thead>
              <tbody className="tabular-nums">
                {[...minutes].reverse().map((m) => (
                  <tr key={`${m.sessionId ?? 'legacy'}:${m.bucketStart}`} className="border-t border-border">
                    <td className="px-3 py-1.5 whitespace-nowrap">{formatDateTime(m.bucketStart)}</td>
                    <td className="px-3 py-1.5 text-right">{m.fps?.toFixed(1) ?? '—'}</td>
                    <td className={cn('px-3 py-1.5 text-right', (m.latePct ?? 0) >= 25 && 'text-state-warn')}>{m.latePct?.toFixed(0) ?? '—'}</td>
                    <td className="px-3 py-1.5 text-right">{m.p95Ms?.toFixed(0) ?? '—'}</td>
                    <td className="px-3 py-1.5 text-right">{m.maxMs?.toFixed(0) ?? '—'}</td>
                    <td className="px-3 py-1.5 text-right">{m.socC?.toFixed(0) ?? '—'}</td>
                    <td className="px-3 py-1.5 text-right">{m.load?.toFixed(1) ?? '—'}</td>
                    <td className="px-3 py-1.5 text-right">{m.zlinkCpuPct?.toFixed(0) ?? '—'}</td>
                    <td className="px-3 py-1.5 text-right">{m.hotspotRxKbit?.toFixed(0) ?? '—'}</td>
                    <td className="px-3 py-1.5 text-right">{m.staMhz ?? '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
      <div className="card p-4 text-sm">
        <div className="font-medium">Capture events</div>
        <p className="mt-1 text-xs text-content-muted">
          Neighbour and process observations do not establish an active CarPlay connection.
          A surface with no new frames may be static, stalled or hidden.
        </p>
        {events.length ? (
          <ul className="mt-3 space-y-2">
            {[...events].reverse().slice(0, 12).map((event, index) => (
              <li key={`${event.occurredAt}:${index}`} className="flex flex-wrap gap-x-3">
                <span className="text-content-muted">{formatDateTime(event.occurredAt)}</span>
                <span>{event.kind.replaceAll('_', ' ')}{event.layer ? ` · ${event.layer}` : ''}</span>
              </li>
            ))}
          </ul>
        ) : <p className="mt-3 text-content-muted">No capture events in this period. Older samplers did not record these events.</p>}
      </div>
    </div>
  )
}
