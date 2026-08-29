import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link, useParams } from 'react-router-dom'

import Spinner from '@/components/Spinner'
import { EmptyState, ErrorState, PageHeader, StatTile } from '@/components/ui'
import { api, type OBDSeriesSample } from '@/lib/api'
import { formatDateTime, formatDuration, formatSpeed, formatTime } from '@/lib/format'

/** One metric drawn against elapsed drive time. */
interface Series {
  label: string
  /** Tailwind text colour; the SVG strokes with currentColor so the theme decides. */
  colorClass: string
  values: (number | null)[]
}

const CHART_W = 720
const CHART_H = 200
const PAD_L = 44
const PAD_R = 10
const PAD_T = 10
const PAD_B = 22

function formatTick(value: number): string {
  const magnitude = Math.abs(value)
  if (magnitude >= 1000) return Math.round(value).toLocaleString()
  if (magnitude >= 10) return value.toFixed(0)
  return value.toFixed(1)
}

function formatElapsed(seconds: number): string {
  const m = Math.floor(seconds / 60)
  const s = Math.round(seconds % 60)
  return `${m}:${String(s).padStart(2, '0')}`
}

/**
 * A drive lasts minutes and holds a few hundred samples, so the whole series is drawn
 * as-is — no windowing or downsampling. Null values split the line into segments rather
 * than being interpolated over: a gap in the data should look like a gap.
 */
function TimeChart({
  title,
  unit,
  elapsedS,
  series,
}: {
  title: string
  unit: string
  elapsedS: number[]
  series: Series[]
}) {
  const drawn = series.filter((s) => s.values.some((v) => v != null))
  const tFirst = elapsedS[0]
  const tLast = elapsedS[elapsedS.length - 1]
  if (drawn.length === 0 || tFirst == null || tLast == null) return null

  const all = drawn.flatMap((s) => s.values).filter((v): v is number => v != null)
  let min = Math.min(...all)
  let max = Math.max(...all)
  if (min === max) {
    min -= 1
    max += 1
  }
  const span = max - min
  min -= span * 0.06
  max += span * 0.06

  const domain = tLast - tFirst || 1
  const x = (t: number) => PAD_L + ((t - tFirst) / domain) * (CHART_W - PAD_L - PAD_R)
  const y = (v: number) => PAD_T + (1 - (v - min) / (max - min)) * (CHART_H - PAD_T - PAD_B)

  const gridValues = [0, 1, 2, 3].map((i) => min + ((max - min) * i) / 3)
  const tickTimes = [0, 0.25, 0.5, 0.75, 1].map((f) => tFirst + domain * f)

  const segments = (values: (number | null)[]): string[] => {
    const out: string[] = []
    let current: string[] = []
    values.forEach((v, i) => {
      const t = elapsedS[i]
      if (v == null || t == null) {
        if (current.length > 1) out.push(current.join(' '))
        current = []
      } else {
        current.push(`${x(t).toFixed(1)},${y(v).toFixed(1)}`)
      }
    })
    if (current.length > 1) out.push(current.join(' '))
    return out
  }

  const stats = (values: (number | null)[]) => {
    const present = values.filter((v): v is number => v != null)
    return {
      min: Math.min(...present),
      max: Math.max(...present),
      last: present[present.length - 1] ?? 0,
    }
  }

  return (
    <section className="card p-4 sm:p-5">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="section-title">{title}</h2>
        <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-content-muted">
          {drawn.map((s) => {
            const { min: lo, max: hi, last } = stats(s.values)
            return (
              <span key={s.label} className="flex items-center gap-1.5">
                <span className={`h-0.5 w-4 rounded-full bg-current ${s.colorClass}`} />
                {s.label}
                <span className="tabular text-content-faint">
                  {formatTick(lo)}–{formatTick(hi)}, last {formatTick(last)} {unit}
                </span>
              </span>
            )
          })}
        </div>
      </div>
      <svg
        viewBox={`0 0 ${CHART_W} ${CHART_H}`}
        className="mt-3 w-full"
        role="img"
        aria-label={`${title} over the drive`}
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
              {formatTick(v)}
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
        {drawn.map((s) =>
          segments(s.values).map((points, i) => (
            <polyline
              key={`${s.label}-${i}`}
              points={points}
              fill="none"
              stroke="currentColor"
              strokeWidth={1.75}
              strokeLinejoin="round"
              strokeLinecap="round"
              className={s.colorClass}
            />
          )),
        )}
      </svg>
    </section>
  )
}

const metric = (samples: OBDSeriesSample[], pick: (s: OBDSeriesSample) => number | null) =>
  samples.map(pick)

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

  if (query.isLoading) return <Spinner label="Loading drive…" className="py-24" />
  if (query.isError) return <ErrorState error={query.error} retry={() => query.refetch()} />
  if (!query.data) return null
  const { drive, samples, diagnostics } = query.data

  return (
    <div className="space-y-4 pb-6">
      <PageHeader
        title={`Drive · ${formatDateTime(drive.startedAt)}`}
        subtitle={
          <>
            {formatTime(drive.startedAt)} – {formatTime(drive.finishedAt)} · {drive.vehicleId}
            {!drive.cleanEnd && <span className="ml-2 text-state-warn">interrupted end</span>}
          </>
        }
        actions={
          <Link to="/obd" className="btn">
            All drives
          </Link>
        }
      />

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-6">
        <StatTile label="Duration" value={formatDuration(drive.durationS)} />
        <StatTile
          label="Distance"
          value={drive.distanceKm != null ? `${drive.distanceKm.toFixed(1)} km` : '—'}
        />
        <StatTile label="Average speed" value={formatSpeed(drive.averageSpeedKmh)} />
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

      {samples.length < 2 ? (
        <EmptyState
          title="Not enough samples to chart"
          description="This drive holds fewer than two samples."
        />
      ) : (
        <div className="grid gap-4 xl:grid-cols-2">
          <TimeChart
            title="Speed"
            unit="km/h"
            elapsedS={elapsedS}
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
            series={[
              {
                label: 'Coolant',
                colorClass: 'text-state-error',
                values: metric(samples, (s) => s.coolantTemperatureC),
              },
              {
                label: 'Intake air',
                colorClass: 'text-accent',
                values: metric(samples, (s) => s.intakeAirTemperatureC),
              },
            ]}
          />
          <TimeChart
            title="Adapter voltage"
            unit="V"
            elapsedS={elapsedS}
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
            series={[
              {
                label: 'Short term',
                colorClass: 'text-accent',
                values: metric(samples, (s) => s.shortTermFuelTrimPct),
              },
              {
                label: 'Long term',
                colorClass: 'text-state-warn',
                values: metric(samples, (s) => s.longTermFuelTrimPct),
              },
            ]}
          />
          <TimeChart
            title="Fuel rate"
            unit="L/h"
            elapsedS={elapsedS}
            series={[
              {
                label: 'Estimated fuel rate',
                colorClass: 'text-accent',
                values: metric(samples, (s) => s.estimatedFuelRateLH),
              },
            ]}
          />
          <TimeChart
            title="Mass air flow and timing"
            unit="g/s · °"
            elapsedS={elapsedS}
            series={[
              {
                label: 'MAF',
                colorClass: 'text-accent',
                values: metric(samples, (s) => s.massAirFlowGS),
              },
              {
                label: 'Timing advance',
                colorClass: 'text-state-warn',
                values: metric(samples, (s) => s.timingAdvanceDeg),
              },
            ]}
          />
        </div>
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
