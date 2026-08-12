import { Suspense, lazy, useEffect } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Navigate, Route, Routes } from 'react-router-dom'

import Layout from '@/components/Layout'
import Spinner from '@/components/Spinner'
import { api } from '@/lib/api'
import { setDisplayTimeZone } from '@/lib/format'

// Route-level code splitting. The map and chart bundles are large and most sessions never
// open a journey, so they should not sit in the initial payload.
const Dashboard = lazy(() => import('@/pages/Dashboard'))
const Recordings = lazy(() => import('@/pages/Recordings'))
const RecordingViewer = lazy(() => import('@/pages/RecordingViewer'))
const Journeys = lazy(() => import('@/pages/Journeys'))
const Heatmap = lazy(() => import('@/pages/Heatmap'))
const TelemetryHealth = lazy(() => import('@/pages/TelemetryHealth'))
const JourneyDetail = lazy(() => import('@/pages/JourneyDetail'))
const Plates = lazy(() => import('@/pages/Plates'))
const PlateDetail = lazy(() => import('@/pages/PlateDetail'))
const Vehicles = lazy(() => import('@/pages/Vehicles'))
const Queue = lazy(() => import('@/pages/Queue'))
const Backup = lazy(() => import('@/pages/Backup'))
const Logs = lazy(() => import('@/pages/Logs'))
const Settings = lazy(() => import('@/pages/Settings'))
const Search = lazy(() => import('@/pages/Search'))
const NotFound = lazy(() => import('@/pages/NotFound'))

export default function App() {
  // Timestamps are rendered in the camera's timezone, not the viewer's — the footage, the
  // burned-in overlay and the filenames are all in the camera's local time, so a clip
  // driven at 4pm should read as 4pm wherever it is being looked at. Set here rather than
  // in a page, because every page formats dates and the first one rendered would otherwise
  // use the browser's zone until something else happened to fetch the status.
  const status = useQuery({ queryKey: ['status'], queryFn: api.status, staleTime: 300_000 })
  useEffect(() => {
    setDisplayTimeZone(status.data?.timezone)
  }, [status.data?.timezone])

  return (
    <Layout>
      <Suspense fallback={<Spinner label="Loading…" className="py-24" />}>
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/recordings" element={<Recordings />} />
          <Route path="/recordings/:id" element={<RecordingViewer />} />
          <Route path="/journeys" element={<Journeys />} />
          <Route path="/journeys/:id" element={<JourneyDetail />} />
          <Route path="/heatmap" element={<Heatmap />} />
          <Route path="/telemetry-health" element={<TelemetryHealth />} />
          <Route path="/plates" element={<Plates />} />
          <Route path="/plates/:id" element={<PlateDetail />} />
          <Route path="/vehicles" element={<Vehicles />} />
          <Route path="/queue" element={<Queue />} />
          <Route path="/backup" element={<Backup />} />
          <Route path="/logs" element={<Logs />} />
          <Route path="/settings" element={<Settings />} />
          <Route path="/search" element={<Search />} />
          <Route path="/index.html" element={<Navigate to="/" replace />} />
          <Route path="*" element={<NotFound />} />
        </Routes>
      </Suspense>
    </Layout>
  )
}
