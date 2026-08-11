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
    busy: 'text-state-busy',
  }[tone]

  const body = (
    <div className="flex items-start gap-3">
      {icon && (
        <div className="grid h-11 w-11 shrink-0 place-items-center rounded-xl bg-accent-muted text-accent">
          {icon}
        </div>
      )}
      <div className="min-w-0">
        <div className={cn('tabular text-2xl font-bold leading-none tracking-tight sm:text-3xl', toneClass)}>
          {value}
        </div>
        <div className="mt-2 text-sm font-medium text-content-muted">{label}</div>
        {hint && <div className="mt-1 text-xs text-content-faint">{hint}</div>}
      </div>
    </div>
  )

  const className = cn('card p-4 sm:p-5', href && 'transition-all hover:-translate-y-0.5 hover:border-accent/40 hover:shadow-md')
  return href ? (
    <a href={href} className={className}>
      {body}
    </a>
  ) : (
    <div className={className}>{body}</div>
  )
}

const RECORDING_STATE_STYLE: Record<RecordingState, { label: string; className: string }> = {
  discovered: { label: 'Discovered', className: 'bg-surface-sunken text-content-muted' },
  // Deliberately neutral rather than a warning colour: a file the camera is still writing
  // is the system working, not a problem, and the next scan picks it up.
  settling: { label: 'Still writing', className: 'bg-surface-sunken text-content-muted' },
  metadata_extracted: { label: 'Inspected', className: 'bg-surface-sunken text-content-muted' },
  queued: { label: 'Queued', className: 'bg-accent-muted text-accent' },
  processing: { label: 'Processing', className: 'bg-accent-muted text-state-busy' },
  completed: { label: 'Completed', className: 'bg-state-ok/15 text-state-ok' },
  failed: { label: 'Failed', className: 'bg-state-error/15 text-state-error' },
  // Distinct from Failed on purpose. Failed will be retried; this will not, and a user
  // looking at the list has to be able to tell which of the two they are seeing.
  invalid: { label: 'Unusable file', className: 'bg-state-warn/15 text-state-warn' },
  ignored: { label: 'Ignored', className: 'bg-surface-sunken text-content-faint' },
  deleted: { label: 'Deleted', className: 'bg-surface-sunken text-content-faint' },
}

export function StateBadge({ state }: { state: RecordingState }) {
  const style = RECORDING_STATE_STYLE[state] ?? RECORDING_STATE_STYLE.discovered
  return <span className={cn('badge', style.className)}>{style.label}</span>
}

const JOB_STATE_STYLE: Record<JobState, string> = {
  queued: 'bg-surface-sunken text-content-muted',
  running: 'bg-accent-muted text-state-busy',
  completed: 'bg-state-ok/15 text-state-ok',
  failed: 'bg-state-error/15 text-state-error',
  cancelled: 'bg-surface-sunken text-content-faint',
}

export function JobStateBadge({ state }: { state: JobState }) {
  return <span className={cn('badge capitalize', JOB_STATE_STYLE[state])}>{state}</span>
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
    high: 'bg-state-ok/15 text-state-ok',
    medium: 'bg-state-warn/15 text-state-warn',
    low: 'bg-state-error/15 text-state-error',
  }[band]
  return (
    <span className={cn('badge tabular', className)} title={`${label} confidence`}>
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
    <span className={cn('inline-flex items-center gap-1.5', className)}>
      <span className="tabular font-mono font-semibold tracking-wide">{text}</span>
      {confidence !== undefined && <ConfidenceBadge confidence={confidence} />}
      {matchedPattern === null && (
        <span
          className="badge bg-state-warn/15 text-state-warn"
          title="This reading did not match a known Australian plate format"
        >
          unverified format
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
