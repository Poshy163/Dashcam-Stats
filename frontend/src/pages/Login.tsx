import { useState, type FormEvent } from 'react'
import { useMutation } from '@tanstack/react-query'

import { ApiError, api } from '@/lib/api'

/**
 * The one page that renders without the application shell.
 *
 * It is shown in place of the whole app rather than at a `/login` route, so the URL never
 * changes. Signing in therefore lands back on whatever the visitor was trying to reach —
 * a shared journey link opens that journey — with no redirect to remember or get wrong.
 */
export default function Login({
  rememberDays,
  onSignedIn,
}: {
  rememberDays: number | null
  onSignedIn: () => void
}) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [remember, setRemember] = useState(true)

  const signIn = useMutation({
    mutationFn: () => api.auth.login(username, password, remember),
    onSuccess: onSignedIn,
  })

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (username.trim() && password) signIn.mutate()
  }

  // A throttled attempt is a different situation from a wrong password and has to read as
  // one, or the honest answer ("wait") looks like the app is broken.
  const throttled = signIn.error instanceof ApiError && signIn.error.status === 429
  const message = signIn.error
    ? throttled
      ? 'Too many attempts from this address. Wait a few minutes and try again.'
      : signIn.error.message
    : null

  return (
    <div className="grid min-h-full place-items-center bg-surface px-4 py-12 relative overflow-hidden">
      {/* Subtle radial glow background */}
      <div className="absolute inset-0 pointer-events-none [background:radial-gradient(circle_at_50%_40%,rgba(255,107,0,0.06)_0%,transparent_60%)]" />

      <div className="w-full max-w-sm relative z-10">
        <div className="mb-7 flex flex-col items-center text-center">
          <span className="grid h-16 w-16 place-items-center rounded-2xl border border-accent/40 bg-surface-raised text-accent shadow-glow-orange">
            <LogoIcon className="h-8 w-8" />
          </span>
          <div className="mt-4 flex items-center gap-2">
            <h1 className="text-2xl font-black tracking-wider text-white">DASHCAM</h1>
            <span className="rounded bg-accent/20 px-1.5 py-0.5 text-[10px] font-mono font-bold text-accent">HUD</span>
          </div>
          <p className="font-mono text-xs uppercase tracking-wider text-content-muted mt-1">Cockpit Authentication</p>
        </div>

        <form className="card cockpit-panel space-y-4 p-6 shadow-2xl" onSubmit={submit}>
          <div className="space-y-1.5">
            <label className="label" htmlFor="username">
              Operator Username
            </label>
            <input
              id="username"
              className="input font-mono"
              value={username}
              onChange={(event) => setUsername(event.target.value)}
              autoComplete="username"
              autoFocus
              required
            />
          </div>

          <div className="space-y-1.5">
            <label className="label" htmlFor="password">
              Security Key / Password
            </label>
            <input
              id="password"
              className="input font-mono"
              type="password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              autoComplete="current-password"
              required
            />
          </div>

          <label className="flex items-center gap-2 text-xs font-mono text-content-muted">
            <input
              type="checkbox"
              className="h-4 w-4 rounded border-border bg-surface-sunken text-accent focus:ring-accent"
              checked={remember}
              onChange={(event) => setRemember(event.target.checked)}
            />
            Stay signed in{rememberDays ? ` for ${rememberDays} days` : ''}
          </label>

          {message && (
            <p
              className="rounded-lg border border-state-error/40 bg-state-error/10 px-3 py-2 text-xs font-mono text-state-error"
              role="alert"
            >
              {message}
            </p>
          )}

          <button
            className="btn btn-primary w-full font-mono uppercase tracking-wider text-xs py-3"
            type="submit"
            disabled={signIn.isPending || !username.trim() || !password}
          >
            {signIn.isPending ? 'Authenticating…' : 'Initialize Session ⚡'}
          </button>
        </form>

        <p className="hint mt-4 text-center font-mono text-2xs">
          Forgot password? Run{' '}
          <code className="rounded bg-surface-sunken px-1.5 py-0.5 text-2xs border border-border">
            docker compose exec dashcam entrypoint.sh recover-login set-password
          </code>{' '}
          on host.
        </p>
      </div>
    </div>
  )
}

function LogoIcon({ className = 'h-7 w-7' }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
      <path d="M4 17.5A9.5 9.5 0 1 1 20 17.5" stroke="currentColor" strokeLinecap="round" opacity="0.85" />
      <path d="M16 5.2A9.5 9.5 0 0 1 20 17.5" stroke="rgb(var(--accent))" strokeWidth="2.4" strokeLinecap="round" />
      <path d="M12 13.5l4.8-4.8" stroke="rgb(var(--accent))" strokeWidth="2.2" strokeLinecap="round" />
      <circle cx="12" cy="13.5" r="2.2" fill="rgb(var(--accent))" />
      <path d="M6 14.5l1.2-.7M7.5 9.5l1.2.7M12 4.5v1.5M16.5 9.5l-1.2.7" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" opacity="0.6" />
    </svg>
  )
}
