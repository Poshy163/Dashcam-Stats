import { MapContainer, TileLayer } from 'react-leaflet'
import 'leaflet/dist/leaflet.css'

import FitMapToData from '@/components/FitMapToData'
import HeatLayer from '@/components/HeatLayer'
import RouteLines from '@/components/RouteLines'
import type { Heatmap, Routes } from '@/lib/types'

interface Props {
  fitPoints: [number, number][]
  settingsLoaded: boolean
  tileUrl?: string
  attribution?: string
  maxZoom?: number
  heat?: Heatmap
  routes?: Routes
}

export default function HeatmapCanvas({ fitPoints, settingsLoaded, tileUrl, attribution, maxZoom, heat, routes }: Props) {
  if (!settingsLoaded || fitPoints.length === 0) return null
  return (
    <div className="overflow-hidden rounded-lg border border-border">
      <MapContainer bounds={fitPoints} boundsOptions={{ padding: [32, 32] }} maxZoom={maxZoom ?? 19} scrollWheelZoom className="h-[70vh] w-full">
        <TileLayer
          url={tileUrl ?? 'https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png'}
          attribution={attribution ?? '&copy; OpenStreetMap contributors'}
          maxZoom={maxZoom ?? 19}
        />
        <FitMapToData points={fitPoints} />
        {heat && <HeatLayer points={heat.points} maxWeight={heat.maxWeight} precision={heat.precision} />}
        {routes && <RouteLines lines={routes.lines} />}
      </MapContainer>
    </div>
  )
}
