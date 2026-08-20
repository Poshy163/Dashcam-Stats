import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useSearchParams } from 'react-router-dom'

import Spinner from '@/components/Spinner'
import { ErrorState, PageHeader } from '@/components/ui'
import { api } from '@/lib/api'
import { cn } from '@/lib/cn'
import { formatBytes, formatDate, formatRelative } from '@/lib/format'
import { invalidateAnalysisQueries } from '@/lib/queryInvalidation'
import type { RetentionPlan, SettingDef } from '@/lib/types'

export default function Settings() {
  const client = useQueryClient()
  // The category lives in the URL so other pages can link straight to their own settings
  // — the Backup page does — and so a reload keeps you where you were.
  const [params, setParams] = useSearchParams()
  const active = params.get('category') ?? 'general'
  const setActive = (key: string) => setParams({ category: key }, { replace: true })
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

  const refreshQueueAndAnalysis = () => {
    void client.invalidateQueries({ queryKey: ['jobs'] })
    void client.invalidateQueries({ queryKey: ['queue-stats'] })
    // The Queue page keeps its own copy of the same counts, and a reset changes every one
    // of them. Left cached, it shows the previous run's figures until its next poll.
    void client.invalidateQueries({ queryKey: ['queue-stats-page'] })
    void invalidateAnalysisQueries(client)
  }

  const scanNow = useMutation({ mutationFn: api.scan.now, onSuccess: refreshQueueAndAnalysis })
  const processNew = useMutation({
    mutationFn: api.scan.processNew,
    onSuccess: refreshQueueAndAnalysis,
  })
  const [reprocessStage, setReprocessStage] = useState('everything')
  const reprocessAll = useMutation({
    mutationFn: ({ onlyFailed = false, onlyOutdated = false }: { onlyFailed?: boolean; onlyOutdated?: boolean }) =>
      api.scan.reprocessAll([reprocessStage], onlyFailed, onlyOutdated),
    onSuccess: refreshQueueAndAnalysis,
  })
  const restore = useMutation({ mutationFn: api.system.restore })
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
                  // Read-only settings are ones the app derives and writes for itself, so
                  // the box is shown but not typed into — the API refuses the write anyway,
                  // and an editable field that always fails to save is worse than none.
                  disabled={
                    setting.read_only || (setting.requires ? !valueOf(setting.requires) : false)
                  }
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
                  {scanNow.data.damagedRestored > 0 && (
                    <>
                      {' '}&mdash; {scanNow.data.damagedRestored} playable recordings restored
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
                  overlay region.
                </p>
                <p className="hint mb-2">
                  <strong>Reprocess all footage</strong> starts again: the queue is emptied,
                  anything in flight is stopped, the counts reset, and it is rebuilt from
                  the footage — every missing thumbnail first, then the full analysis oldest
                  first. The other two add to the existing queue and leave the rest of it
                  alone.
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
                    onClick={() => {
                      // It discards the queue, including work in progress, so it asks.
                      if (
                        window.confirm(
                          'Clear the processing queue and rebuild it from the footage?\n\n' +
                            'Anything currently processing is stopped, waiting and failed ' +
                            'jobs are discarded, and every recording is queued again — ' +
                            'missing thumbnails first, then full analysis oldest first. ' +
                            'No footage or history is deleted.',
                        )
                      ) {
                        reprocessAll.mutate({})
                      }
                    }}
                    disabled={reprocessAll.isPending}
                  >
                    Reprocess all footage
                  </button>
                  <button
                    className="btn"
                    onClick={() => reprocessAll.mutate({ onlyFailed: true })}
                    disabled={reprocessAll.isPending}
                  >
                    Reprocess failed only
                  </button>
                  <button
                    className="btn btn-primary"
                    onClick={() => reprocessAll.mutate({ onlyOutdated: true })}
                    disabled={reprocessAll.isPending}
                  >
                    Reprocess outdated only
                  </button>
                </div>
                {reprocessAll.data && (
                  <p className="hint mt-2">
                    {reprocessAll.data.reset ? (
                      <>
                        Queue rebuilt: {reprocessAll.data.queued} recordings,{' '}
                        {reprocessAll.data.thumbnailsQueued ?? 0} of them queued for a
                        thumbnail first. Cleared{' '}
                        {Object.values(reprocessAll.data.cleared ?? {}).reduce(
                          (total, n) => total + n,
                          0,
                        )}{' '}
                        old jobs
                        {(reprocessAll.data.aborted ?? 0) > 0 &&
                          ` and stopped ${reprocessAll.data.aborted} in flight`}
                        .{' '}
                        {reprocessAll.data.paused
                          ? 'The queue is paused — resume it to start.'
                          : 'Watch progress on the Queue page.'}
                      </>
                    ) : (
                      <>
                        Queued {reprocessAll.data.queued} recordings. Watch progress on the
                        Queue page.
                      </>
                    )}
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

          {category.key === 'security' && <SecurityPanel />}

          {category.key === 'advanced' && (
            <section className="card space-y-4 p-4">
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
              <div className="border-t border-border pt-3">
                <h3 className="text-sm font-semibold">Backup and recovery</h3>
                <p className="hint mt-1">
                  Backups are consistent while analysis is running. A validated restore is applied on the next container restart, and the current database is retained as a pre-restore backup.
                </p>
                <div className="mt-2 flex flex-wrap items-center gap-2">
                  <a className="btn" href={api.system.backupUrl()}>Download database backup</a>
                  <label className="btn cursor-pointer">
                    Validate restore…
                    <input
                      className="sr-only"
                      type="file"
                      accept=".db,.sqlite,.sqlite3,application/vnd.sqlite3"
                      onChange={(event) => {
                        const file = event.target.files?.[0]
                        if (file && window.confirm('Validate and stage this database for restore on the next restart?')) {
                          restore.mutate(file)
                        }
                        event.target.value = ''
                      }}
                    />
                  </label>
                </div>
                {restore.isPending && <p className="hint mt-2">Validating backup…</p>}
                {restore.data && <p className="mt-2 text-sm text-state-ok">{restore.data.message}</p>}
                {restore.isError && <p className="mt-2 text-sm text-state-error">{restore.error.message}</p>}
              </div>
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
          <div className="flex gap-2">
            <input
              type="text"
              className="input"
              value={String(value ?? '')}
              disabled={disabled}
              onChange={(e) => onChange(e.target.value)}
            />
            {setting.key === API_KEY_SETTING && !disabled && (
              // A key nobody types is a key nobody makes weak. Left to invent one, the
              // realistic outcome is a short memorable string standing in for the password
              // on a deployment with a public hostname.
              <button
                type="button"
                className="btn shrink-0"
                onClick={() => onChange(generateApiKey())}
              >
                Generate
              </button>
            )}
          </div>
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

/** The one setting that gets a generator beside it. */
const API_KEY_SETTING = 'security.api_key'

/**
 * A key of the same shape the backend's `generate_api_key` produces.
 *
 * 32 bytes of `crypto.getRandomValues`, base64url so it survives being written into the
 * URL the dashcam's head unit is opened on — which is the entire reason the key exists.
 */
function generateApiKey(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(32))
  return btoa(String.fromCharCode(...bytes))
    .replace(/\+/g, '-')
    .replace(/\//g, '_')
    .replace(/=+$/, '')
}

/** Shortest password the backend will accept. Kept in step with `MIN_PASSWORD_LENGTH`. */
const MIN_PASSWORD_LENGTH = 12

/**
 * The account behind the “Require sign-in” switch, and the browsers currently holding a
 * session against it.
 *
 * The password cannot go through the generic settings grid above: every value there is
 * echoed back by `GET /api/settings` to render this page, so a password field would hand
 * itself to anyone who asked. It has its own endpoint and its own panel for that reason.
 */
function SecurityPanel() {
  const client = useQueryClient()
  const auth = useQuery({ queryKey: ['auth-state'], queryFn: api.auth.state })
  const sessions = useQuery({
    queryKey: ['auth-sessions'],
    queryFn: api.auth.sessions,
    enabled: auth.data?.configured === true,
  })

  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [current, setCurrent] = useState('')
  const [done, setDone] = useState<string | null>(null)

  const configured = auth.data?.configured === true
  const refresh = () => {
    void client.invalidateQueries({ queryKey: ['auth-state'] })
    void client.invalidateQueries({ queryKey: ['auth-sessions'] })
    void client.invalidateQueries({ queryKey: ['settings'] })
  }

  const save = useMutation({
    mutationFn: () => api.auth.setCredential(username.trim(), password, current || undefined),
    onSuccess: () => {
      setPassword('')
      setConfirm('')
      setCurrent('')
      setDone(configured ? 'Password changed. Every other browser was signed out.' : 'Account created.')
      refresh()
    },
  })

  const clear = useMutation({
    mutationFn: () => api.auth.clearCredential(current || undefined),
    onSuccess: () => {
      setCurrent('')
      setDone('Account deleted and sign-in switched off.')
      refresh()
    },
  })

  const revokeOthers = useMutation({
    mutationFn: api.auth.revokeOtherSessions,
    onSuccess: (result) => {
      setDone(`Signed out ${result.revoked} other browser${result.revoked === 1 ? '' : 's'}.`)
      refresh()
    },
  })

  const revokeOne = useMutation({
    mutationFn: api.auth.revokeSession,
    onSuccess: refresh,
  })

  useEffect(() => {
    if (auth.data?.username && !username) setUsername(auth.data.username)
  }, [auth.data?.username, username])

  const mismatch = confirm.length > 0 && password !== confirm
  const tooShort = password.length > 0 && password.length < MIN_PASSWORD_LENGTH
  const canSave =
    username.trim().length > 0 &&
    password.length >= MIN_PASSWORD_LENGTH &&
    password === confirm &&
    (!configured || current.length > 0)

  const error = save.error ?? clear.error ?? revokeOthers.error ?? revokeOne.error

  return (
    <section className="card space-y-4 p-4">
      <div>
        <h3 className="text-sm font-semibold">Sign-in account</h3>
        <p className="hint mt-1">
          {configured ? (
            <>
              One account, <strong className="text-content">{auth.data?.username}</strong>. There
              is no user list and no roles — this is the person who owns the footage.
            </>
          ) : (
            <>
              No account yet. Set one here, then turn on “Require sign-in” above. The first
              account can only be claimed from your own network; after that it can be changed
              from anywhere with the current password.
            </>
          )}
        </p>
      </div>

      {auth.data?.misconfigured && (
        <p className="rounded border border-state-warn/40 bg-state-warn/10 p-2 text-xs text-state-warn">
          Sign-in is switched on with no account behind it, so nothing is actually being
          asked for. Setting a password below closes this.
        </p>
      )}

      {!configured && auth.data?.canClaimAccount === false && (
        <p className="rounded border border-state-warn/40 bg-state-warn/10 p-2 text-xs text-state-warn">
          You are reaching this from outside the local network, so the first account cannot
          be claimed here. Open the app from home, or run{' '}
          <code>entrypoint.sh recover-login set-password</code> on the host.
        </p>
      )}

      <div className="grid max-w-md gap-3">
        <div className="space-y-1">
          <label className="label" htmlFor="auth-username">Username</label>
          <input
            id="auth-username"
            className="input"
            value={username}
            autoComplete="username"
            onChange={(event) => setUsername(event.target.value)}
          />
        </div>

        {configured && (
          <div className="space-y-1">
            <label className="label" htmlFor="auth-current">Current password</label>
            <input
              id="auth-current"
              className="input"
              type="password"
              value={current}
              autoComplete="current-password"
              onChange={(event) => setCurrent(event.target.value)}
            />
            <p className="hint">
              Asked for so that a session someone else is holding cannot be used to change
              the password and lock you out.
            </p>
          </div>
        )}

        <div className="space-y-1">
          <label className="label" htmlFor="auth-password">
            {configured ? 'New password' : 'Password'}
          </label>
          <input
            id="auth-password"
            className="input"
            type="password"
            value={password}
            autoComplete="new-password"
            onChange={(event) => setPassword(event.target.value)}
          />
          <p className={cn('hint', tooShort && 'text-state-error')}>
            At least {MIN_PASSWORD_LENGTH} characters. The database this is stored in can be
            downloaded from Settings → Advanced, so length is what protects it if that file
            ever leaves the machine.
          </p>
        </div>

        <div className="space-y-1">
          <label className="label" htmlFor="auth-confirm">Repeat password</label>
          <input
            id="auth-confirm"
            className="input"
            type="password"
            value={confirm}
            autoComplete="new-password"
            onChange={(event) => setConfirm(event.target.value)}
          />
          {mismatch && <p className="text-xs text-state-error">Those do not match.</p>}
        </div>
      </div>

      <div className="flex flex-wrap gap-2">
        <button
          className="btn btn-primary"
          disabled={!canSave || save.isPending}
          onClick={() => {
            setDone(null)
            save.mutate()
          }}
        >
          {save.isPending ? 'Saving…' : configured ? 'Change password' : 'Create account'}
        </button>
        {configured && (
          <button
            className="btn btn-danger"
            disabled={clear.isPending || current.length === 0}
            onClick={() => {
              if (
                window.confirm(
                  'Delete the sign-in account?\n\nSign-in is switched off at the same time, ' +
                    'every browser is signed out, and anyone who can reach this address will ' +
                    'have full access again.',
                )
              ) {
                setDone(null)
                clear.mutate()
              }
            }}
          >
            Delete account and turn sign-in off
          </button>
        )}
      </div>

      {done && <p className="text-sm text-state-ok">{done}</p>}
      {error && <p className="text-sm text-state-error">{error.message}</p>}

      {configured && (
        <div className="border-t border-border pt-3">
          <div className="flex items-center gap-2">
            <h3 className="text-sm font-semibold">Signed-in browsers</h3>
            <button
              className="btn ml-auto"
              disabled={revokeOthers.isPending}
              onClick={() => revokeOthers.mutate()}
            >
              Sign out everywhere else
            </button>
          </div>
          <p className="hint mt-1">
            A “stay signed in” session lasts as long as the setting above. Changing the
            password ends all of them.
          </p>
          <ul className="mt-2 space-y-1.5">
            {(sessions.data ?? []).map((session) => (
              <li
                key={session.id}
                className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded border border-border px-2.5 py-2 text-xs"
              >
                <span className="font-medium text-content">
                  {session.current ? 'This browser' : describeAgent(session.userAgent)}
                </span>
                <span className="tabular text-content-muted">
                  last used {formatRelative(session.lastUsedAt)}
                </span>
                <span className="tabular text-content-faint">
                  {session.remembered ? 'stays signed in' : 'until the browser closes'} · expires{' '}
                  {formatDate(session.expiresAt)}
                </span>
                {session.createdIp && (
                  <span className="tabular text-content-faint">from {session.createdIp}</span>
                )}
                {!session.current && (
                  <button
                    className="ml-auto text-2xs text-content-faint hover:text-state-error"
                    onClick={() => revokeOne.mutate(session.id)}
                  >
                    sign out
                  </button>
                )}
              </li>
            ))}
            {sessions.data?.length === 0 && (
              <li className="hint">No sessions — sign-in is set up but nobody is signed in.</li>
            )}
          </ul>
        </div>
      )}
    </section>
  )
}

/** Enough of a User-Agent to recognise a row by, without pretending to parse one. */
function describeAgent(agent: string | null): string {
  if (!agent) return 'Unknown browser'
  const browser = ['Firefox', 'Edg', 'Chrome', 'Safari'].find((name) => agent.includes(name))
  const platform = ['Windows', 'Android', 'iPhone', 'iPad', 'Mac', 'Linux'].find((name) =>
    agent.includes(name),
  )
  const label = [browser === 'Edg' ? 'Edge' : browser, platform].filter(Boolean).join(' on ')
  return label || 'Unknown browser'
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
                    {c.reason ? (
                      <span className="text-content-muted"> · {c.reason}</span>
                    ) : null}
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
