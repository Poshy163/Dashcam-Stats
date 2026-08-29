import { Suspense, lazy, useEffect } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Navigate, Route, Routes, useLocation } from 'react-router-dom'

import Layout from '@/components/Layout'
import RouteBoundary from '@/components/RouteBoundary'
import Spinner from '@/components/Spinner'
import { api, setUnauthorizedHandler } from '@/lib/api'
import { setDisplayTimeZone } from '@/lib/format'
import { AUTH_STATE_KEY, resetForIdentityChange } from '@/lib/queryInvalidation'

// Route-level code splitting. The map and chart bundles are large and most sessions never
// open a journey, so they should not sit in the initial payload.
const Dashboard = lazy(() => import('@/pages/Dashboard'))
const Recordings = lazy(() => import('@/pages/Recordings'))
const RecordingViewer = lazy(() => import('@/pages/RecordingViewer'))
const Journeys = lazy(() => import('@/pages/Journeys'))
const Heatmap = lazy(() => import('@/pages/Heatmap'))
const TelemetryHealth = lazy(() => import('@/pages/TelemetryHealth'))
const JourneyDetail = lazy(() => import('@/pages/JourneyDetail'))
const ObdDrives = lazy(() => import('@/pages/ObdDrives'))
const ObdDriveDetail = lazy(() => import('@/pages/ObdDriveDetail'))
const Plates = lazy(() => import('@/pages/Plates'))
const PlateDetail = lazy(() => import('@/pages/PlateDetail'))
const Vehicles = lazy(() => import('@/pages/Vehicles'))
const Queue = lazy(() => import('@/pages/Queue'))
const Backup = lazy(() => import('@/pages/Backup'))
const Logs = lazy(() => import('@/pages/Logs'))
const Settings = lazy(() => import('@/pages/Settings'))
const Search = lazy(() => import('@/pages/Search'))
const NotFound = lazy(() => import('@/pages/NotFound'))
// Not lazy. It is what a signed-out visitor sees first, and a chunk fetch in front of it
// would show a spinner before the password box on every cold load.
import Login from '@/pages/Login'

export default function App() {
  const client = useQueryClient()
  const location = useLocation()

  // Asked for before anything else renders. Every page in the app opens by fetching
  // something, so mounting the shell before this resolves would fire a screenful of
  // requests that can only 401.
  const auth = useQuery({ queryKey: AUTH_STATE_KEY, queryFn: api.auth.state, staleTime: 60_000 })
  const locked = auth.data?.required === true && auth.data.authenticated === false

  // A thirty-day session expires while a tab is open, and the first request to notice is
  // whichever one happened to fire. Rather than have that page render an error, any 401
  // re-asks who we are, which flips `locked` and puts the login page up.
  useEffect(() => {
    setUnauthorizedHandler(() => {
      void client.invalidateQueries({ queryKey: AUTH_STATE_KEY })
    })
    return () => setUnauthorizedHandler(null)
  }, [client])

  // Timestamps are rendered in the camera's timezone, not the viewer's — the footage, the
  // burned-in overlay and the filenames are all in the camera's local time, so a clip
  // driven at 4pm should read as 4pm wherever it is being looked at. Set here rather than
  // in a page, because every page formats dates and the first one rendered would otherwise
  // use the browser's zone until something else happened to fetch the status.
  const status = useQuery({
    queryKey: ['status'],
    queryFn: api.status,
    staleTime: 300_000,
    enabled: !locked,
  })
  useEffect(() => {
    setDisplayTimeZone(status.data?.timezone)
  }, [status.data?.timezone])

  if (auth.isLoading) return <Spinner label="Loading…" className="py-24" />

  if (locked) {
    return (
      <Login
        rememberDays={auth.data?.rememberDays ?? null}
        // Everything cached was fetched as somebody else, or as nobody, so none of it is
        // kept — but the auth-state query itself has to survive that, or nothing is left
        // to refetch and the login page stays up over a session that already exists.
        onSignedIn={() => resetForIdentityChange(client)}
      />
    )
  }

  return (
    <Layout auth={auth.data}>
      {/* Keyed on the path so a page that failed does not keep its error across a
          navigation — otherwise one broken route makes the whole app look broken. */}
      <RouteBoundary key={location.pathname}>
        <Suspense fallback={<Spinner label="Loading…" className="py-24" />}>
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/recordings" element={<Recordings />} />
            <Route path="/recordings/:id" element={<RecordingViewer />} />
            <Route path="/journeys" element={<Journeys />} />
            <Route path="/journeys/:id" element={<JourneyDetail />} />
            <Route path="/heatmap" element={<Heatmap />} />
            <Route path="/telemetry-health" element={<TelemetryHealth />} />
            <Route path="/obd" element={<ObdDrives />} />
            <Route path="/obd/:driveId" element={<ObdDriveDetail />} />
            <Route path="/plates" element={<Plates />} />
            <Route path="/plates/:id" element={<PlateDetail />} />
            <Route path="/vehicles" element={<Vehicles />} />
            <Route path="/queue" element={<Queue />} />
            <Route path="/backup" element={<Backup />} />
            <Route path="/logs" element={<Logs />} />
            <Route path="/settings" element={<Settings />} />
            <Route path="/search" element={<Search />} />
            {/* Reached only by someone who signed in and then typed the URL, since the login
                page replaces the app rather than living at a path of its own. */}
            <Route path="/login" element={<Navigate to="/" replace />} />
            <Route path="/index.html" element={<Navigate to="/" replace />} />
            <Route path="*" element={<NotFound />} />
          </Routes>
        </Suspense>
      </RouteBoundary>
    </Layout>
  )
}
