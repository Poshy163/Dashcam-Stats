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
    <div className="grid min-h-full place-items-center bg-surface px-4 py-12">
      <div className="w-full max-w-sm">
        <div className="mb-7 flex flex-col items-center text-center">
          <span className="grid h-14 w-14 place-items-center rounded-2xl bg-nav text-nav-content shadow-card">
            <LogoIcon />
          </span>
          <h1 className="mt-4 text-2xl font-bold tracking-tight">Dashcam Analyser</h1>
          <p className="hint mt-1">Sign in to reach your footage.</p>
        </div>

        <form className="card space-y-4 p-6" onSubmit={submit}>
          <div className="space-y-1.5">
            <label className="label" htmlFor="username">
              Username
            </label>
            <input
              id="username"
              className="input"
              value={username}
              onChange={(event) => setUsername(event.target.value)}
              autoComplete="username"
              autoFocus
              required
            />
          </div>

          <div className="space-y-1.5">
            <label className="label" htmlFor="password">
              Password
            </label>
            <input
              id="password"
              className="input"
              type="password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              autoComplete="current-password"
              required
            />
          </div>

          <label className="flex items-center gap-2 text-sm text-content-muted">
            <input
              type="checkbox"
              checked={remember}
              onChange={(event) => setRemember(event.target.checked)}
            />
            Stay signed in{rememberDays ? ` for ${rememberDays} days` : ''}
          </label>

          {message && (
            <p
              className="rounded-lg border border-state-error/40 bg-state-error/10 px-3 py-2 text-sm text-state-error"
              role="alert"
            >
              {message}
            </p>
          )}

          <button
            className="btn btn-primary w-full"
            type="submit"
            disabled={signIn.isPending || !username.trim() || !password}
          >
            {signIn.isPending ? 'Signing in…' : 'Sign in'}
          </button>
        </form>

        <p className="hint mt-4 text-center">
          Forgotten it? Run{' '}
          <code className="rounded bg-surface-sunken px-1 py-0.5 text-2xs">
            docker compose exec dashcam entrypoint.sh recover-login set-password
          </code>{' '}
          on the host.
        </p>
      </div>
    </div>
  )
}

function LogoIcon() {
  return (
    <svg className="h-7 w-7" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
      <rect x="2.5" y="6.5" width="19" height="11" rx="2.5" />
      <circle cx="12" cy="12" r="3.2" />
      <path d="M7 4.5h4" strokeLinecap="round" />
    </svg>
  )
}
