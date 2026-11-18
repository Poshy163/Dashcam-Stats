import { useDisplaySettings } from './useDisplaySettings'

export function useMapSettings() {
  const query = useDisplaySettings()
  return {
    tileUrl: query.data?.tileUrl || undefined,
    attribution: query.data?.attribution || undefined,
    maxZoom: query.data?.maxZoom,
    query,
  }
}
