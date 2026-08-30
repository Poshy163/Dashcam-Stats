import { useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link, useParams } from 'react-router-dom'
import type { PointerEvent as ReactPointerEvent } from 'react'

import Spinner from '@/components/Spinner'
import { EmptyState, ErrorState, PageHeader, StatTile } from '@/components/ui'
import { api, type OBDSeriesSample } from '@/lib/api'
import { formatDateTime, formatDuration, formatRelative, formatSpeed, formatTime } from '@/lib/format'

/** One metric drawn against elapsed drive time. */
interface Series {
  label: string
  /** Tailwind text colour; the SVG strokes with currentColor so the theme decides. */
  colorClass: string
  values: (number | null)[]
  unit?: string
  /** Logger poll-plan cadence, never inferred from a sparse or interrupted trace. */
  expectedCadenceS?: number
  provenance?: 'measured' | 'derived'
}

const CHART_W = 720
const CHART_H = 200
const PAD_L = 44
const PAD_R = 10
const PAD_T = 10
const PAD_B = 22

/** Fast samples beyond the documented 5 s cadence plus 50% tolerance are a gap. */
const FAST_GAP_THRESHOLD_S = 7.5

function formatTick(value: number, span?: number): string {
  // Decimals follow the axis range, not the value's size: a 12.1–12.9 V axis labelled
  // to whole volts reads 13/13/13/12, which is four gridlines and two numbers.
  const scale = span ?? Math.abs(value)
  if (Math.abs(value) >= 1000) return Math.round(value).toLocaleString()
  if (scale >= 20) return value.toFixed(0)
  if (scale >= 2) return value.toFixed(1)
  return value.toFixed(2)
}

function formatElapsed(seconds: number): string {
  const total = Math.max(0, Math.round(seconds))
  const m = Math.floor(total / 60)
  const s = total % 60
  return `${m}:${String(s).padStart(2, '0')}`
}

/**
 * A drive lasts minutes and holds a few hundred samples, so the whole series is drawn
 * as-is — no windowing or downsampling. Null values split the line into segments rather
 * than being interpolated over: a gap in the data should look like a gap. Pointer moves
 * (mouse or the head unit's touchscreen) pin the nearest sample and show its values;
 * `touch-action: pan-y` keeps vertical page scrolling alive while a finger scrubs.
 */
function TimeChart({
  title,
  unit,
  elapsedS,
  timesIso,
  series,
}: {
  title: string
  unit: string
  elapsedS: number[]
  /** Original sample timestamps, so the tooltip can name the wall-clock moment. */
  timesIso: string[]
  series: Series[]
}) {
  const svgRef = useRef<SVGSVGElement>(null)
  const [hover, setHover] = useState<number | null>(null)

  const drawn = series.filter((s) => s.values.some((v) => v != null))
  const tFirst = elapsedS[0]
  const tLast = elapsedS[elapsedS.length - 1]
  if (drawn.length === 0 || tFirst == null || tLast == null) return null

  const all = drawn.flatMap((s) => s.values).filter((v): v is number => v != null)
  const rawMin = Math.min(...all)
  let min = rawMin
  let max = Math.max(...all)
  if (min === max) {
    min -= 1
    max += 1
  }
  const span = max - min
  min -= span * 0.06
  max += span * 0.06
  // A speed axis that dips to −2.8 km/h invents readings the car never produced.
  if (rawMin >= 0 && min < 0) min = 0

  const domain = tLast - tFirst || 1
  const x = (t: number) => PAD_L + ((t - tFirst) / domain) * (CHART_W - PAD_L - PAD_R)
  const y = (v: number) => PAD_T + (1 - (v - min) / (max - min)) * (CHART_H - PAD_T - PAD_B)

  const gridValues = [0, 1, 2, 3].map((i) => min + ((max - min) * i) / 3)
  const tickTimes = [0, 0.25, 0.5, 0.75, 1].map((f) => tFirst + domain * f)

  // Not every metric arrives every cycle. Connect only across the explicit logger-plan
  // cadence plus its fixed tolerance: sparse traces cannot teach the UI that a long outage
  // was normal. Anything left isolated is drawn as a measured dot.
  const segments = (item: Series): { lines: string[]; dots: [number, number][] } => {
    const values = item.values
    const observed: { t: number; v: number }[] = []
    values.forEach((v, i) => {
      const t = elapsedS[i]
      if (v != null && t != null) observed.push({ t, v })
    })
    if (observed.length === 0) return { lines: [], dots: [] }
    const bridge = (item.expectedCadenceS ?? 5) * 1.5

    const lines: string[] = []
    const dots: [number, number][] = []
    let run: { t: number; v: number }[] = []
    const flush = () => {
      if (run.length > 1) {
        lines.push(run.map((o) => `${x(o.t).toFixed(1)},${y(o.v).toFixed(1)}`).join(' '))
      } else if (run.length === 1 && run[0]) {
        dots.push([x(run[0].t), y(run[0].v)])
      }
      run = []
    }
    for (const o of observed) {
      const previous = run[run.length - 1]
      if (previous && o.t - previous.t > bridge) flush()
      run.push(o)
    }
    flush()
    return { lines, dots }
  }

  const stats = (values: (number | null)[]) => {
    const present = values
      .map((value, index) => ({ value, index }))
      .filter((item): item is { value: number; index: number } => item.value != null)
    const last = present[present.length - 1]
    return {
      min: Math.min(...present.map((item) => item.value)),
      max: Math.max(...present.map((item) => item.value)),
      last: last?.value ?? 0,
      lastAt: last ? elapsedS[last.index] : null,
    }
  }

  const onPointerMove = (event: ReactPointerEvent<SVGSVGElement>) => {
    const svg = svgRef.current
    if (!svg) return
    const rect = svg.getBoundingClientRect()
    const t = tFirst + (((event.clientX - rect.left) / rect.width) * CHART_W - PAD_L) * (domain / (CHART_W - PAD_L - PAD_R))
    // Nearest sample by time. The list is sorted, so binary-search the insertion point.
    let lo = 0
    let hi = elapsedS.length - 1
    while (lo < hi) {
      const mid = (lo + hi) >> 1
      if ((elapsedS[mid] ?? 0) < t) lo = mid + 1
      else hi = mid
    }
    const before = elapsedS[lo - 1]
    const at = elapsedS[lo]
    const index =
      lo > 0 && before != null && at != null && t - before < at - t ? lo - 1 : lo
    setHover(Math.max(0, Math.min(index, elapsedS.length - 1)))
  }

  const hoverT = hover != null ? elapsedS[hover] : null
  const hoverClock = hover != null ? formatDateTime(timesIso[hover]) : null
  const tooltipTitle = hoverT != null ? `${formatElapsed(hoverT)} · ${hoverClock}` : ''
  // Never silently substitute a nearby reading. A null is a null at this sample; the
  // cadence-aware line is presentation only and carries no invented point value.
  const valueAt = (values: (number | null)[]): number | null =>
    hover == null ? null : (values[hover] ?? null)
  const tooltipRows =
    hover != null
      ? drawn.map((s) => ({
          label: s.label,
          colorClass: s.colorClass,
          value: valueAt(s.values),
          provenance: s.provenance ?? 'measured',
          unit: s.unit ?? unit,
        }))
      : []
  const tooltipW =
    12 +
    6.4 *
      Math.max(
        8,
        tooltipTitle.length,
        ...tooltipRows.map(
          (row) =>
            `${row.label} ${row.value != null ? formatTick(row.value) : '—'} ${row.unit} ${row.provenance}`
              .length,
        ),
      )
  const tooltipH = 16 + tooltipRows.length * 13
  const tooltipX =
    hoverT != null ? (x(hoverT) > PAD_L + (CHART_W - PAD_L - PAD_R) * 0.62 ? x(hoverT) - tooltipW - 8 : x(hoverT) + 8) : 0

  return (
    <section className="card p-4 sm:p-5">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="section-title">{title}</h2>
        <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-content-muted">
          {drawn.map((s) => {
            const { min: lo, max: hi, last, lastAt } = stats(s.values)
            const stale =
              lastAt != null && tLast - lastAt > (s.expectedCadenceS ?? 5) * 1.5
            const seriesUnit = s.unit ?? unit
            return (
              <span key={s.label} className="flex items-center gap-1.5">
                <span className={`h-0.5 w-4 rounded-full bg-current ${s.colorClass}`} />
                {s.label}
                <span className="tabular text-content-faint">
                  {formatTick(lo)}–{formatTick(hi)}, last {formatTick(last)} {seriesUnit} at{' '}
                  {lastAt != null ? formatElapsed(lastAt) : '—'}
                  {stale && <span className="ml-1 text-state-warn">stale</span>}
                </span>
              </span>
            )
          })}
        </div>
      </div>
      <svg
        ref={svgRef}
        viewBox={`0 0 ${CHART_W} ${CHART_H}`}
        className="mt-3 w-full"
        style={{ touchAction: 'pan-y' }}
        role="img"
        aria-label={`${title} over the drive`}
        onPointerMove={onPointerMove}
        onPointerLeave={() => setHover(null)}
      >
        {gridValues.map((v) => (
          <g key={v}>
            <line
              x1={PAD_L}
              x2={CHART_W - PAD_R}
              y1={y(v)}
              y2={y(v)}
              className="stroke-border"
              strokeWidth={1}
            />
            <text
              x={PAD_L - 6}
              y={y(v) + 3}
              textAnchor="end"
              fontSize={10}
              fill="currentColor"
              className="text-content-faint"
            >
              {formatTick(v, max - min)}
            </text>
          </g>
        ))}
        {tickTimes.map((t) => (
          <text
            key={t}
            x={x(t)}
            y={CHART_H - 6}
            textAnchor="middle"
            fontSize={10}
            fill="currentColor"
            className="text-content-faint"
          >
            {formatElapsed(t)}
          </text>
        ))}
        {drawn.map((s) => {
          const { lines, dots } = segments(s)
          return (
            <g key={s.label} className={s.colorClass}>
              {lines.map((points, i) => (
                <polyline
                  key={i}
                  points={points}
                  fill="none"
                  stroke="currentColor"
                  strokeWidth={1.75}
                  strokeLinejoin="round"
                  strokeLinecap="round"
                />
              ))}
              {dots.map(([cx, cy], i) => (
                <circle key={`dot-${i}`} cx={cx} cy={cy} r={2.5} fill="currentColor" />
              ))}
            </g>
          )
        })}
        {hover != null && hoverT != null && (
          <g pointerEvents="none">
            <line
              x1={x(hoverT)}
              x2={x(hoverT)}
              y1={PAD_T}
              y2={CHART_H - PAD_B}
              className="stroke-content-faint"
              strokeWidth={1}
              strokeDasharray="3 3"
            />
            {tooltipRows.map(
              (row) =>
                row.value != null && (
                  <circle
                    key={row.label}
                    cx={x(hoverT)}
                    cy={y(row.value)}
                    r={3}
                    fill="currentColor"
                    className={row.colorClass}
                  />
                ),
            )}
            <rect
              x={tooltipX}
              y={PAD_T}
              width={tooltipW}
              height={tooltipH}
              rx={4}
              className="fill-surface-raised stroke-border"
              strokeWidth={1}
            />
            <text
              x={tooltipX + 6}
              y={PAD_T + 12}
              fontSize={10}
              fontWeight={600}
              fill="currentColor"
              className="text-content"
            >
              {tooltipTitle}
            </text>
            {tooltipRows.map((row, i) => (
              <text
                key={row.label}
                x={tooltipX + 6}
                y={PAD_T + 25 + i * 13}
                fontSize={10}
                fill="currentColor"
                className={row.colorClass}
              >
                {row.label} {row.value != null ? `${formatTick(row.value)} ${row.unit}` : '—'} ·{' '}
                {row.provenance}
              </text>
            ))}
          </g>
        )}
      </svg>
    </section>
  )
}

/** Time-in-band bars, e.g. how much of the drive was spent in each speed range. */
function BandBars({ title, bands }: { title: string; bands: { label: string; seconds: number }[] }) {
  const total = bands.reduce((sum, band) => sum + band.seconds, 0)
  if (total <= 0) return null
  return (
    <section className="card p-4 sm:p-5">
      <h2 className="section-title">{title}</h2>
      <div className="mt-3 space-y-2">
        {bands.map((band) => {
          const share = band.seconds / total
          return (
            <div key={band.label} className="flex items-center gap-3 text-xs">
              <span className="w-24 shrink-0 text-content-muted">{band.label}</span>
              <div className="h-3 min-w-0 flex-1 overflow-hidden rounded-full bg-surface-sunken">
                <div
                  className="h-full rounded-full bg-accent"
                  style={{ width: `${Math.max(share * 100, band.seconds > 0 ? 1 : 0)}%` }}
                />
              </div>
              <span className="tabular w-28 shrink-0 text-right text-content-faint">
                {formatDuration(band.seconds)} · {Math.round(share * 100)}%
              </span>
            </div>
          )
        })}
      </div>
    </section>
  )
}

const metric = (samples: OBDSeriesSample[], pick: (s: OBDSeriesSample) => number | null) =>
  samples.map(pick)

interface DerivedStats {
  movingAverageKmh: number | null
  stoppedShare: number | null
  maxAccelKmhS: number | null
  maxBrakeKmhS: number | null
  warmupS: number | null
  speedBands: { label: string; seconds: number }[]
  rpmBands: { label: string; seconds: number }[]
}

/**
 * Everything here is derived from the raw samples rather than sent by the server, so the
 * page and the stored data can never disagree. Interval time is charged to the sample
 * ending it only inside the fast-tier cadence tolerance; a dropout contributes no
 * inferred time to either endpoint's band.
 */
function deriveStats(samples: OBDSeriesSample[], elapsedS: number[]): DerivedStats {
  const speedBandEdges = [
    { label: 'Stopped', match: (v: number) => v < 1 },
    { label: '1–30 km/h', match: (v: number) => v >= 1 && v < 30 },
    { label: '30–50 km/h', match: (v: number) => v >= 30 && v < 50 },
    { label: '50–60 km/h', match: (v: number) => v >= 50 && v < 60 },
    { label: '60+ km/h', match: (v: number) => v >= 60 },
  ]
  const rpmBandEdges = [
    { label: 'Under 1,000', match: (v: number) => v < 1000 },
    { label: '1,000–1,500', match: (v: number) => v >= 1000 && v < 1500 },
    { label: '1,500–2,000', match: (v: number) => v >= 1500 && v < 2000 },
    { label: '2,000–2,500', match: (v: number) => v >= 2000 && v < 2500 },
    { label: '2,500+', match: (v: number) => v >= 2500 },
  ]
  const speedSeconds = speedBandEdges.map(() => 0)
  const rpmSeconds = rpmBandEdges.map(() => 0)

  let movingWeighted = 0
  let movingTime = 0
  let stoppedTime = 0
  let speedTime = 0
  let maxAccel: number | null = null
  let maxBrake: number | null = null
  let warmupS: number | null = null

  for (let i = 0; i < samples.length; i += 1) {
    const sample = samples[i]
    const t = elapsedS[i]
    if (!sample || t == null) continue

    const prevT = i > 0 ? elapsedS[i - 1] : null
    const interval = prevT != null ? Math.max(t - prevT, 0) : 0
    const gap = prevT != null && interval > FAST_GAP_THRESHOLD_S
    const dt = gap ? 0 : interval

    const speed = sample.vehicleSpeedKmh
    if (speed != null && dt > 0) {
      speedTime += dt
      const band = speedBandEdges.findIndex((b) => b.match(speed))
      if (band >= 0) speedSeconds[band] = (speedSeconds[band] ?? 0) + dt
      if (speed < 1) stoppedTime += dt
      else {
        movingWeighted += speed * dt
        movingTime += dt
      }
      const prevSpeed = i > 0 ? samples[i - 1]?.vehicleSpeedKmh : null
      if (!gap && prevSpeed != null && prevT != null && t > prevT) {
        const accel = (speed - prevSpeed) / (t - prevT)
        if (accel >= 0) maxAccel = maxAccel == null ? accel : Math.max(maxAccel, accel)
        else maxBrake = maxBrake == null ? -accel : Math.max(maxBrake, -accel)
      }
    }

    const rpm = sample.engineRpm
    if (rpm != null && dt > 0) {
      const band = rpmBandEdges.findIndex((b) => b.match(rpm))
      if (band >= 0) rpmSeconds[band] = (rpmSeconds[band] ?? 0) + dt
    }

    const coolant = sample.coolantTemperatureC
    if (warmupS == null && coolant != null && coolant >= 80) {
      const first = samples[0]?.coolantTemperatureC
      if (first != null && first < 80) warmupS = t
    }
  }

  return {
    movingAverageKmh: movingTime > 0 ? movingWeighted / movingTime : null,
    stoppedShare: speedTime > 0 ? stoppedTime / speedTime : null,
    maxAccelKmhS: maxAccel,
    maxBrakeKmhS: maxBrake,
    warmupS,
    speedBands: speedBandEdges.map((band, i) => ({
      label: band.label,
      seconds: speedSeconds[i] ?? 0,
    })),
    rpmBands: rpmBandEdges.map((band, i) => ({
      label: band.label,
      seconds: rpmSeconds[i] ?? 0,
    })),
  }
}

export default function ObdDriveDetail() {
  const { driveId } = useParams()
  const query = useQuery({
    queryKey: ['obd-drive', driveId],
    queryFn: () => api.obd.driveSeries(driveId ?? ''),
    enabled: Boolean(driveId),
  })

  const elapsedS = useMemo(() => {
    const samples = query.data?.samples ?? []
    const first = samples[0]
    if (!first) return []
    const t0 = Date.parse(first.t)
    return samples.map((s) => (Date.parse(s.t) - t0) / 1000)
  }, [query.data])

  const derived = useMemo(
    () => deriveStats(query.data?.samples ?? [], elapsedS),
    [query.data, elapsedS],
  )

  if (query.isLoading) return <Spinner label="Loading drive…" className="py-24" />
  if (query.isError) return <ErrorState error={query.error} retry={() => query.refetch()} />
  if (!query.data) return null
  const { drive, journey, samples, diagnostics } = query.data
  const timesIso = samples.map((s) => s.t)

  // Instantaneous L/100 km is meaningless while (nearly) stopped — the divisor is the
  // speed — so the trace only claims the moving parts of the drive.
  const movingConsumption = samples.map((s) =>
    s.vehicleSpeedKmh != null && s.vehicleSpeedKmh >= 5 ? s.estimatedFuelConsumptionL100km : null,
  )

  return (
    <div className="space-y-4 pb-6">
      <PageHeader
        title={`Drive · ${formatDateTime(drive.startedAt)}`}
        subtitle={
          <>
            {formatTime(drive.startedAt)} – {formatTime(drive.finishedAt)} · {drive.vehicleId}
            {drive.lifecycleStatus !== 'complete' && (
              <span className="ml-2 text-state-warn">
                {drive.lifecycleStatus.replace(/_/g, ' ')} end
              </span>
            )}
          </>
        }
        actions={
          <>
            {journey && (
              <Link to={`/journeys/${journey.id}`} className="btn">
                {journey.title ? `Journey: ${journey.title}` : 'View journey footage'}
              </Link>
            )}
            {drive.bundleDownloadUrl && (
              <a className="btn" href={drive.bundleDownloadUrl} download={drive.bundleFilename}>
                Download verified bundle
              </a>
            )}
            <Link to="/obd" className="btn">
              All drives
            </Link>
          </>
        }
      />

      <section className="card p-4 sm:p-5">
        <div className="flex flex-wrap items-center gap-2">
          <span
            className={`rounded-full px-2.5 py-1 text-xs font-semibold ${
              drive.lifecycleStatus === 'complete'
                ? 'bg-state-ok/15 text-state-ok'
                : drive.lifecycleStatus === 'recovered'
                  ? 'bg-accent-muted text-accent'
                  : 'bg-state-warn/15 text-state-warn'
            }`}
          >
            {drive.lifecycleStatus.replace(/_/g, ' ')}
          </span>
          {drive.interruptionReason && (
            <span className="text-sm text-content-muted">
              {drive.interruptionReason.replace(/_/g, ' ')}
            </span>
          )}
          <span className="ml-auto text-xs text-content-faint">
            projection {drive.processingStatus} · summary {drive.summarySource}
          </span>
        </div>
        <dl className="mt-4 grid gap-3 text-sm sm:grid-cols-2 lg:grid-cols-4">
          <div>
            <dt className="text-xs text-content-faint">Drive started</dt>
            <dd className="mt-0.5 tabular">{formatDateTime(drive.startedAt)}</dd>
          </div>
          <div>
            <dt className="text-xs text-content-faint">Effective end</dt>
            <dd className="mt-0.5 tabular">{formatDateTime(drive.finishedAt)}</dd>
          </div>
          <div>
            <dt className="text-xs text-content-faint">First OBD sample</dt>
            <dd className="mt-0.5 tabular">
              {drive.firstSampleAt ? formatDateTime(drive.firstSampleAt) : 'No valid sample'}
            </dd>
          </div>
          <div>
            <dt className="text-xs text-content-faint">Last valid sample</dt>
            <dd className="mt-0.5 tabular">
              {drive.lastSampleAt ? formatDateTime(drive.lastSampleAt) : 'No valid sample'}
            </dd>
          </div>
          <div>
            <dt className="text-xs text-content-faint">Last successful OBD response</dt>
            <dd className="mt-0.5 tabular">
              {drive.lastSuccessfulResponseAt
                ? formatDateTime(drive.lastSuccessfulResponseAt)
                : 'Not recorded'}
            </dd>
          </div>
          <div>
            <dt className="text-xs text-content-faint">Termination observed</dt>
            <dd className="mt-0.5 tabular">
              {drive.finalizationObservedAt
                ? formatDateTime(drive.finalizationObservedAt)
                : 'Not recorded'}
            </dd>
          </div>
          <div>
            <dt className="text-xs text-content-faint">Bluetooth disconnects</dt>
            <dd className="mt-0.5 tabular">{drive.connectionLossCount.toLocaleString()}</dd>
          </div>
          <div>
            <dt className="text-xs text-content-faint">Transport gap duration</dt>
            <dd className="mt-0.5 tabular">
              {formatDuration(
                drive.gapAnalysis?.transport.totalGapDurationS ?? drive.missingDataDurationS,
              )}
            </dd>
          </div>
          <div>
            <dt className="text-xs text-content-faint">Export / backup</dt>
            <dd className="mt-0.5">
              {drive.exportStatus} · {drive.backupStatus}
              {drive.verifiedAt ? ` ${formatRelative(drive.verifiedAt)}` : ''}
            </dd>
          </div>
          <div>
            <dt className="text-xs text-content-faint">Home Assistant</dt>
            <dd className="mt-0.5">
              {drive.importState.replace(/_/g, ' ')}
              {drive.importedAt ? ` ${formatRelative(drive.importedAt)}` : ''}
            </dd>
          </div>
        </dl>
        {(drive.bundleError || drive.lastProcessingError || drive.validationWarnings.length > 0) && (
          <div className="mt-4 space-y-1 rounded-md bg-state-warn/10 p-3 text-xs text-state-warn">
            {drive.bundleError && <p>{drive.bundleError}</p>}
            {drive.lastProcessingError && <p>{drive.lastProcessingError}</p>}
            {drive.validationWarnings.map((warning) => (
              <p key={warning}>{warning}</p>
            ))}
          </div>
        )}
      </section>

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-6">
        <StatTile label="Duration" value={formatDuration(drive.durationS)} />
        <StatTile
          label="Distance"
          value={drive.distanceKm != null ? `${drive.distanceKm.toFixed(1)} km` : '—'}
        />
        <StatTile
          label="Average speed"
          value={formatSpeed(drive.averageSpeedKmh)}
          hint={
            derived.movingAverageKmh != null
              ? `${formatSpeed(derived.movingAverageKmh)} while moving`
              : undefined
          }
        />
        <StatTile label="Top speed" value={formatSpeed(drive.maximumSpeedKmh)} />
        <StatTile
          label="Fuel used"
          value={drive.estimatedFuelUsedL != null ? `${drive.estimatedFuelUsedL.toFixed(2)} L` : '—'}
          hint={
            drive.averageFuelConsumptionL100km != null
              ? `${drive.averageFuelConsumptionL100km.toFixed(1)} L/100 km`
              : undefined
          }
        />
        <StatTile
          label="Samples"
          value={drive.sampleCount.toLocaleString()}
          hint={
            drive.receivedSamplePercentage != null
              ? `${Math.round(drive.receivedSamplePercentage)}% of expected`
              : undefined
          }
          tone={drive.errorCount > 0 ? 'warn' : 'default'}
        />
      </div>

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-6">
        <StatTile
          label="Idle time"
          value={formatDuration(drive.idleDurationS)}
          hint={
            derived.stoppedShare != null
              ? `stopped ${Math.round(derived.stoppedShare * 100)}% of the drive`
              : undefined
          }
        />
        <StatTile
          label="Average RPM"
          value={drive.averageRpm != null ? Math.round(drive.averageRpm).toLocaleString() : '—'}
          hint={
            drive.maximumRpm != null
              ? `peak ${Math.round(drive.maximumRpm).toLocaleString()}`
              : undefined
          }
        />
        <StatTile
          label="Peak engine load"
          value={
            drive.maximumEngineLoadPct != null ? `${Math.round(drive.maximumEngineLoadPct)}%` : '—'
          }
        />
        <StatTile
          label="Hardest acceleration"
          value={
            derived.maxAccelKmhS != null ? `${derived.maxAccelKmhS.toFixed(1)} km/h·s` : '—'
          }
          hint={
            derived.maxBrakeKmhS != null
              ? `hardest braking ${derived.maxBrakeKmhS.toFixed(1)} km/h·s`
              : undefined
          }
        />
        <StatTile
          label="Coolant warm-up"
          value={derived.warmupS != null ? formatDuration(derived.warmupS) : '—'}
          hint={
            drive.maximumCoolantTemperatureC != null
              ? `peak ${Math.round(drive.maximumCoolantTemperatureC)}°C`
              : undefined
          }
        />
        <StatTile
          label="Data gaps"
          value={
            drive.gapCount > 0 ? drive.gapCount.toLocaleString() : 'none'
          }
          hint={
            drive.longestGapS != null
              ? `longest ${formatDuration(drive.longestGapS)} · ${
                  drive.dataCompletenessPercentage != null
                    ? `${drive.dataCompletenessPercentage.toFixed(1)}% signal coverage`
                    : 'coverage unavailable'
                }`
              : undefined
          }
          tone={
            drive.gapCount > 0 ? 'warn' : 'default'
          }
        />
      </div>

      {drive.gapAnalysis && (
        <section className="card overflow-x-auto">
          <div className="border-b border-border p-3">
            <h2 className="section-title">Signal cadence and gaps</h2>
            <p className="mt-1 text-xs text-content-muted">
              Expected cadence comes from logger poll plan v{drive.gapAnalysis.pollPlanVersion};
              tier spacing is not counted as missing data.
            </p>
          </div>
          <table className="w-full min-w-[52rem] text-sm">
            <thead className="border-b border-border text-left text-xs text-content-muted">
              <tr>
                <th className="p-2 font-medium">Signal</th>
                <th className="p-2 font-medium">Source</th>
                <th className="p-2 font-medium">Cadence</th>
                <th className="p-2 font-medium">Observed / expected</th>
                <th className="p-2 font-medium">Coverage</th>
                <th className="p-2 font-medium">p95 / max</th>
                <th className="p-2 font-medium">Gaps</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {drive.gapAnalysis.signals
                .filter((signal) => signal.supported || signal.observationCount > 0)
                .map((signal) => (
                  <tr key={signal.name}>
                    <td className="p-2">
                      {signal.label}
                      {signal.pid && <span className="ml-1 text-xs text-content-faint">PID {signal.pid}</span>}
                    </td>
                    <td className="p-2 text-content-muted">
                      {signal.provenance} · {signal.tier}
                    </td>
                    <td className="tabular p-2">{formatDuration(signal.expectedCadenceS)}</td>
                    <td className="tabular p-2">
                      {signal.receivedObservationCount.toLocaleString()} /{' '}
                      {signal.expectedObservationCount.toLocaleString()}
                    </td>
                    <td className="tabular p-2">
                      {signal.coveragePercentage != null
                        ? `${signal.coveragePercentage.toFixed(1)}%`
                        : '—'}
                    </td>
                    <td className="tabular p-2">
                      {signal.p95CadenceS != null ? signal.p95CadenceS.toFixed(1) : '—'} /{' '}
                      {signal.maximumCadenceS != null ? signal.maximumCadenceS.toFixed(1) : '—'} s
                    </td>
                    <td className="tabular p-2">
                      {signal.gapCount}
                      {signal.missingObservationCount > 0
                        ? ` · ${signal.missingObservationCount} missing reads`
                        : ''}
                    </td>
                  </tr>
                ))}
            </tbody>
          </table>
        </section>
      )}

      {samples.length < 2 ? (
        <EmptyState
          title="Not enough samples to chart"
          description="This drive holds fewer than two samples."
        />
      ) : (
        <>
          <div className="grid gap-4 xl:grid-cols-2">
            <TimeChart
              title="Speed"
              unit="km/h"
              elapsedS={elapsedS}
              timesIso={timesIso}
              series={[
                {
                  label: 'Vehicle speed',
                  colorClass: 'text-accent',
                  values: metric(samples, (s) => s.vehicleSpeedKmh),
                },
              ]}
            />
            <TimeChart
              title="Engine RPM"
              unit="rpm"
              elapsedS={elapsedS}
              timesIso={timesIso}
              series={[
                {
                  label: 'RPM',
                  colorClass: 'text-accent',
                  values: metric(samples, (s) => s.engineRpm),
                },
              ]}
            />
            <TimeChart
              title="Temperatures"
              unit="°C"
              elapsedS={elapsedS}
              timesIso={timesIso}
              series={[
                {
                  label: 'Coolant',
                  colorClass: 'text-state-error',
                  values: metric(samples, (s) => s.coolantTemperatureC),
                  expectedCadenceS: 15,
                },
                {
                  label: 'Intake air',
                  colorClass: 'text-accent',
                  values: metric(samples, (s) => s.intakeAirTemperatureC),
                  expectedCadenceS: 15,
                },
              ]}
            />
            <TimeChart
              title="Adapter voltage"
              unit="V"
              elapsedS={elapsedS}
              timesIso={timesIso}
              series={[
                {
                  label: 'Voltage',
                  colorClass: 'text-state-ok',
                  values: metric(samples, (s) => s.adapterVoltageV),
                },
              ]}
            />
            <TimeChart
              title="Load and throttle"
              unit="%"
              elapsedS={elapsedS}
              timesIso={timesIso}
              series={[
                {
                  label: 'Engine load',
                  colorClass: 'text-accent',
                  values: metric(samples, (s) => s.engineLoadPct),
                },
                {
                  label: 'Throttle',
                  colorClass: 'text-state-warn',
                  values: metric(samples, (s) => s.throttlePositionPct),
                },
              ]}
            />
            <TimeChart
              title="Fuel trims"
              unit="%"
              elapsedS={elapsedS}
              timesIso={timesIso}
              series={[
                {
                  label: 'Short term',
                  colorClass: 'text-accent',
                  values: metric(samples, (s) => s.shortTermFuelTrimPct),
                  expectedCadenceS: 15,
                },
                {
                  label: 'Long term',
                  colorClass: 'text-state-warn',
                  values: metric(samples, (s) => s.longTermFuelTrimPct),
                  expectedCadenceS: 15,
                },
              ]}
            />
            <TimeChart
              title="Fuel rate"
              unit="L/h"
              elapsedS={elapsedS}
              timesIso={timesIso}
              series={[
                {
                  label: 'Estimated fuel rate',
                  colorClass: 'text-accent',
                  values: metric(samples, (s) => s.estimatedFuelRateLH),
                  provenance: 'derived',
                },
              ]}
            />
            <TimeChart
              title="Consumption while moving"
              unit="L/100 km"
              elapsedS={elapsedS}
              timesIso={timesIso}
              series={[
                {
                  label: 'Est. consumption (5+ km/h)',
                  colorClass: 'text-accent',
                  values: movingConsumption,
                  provenance: 'derived',
                },
              ]}
            />
            <TimeChart
              title="Oxygen sensors"
              unit="V"
              elapsedS={elapsedS}
              timesIso={timesIso}
              series={[
                {
                  label: 'Sensor 1',
                  colorClass: 'text-accent',
                  values: metric(samples, (s) => s.oxygenSensor1VoltageV),
                  expectedCadenceS: 15,
                },
                {
                  label: 'Sensor 2',
                  colorClass: 'text-state-warn',
                  values: metric(samples, (s) => s.oxygenSensor2VoltageV),
                  expectedCadenceS: 15,
                },
              ]}
            />
            <TimeChart
              title="Mass air flow and timing"
              unit="g/s · °"
              elapsedS={elapsedS}
              timesIso={timesIso}
              series={[
                {
                  label: 'MAF',
                  colorClass: 'text-accent',
                  values: metric(samples, (s) => s.massAirFlowGS),
                  unit: 'g/s',
                },
                {
                  label: 'Timing advance',
                  colorClass: 'text-state-warn',
                  values: metric(samples, (s) => s.timingAdvanceDeg),
                  unit: '°',
                },
              ]}
            />
          </div>

          <div className="grid gap-4 xl:grid-cols-2">
            <BandBars title="Time at speed" bands={derived.speedBands} />
            <BandBars title="Time at RPM" bands={derived.rpmBands} />
          </div>
        </>
      )}

      {diagnostics.length > 0 && (
        <div className="card overflow-x-auto">
          <div className="border-b border-border p-3">
            <h2 className="section-title">Diagnostic events</h2>
          </div>
          <table className="w-full min-w-[36rem] text-sm">
            <thead className="border-b border-border text-left text-xs text-content-muted">
              <tr>
                <th className="p-2 font-medium">Observed</th>
                <th className="p-2 font-medium">Kind</th>
                <th className="p-2 font-medium">Detail</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {diagnostics.map((event, i) => (
                <tr key={i} className="hover:bg-surface-sunken">
                  <td className="tabular p-2 text-content-muted">
                    {event.observedAt ? formatTime(event.observedAt) : '—'}
                  </td>
                  <td className="p-2">{event.kind.replace(/_/g, ' ')}</td>
                  <td className="p-2 text-xs text-content-muted">
                    <code>{JSON.stringify(event.payload)}</code>
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
