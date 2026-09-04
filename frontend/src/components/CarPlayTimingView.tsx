/**
 * CarPlay frame timing, as sampled on the head unit itself.
 *
 * The lag the operator feels in CarPlay was measured to live in the video path: Zlink's
 * own views render cleanly while the surface the CarPlay picture is drawn on delivers
 * frames late. A sampler on the unit reads that surface's timing from SurfaceFlinger every
 * few seconds while a phone is attached, alongside what could be starving it -- load,
 * temperature, Zlink's CPU, the hotspot's bitrate -- and the unit-log collector carries the
 * lines home. This view turns them back into a chart and a per-minute table, so a long
 * drive reads as a shape rather than a thousand log lines.
 */
import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'

import Spinner from '@/components/Spinner'
import { EmptyState, ErrorState } from '@/components/ui'
import { api } from '@/lib/api'
import { cn } from '@/lib/cn'
import { formatDateTime } from '@/lib/format'
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
        const points = s.values
          .map((v, i) => (v == null ? null : `${x(i).toFixed(1)},${y(Math.max(s.min, Math.min(s.max, v)), s).toFixed(1)}`))
          .filter((p): p is string => p != null)
          .join(' ')
        return (
          <g key={s.label} className={s.colorClass}>
            <polyline points={points} fill="none" stroke="currentColor" strokeWidth={1.75} strokeLinejoin="round" />
          </g>
        )
      })}
    </svg>
  )
}

export default function CarPlayTimingView({ live }: { live: boolean }) {
  const [hours, setHours] = useState(24)
  const [surface, setSurface] = useState('')
  const query = useQuery({
    queryKey: ['carplay-timing', hours],
    queryFn: () => api.unitLogs.carplayTiming({ hours }),
    refetchInterval: live ? 15_000 : false,
  })

  if (query.isLoading) return <Spinner />
  if (query.isError) return <ErrorState error={query.error} />
  const data = query.data
  const allMinutes = data?.minutes ?? []

  // The unit always draws on more than one SurfaceView, and nothing in the sampler's
  // output says which is CarPlay's video: the layer's `#N` is reassigned between
  // sessions and the name carries no package. Measured live, two surfaces in the same
  // minute ran 35 ms and 53 ms cadences, so an average across them described neither.
  // They are shown one at a time instead, busiest first, and the operator picks.
  const layers = [...new Set(allMinutes.map((m) => m.layer).filter(Boolean))].sort(
    (a, b) =>
      allMinutes.filter((m) => m.layer === b).length -
      allMinutes.filter((m) => m.layer === a).length,
  )
  const layer = layers.includes(surface) ? surface : layers[0] ?? ''
  const minutes = layer ? allMinutes.filter((m) => m.layer === layer) : allMinutes

  const meanFps = minutes.length ? minutes.reduce((a, m) => a + (m.fps ?? 0), 0) / minutes.length : null
  const meanLate = minutes.length ? minutes.reduce((a, m) => a + (m.latePct ?? 0), 0) / minutes.length : null
  const worstLate = minutes.length ? Math.max(...minutes.map((m) => m.latePct ?? 0)) : null
  const hottest = minutes.length ? Math.max(...minutes.map((m) => m.socC ?? 0)) : null
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
        {layers.length > 1 && (
          <>
            <span className="label ml-3 text-xs">Surface</span>
            {layers.map((l) => (
              <button
                key={l}
                onClick={() => setSurface(l)}
                title="The unit draws on more than one SurfaceView and does not say which is CarPlay's video. Each is shown separately rather than averaged together."
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
          {data?.total ?? 0} samples · {minutes.length} minutes with a phone attached
        </span>
      </div>

      {minutes.length === 0 ? (
        <EmptyState
          title="No CarPlay timing yet"
          description="Samples appear once the car has been driven with a phone attached to CarPlay and the unit has been home to hand them over. While no phone is attached the sampler only heartbeats, which shows under the Head unit log as CarPlayTiming."
        />
      ) : (
        <>
          <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
            <div className="card p-3">
              <div className="label text-xs">Late frames</div>
              <div className="text-2xl font-semibold tabular-nums">{meanLate?.toFixed(0)}%</div>
              <div className="text-xs text-content-muted">mean · worst minute {worstLate?.toFixed(0)}%</div>
            </div>
            <div className="card p-3">
              <div className="label text-xs">Delivered</div>
              <div className="text-2xl font-semibold tabular-nums">{meanFps?.toFixed(1)} fps</div>
              <div className="text-xs text-content-muted">CarPlay video surface</div>
            </div>
            <div className="card p-3">
              <div className="label text-xs">Hottest</div>
              <div className="text-2xl font-semibold tabular-nums">{hottest?.toFixed(0)} °C</div>
              <div className="text-xs text-content-muted">SoC, worst minute</div>
            </div>
            <div className="card p-3">
              <div className="label text-xs">Radio roles</div>
              <div className="text-2xl font-semibold tabular-nums">
                {last?.apMhz ?? '—'} / {last?.staMhz ?? '—'}
              </div>
              <div className="text-xs text-content-muted">hotspot MHz / home link MHz (last)</div>
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
              Late = the share of frames that landed two or more display refreshes after their slot. A home-link
              value beside the hotspot means the unit was on your Wi-Fi as well, so its single radio was hopping
              between two channels; on the road that column is empty.
            </p>
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
                  <th className="px-3 py-2 text-right">home link</th>
                </tr>
              </thead>
              <tbody className="tabular-nums">
                {[...minutes].reverse().map((m) => (
                  <tr key={m.bucketStart} className="border-t border-border">
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
    </div>
  )
}
