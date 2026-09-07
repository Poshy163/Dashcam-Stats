import { useEffect, useRef, useState, type ComponentType, type ReactNode } from 'react'
import { NavLink, useNavigate } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '@/lib/api'
import { cn } from '@/lib/cn'
import { motion } from '@/lib/kiosk'
import { invalidateAnalysisQueries, resetForIdentityChange } from '@/lib/queryInvalidation'
import type { AuthState } from '@/lib/types'
import type { Theme } from '@/lib/useTheme'

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

export default function Layout({
  children,
  auth,
  theme,
  onToggleTheme,
}: {
  children: ReactNode
  auth?: AuthState
  theme: Theme
  onToggleTheme: () => void
}) {
  const client = useQueryClient()
  const [query, setQuery] = useState('')
  const [navOpen, setNavOpen] = useState(false)
  const navigate = useNavigate()
  const sidebarRef = useRef<HTMLElement>(null)
  const menuButtonRef = useRef<HTMLButtonElement>(null)
  const contentRef = useRef<HTMLDivElement>(null)
  const navWasOpen = useRef(false)

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
    const content = contentRef.current
    if (content) content.inert = navOpen
    document.body.style.overflow = navOpen ? 'hidden' : ''

    if (!navOpen) {
      if (navWasOpen.current) menuButtonRef.current?.focus()
      navWasOpen.current = false
      return
    }

    navWasOpen.current = true
    const sidebar = sidebarRef.current
    const focusable = sidebar?.querySelectorAll<HTMLElement>(
      'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])',
    )
    focusable?.[0]?.focus()

    const handleKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        setNavOpen(false)
        return
      }
      if (event.key !== 'Tab' || !focusable?.length) return
      const first = focusable[0]
      const last = focusable[focusable.length - 1]
      if (!first || !last) return
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }
    window.addEventListener('keydown', handleKey)
    return () => {
      window.removeEventListener('keydown', handleKey)
      document.body.style.overflow = ''
      if (content) content.inert = false
    }
  }, [navOpen])

  useEffect(() => {
    const desktop = window.matchMedia('(min-width: 768px)')
    const closeOnDesktop = () => {
      if (desktop.matches) setNavOpen(false)
    }
    desktop.addEventListener('change', closeOnDesktop)
    return () => desktop.removeEventListener('change', closeOnDesktop)
  }, [])

  const renderNavGroup = (label: string, items: NavItem[]) => (
    <div>
      <div className="mb-2 px-3 font-mono text-2xs font-semibold uppercase tracking-[0.16em] text-nav-muted/60">
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
                  'group flex min-h-11 items-center gap-3 rounded-lg px-3 text-sm font-medium transition-all',
                  isActive
                    ? 'border-l-2 border-accent bg-gradient-to-r from-accent/20 to-accent/5 text-white font-semibold shadow-sm'
                    : 'text-nav-muted hover:bg-nav-raised/80 hover:text-nav-content',
                )
              }
            >
              {({ isActive }) => (
                <>
                  <Icon className={cn('h-[18px] w-[18px] transition-colors', isActive ? 'text-accent' : 'text-nav-muted group-hover:text-nav-content')} />
                  <span>{itemLabel}</span>
                  {to === '/queue' && pending > 0 && (
                    <span className="tabular ml-auto rounded-full border border-cyan/40 bg-cyan/15 px-2 py-0.5 text-2xs font-bold text-cyan">
                      {pending}
                    </span>
                  )}
                </>
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
          className="fixed inset-0 z-40 bg-slate-950/70 backdrop-blur-sm md:hidden"
          onClick={() => setNavOpen(false)}
          aria-label="Close navigation"
        />
      )}

      <aside
        ref={sidebarRef}
        id="primary-navigation"
        aria-label="Primary navigation"
        className={cn(
          'fixed inset-y-0 left-0 z-50 flex w-64 flex-col border-r border-nav-border bg-nav px-3.5 py-5 shadow-float transition-transform md:translate-x-0 md:shadow-none',
          navOpen ? 'visible translate-x-0' : 'invisible -translate-x-full md:visible',
        )}
      >
        <div className="flex h-12 items-center justify-between px-2">
          <NavLink to="/" className="flex items-center gap-3 text-nav-content group" onClick={() => setNavOpen(false)}>
            <span className="grid h-10 w-10 place-items-center rounded-xl border border-nav-border bg-nav-raised shadow-inner group-hover:border-accent/50 transition-colors">
              <LogoIcon className="h-6 w-6" />
            </span>
            <div>
              <span className="text-base font-extrabold tracking-tight text-white">Dashcam Analyser</span>
              <span className="block text-xs text-nav-muted/80">Vehicle insights</span>
            </div>
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
          className="mt-4 rounded-xl border border-nav-border/90 bg-nav-raised/50 p-3.5 transition-all hover:border-accent/40 hover:bg-nav-raised"
        >
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2 text-xs font-bold uppercase tracking-wider text-nav-content">
              <span className={cn('h-2 w-2 rounded-full', busy ? cn('bg-cyan shadow-glow-cyan', motion('animate-pulse')) : 'bg-state-ok')} />
              {busy ? 'Processing active' : 'Queue idle'}
            </div>
            {busy && (
              <span className="font-mono text-2xs font-semibold text-cyan">
                {stats?.running ?? 0} ACTIVE
              </span>
            )}
          </div>
          <div className="mt-2 font-mono text-2xs text-nav-muted">
            {busy ? `${stats?.running ?? 0} active · ${stats?.queued ?? 0} queued` : 'No background jobs running'}
          </div>
        </NavLink>
      </aside>

      <div ref={contentRef} className="min-h-full md:pl-64">
        <header className="sticky top-0 z-30 border-b border-border/80 bg-surface/90 backdrop-blur-xl">
          <div className="mx-auto flex h-16 max-w-[1600px] items-center gap-3 px-4 sm:px-6 lg:px-8">
            <button
              ref={menuButtonRef}
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
              <span className={cn('h-2.5 w-2.5 rounded-full', busy ? cn('bg-state-ok', motion('animate-pulse')) : 'bg-state-idle')} />
              <span className="tabular text-xs font-semibold">{pending}</span>
            </NavLink>

            <button
              className="grid h-10 w-10 shrink-0 place-items-center rounded-lg text-content-muted hover:bg-surface-sunken hover:text-content"
              onClick={onToggleTheme}
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
function LogoIcon({ className = 'h-6 w-6' }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
      {/* Gauge outer arc */}
      <path d="M4 17.5A9.5 9.5 0 1 1 20 17.5" stroke="currentColor" strokeLinecap="round" opacity="0.85" />
      {/* Redline segment */}
      <path d="M16 5.2A9.5 9.5 0 0 1 20 17.5" stroke="rgb(var(--accent))" strokeWidth="2.4" strokeLinecap="round" />
      {/* Speedometer needle */}
      <path d="M12 13.5l4.8-4.8" stroke="rgb(var(--accent))" strokeWidth="2.2" strokeLinecap="round" />
      <circle cx="12" cy="13.5" r="2.2" fill="rgb(var(--accent))" />
      {/* Tick markings */}
      <path d="M6 14.5l1.2-.7M7.5 9.5l1.2.7M12 4.5v1.5M16.5 9.5l-1.2.7" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" opacity="0.6" />
    </svg>
  )
}
