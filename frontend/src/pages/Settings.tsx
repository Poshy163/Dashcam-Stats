import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import Spinner from '@/components/Spinner'
import { ErrorState, PageHeader } from '@/components/ui'
import { api } from '@/lib/api'
import { cn } from '@/lib/cn'
import { formatBytes } from '@/lib/format'
import type { RetentionPlan, SettingDef } from '@/lib/types'

export default function Settings() {
  const client = useQueryClient()
  const [active, setActive] = useState<string>('general')
  const [dirty, setDirty] = useState<Record<string, unknown>>({})
  const [errors, setErrors] = useState<Record<string, string>>({})
  const [saved, setSaved] = useState(false)

  const query = useQuery({ queryKey: ['settings'], queryFn: api.settings.get })

  const save = useMutation({
    mutationFn: () => api.settings.update(dirty),
    onSuccess: () => {
      setDirty({})
      setErrors({})
      setSaved(true)
      setTimeout(() => setSaved(false), 3000)
      client.invalidateQueries({ queryKey: ['settings'] })
      client.invalidateQueries({ queryKey: ['status'] })
    },
    onError: (error: Error & { detail?: unknown }) => {
      // The API names the offending key, so surface it against that field.
      const message = error.message ?? 'Could not save settings'
      const key = Object.keys(dirty).find((k) => message.includes(k))
      setErrors(key ? { [key]: message } : { _: message })
    },
  })

  const reset = useMutation({
    mutationFn: (key: string) => api.settings.reset([key]),
    onSuccess: () => client.invalidateQueries({ queryKey: ['settings'] }),
  })

  const scanNow = useMutation({ mutationFn: api.scan.now })
  const processNew = useMutation({ mutationFn: api.scan.processNew })
  const [reprocessStage, setReprocessStage] = useState('everything')
  const reprocessAll = useMutation({
    mutationFn: (onlyFailed: boolean) => api.scan.reprocessAll([reprocessStage], onlyFailed),
    onSuccess: () => client.invalidateQueries({ queryKey: ['jobs'] }),
  })
  const plan = useMutation({ mutationFn: api.retention.plan })

  const hardware = useQuery({
    queryKey: ['system-hardware'],
    queryFn: api.system.hardware,
    enabled: active === 'advanced',
  })
  const database = useQuery({
    queryKey: ['system-database'],
    queryFn: api.system.database,
    enabled: active === 'advanced',
  })

  if (query.isLoading) return <Spinner label="Loading settings…" className="py-24" />
  if (query.isError) return <ErrorState error={query.error} retry={() => query.refetch()} />
  if (!query.data) return null

  const categories = query.data
  const category = categories.find((c) => c.key === active) ?? categories[0]!
  const dirtyCount = Object.keys(dirty).length

  // A setting gated by an unticked boolean is shown but disabled, so the dependency is
  // visible rather than the control just silently doing nothing.
  const valueOf = (key: string): unknown => {
    if (key in dirty) return dirty[key]
    for (const c of categories) {
      const found = c.settings.find((s) => s.key === key)
      if (found) return found.value
    }
    return undefined
  }

  return (
    <div className="space-y-4 pb-24">
      <PageHeader
        title="Settings"
        subtitle="Changes take effect immediately — the container does not need restarting."
      />

      <div className="grid gap-5 md:grid-cols-[13rem_minmax(0,1fr)]">
        <nav className="card flex h-fit gap-1 overflow-x-auto p-2 md:sticky md:top-24 md:flex-col">
          {categories.map((c) => (
            <button
              key={c.key}
              onClick={() => setActive(c.key)}
              className={cn(
                'min-h-10 whitespace-nowrap rounded-lg px-3 py-2 text-left text-sm transition-colors',
                c.key === category.key
                  ? 'bg-accent-muted font-medium text-accent'
                  : 'text-content-muted hover:bg-surface-sunken hover:text-content',
              )}
            >
              {c.label}
            </button>
          ))}
        </nav>

        <div className="space-y-4">
          <section className="card p-5 sm:p-6">
            <h2 className="text-lg font-semibold tracking-tight">{category.label}</h2>
            {category.description && <p className="hint mt-0.5">{category.description}</p>}

            <div className="mt-4 space-y-4">
              {category.settings.map((setting) => (
                <Field
                  key={setting.key}
                  setting={setting}
                  value={valueOf(setting.key)}
                  disabled={setting.requires ? !valueOf(setting.requires) : false}
                  error={errors[setting.key]}
                  onChange={(v) => setDirty((d) => ({ ...d, [setting.key]: v }))}
                  onReset={() => reset.mutate(setting.key)}
                />
              ))}
            </div>
          </section>

          {category.key === 'scanner' && (
            <section className="card flex flex-wrap gap-2 p-4">
              <button className="btn" onClick={() => scanNow.mutate()} disabled={scanNow.isPending}>
                {scanNow.isPending ? 'Scanning…' : 'Scan Now'}
              </button>
              <button className="btn" onClick={() => processNew.mutate()} disabled={processNew.isPending}>
                Process New Footage
              </button>
              {scanNow.data && (
                <p className="hint w-full">
                  Saw {scanNow.data.seen} files, {scanNow.data.new} new,{' '}
                  {scanNow.data.queued} queued for processing
                  {scanNow.data.unsettled > 0 && (
                    <>
                      {' '}&mdash; {scanNow.data.unsettled} skipped as still being written
                    </>
                  )}
                  {scanNow.data.damagedHidden > 0 && (
                    <> &mdash; {scanNow.data.damagedHidden} damaged hidden</>
                  )}
                  {scanNow.data.damagedDeleted > 0 && (
                    <> &mdash; {scanNow.data.damagedDeleted} damaged deleted</>
                  )}
                  {scanNow.data.damagedDeleteBlocked > 0 && (
                    <>
                      {' '}&mdash; {scanNow.data.damagedDeleteBlocked} deletion blocked and
                      hidden instead
                    </>
                  )}
                  .
                </p>
              )}

              <div className="w-full border-t border-border pt-3">
                <div className="label mb-1 text-xs">Reprocess existing footage</div>
                <p className="hint mb-2">
                  Re-runs analysis on footage already indexed. Needed after a change that
                  invalidates earlier results — a decoder fix, a new model, or a corrected
                  overlay region. Queued below new footage so scanning is not starved.
                </p>
                <div className="flex flex-wrap items-center gap-2">
                  <select
                    className="input w-auto"
                    value={reprocessStage}
                    onChange={(e) => setReprocessStage(e.target.value)}
                  >
                    <option value="everything">Everything</option>
                    <option value="metadata">Metadata only</option>
                    <option value="telemetry">Telemetry only</option>
                    <option value="detection">Object detection</option>
                    <option value="plates">Licence plate detection</option>
                  </select>
                  <button
                    className="btn"
                    onClick={() => reprocessAll.mutate(false)}
                    disabled={reprocessAll.isPending}
                  >
                    Reprocess all footage
                  </button>
                  <button
                    className="btn"
                    onClick={() => reprocessAll.mutate(true)}
                    disabled={reprocessAll.isPending}
                  >
                    Reprocess failed only
                  </button>
                </div>
                {reprocessAll.data && (
                  <p className="hint mt-2">
                    Queued {reprocessAll.data.queued} recordings. Watch progress on the
                    Queue page.
                  </p>
                )}
              </div>
            </section>
          )}

          {category.key === 'storage' && (
            <RetentionPanel
              result={plan.data}
              pending={plan.isPending}
              onRun={() => plan.mutate()}
            />
          )}

          {category.key === 'advanced' && (
            <section className="card space-y-3 p-4">
              <h3 className="text-sm font-semibold">Diagnostics</h3>
              {hardware.data && (
                <pre className="overflow-x-auto rounded bg-surface-sunken p-2 text-2xs text-content-muted">
                  {JSON.stringify(hardware.data, null, 2)}
                </pre>
              )}
              {database.data && (
                <pre className="overflow-x-auto rounded bg-surface-sunken p-2 text-2xs text-content-muted">
                  {JSON.stringify(database.data, null, 2)}
                </pre>
              )}
            </section>
          )}
        </div>
      </div>

      {(dirtyCount > 0 || saved) && (
        <div className="fixed inset-x-0 bottom-0 z-20 border-t border-border bg-surface-raised/95 backdrop-blur">
          <div className="mx-auto flex max-w-[1600px] items-center gap-3 px-4 py-2.5">
            {saved ? (
              <span className="text-sm text-state-ok">Settings saved.</span>
            ) : (
              <>
                <span className="text-sm">
                  {dirtyCount} unsaved change{dirtyCount === 1 ? '' : 's'}
                </span>
                {errors._ && <span className="text-sm text-state-error">{errors._}</span>}
                <div className="ml-auto flex gap-2">
                  <button className="btn" onClick={() => { setDirty({}); setErrors({}) }}>
                    Discard
                  </button>
                  <button className="btn btn-primary" onClick={() => save.mutate()} disabled={save.isPending}>
                    {save.isPending ? 'Saving…' : 'Save changes'}
                  </button>
                </div>
              </>
            )}
          </div>
        </div>
      )}
    </div>
  )
}

function Field({
  setting,
  value,
  disabled,
  error,
  onChange,
  onReset,
}: {
  setting: SettingDef
  value: unknown
  disabled: boolean
  error?: string
  onChange: (value: unknown) => void
  onReset: () => void
}) {
  const [pendingDangerous, setPendingDangerous] = useState<unknown>(undefined)

  const change = (next: unknown) => {
    if (setting.dangerous && (next === true || next === 'delete')) {
      setPendingDangerous(next)
      return
    }
    setPendingDangerous(undefined)
    onChange(next)
  }

  const label = (
    <div className="flex flex-wrap items-baseline gap-2">
      <span className="label">{setting.label}</span>
      {setting.unit && <span className="text-2xs text-content-faint">({setting.unit})</span>}
      {setting.is_default === false && (
        <button className="text-2xs text-content-faint hover:text-accent" onClick={onReset}>
          reset to default
        </button>
      )}
    </div>
  )

  const control = () => {
    switch (setting.type) {
      case 'bool': {
        const checked = Boolean(value)
        return (
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={checked}
              disabled={disabled}
              onChange={(e) => change(e.target.checked)}
            />
            {checked ? 'Enabled' : 'Disabled'}
          </label>
        )
      }
      case 'select':
        return (
          <select
            className="input"
            value={String(value ?? '')}
            disabled={disabled}
            onChange={(e) => change(e.target.value)}
          >
            {setting.choices.map((choice) => (
              <option key={choice.value} value={choice.value}>{choice.label}</option>
            ))}
          </select>
        )
      case 'int':
      case 'float':
      case 'bytes':
        return (
          <input
            type="number"
            className="input"
            value={Number(value ?? 0)}
            min={setting.minimum ?? undefined}
            max={setting.maximum ?? undefined}
            step={setting.type === 'float' ? 0.05 : 1}
            disabled={disabled}
            onChange={(e) => onChange(setting.type === 'float' ? Number(e.target.value) : parseInt(e.target.value, 10))}
          />
        )
      default:
        return (
          <input
            type="text"
            className="input"
            value={String(value ?? '')}
            disabled={disabled}
            onChange={(e) => onChange(e.target.value)}
          />
        )
    }
  }

  return (
    <div className={cn('space-y-1', disabled && 'opacity-50')}>
      {label}
      {setting.description && <p className="hint">{setting.description}</p>}
      <div className="max-w-md">{control()}</div>
      {pendingDangerous !== undefined && (
        <div className="max-w-md rounded border border-state-error/40 p-2 text-xs">
          <p className="text-state-error">
            This option can permanently delete source footage. Deletion cannot be undone;
            path, mount and writability safety checks will still be enforced.
          </p>
          <div className="mt-2 flex gap-2">
            <button
              className="btn btn-danger px-2 py-1 text-xs"
              onClick={() => {
                onChange(pendingDangerous)
                setPendingDangerous(undefined)
              }}
            >
              I understand, apply it
            </button>
            <button
              className="btn px-2 py-1 text-xs"
              onClick={() => setPendingDangerous(undefined)}
            >
              Cancel
            </button>
          </div>
        </div>
      )}
      {disabled && setting.requires && (
        <p className="text-2xs text-content-faint">Requires “{setting.requires}” to be enabled.</p>
      )}
      {error && <p className="text-xs text-state-error">{error}</p>}
    </div>
  )
}

// Takes the pieces it needs rather than the whole mutation object: TanStack's
// UseMutationResult carries generics that would leak into this component's signature for
// no benefit.
function RetentionPanel({
  result,
  pending,
  onRun,
}: {
  result: RetentionPlan | undefined
  pending: boolean
  onRun: () => void
}) {
  return (
    <section className="card space-y-3 p-4">
      <div className="flex items-center gap-2">
        <h3 className="text-sm font-semibold">Retention</h3>
        <button className="btn ml-auto" onClick={onRun} disabled={pending}>
          {pending ? 'Evaluating…' : 'Run Now'}
        </button>
      </div>

      {result && (
        <div className="space-y-2 text-sm">
          <div className="tabular flex flex-wrap gap-x-5 gap-y-1 text-content-muted">
            <span>Current: {formatBytes(result.bytesBefore)}</span>
            <span>Limit: {formatBytes(result.bytesLimit)}</span>
            <span>Would free: {formatBytes(result.wouldFreeBytes)}</span>
            <span>{result.wouldDeleteCount} recordings</span>
          </div>

          {!result.deletionEnabled && (
            <p className="rounded border border-border bg-surface-sunken p-2 text-xs text-content-muted">
              This is a <strong className="text-content">report</strong>. Deletion is
              disabled, so nothing was removed.
            </p>
          )}

          {result.blocked && (
            <p className="rounded border border-state-warn/40 p-2 text-xs text-state-warn">
              Blocked: {result.blockedReason}
            </p>
          )}

          {result.safety && (
            <ul className="space-y-0.5 text-xs">
              {result.safety.checks.map((check) => (
                <li key={check.name} className={check.passed ? 'text-content-muted' : 'text-state-error'}>
                  {check.passed ? '✓' : '✕'} {check.name}
                  {check.reason ? ` — ${check.reason}` : ''}
                </li>
              ))}
            </ul>
          )}

          {result.candidates.length > 0 && (
            <details className="text-xs">
              <summary className="cursor-pointer text-content-muted">
                Recordings that would be removed
              </summary>
              <ul className="tabular mt-1 space-y-0.5">
                {result.candidates.slice(0, 50).map((c) => (
                  <li key={c.recordingId} className="text-content-faint">
                    {c.filename} — {formatBytes(c.sizeBytes)}
                  </li>
                ))}
              </ul>
            </details>
          )}
        </div>
      )}
    </section>
  )
}
