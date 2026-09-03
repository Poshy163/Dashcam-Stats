import type { ReactNode } from 'react'

import { cn } from '@/lib/cn'
import { confidenceBand, formatPercent } from '@/lib/format'
import type { JobState, RecordingState } from '@/lib/types'

export function PageHeader({
  title,
  subtitle,
  actions,
}: {
  title: string
  subtitle?: ReactNode
  actions?: ReactNode
}) {
  return (
    <div className="mb-7 flex flex-wrap items-start justify-between gap-4">
      <div className="min-w-0">
        <h1 className="text-2xl font-bold tracking-tight sm:text-3xl">{title}</h1>
        {subtitle && <div className="mt-1.5 text-sm text-content-muted sm:text-base">{subtitle}</div>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  )
}

export function StatTile({
  label,
  value,
  hint,
  tone = 'default',
  href,
  icon,
}: {
  label: string
  value: ReactNode
  hint?: ReactNode
  tone?: 'default' | 'ok' | 'warn' | 'error' | 'busy'
  href?: string
  icon?: ReactNode
}) {
  const toneClass = {
    default: 'text-content',
    ok: 'text-state-ok',
    warn: 'text-state-warn',
    error: 'text-state-error',
    busy: 'text-cyan',
  }[tone]

  const body = (
    <div className="flex items-start justify-between gap-3">
      <div className="min-w-0">
        <div className="font-mono text-xs font-semibold uppercase tracking-wider text-content-muted">
          {label}
        </div>
        <div className={cn('tabular font-mono text-2xl font-black leading-none tracking-tight sm:text-3xl mt-2', toneClass)}>
          {value}
        </div>
        {hint && <div className="mt-2 font-mono text-2xs text-content-faint">{hint}</div>}
      </div>
      {icon && (
        <div className="grid h-10 w-10 shrink-0 place-items-center rounded-xl border border-accent/30 bg-accent/10 text-accent shadow-sm">
          {icon}
        </div>
      )}
    </div>
  )

  const className = cn(
    'card relative overflow-hidden p-4 sm:p-5 border-border/80 transition-all hover:-translate-y-0.5 hover:border-accent/50 hover:shadow-card',
    href && 'cursor-pointer',
  )
  return href ? (
    <a href={href} className={className}>
      <div className="absolute top-0 left-0 right-0 h-[2px] bg-gradient-to-r from-transparent via-accent/40 to-transparent" />
      {body}
    </a>
  ) : (
    <div className={className}>
      <div className="absolute top-0 left-0 right-0 h-[2px] bg-gradient-to-r from-transparent via-accent/40 to-transparent" />
      {body}
    </div>
  )
}

const RECORDING_STATE_STYLE: Record<RecordingState, { label: string; dot: string; className: string }> = {
  discovered: { label: 'Discovered', dot: 'bg-content-faint', className: 'bg-surface-sunken text-content-muted border-border' },
  settling: { label: 'Still writing', dot: 'bg-content-faint animate-pulse', className: 'bg-surface-sunken text-content-muted border-border' },
  metadata_extracted: { label: 'Inspected', dot: 'bg-cyan', className: 'bg-cyan/10 text-cyan border-cyan/30' },
  queued: { label: 'Queued', dot: 'bg-state-warn', className: 'bg-state-warn/10 text-state-warn border-state-warn/30' },
  processing: { label: 'Processing', dot: 'bg-cyan animate-ping', className: 'bg-cyan/15 text-cyan border-cyan/40' },
  completed: { label: 'Completed', dot: 'bg-state-ok', className: 'bg-state-ok/15 text-state-ok border-state-ok/30' },
  failed: { label: 'Failed', dot: 'bg-state-error', className: 'bg-state-error/15 text-state-error border-state-error/30' },
  invalid: { label: 'Unusable', dot: 'bg-state-warn', className: 'bg-state-warn/15 text-state-warn border-state-warn/30' },
  ignored: { label: 'Ignored', dot: 'bg-content-faint', className: 'bg-surface-sunken text-content-faint border-border' },
  deleted: { label: 'Deleted', dot: 'bg-content-faint', className: 'bg-surface-sunken text-content-faint border-border' },
}

export function StateBadge({ state }: { state: RecordingState }) {
  const style = RECORDING_STATE_STYLE[state] ?? RECORDING_STATE_STYLE.discovered
  return (
    <span className={cn('badge border', style.className)}>
      <span className={cn('h-1.5 w-1.5 rounded-full', style.dot)} />
      {style.label}
    </span>
  )
}

const JOB_STATE_STYLE: Record<JobState, { dot: string; className: string }> = {
  queued: { dot: 'bg-state-warn', className: 'bg-state-warn/10 text-state-warn border-state-warn/30' },
  running: { dot: 'bg-cyan animate-pulse', className: 'bg-cyan/15 text-cyan border-cyan/40' },
  completed: { dot: 'bg-state-ok', className: 'bg-state-ok/15 text-state-ok border-state-ok/30' },
  failed: { dot: 'bg-state-error', className: 'bg-state-error/15 text-state-error border-state-error/30' },
  cancelled: { dot: 'bg-content-faint', className: 'bg-surface-sunken text-content-faint border-border' },
}

export function JobStateBadge({ state }: { state: JobState }) {
  const style = JOB_STATE_STYLE[state] ?? JOB_STATE_STYLE.queued
  return (
    <span className={cn('badge border capitalize', style.className)}>
      <span className={cn('h-1.5 w-1.5 rounded-full', style.dot)} />
      {state}
    </span>
  )
}

/**
 * A plate is never shown without its confidence. The product rule is that an uncertain
 * OCR read must look uncertain rather than being presented as fact, so the number travels
 * with the text everywhere it appears.
 */
export function ConfidenceBadge({
  confidence,
  label = 'OCR',
}: {
  confidence: number
  label?: string
}) {
  const band = confidenceBand(confidence)
  const className = {
    high: 'border-state-ok/40 bg-state-ok/10 text-state-ok',
    medium: 'border-state-warn/40 bg-state-warn/10 text-state-warn',
    low: 'border-state-error/40 bg-state-error/10 text-state-error',
  }[band]
  return (
    <span className={cn('badge tabular font-mono border', className)} title={`${label} confidence`}>
      {label} {formatPercent(confidence)}
    </span>
  )
}

/**
 * Renders plate text with its confidence, and marks reads that matched no known
 * Australian format so a guess is never mistaken for a verified plate.
 */
export function PlateText({
  text,
  confidence,
  matchedPattern,
  className,
}: {
  text: string
  confidence?: number
  matchedPattern?: string | null
  className?: string
}) {
  return (
    <span className={cn('inline-flex items-center gap-2', className)}>
      <span className="tabular font-mono font-black tracking-widest uppercase rounded border border-border/90 bg-surface-sunken px-2 py-0.5 shadow-inner text-content">
        {text}
      </span>
      {confidence !== undefined && <ConfidenceBadge confidence={confidence} />}
      {matchedPattern === null && (
        <span
          className="badge border border-state-warn/30 bg-state-warn/10 text-state-warn"
          title="This reading did not match a known Australian plate format"
        >
          unverified
        </span>
      )}
    </span>
  )
}

export function EmptyState({
  title,
  description,
  action,
}: {
  title: string
  description?: ReactNode
  action?: ReactNode
}) {
  return (
    <div className="card flex flex-col items-center gap-2 px-6 py-16 text-center">
      <div className="grid h-12 w-12 place-items-center rounded-full bg-accent-muted text-xl text-accent">·</div>
      <div className="mt-1 text-base font-semibold">{title}</div>
      {description && <div className="max-w-md text-sm text-content-muted">{description}</div>}
      {action && <div className="mt-2">{action}</div>}
    </div>
  )
}

export function ErrorState({ error, retry }: { error: unknown; retry?: () => void }) {
  const message = error instanceof Error ? error.message : 'Something went wrong'
  return (
    <div className="card border-state-error/40 px-6 py-10 text-center">
      <div className="text-sm font-medium text-state-error">Could not load this page</div>
      <div className="mt-1 text-sm text-content-muted">{message}</div>
      {retry && (
        <button className="btn mt-3" onClick={retry}>
          Try again
        </button>
      )}
    </div>
  )
}

export function Pagination({
  page,
  pages,
  total,
  onChange,
}: {
  page: number
  pages: number
  total: number
  onChange: (page: number) => void
}) {
  if (pages <= 1) {
    return <div className="tabular py-3 text-xs text-content-faint">{total} results</div>
  }
  return (
    <div className="flex items-center justify-between gap-3 py-3">
      <div className="tabular text-xs text-content-faint">
        Page {page} of {pages} · {total} results
      </div>
      <div className="flex gap-1.5">
        <button className="btn" disabled={page <= 1} onClick={() => onChange(page - 1)}>
          Previous
        </button>
        <button className="btn" disabled={page >= pages} onClick={() => onChange(page + 1)}>
          Next
        </button>
      </div>
    </div>
  )
}

export function ProgressBar({ value, className }: { value: number; className?: string }) {
  const pct = Math.max(0, Math.min(100, value * 100))
  return (
    <div className={cn('h-2.5 w-full overflow-hidden rounded-full bg-surface-sunken', className)}>
      <div
        className="h-full rounded-full bg-accent transition-[width] duration-500"
        style={{ width: `${pct}%` }}
      />
    </div>
  )
}

/** Used wherever a value is computed from GPS rather than reported by the camera. */
export function DerivedHint({ children }: { children: ReactNode }) {
  return (
    <span
      className="cursor-help text-content-faint"
      title="Derived from consecutive GPS fixes — this dashcam does not report it directly"
    >
      {children}
    </span>
  )
}
