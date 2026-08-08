import { useEffect, useState, type ReactNode } from 'react'
import { NavLink, useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'

import { api } from '@/lib/api'
import { cn } from '@/lib/cn'

const NAV = [
  { to: '/', label: 'Dashboard', end: true, icon: GridIcon },
  { to: '/recordings', label: 'Recordings', icon: FilmIcon },
  { to: '/journeys', label: 'Journeys', icon: RouteIcon },
  { to: '/heatmap', label: 'Heatmap', icon: HeatIcon },
  { to: '/plates', label: 'Plates', icon: PlateIcon },
  { to: '/vehicles', label: 'Vehicles', icon: CarIcon },
  { to: '/queue', label: 'Queue', icon: QueueIcon },
  { to: '/logs', label: 'Logs', icon: LogIcon },
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

  return { theme, toggle: () => setTheme((t) => (t === 'dark' ? 'light' : 'dark')) }
}

export default function Layout({ children }: { children: ReactNode }) {
  const { theme, toggle } = useTheme()
  const [query, setQuery] = useState('')
  const [navOpen, setNavOpen] = useState(false)
  const navigate = useNavigate()

  // The queue badge is the one piece of chrome that must feel live — it is how you know
  // the app is doing something without opening the queue page.
  const { data: stats } = useQuery({
    queryKey: ['queue-stats'],
    queryFn: api.jobs.stats,
    refetchInterval: 5_000,
  })

  const busy = (stats?.running ?? 0) > 0
  const pending = (stats?.queued ?? 0) + (stats?.running ?? 0)

  return (
    <div className="flex min-h-full flex-col bg-surface">
      <header className="sticky top-0 z-30 border-b border-border bg-surface-raised/95 backdrop-blur">
        <div className="mx-auto flex h-14 max-w-[1600px] items-center gap-3 px-4">
          <button
            className="btn -ml-1 border-transparent px-2 md:hidden"
            onClick={() => setNavOpen((o) => !o)}
            aria-label="Toggle navigation"
            aria-expanded={navOpen}
          >
            <MenuIcon />
          </button>

          <NavLink to="/" className="flex shrink-0 items-center gap-2">
            <LogoIcon />
            <span className="hidden text-sm font-semibold tracking-tight sm:inline">
              Dashcam Analyser
            </span>
          </NavLink>

          <form
            className="ml-auto max-w-md flex-1"
            onSubmit={(e) => {
              e.preventDefault()
              if (query.trim()) navigate(`/search?q=${encodeURIComponent(query.trim())}`)
            }}
          >
            <label className="sr-only" htmlFor="global-search">
              Search plates, recordings and journeys
            </label>
            <div className="relative">
              <SearchIcon className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-content-faint" />
              <input
                id="global-search"
                className="input pl-8"
                placeholder="Search plates, dates, filenames…"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
              />
            </div>
          </form>

          <NavLink
            to="/queue"
            className="btn shrink-0 gap-2 border-transparent px-2"
            title={busy ? 'Processing' : 'Queue idle'}
          >
            <span
              className={cn(
                'h-2 w-2 rounded-full',
                busy ? 'animate-pulse bg-state-busy' : 'bg-state-idle',
              )}
            />
            <span className="tabular text-xs text-content-muted">{pending}</span>
          </NavLink>

          <button className="btn shrink-0 border-transparent px-2" onClick={toggle} aria-label="Toggle theme">
            {theme === 'dark' ? <SunIcon /> : <MoonIcon />}
          </button>
        </div>
      </header>

      <div className="mx-auto flex w-full max-w-[1600px] flex-1 gap-6 px-4 py-5">
        <nav
          className={cn(
            'w-48 shrink-0 md:block',
            navOpen
              ? 'fixed inset-x-0 top-14 z-20 block border-b border-border bg-surface-raised p-4 md:static md:border-0 md:bg-transparent md:p-0'
              : 'hidden',
          )}
        >
          <ul className="space-y-0.5">
            {NAV.map(({ to, label, end, icon: Icon }) => (
              <li key={to}>
                <NavLink
                  to={to}
                  end={end}
                  onClick={() => setNavOpen(false)}
                  className={({ isActive }) =>
                    cn(
                      'flex items-center gap-2.5 rounded-md px-2.5 py-1.5 text-sm transition-colors',
                      isActive
                        ? 'bg-accent-muted font-medium text-accent'
                        : 'text-content-muted hover:bg-surface-sunken hover:text-content',
                    )
                  }
                >
                  <Icon />
                  {label}
                </NavLink>
              </li>
            ))}
          </ul>
        </nav>

        <main className="min-w-0 flex-1">{children}</main>
      </div>
    </div>
  )
}

/* Inline SVGs rather than an icon package: a strict CSP and an offline-capable container
   make a bundled dependency more trouble than eight paths are worth. */
type IconProps = { className?: string }
const base = 'h-4 w-4 shrink-0'

function GridIcon({ className }: IconProps) {
  return (
    <svg className={cn(base, className)} viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6">
      <rect x="2.5" y="2.5" width="6" height="6" rx="1" />
      <rect x="11.5" y="2.5" width="6" height="6" rx="1" />
      <rect x="2.5" y="11.5" width="6" height="6" rx="1" />
      <rect x="11.5" y="11.5" width="6" height="6" rx="1" />
    </svg>
  )
}
function FilmIcon({ className }: IconProps) {
  return (
    <svg className={cn(base, className)} viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6">
      <rect x="2" y="4" width="16" height="12" rx="1.5" />
      <path d="M6 4v12M14 4v12M2 10h16" />
    </svg>
  )
}
function RouteIcon({ className }: IconProps) {
  return (
    <svg className={cn(base, className)} viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6">
      <circle cx="5" cy="15" r="2.2" />
      <circle cx="15" cy="5" r="2.2" />
      <path d="M7 14c4 0 3-9 6-9" strokeLinecap="round" />
    </svg>
  )
}
function HeatIcon({ className }: IconProps) {
  return (
    <svg className={cn(base, className)} viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6">
      <circle cx="10" cy="10" r="2" />
      <circle cx="10" cy="10" r="5" opacity="0.6" />
      <circle cx="10" cy="10" r="8" opacity="0.3" />
    </svg>
  )
}
function PlateIcon({ className }: IconProps) {
  return (
    <svg className={cn(base, className)} viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6">
      <rect x="1.5" y="5" width="17" height="10" rx="1.5" />
      <path d="M5 9v2M8 9v2M11 9v2M14 9v2" strokeLinecap="round" />
    </svg>
  )
}
function CarIcon({ className }: IconProps) {
  return (
    <svg className={cn(base, className)} viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6">
      <path d="M2.5 12.5h15v-2l-1.5-1-1.5-3.5h-9L4 9.5l-1.5 1z" strokeLinejoin="round" />
      <circle cx="6" cy="13.5" r="1.5" />
      <circle cx="14" cy="13.5" r="1.5" />
    </svg>
  )
}
function QueueIcon({ className }: IconProps) {
  return (
    <svg className={cn(base, className)} viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6">
      <path d="M3 5h14M3 10h14M3 15h9" strokeLinecap="round" />
    </svg>
  )
}
function LogIcon({ className }: IconProps) {
  return (
    <svg className={cn(base, className)} viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6">
      <rect x="3.5" y="2.5" width="13" height="15" rx="1.5" />
      <path d="M6.5 6.5h7M6.5 10h7M6.5 13.5h4" strokeLinecap="round" />
    </svg>
  )
}
function GearIcon({ className }: IconProps) {
  return (
    <svg className={cn(base, className)} viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6">
      <circle cx="10" cy="10" r="2.5" />
      <path d="M10 2.5v2M10 15.5v2M2.5 10h2M15.5 10h2M4.7 4.7l1.4 1.4M13.9 13.9l1.4 1.4M15.3 4.7l-1.4 1.4M6.1 13.9l-1.4 1.4" strokeLinecap="round" />
    </svg>
  )
}
function SearchIcon({ className }: IconProps) {
  return (
    <svg className={cn(base, className)} viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.8">
      <circle cx="9" cy="9" r="5.5" />
      <path d="M13.2 13.2 17 17" strokeLinecap="round" />
    </svg>
  )
}
function MenuIcon({ className }: IconProps) {
  return (
    <svg className={cn(base, className)} viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.8">
      <path d="M3 5.5h14M3 10h14M3 14.5h14" strokeLinecap="round" />
    </svg>
  )
}
function SunIcon({ className }: IconProps) {
  return (
    <svg className={cn(base, className)} viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6">
      <circle cx="10" cy="10" r="3.5" />
      <path d="M10 2v1.8M10 16.2V18M2 10h1.8M16.2 10H18M4.3 4.3l1.3 1.3M14.4 14.4l1.3 1.3M15.7 4.3l-1.3 1.3M5.6 14.4l-1.3 1.3" strokeLinecap="round" />
    </svg>
  )
}
function MoonIcon({ className }: IconProps) {
  return (
    <svg className={cn(base, className)} viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6">
      <path d="M16 11.5A6.5 6.5 0 0 1 8.5 4a6.5 6.5 0 1 0 7.5 7.5z" strokeLinejoin="round" />
    </svg>
  )
}
function LogoIcon() {
  return (
    <svg className="h-6 w-6 text-accent" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
      <rect x="2.5" y="6.5" width="19" height="11" rx="2.5" />
      <circle cx="12" cy="12" r="3.2" />
      <path d="M7 4.5h4" strokeLinecap="round" />
    </svg>
  )
}
