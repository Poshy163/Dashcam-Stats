import { useEffect, useId, useRef, useState } from 'react'
import type { PointerEvent } from 'react'
import { useQuery } from '@tanstack/react-query'

import Spinner from '@/components/Spinner'
import { EmptyState, ErrorState, Pagination } from '@/components/ui'
import { api } from '@/lib/api'
import { cn } from '@/lib/cn'
import { QUIET_ANALYSIS_LIMIT_C, temperatureHistoryIsOld, temperatureSegments, temperatureValue } from '@/lib/dashcamTemperature'
import { formatDateTime, formatShortDateTime, formatTime } from '@/lib/format'
import type { DashcamTemperatureHistory } from '@/lib/types'

const DEFAULT_W = 720
const H = 220
const LEFT = 45
const RIGHT = 18
const TOP = 16
const BOTTOM = 28
const reading = (value: number | null | undefined) => value == null || !Number.isFinite(value) ? 'Unavailable' : `${value.toFixed(1)} °C`

function TemperatureChart({ data }: { data: DashcamTemperatureHistory }) {
  const id = useId()
  const container = useRef<HTMLDivElement>(null)
  const svg = useRef<SVGSVGElement>(null)
  const [width, setWidth] = useState(DEFAULT_W)
  const [selected, setSelected] = useState<number | null>(null)
  const [tableOpen, setTableOpen] = useState(false)
  const [page, setPage] = useState(1)
  const points = data.points.filter((point) => Number.isFinite(Date.parse(point.occurredAt)))
  const values = points.map(temperatureValue).filter((value): value is number => value != null)
  const usable = points.length > 0 && values.length > 0
  useEffect(() => {
    if (!usable) return
    const element = container.current
    if (!element) return
    const resized = (next: number) => { if (Number.isFinite(next) && next > 0) setWidth(Math.max(160, Math.round(next))) }
    const measure = () => resized(element.getBoundingClientRect().width)
    measure()
    if (typeof ResizeObserver !== 'undefined') {
      const observer = new ResizeObserver((entries) => {
        const entry = entries.find((item) => item.target === element)
        if (entry) resized(entry.contentRect.width)
      })
      observer.observe(element)
      return () => observer.disconnect()
    }
    if (typeof window !== 'undefined') {
      window.addEventListener('resize', measure)
      return () => window.removeEventListener('resize', measure)
    }
  }, [usable])
  if (!usable) return <EmptyState title="No usable temperature readings" description="The retained captures contain no valid camera processor temperature for this range." />
  const W = width
  const lo = Math.floor((Math.min(QUIET_ANALYSIS_LIMIT_C, ...values) - 5) / 5) * 5
  const hi = Math.ceil((Math.max(QUIET_ANALYSIS_LIMIT_C, ...values) + 5) / 5) * 5
  const start = Date.parse(data.rangeStart)
  const end = Date.parse(data.rangeEnd)
  const x = (time: string) => LEFT + (Date.parse(time) - start) / Math.max(1, end - start) * (W - LEFT - RIGHT)
  const y = (value: number) => TOP + (1 - (value - lo) / (hi - lo)) * (H - TOP - BOTTOM)
  const selection = selected == null ? null : Math.min(selected, points.length - 1)
  const point = selection == null ? null : points[selection]!
  const segments = temperatureSegments(points, data.gapThresholdS, data.outputTruncated || points.length !== data.points.length)
  const pages = Math.ceil(points.length / 25)
  const currentPage = Math.min(page, pages)
  const onPointerMove = (event: PointerEvent<SVGSVGElement>) => {
    const rect = svg.current?.getBoundingClientRect()
    if (!rect?.width) return
    const target = start + (((event.clientX - rect.left) / rect.width) * W - LEFT) / (W - LEFT - RIGHT) * (end - start)
    let nearest = 0
    for (let index = 1; index < points.length; index++) if (Math.abs(Date.parse(points[index]!.occurredAt) - target) < Math.abs(Date.parse(points[nearest]!.occurredAt) - target)) nearest = index
    setSelected(nearest)
  }
  return (
    <>
      <div ref={container} className="w-full"><svg ref={svg} viewBox={`0 0 ${W} ${H}`} height={H} className="mt-4 w-full" style={{ touchAction: 'pan-y' }} role="img"
        aria-labelledby={`${id}-title ${id}-description`} onPointerMove={onPointerMove}>
        <title id={`${id}-title`}>Dashcam processor temperature over time, in degrees Celsius</title>
        <desc id={`${id}-description`}>Captured observations; gaps remain blank. The dashed line is the 85 degree quiet-analysis limit.</desc>
        {[0, .25, .5, .75, 1].map((fraction) => {
          const value = lo + (hi - lo) * fraction
          return <g key={fraction}><line x1={LEFT} x2={W - RIGHT} y1={y(value)} y2={y(value)} className="stroke-border" />
            <text x={LEFT - 7} y={y(value) + 3} textAnchor="end" fontSize={11} fill="currentColor" className="text-content-faint">{value.toFixed(0)} °C</text></g>
        })}
        <line x1={LEFT} x2={W - RIGHT} y1={y(QUIET_ANALYSIS_LIMIT_C)} y2={y(QUIET_ANALYSIS_LIMIT_C)} className="stroke-state-warn" strokeDasharray="5 4" />
        {(W < 480 ? [0, 1] : [0, .5, 1]).map((fraction) => <text key={fraction} x={LEFT + fraction * (W - LEFT - RIGHT)} y={H - 7}
          textAnchor={fraction === 0 ? 'start' : fraction === 1 ? 'end' : 'middle'} fontSize={11} fill="currentColor" className="text-content-faint">
          {(data.hours > 24 ? formatShortDateTime : formatTime)(new Date(start + fraction * (end - start)).toISOString())}
        </text>)}
        {segments.map((indices) => indices.length > 1
          ? <polyline key={indices[0]} points={indices.map((index) => `${x(points[index]!.occurredAt).toFixed(1)},${y(temperatureValue(points[index])!).toFixed(1)}`).join(' ')}
            fill="none" stroke="currentColor" strokeWidth={2} strokeLinejoin="round" className="text-accent" />
          : <circle key={indices[0]} cx={x(points[indices[0]!]!.occurredAt)} cy={y(temperatureValue(points[indices[0]!])!)} r={3} fill="currentColor" className="text-accent" />)}
        {point && <line x1={x(point.occurredAt)} x2={x(point.occurredAt)} y1={TOP} y2={H - BOTTOM} className="stroke-content-faint" strokeDasharray="2 3" />}
      </svg></div>
      <p className="mt-1 text-xs text-content-muted">Dashed line: {QUIET_ANALYSIS_LIMIT_C} °C quiet-analysis limit. Gaps are unobserved; this line is not a manufacturer safety rating.</p>
      <div className="mt-3 text-sm">
        <label htmlFor={`${id}-sample`} className="text-xs text-content-muted">Explore captured temperatures</label>
        <input id={`${id}-sample`} className="mt-1 w-full" type="range" min={0} max={points.length - 1} step={1}
          value={selection ?? points.length - 1} onChange={(event) => setSelected(Number(event.target.value))}
          aria-valuetext={`${formatDateTime((point ?? points[points.length - 1]!).occurredAt)}: ${reading(temperatureValue(point ?? points[points.length - 1]))}`} />
        <p aria-live="polite" className="tabular text-xs text-content-muted">{formatDateTime((point ?? points[points.length - 1]!).occurredAt)} · {reading(temperatureValue(point ?? points[points.length - 1]))}</p>
      </div>
      <details className="mt-3 text-sm" onToggle={(event) => setTableOpen(event.currentTarget.open)}>
        <summary className="cursor-pointer text-content-muted">View observed temperatures</summary>
        {tableOpen && <div className="overflow-x-auto"><table className="mt-2 w-full text-left text-xs">
          <caption className="sr-only">Retained dashcam processor temperature observations</caption>
          <thead><tr><th scope="col">Captured at</th><th scope="col">SoC temperature</th><th scope="col">Quality</th></tr></thead>
          <tbody>{points.slice((currentPage - 1) * 25, currentPage * 25).map((item, index) => <tr key={`${item.occurredAt}:${item.segmentId}:${index}`}>
            <td className="py-1 pr-3">{formatDateTime(item.occurredAt)}</td><td className="tabular pr-3">{reading(temperatureValue(item))}</td><td>{item.quality}</td></tr>)}</tbody>
        </table><Pagination page={currentPage} pages={pages} total={points.length} onChange={setPage} /></div>}
      </details>
    </>
  )
}

export function DashcamTemperatureEvidence({ data, error = false, nowMs = Date.now() }: { data: DashcamTemperatureHistory; error?: boolean; nowMs?: number }) {
  const latest = data.latest
  const old = temperatureHistoryIsOld(latest, nowMs, data.gapThresholdS)
  return (
    <>
      {error && <p role="alert" className="mt-3 text-sm text-state-warn">Temperature refresh failed. Showing previously loaded historical captures; current temperature is unknown.</p>}
      <div className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-3">
        <div><div className="label text-xs">Last captured</div><div className="tabular text-xl font-semibold">{reading(temperatureValue(latest))}</div>
          <div className="text-xs text-content-muted">{latest ? formatDateTime(latest.occurredAt) : 'No observation in this range'}</div>
          {latest && <div className={cn('mt-1 text-xs', old || error ? 'text-state-warn' : 'text-content-faint')}>{old || error ? 'Historical reading · current temperature unknown' : 'Captured observation, not a live reading'}</div>}
        </div>
        <div><div className="label text-xs">Minimum in range</div><div className="tabular text-xl font-semibold">{reading(data.minimumC)}</div></div>
        <div><div className="label text-xs">Maximum in range</div><div className="tabular text-xl font-semibold">{reading(data.maximumC)}</div></div>
      </div>
      <p className="mt-3 text-xs text-content-faint">{formatDateTime(data.rangeStart)} – {formatDateTime(data.rangeEnd)} · {data.uniqueObservationCount.toLocaleString()} captured observations.</p>
      {data.truncated && <p role="status" className="mt-2 text-sm text-state-warn">History is incomplete because a capture limit was reached. Statistics describe the available observations.{data.outputTruncated ? ' Displayed points are unconnected because some observations were omitted.' : ''}</p>}
      {data.points.length === 0 ? <EmptyState title="No temperature history in this range" description="History appears when retained on-device timing captures contain camera temperature. Offline periods and uncaptured time remain blank." />
        : <TemperatureChart data={data} />}
    </>
  )
}

export function DashcamTemperatureCard() {
  const [hours, setHours] = useState<1 | 6 | 24 | 168>(24)
  const query = useQuery({ queryKey: ['dashcam-temperature', hours], queryFn: () => api.unitLogs.temperature({ hours }), refetchInterval: 30_000 })
  return (
    <section className="card mb-6 px-5 py-4" aria-label="Dashcam temperature">
      <div className="flex flex-wrap items-center justify-between gap-3"><h2 className="text-base font-semibold">Dashcam temperature</h2>
        <div role="group" aria-label="Temperature time range" className="flex flex-wrap gap-1">{([1, 6, 24, 168] as const).map((range) => <button type="button" key={range}
          className={cn('rounded px-3 py-1 text-sm', range === hours ? 'bg-surface text-content shadow-sm' : 'bg-surface-sunken text-content-muted hover:text-content')}
          aria-pressed={range === hours} onClick={() => setHours(range)}>{range === 168 ? '7 d' : `${range} h`}</button>)}</div>
      </div>
      <p className="mt-1 text-sm text-content-muted">Camera processor (SoC), °C — separate from engine coolant. Stored on-device timing captures; gaps mean no capture was available. These historical observations are not continuous monitoring.</p>
      {query.isPending && <Spinner label="Loading temperature history…" />}
      {query.data ? <DashcamTemperatureEvidence key={hours} data={query.data} error={query.isError} nowMs={Math.max(query.dataUpdatedAt, Date.now())} />
        : query.isError && <ErrorState title="Could not load temperature history" error={query.error} retry={() => void query.refetch()} />}
    </section>
  )
}
