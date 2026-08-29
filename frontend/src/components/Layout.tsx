import { useEffect, useRef, useState, type ComponentType, type ReactNode } from 'react'
import { NavLink, useNavigate } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '@/lib/api'
import { cn } from '@/lib/cn'
import { invalidateAnalysisQueries, resetForIdentityChange } from '@/lib/queryInvalidation'
import type { AuthState } from '@/lib/types'

type IconProps = { className?: string }
type NavItem = {
  to: string
  label: string
  end?: boolean
  icon: ComponentType<IconProps>
}

const LIBRARY_NAV: NavItem[] = [
  { to: '/', label: 'Overview', end: true, icon: GridIcon },
  { to: '/recordings', label: 'Recordings', icon: FilmIcon },
  { to: '/journeys', label: 'Journeys', icon: RouteIcon },
  { to: '/heatmap', label: 'Map', icon: HeatIcon },
  { to: '/telemetry-health', label: 'Telemetry health', icon: PulseIcon },
  { to: '/obd', label: 'OBD drives', icon: GaugeIcon },
  { to: '/plates', label: 'Plates', icon: PlateIcon },
  { to: '/vehicles', label: 'Vehicles', icon: CarIcon },
]

const SYSTEM_NAV: NavItem[] = [
  { to: '/queue', label: 'Queue', icon: QueueIcon },
  { to: '/backup', label: 'Backup', icon: DownloadIcon },
  { to: '/logs', label: 'Activity logs', icon: LogIcon },
  { to: '/settings', label: 'Settings', icon: GearIcon },
]

function useTheme() {
  const [theme, setTheme] = useState<'dark' | 'light'>(() => {
    const stored = localStorage.getItem('dashcam-theme')
    if (stored === 'dark' || stored === 'light') return stored
    return window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark'
  })

  useEffect(() => {
    document.documentElement.classList.toggle('dark', theme === 'dark')
    localStorage.setItem('dashcam-theme', theme)
  }, [theme])

  return { theme, toggle: () => setTheme((current) => (current === 'dark' ? 'light' : 'dark')) }
}

export default function Layout({
  children,
  auth,
}: {
  children: ReactNode
  auth?: AuthState
}) {
  const client = useQueryClient()
  const { theme, toggle } = useTheme()
  const [query, setQuery] = useState('')
  const [navOpen, setNavOpen] = useState(false)
  const navigate = useNavigate()

  const signOut = useMutation({
    mutationFn: api.auth.logout,
    // Drop the cache before asking who we are: the answer will be "nobody", and every
    // page's data belongs to the session that just ended. The auth-state query is the one
    // thing kept, because dropping it too leaves nothing for the refetch to find and the
    // shell carries on rendering the signed-in app over a session that has ended.
    onSuccess: () => resetForIdentityChange(client),
  })

  const { data: stats } = useQuery({
    queryKey: ['queue-stats'],
    queryFn: api.jobs.stats,
    // Five seconds while there is work to watch; twenty when there is not.
    //
    // This is mounted on every page, so the fixed five-second poll ran for as long as any
    // tab was open — twelve requests a minute, each running four aggregate queries, against
    // a queue that is empty for most of the life of a deployment. Off the Queue page the
    // only consumers are the nav badge and the derived-page refresh below, and neither is
    // worth that. Everything that *creates* work invalidates this key directly, so the
    // slow interval is a backstop rather than how a change is noticed; the Queue page,
    // which observes the same key, keeps its own three-second interval.
    refetchInterval: (query) => {
      const counts = query.state.data
      return (counts?.queued ?? 0) + (counts?.running ?? 0) > 0 ? 5_000 : 20_000
    },
  })

  const busy = (stats?.running ?? 0) > 0
  const pending = (stats?.queued ?? 0) + (stats?.running ?? 0)
  const done = stats?.completedToday ?? 0
  const wasBusy = useRef(false)
  const lastMapRefresh = useRef(0)
  const lastCounts = useRef({ pending: 0, done: 0 })

  // Refresh derived pages when work actually lands, and once more on the transition to
  // idle — so a thumbnail written by the metadata stage appears without a hard refresh.
  //
  // Keyed on the counts, not on when the poll last answered. `dataUpdatedAt` changes on
  // every successful fetch whether or not anything moved, so this used to invalidate
  // sixteen query roots — including /api/status, which is a dozen queries with two
  // full-table joined counts — every five seconds for as long as anything sat in the
  // queue. A paused backlog did it forever, and on single-writer SQLite that is direct
  // contention with the very work it is reporting on. `completedToday` and `queued` both
  // move whenever a job finishes, which is the event this actually cares about.
  useEffect(() => {
    const isBusy = pending > 0
    // The guard tests what the deps test. `done` was added as a trigger so a job finishing
    // refreshes the derived pages, but the body still only ran while the queue was busy --
    // and at the twenty-second idle interval a burst of background work can start and
    // finish entirely between two polls, so the one case the trigger was added for was the
    // one it could not serve. Comparing against the last observed counts covers it.
    const moved = pending !== lastCounts.current.pending || done !== lastCounts.current.done
    lastCounts.current = { pending, done }
    if (isBusy || wasBusy.current || moved) {
      const now = Date.now()
      // The heat aggregation is deliberately heavier than a list read. Thirty seconds is
      // live enough to watch it repopulate without executing it every five seconds for a
      // multi-hour library rebuild.
      const includeMaps = !isBusy || now - lastMapRefresh.current >= 30_000
      if (includeMaps) lastMapRefresh.current = now
      void invalidateAnalysisQueries(client, { includeMaps })
    }
    wasBusy.current = isBusy
  }, [client, pending, done])

  useEffect(() => {
    const close = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setNavOpen(false)
    }
    window.addEventListener('keydown', close)
    return () => window.removeEventListener('keydown', close)
  }, [])

  const renderNavGroup = (label: string, items: NavItem[]) => (
    <div>
      <div className="mb-2 px-3 text-2xs font-semibold uppercase tracking-[0.14em] text-nav-muted/70">
        {label}
      </div>
      <ul className="space-y-1">
        {items.map(({ to, label: itemLabel, end, icon: Icon }) => (
          <li key={to}>
            <NavLink
              to={to}
              end={end}
              onClick={() => setNavOpen(false)}
              className={({ isActive }) =>
                cn(
                  'flex min-h-11 items-center gap-3 rounded-lg px-3 text-sm font-medium transition-colors',
                  isActive
                    ? 'bg-accent text-white shadow-sm'
                    : 'text-nav-muted hover:bg-nav-raised hover:text-nav-content',
                )
              }
            >
              <Icon className="h-[18px] w-[18px]" />
              {itemLabel}
              {to === '/queue' && pending > 0 && (
                <span className="tabular ml-auto rounded-full bg-white/10 px-2 py-0.5 text-2xs text-nav-content">
                  {pending}
                </span>
              )}
            </NavLink>
          </li>
        ))}
      </ul>
    </div>
  )

  const submitSearch = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (query.trim()) navigate(`/search?q=${encodeURIComponent(query.trim())}`)
  }

  return (
    <div className="min-h-full bg-surface">
      {navOpen && (
        <button
          className="fixed inset-0 z-40 bg-slate-950/50 backdrop-blur-sm md:hidden"
          onClick={() => setNavOpen(false)}
          aria-label="Close navigation"
        />
      )}

      <aside
        id="primary-navigation"
        className={cn(
          'fixed inset-y-0 left-0 z-50 flex w-64 flex-col border-r border-nav-border bg-nav px-3 py-5 shadow-float transition-transform md:translate-x-0 md:shadow-none',
          navOpen ? 'translate-x-0' : '-translate-x-full',
        )}
      >
        <div className="flex h-12 items-center justify-between px-2">
          <NavLink to="/" className="flex items-center gap-3 text-nav-content" onClick={() => setNavOpen(false)}>
            <span className="grid h-10 w-10 place-items-center rounded-xl bg-white/10">
              <LogoIcon />
            </span>
            <span>
              <span className="block text-base font-bold leading-tight tracking-tight">Dashcam</span>
              <span className="block text-sm font-medium leading-tight text-nav-muted">Analyser</span>
            </span>
          </NavLink>
          <button
            className="grid h-9 w-9 place-items-center rounded-lg text-nav-muted hover:bg-nav-raised hover:text-nav-content md:hidden"
            onClick={() => setNavOpen(false)}
            aria-label="Close navigation"
          >
            <CloseIcon />
          </button>
        </div>

        <nav className="mt-8 flex-1 space-y-7 overflow-y-auto px-1">
          {renderNavGroup('Library', LIBRARY_NAV)}
          {renderNavGroup('System', SYSTEM_NAV)}
        </nav>

        <NavLink
          to="/queue"
          onClick={() => setNavOpen(false)}
          className="mt-4 rounded-xl border border-nav-border bg-nav-raised/70 p-3.5 transition-colors hover:bg-nav-raised"
        >
          <div className="flex items-center gap-2 text-sm font-semibold text-nav-content">
            <span className={cn('h-2.5 w-2.5 rounded-full', busy ? 'animate-pulse bg-state-ok' : 'bg-nav-muted')} />
            {busy ? 'Analysis in progress' : 'Queue is idle'}
          </div>
          <div className="mt-1 pl-[18px] text-xs text-nav-muted">
            {busy ? `${stats?.running ?? 0} active · ${stats?.queued ?? 0} waiting` : 'No active processing jobs'}
          </div>
        </NavLink>
      </aside>

      <div className="min-h-full md:pl-64">
        <header className="sticky top-0 z-30 border-b border-border/80 bg-surface/90 backdrop-blur-xl">
          <div className="mx-auto flex h-16 max-w-[1600px] items-center gap-3 px-4 sm:px-6 lg:px-8">
            <button
              className="grid h-10 w-10 shrink-0 place-items-center rounded-lg border border-border bg-surface-raised text-content shadow-sm md:hidden"
              onClick={() => setNavOpen((open) => !open)}
              aria-label="Toggle navigation"
              aria-expanded={navOpen}
              aria-controls="primary-navigation"
            >
              <MenuIcon />
            </button>

            <NavLink to="/" className="flex shrink-0 items-center gap-2 font-bold text-content md:hidden">
              <LogoIcon />
              <span className="hidden sm:inline">Dashcam Analyser</span>
            </NavLink>

            {/* Hidden below 640px, where the header has no room for it -- and Search has no
                nav entry, so /search used to be unreachable on a phone entirely. The icon
                below is the way in on those widths. */}
            <form className="ml-auto hidden max-w-lg flex-1 sm:block" onSubmit={submitSearch}>
              <label className="sr-only" htmlFor="global-search">
                Search plates, recordings and journeys
              </label>
              <div className="relative">
                <SearchIcon className="pointer-events-none absolute left-3.5 top-1/2 h-4 w-4 -translate-y-1/2 text-content-faint" />
                <input
                  id="global-search"
                  className="input bg-surface-raised pl-10"
                  placeholder="Search recordings, plates, journeys…"
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                />
              </div>
            </form>

            <NavLink
              to="/search"
              className="ml-auto grid h-10 w-10 shrink-0 place-items-center rounded-lg text-content-muted hover:bg-surface-sunken hover:text-content sm:hidden"
              aria-label="Search"
              title="Search recordings, plates and journeys"
            >
              <SearchIcon className="h-4 w-4" />
            </NavLink>

            <NavLink
              to="/queue"
              className="flex min-h-10 shrink-0 items-center gap-2 rounded-lg px-2.5 text-content-muted hover:bg-surface-sunken"
              title={busy ? 'Processing' : 'Queue idle'}
            >
              <span className={cn('h-2.5 w-2.5 rounded-full', busy ? 'animate-pulse bg-state-ok' : 'bg-state-idle')} />
              <span className="tabular text-xs font-semibold">{pending}</span>
            </NavLink>

            <button
              className="grid h-10 w-10 shrink-0 place-items-center rounded-lg text-content-muted hover:bg-surface-sunken hover:text-content"
              onClick={toggle}
              aria-label="Toggle theme"
            >
              {theme === 'dark' ? <SunIcon /> : <MoonIcon />}
            </button>

            {auth?.authenticated && (
              <button
                className="grid h-10 w-10 shrink-0 place-items-center rounded-lg text-content-muted hover:bg-surface-sunken hover:text-content"
                onClick={() => signOut.mutate()}
                disabled={signOut.isPending}
                aria-label={`Sign out${auth.username ? ` (${auth.username})` : ''}`}
                title={auth.username ? `Signed in as ${auth.username} — sign out` : 'Sign out'}
              >
                <SignOutIcon />
              </button>
            )}
          </div>
        </header>

        {auth?.misconfigured && (
          <div className="border-b border-state-warn/40 bg-state-warn/10 px-4 py-2.5 text-sm text-state-warn sm:px-6 lg:px-8">
            Sign-in is switched on but no account is set, so this deployment is serving
            without a password. Set one in{' '}
            <NavLink to="/settings?category=security" className="font-semibold underline">
              Settings → Access
            </NavLink>
            .
          </div>
        )}

        <main className="mx-auto w-full max-w-[1600px] px-4 py-6 sm:px-6 sm:py-8 lg:px-8">
          {children}
        </main>
      </div>
    </div>
  )
}

const base = 'h-4 w-4 shrink-0'

function GridIcon({ className }: IconProps) {
  return <svg className={cn(base, className)} viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6"><rect x="2.5" y="2.5" width="6" height="6" rx="1" /><rect x="11.5" y="2.5" width="6" height="6" rx="1" /><rect x="2.5" y="11.5" width="6" height="6" rx="1" /><rect x="11.5" y="11.5" width="6" height="6" rx="1" /></svg>
}
function FilmIcon({ className }: IconProps) {
  return <svg className={cn(base, className)} viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6"><rect x="2" y="4" width="16" height="12" rx="1.5" /><path d="M6 4v12M14 4v12M2 10h16" /></svg>
}
function RouteIcon({ className }: IconProps) {
  return <svg className={cn(base, className)} viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6"><circle cx="5" cy="15" r="2.2" /><circle cx="15" cy="5" r="2.2" /><path d="M7 14c4 0 3-9 6-9" strokeLinecap="round" /></svg>
}
function HeatIcon({ className }: IconProps) {
  return <svg className={cn(base, className)} viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6"><path d="M3 4.5 7.5 2l5 2.5L17 2v13.5L12.5 18l-5-2.5L3 18z" /><path d="M7.5 2v13.5m5-11V18" /></svg>
}
function PulseIcon({ className }: IconProps) {
  return <svg className={cn(base, className)} viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6"><path d="M2 10h3l2-5 3.5 10 2.5-7 1.5 2H18" strokeLinecap="round" strokeLinejoin="round" /></svg>
}
function GaugeIcon({ className }: IconProps) {
  return <svg className={cn(base, className)} viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6"><path d="M3 14a7.5 7.5 0 1 1 14 0" strokeLinecap="round" /><path d="M10 13.5 13.5 8" strokeLinecap="round" /><circle cx="10" cy="14" r="1.2" /></svg>
}
function PlateIcon({ className }: IconProps) {
  return <svg className={cn(base, className)} viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6"><rect x="1.5" y="5" width="17" height="10" rx="1.5" /><path d="M5 9v2M8 9v2M11 9v2M14 9v2" strokeLinecap="round" /></svg>
}
function CarIcon({ className }: IconProps) {
  return <svg className={cn(base, className)} viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6"><path d="M2.5 12.5h15v-2l-1.5-1-1.5-3.5h-9L4 9.5l-1.5 1z" strokeLinejoin="round" /><circle cx="6" cy="13.5" r="1.5" /><circle cx="14" cy="13.5" r="1.5" /></svg>
}
function QueueIcon({ className }: IconProps) {
  return <svg className={cn(base, className)} viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6"><path d="M3 5h14M3 10h14M3 15h9" strokeLinecap="round" /></svg>
}
function DownloadIcon({ className }: IconProps) {
  return <svg className={cn(base, className)} viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6"><path d="M10 2.5v9m0 0 3.5-3.5M10 11.5 6.5 8" strokeLinecap="round" strokeLinejoin="round" /><path d="M3 13v3.5h14V13" strokeLinecap="round" /></svg>
}
function LogIcon({ className }: IconProps) {
  return <svg className={cn(base, className)} viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6"><rect x="3.5" y="2.5" width="13" height="15" rx="1.5" /><path d="M6.5 6.5h7M6.5 10h7M6.5 13.5h4" strokeLinecap="round" /></svg>
}
function GearIcon({ className }: IconProps) {
  return <svg className={cn(base, className)} viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6"><circle cx="10" cy="10" r="2.5" /><path d="M10 2.5v2M10 15.5v2M2.5 10h2M15.5 10h2M4.7 4.7l1.4 1.4M13.9 13.9l1.4 1.4M15.3 4.7l-1.4 1.4M6.1 13.9l-1.4 1.4" strokeLinecap="round" /></svg>
}
function SearchIcon({ className }: IconProps) {
  return <svg className={cn(base, className)} viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.8"><circle cx="9" cy="9" r="5.5" /><path d="M13.2 13.2 17 17" strokeLinecap="round" /></svg>
}
function MenuIcon({ className }: IconProps) {
  return <svg className={cn(base, className)} viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.8"><path d="M3 5.5h14M3 10h14M3 14.5h14" strokeLinecap="round" /></svg>
}
function CloseIcon({ className }: IconProps) {
  return <svg className={cn(base, className)} viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.8"><path d="m5 5 10 10M15 5 5 15" strokeLinecap="round" /></svg>
}
function SunIcon({ className }: IconProps) {
  return <svg className={cn(base, className)} viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6"><circle cx="10" cy="10" r="3.5" /><path d="M10 2v1.8M10 16.2V18M2 10h1.8M16.2 10H18M4.3 4.3l1.3 1.3M14.4 14.4l1.3 1.3M15.7 4.3l-1.3 1.3M5.6 14.4l-1.3 1.3" strokeLinecap="round" /></svg>
}
function SignOutIcon({ className }: IconProps) {
  return <svg className={cn(base, className)} viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6"><path d="M12.5 3.5H16a1.5 1.5 0 0 1 1.5 1.5v10a1.5 1.5 0 0 1-1.5 1.5h-3.5" strokeLinecap="round" /><path d="M9 13.5 12.5 10 9 6.5M12.5 10h-10" strokeLinecap="round" strokeLinejoin="round" /></svg>
}
function MoonIcon({ className }: IconProps) {
  return <svg className={cn(base, className)} viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6"><path d="M16 11.5A6.5 6.5 0 0 1 8.5 4a6.5 6.5 0 1 0 7.5 7.5z" strokeLinejoin="round" /></svg>
}
function LogoIcon() {
  return <svg className="h-6 w-6 text-current" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"><rect x="2.5" y="6.5" width="19" height="11" rx="2.5" /><circle cx="12" cy="12" r="3.2" /><path d="M7 4.5h4" strokeLinecap="round" /></svg>
}
