import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'

import { api } from './api'

/**
 * The operator's tile provider, as props for `RouteMap` and Leaflet's `TileLayer`.
 *
 * `maps.tile_url`, `maps.attribution` and `maps.max_zoom` are ordinary editable settings on
 * the Settings page, and exactly one map honoured them: the journey detail view, which had
 * this `useMemo` inline. The Heatmap hard-coded OpenStreetMap in two module constants and
 * the plate map passed nothing at all, so a user who pointed the app at their own tile
 * server got a mixture — one map theirs, two still hitting OSM, with an attribution line
 * naming a provider that was not serving the tiles.
 *
 * Undefined for an unset value on purpose, so each consumer falls back to its own default
 * rather than rendering an empty tile URL.
 */
export function useMapSettings() {
  const settings = useQuery({ queryKey: ['settings'], queryFn: api.settings.get })
  return useMemo(() => {
    const maps = settings.data?.find((c) => c.key === 'maps')
    const value = (key: string) => maps?.settings.find((s) => s.key === key)?.value
    return {
      tileUrl: (value('maps.tile_url') as string) || undefined,
      attribution: (value('maps.attribution') as string) || undefined,
      maxZoom: (value('maps.max_zoom') as number) || undefined,
    }
  }, [settings.data])
}
